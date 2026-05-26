"""Rule-based partial decompiler: bytecode → IR.

Two passes ship in pychd:

1. **Native 3.14 pass** (this module). Operates on a stdlib
   :class:`types.CodeType` produced by the *currently running*
   interpreter — typically a ``.pyc`` whose magic number matches
   :data:`sys.version_info`. Recovers the full module skeleton:
   imports, function and class signatures, docstrings, simple
   module-level constants, PEP 749 lazy annotations, PEP 695 generic
   type-parameter syntax, decorators, and dotted bases. Function
   bodies remain as :class:`pychd.ir.UnknownBlock` for the LLM pass.

2. **Cross-version pass** (:mod:`pychd.cross_version`). Operates on an
   :mod:`xdis` code object for any other CPython 3.x release.
   Restricts itself to declaration-shaped opcode patterns that have
   been stable across every Python 3 release (``IMPORT_NAME``,
   ``MAKE_FUNCTION``, ``LOAD_BUILD_CLASS``, ``STORE_NAME``…).
   Intentionally lower-fidelity — defaults / annotations / decorator
   arguments are dropped — in exchange for working on every release.

The :func:`supported_version` helper reports whether *either* pass
will handle a given ``(major, minor)`` tuple; the caller in
:mod:`pychd.decompile` dispatches between the two automatically.
"""

from __future__ import annotations

import ast
import dis
import io
import sys
from dataclasses import dataclass
from types import CodeType
from typing import Any

from pychd import ir

CO_VARARGS = 0x04
CO_VARKEYWORDS = 0x08
CO_GENERATOR = 0x20
CO_COROUTINE = 0x80
CO_ASYNC_GENERATOR = 0x200

# SET_FUNCTION_ATTRIBUTE oparg values (Python 3.13+).
SFA_DEFAULTS = 0x01
SFA_KWDEFAULTS = 0x02
SFA_CLOSURE = 0x08
SFA_ANNOTATE = 0x10

_CLASS_IMPLICIT_NAMES = frozenset(
    {
        "__module__",
        "__qualname__",
        "__firstlineno__",
        "__static_attributes__",
        "__classdictcell__",
        "__annotate_func__",
        "__conditional_annotations__",
        "__type_params__",
    }
)

_MODULE_IMPLICIT_NAMES = frozenset(
    {
        "__conditional_annotations__",
    }
)


class UnsupportedVersionError(Exception):
    """Raised when the rule engine does not know how to read this bytecode."""


@dataclass
class RuleResult:
    module: ir.Module
    recovered: bool
    confidence: float


def supported_version(version_tuple: tuple) -> bool:
    """Return True if *some* rule pass handles this Python version.

    The native 3.14 pass requires the running interpreter to be 3.14
    *and* the .pyc to have been compiled for 3.14. Every other
    CPython 3.x release is handled by the cross-version pass in
    :mod:`pychd.cross_version`, provided xdis ships an opcode module
    for it (every release since 3.0 does).
    """
    from pychd import cross_version

    if version_tuple[:2] == (3, 14) and sys.version_info[:2] == (3, 14):
        return True
    return cross_version.supports(version_tuple)


def native_supported(version_tuple: tuple) -> bool:
    """True iff the *native* 3.14 walker can handle this bytecode."""
    return version_tuple[:2] == (3, 14) and sys.version_info[:2] == (3, 14)


def extract_module(code: CodeType) -> RuleResult:
    """Extract an `ir.Module` from a *native* module-level code object."""
    if sys.version_info[:2] != (3, 14):  # pragma: no cover - env guard
        raise UnsupportedVersionError(
            f"Native rule pass targets Python 3.14, got {sys.version_info[:2]}"
        )

    walker = _Walker(code)
    docstring, body = walker.walk_module()
    module = ir.Module(docstring=docstring, body=body)
    unknowns = module.unknown_blocks()
    return RuleResult(
        module=module,
        recovered=not unknowns,
        confidence=walker.confidence(),
    )


class _Walker:
    def __init__(self, code: CodeType) -> None:
        self.code = code
        self.instructions: list[dis.Instruction] = list(dis.Bytecode(code))
        self.total_ops = len(self.instructions)
        self.explained = 0

    def walk_module(self) -> tuple[str | None, list[ir.Stmt]]:
        docstring: str | None = None
        body: list[ir.Stmt] = []
        stmts = self._walk_body(self.code, is_class=False)
        for s in stmts:
            if (
                isinstance(s, ir.Assign)
                and s.target == "__doc__"
                and _is_string_literal(s.value)
            ):
                docstring = _strip_string_literal(s.value)
                continue
            body.append(s)
        # PEP 749: module-level annotations live in a synthetic
        # ``__annotate__`` closure (top of co_consts). Promote each
        # recovered annotation into the module body in the recorded
        # source order.
        annotations = _extract_annotations_from_annotate(self.code)
        if annotations:
            body = _splice_annotations(body, annotations)
        return docstring, body

    def confidence(self) -> float:
        if not self.total_ops:
            return 1.0
        return min(1.0, self.explained / self.total_ops)

    def _walk_body(self, code: CodeType, *, is_class: bool) -> list[ir.Stmt]:
        instructions = list(dis.Bytecode(code))
        ctx = _Context(self, instructions, is_class=is_class)
        return ctx.run()


class _Context:
    def __init__(
        self,
        walker: _Walker,
        instructions: list[dis.Instruction],
        *,
        is_class: bool,
    ) -> None:
        self.walker = walker
        self.ins = instructions
        self.pos = 0
        self.stack: list[Any] = []
        self.body: list[ir.Stmt] = []
        self.is_class = is_class

    def run(self) -> list[ir.Stmt]:
        while self.pos < len(self.ins):
            if not self._match():
                self.pos += 1
        return self.body

    def _match(self) -> bool:
        op = self.ins[self.pos].opname
        if op in {
            "RESUME",
            "MAKE_CELL",
            "COPY_FREE_VARS",
            "PUSH_NULL",
            "NOT_TAKEN",
            "CACHE",
            "SETUP_ANNOTATIONS",
            "LOAD_LOCALS",
            "STORE_DEREF",
            "LOAD_FAST_BORROW",
            # ``dis.Bytecode`` already merges EXTENDED_ARG into the
            # following instruction's `.arg`, but the EXTENDED_ARG marker
            # itself still appears in the stream. Skip silently.
            "EXTENDED_ARG",
        }:
            self._consume(1)
            self.pos += 1
            return True
        if op in {"RETURN_VALUE", "RETURN_CONST", "RETURN_GENERATOR"}:
            self._consume(1)
            self.pos += 1
            return True

        # ``_try_annotation_store`` recognises a 4-instruction window
        # (``LOAD_CONST ann ; LOAD_NAME __annotations__ ; LOAD_CONST
        # name ; STORE_SUBSCR``). It must run *before* ``_try_push``
        # eats the first ``LOAD_CONST`` — otherwise the pattern is
        # invisible by the time we reach STORE_SUBSCR.
        if self._try_annotation_store():
            return True
        # Module-level for-loops (and PEP 709 inlined comprehensions)
        # leak loop variables into our top-level body as bogus
        # ``excname = PYTHON2_EXCEPTIONS`` assigns. Detect the
        # ``GET_ITER`` / ``FOR_ITER`` boundary and skip the loop slice
        # entirely. This must run before ``_try_simple_store`` so the
        # loop-variable ``STORE_NAME`` never reaches it.
        if self._try_skip_loop():
            return True
        # Module-level subscript assigns (``dict[key] = value``) consume
        # 3 stack slots without leaving any value on stack. Recognise
        # this and emit a placeholder ``UnknownBlock`` so the body of
        # the surrounding scope keeps its declarations intact instead
        # of accumulating phantom Assigns from drained stack values.
        if self._try_store_subscr():
            return True
        # ``_try_if_block`` fires at module scope, before the
        # conditional's test pushes its ``LOAD_NAME`` onto our model
        # stack. It's a no-op inside function / class bodies where
        # structured control flow is the LLM pass's responsibility.
        #
        # A symmetric ``_try_except_block`` matcher (the
        # ``try: from X import Y except ImportError: from A import Y``
        # shape) is implemented in this module but not wired in: even
        # with a strict import-only gate it regressed ~15 modules
        # across the benchmark corpus from mis-bounded handler ranges.
        # Cleanly enabling it requires walking the exception table for
        # *all* nested entries, not just the entry starting at the
        # current offset.
        if not self.is_class and self._try_if_block():
            return True
        if self._try_push():
            return True
        if self._try_import():
            return True
        if self._try_class_def():
            return True
        if self._try_function_or_call():
            return True
        if self._try_simple_store():
            return True
        if self._try_pop_top():
            return True
        if self._try_copy():
            return True
        if self._try_build():
            return True
        if self._try_binop_call():
            return True

        return False

    def _try_skip_loop(self) -> bool:
        """Skip a module-level ``for`` loop entirely.

        Detects ``GET_ITER`` (followed by ``FOR_ITER target``) and
        advances past the entire loop body (up to and including the
        ``END_FOR`` / ``POP_TOP`` cleanup that immediately follows the
        loop target). The iterator value and per-iteration loop
        variables are *not* emitted into the recovered body — they're
        scope-locals that don't survive past the loop in source form
        either, and emitting them as module-level Assigns was the
        single biggest source of false-positive declarations in the
        earlier rule pass (the ``excname = PYTHON2_EXCEPTIONS`` leak
        from ``_compat_pickle.py`` is the canonical example).

        Any accumulator value present on the model stack before
        ``GET_ITER`` (e.g. a ``BUILD_MAP`` for an inlined dict
        comprehension) is preserved so the trailing ``STORE_NAME``
        can still pick it up — we just lose the comprehension body
        and report ``X = {}`` / ``X = []`` rather than the full
        ``{k: v for ...}``. Better than silently dropping the assign.
        """
        ins = self.ins[self.pos]
        if ins.opname not in {"GET_ITER", "FOR_ITER"}:
            return False
        # If we're sitting on GET_ITER, the iter argument is on stack
        # and FOR_ITER follows. Advance to FOR_ITER.
        if ins.opname == "GET_ITER":
            # Pop the iterable from our model stack (the real iter
            # value is intentionally untracked).
            if self.stack:
                self.stack.pop()
            self._consume(1)
            self.pos += 1
            # Optional CACHE / EXTENDED_ARG bookkeeping between
            # GET_ITER and FOR_ITER on some 3.x releases.
            while self.pos < len(self.ins) and self.ins[self.pos].opname in {
                "CACHE",
                "EXTENDED_ARG",
            }:
                self._consume(1)
                self.pos += 1
            if self.pos >= len(self.ins) or self.ins[self.pos].opname != "FOR_ITER":
                return True
        # Now at FOR_ITER. Walk to the jump target.
        for_iter = self.ins[self.pos]
        target_offset = for_iter.argval
        if target_offset is None:
            self._consume(1)
            self.pos += 1
            return True
        try:
            target_idx = next(
                i for i, x in enumerate(self.ins) if x.offset == target_offset
            )
        except StopIteration:
            self._consume(1)
            self.pos += 1
            return True
        # Consume everything from FOR_ITER up to (but not including) the
        # target instruction.
        skip = max(1, target_idx - self.pos)
        self._consume(skip)
        self.pos = target_idx
        # Past the target, CPython emits the loop cleanup epilogue:
        # ``END_FOR`` (3.12+) or ``POP_TOP`` (3.11-). Swallow it so
        # the outer walker doesn't try to interpret it as a stack op.
        while self.pos < len(self.ins) and self.ins[self.pos].opname in {
            "END_FOR",
            "POP_TOP",
            "POP_BLOCK",
        }:
            # ``POP_TOP`` here is the loop's iterator cleanup, not a
            # real expression statement — gate on the previous op
            # being a loop end.
            if self.ins[self.pos].opname == "POP_TOP" and self.stack:
                # POP_TOP after a loop drops the iter sentinel; we
                # didn't model the iter on our stack, so just consume.
                pass
            self._consume(1)
            self.pos += 1
            break
        return True

    def _try_store_subscr(self) -> bool:
        """Consume ``STORE_SUBSCR`` without emitting a phantom Assign.

        ``a[k] = v`` compiles to ``<v> ; <a> ; <k> ; STORE_SUBSCR``.
        The rule pass models scalars on its shadow stack but cannot
        reconstruct the original ``a[k] = v`` statement, so the
        cleanest recovery is to drop the three operands and emit
        nothing at module/class scope. Without this matcher the
        operands stay on stack and the next ``STORE_NAME`` would pick
        up the wrong value (the canonical ``NAME_MAPPING = (...)``
        leak from for-loop bodies in ``_compat_pickle.py``).
        """
        if self.ins[self.pos].opname != "STORE_SUBSCR":
            return False
        # Pop up to three values: key, container, value.
        for _ in range(3):
            if not self.stack:
                break
            self.stack.pop()
        self._consume(1)
        self.pos += 1
        return True

    def _try_push(self) -> bool:
        ins = self.ins[self.pos]
        op = ins.opname
        if op == "LOAD_CONST":
            self.stack.append(_Literal(ins.argval))
            self._consume(1)
            self.pos += 1
            return True
        if op == "LOAD_SMALL_INT":
            self.stack.append(_Literal(ins.argval))
            self._consume(1)
            self.pos += 1
            return True
        if op in {"LOAD_NAME", "LOAD_GLOBAL", "LOAD_FAST", "LOAD_DEREF"}:
            name = ins.argval
            self.stack.append(_Name(str(name)))
            self._consume(1)
            self.pos += 1
            return True
        if op == "LOAD_ATTR":
            # Extend the dotted name on top-of-stack: ``abc.ABC`` patterns.
            if self.stack and isinstance(self.stack[-1], _Name):
                self.stack[-1] = _Name(self.stack[-1].name + "." + str(ins.argval))
            else:
                # Attribute on a non-name expression — fall back to argval.
                self.stack.append(_Name(str(ins.argval)))
            self._consume(1)
            self.pos += 1
            return True
        if op == "LOAD_BUILD_CLASS":
            self.stack.append(_Sentinel("LOAD_BUILD_CLASS"))
            self._consume(1)
            self.pos += 1
            return True
        return False

    def _try_import(self) -> bool:
        if self.ins[self.pos].opname != "IMPORT_NAME":
            return False
        if len(self.stack) < 2:
            return False
        fromlist_val = self.stack[-1]
        level_val = self.stack[-2]
        if not (isinstance(fromlist_val, _Literal) and isinstance(level_val, _Literal)):
            return False
        fromlist = fromlist_val.value
        level = level_val.value if isinstance(level_val.value, int) else 0
        module = self.ins[self.pos].argval
        self.stack.pop()
        self.stack.pop()
        self._consume(1)
        self.pos += 1

        if fromlist is None:
            return self._finalise_plain_import(module)
        if isinstance(fromlist, tuple):
            names_list = [str(n) for n in fromlist]
            return self._finalise_from_import(module, level, names_list)
        return True

    def _finalise_plain_import(self, module: str) -> bool:
        # CPython uses STORE_GLOBAL instead of STORE_NAME at module
        # level when the module body shadows a name referenced from
        # nested functions (e.g. ``try: import _wmi`` in
        # ``platform.py``). Accept both — semantically identical for
        # a top-level import.
        store_ops = {"STORE_NAME", "STORE_GLOBAL"}
        if self.pos < len(self.ins) and self.ins[self.pos].opname in store_ops:
            target = self.ins[self.pos].argval
            self._consume(1)
            self.pos += 1
            top = module.split(".")[0]
            if target == top:
                self.body.append(ir.Import(names=[(module, None)]))
            else:
                self.body.append(ir.Import(names=[(module, target)]))
            return True
        if self.pos + 2 < len(self.ins) and (
            self.ins[self.pos].opname == "IMPORT_FROM"
            and self.ins[self.pos + 1].opname in store_ops
            and self.ins[self.pos + 2].opname == "POP_TOP"
        ):
            asname = self.ins[self.pos + 1].argval
            self._consume(3)
            self.pos += 3
            self.body.append(ir.Import(names=[(module, asname)]))
            return True
        return True

    def _finalise_from_import(
        self, module: str, level: int, fromlist: list[str]
    ) -> bool:
        if fromlist == ["*"]:
            # Python 3.13+ replaced the legacy `IMPORT_STAR` opcode with
            # `CALL_INTRINSIC_1 INTRINSIC_IMPORT_STAR` (intrinsic id 2).
            if self.pos < len(self.ins):
                star_op = self.ins[self.pos].opname
                star_arg = self.ins[self.pos].arg
                is_star = star_op == "IMPORT_STAR" or (
                    star_op == "CALL_INTRINSIC_1" and star_arg == 2
                )
            else:
                is_star = False
            if is_star:
                self._consume(1)
                self.pos += 1
                # CALL_INTRINSIC_1 leaves a None on the stack which a
                # trailing POP_TOP discards.
                if self.pos < len(self.ins) and self.ins[self.pos].opname == "POP_TOP":
                    self._consume(1)
                    self.pos += 1
                self.body.append(
                    ir.FromImport(module=module, level=level, names=[("*", None)])
                )
                return True
        names: list[tuple[str, str | None]] = []
        for expected in fromlist:
            if (
                self.pos + 1 < len(self.ins)
                and self.ins[self.pos].opname == "IMPORT_FROM"
                and self.ins[self.pos + 1].opname in {"STORE_NAME", "STORE_GLOBAL"}
            ):
                stored = self.ins[self.pos + 1].argval
                asname = stored if stored != expected else None
                names.append((expected, asname))
                self._consume(2)
                self.pos += 2
            else:
                break
        if self.pos < len(self.ins) and self.ins[self.pos].opname == "POP_TOP":
            self._consume(1)
            self.pos += 1
        if names:
            self.body.append(ir.FromImport(module=module, level=level, names=names))
        return True

    def _try_class_def(self) -> bool:
        # LOAD_BUILD_CLASS sentinel must be somewhere on stack.
        try:
            sent_idx = next(
                i
                for i, v in enumerate(reversed(self.stack))
                if isinstance(v, _Sentinel) and v.kind == "LOAD_BUILD_CLASS"
            )
        except StopIteration:
            return False
        sent_pos_in_stack = len(self.stack) - 1 - sent_idx

        if self.ins[self.pos].opname != "MAKE_FUNCTION":
            return False
        if not (
            self.stack
            and isinstance(self.stack[-1], _Literal)
            and isinstance(self.stack[-1].value, CodeType)
        ):
            return False
        candidate_code = self.stack[-1].value
        # The code object must be directly above the LOAD_BUILD_CLASS sentinel.
        if sent_pos_in_stack >= len(self.stack) - 1:
            return False
        self.stack.pop()  # the code-object literal
        self._consume(1)
        self.pos += 1

        # Skip any EXTENDED_ARG markers between MAKE_FUNCTION and the
        # class-name LOAD_CONST.
        while self.pos < len(self.ins) and self.ins[self.pos].opname == "EXTENDED_ARG":
            self._consume(1)
            self.pos += 1
        # LOAD_CONST <class_name>
        class_name: str | None = None
        if self.pos < len(self.ins) and self.ins[self.pos].opname == "LOAD_CONST":
            class_name = self.ins[self.pos].argval
            self._consume(1)
            self.pos += 1
        bases: list[str] = []
        keywords: list[tuple[str, str]] = []
        while self.pos < len(self.ins):
            op = self.ins[self.pos].opname
            if op == "EXTENDED_ARG":
                self._consume(1)
                self.pos += 1
                continue
            if op == "CALL":
                self._consume(1)
                self.pos += 1
                break
            if op in {"LOAD_NAME", "LOAD_GLOBAL", "LOAD_FAST", "LOAD_DEREF"}:
                bases.append(str(self.ins[self.pos].argval))
                self._consume(1)
                self.pos += 1
                continue
            if op == "LOAD_ATTR" and bases:
                # Dotted base classes: ``LOAD_NAME abc; LOAD_ATTR ABC`` →
                # ``abc.ABC``.
                bases[-1] = bases[-1] + "." + str(self.ins[self.pos].argval)
                self._consume(1)
                self.pos += 1
                continue
            if op == "LOAD_CONST":
                self._consume(1)
                self.pos += 1
                continue
            if op == "KW_NAMES":
                self._consume(1)
                self.pos += 1
                continue
            break

        # Decorator-with-args chain applied to a class produces extra
        # ``CALL`` opcodes after the class body and before STORE_NAME:
        #
        #   ``@dataclass(frozen=True) class C:`` →
        #     LOAD_NAME dataclass ; CALL_KW 1 ; LOAD_BUILD_CLASS …
        #     CALL 2 (build class) ; CALL 0 (apply decorator) ; STORE_NAME C
        #
        # Consume those decorator-application CALLs so we don't emit
        # ``C = ...`` *as well as* ``class C:`` below.
        decorator_calls = 0
        while self.pos < len(self.ins) and self.ins[self.pos].opname == "CALL":
            decorator_calls += 1
            self._consume(1)
            self.pos += 1

        store_name = class_name
        if self.pos < len(self.ins) and self.ins[self.pos].opname == "STORE_NAME":
            store_name = self.ins[self.pos].argval
            self._consume(1)
            self.pos += 1
        # The class's decorator values sit on the stack *below* the
        # LOAD_BUILD_CLASS sentinel: each decorator-application CALL
        # consumes one. Slice them out in their original (top-down)
        # source order. Previously discarded silently; now we render
        # each one if possible so ``@dataclass(frozen=True)`` survives
        # the round-trip.
        deco_start = max(0, sent_pos_in_stack - decorator_calls)
        deco_vals = self.stack[deco_start:sent_pos_in_stack]
        decorators: list[str] = []
        for value in deco_vals:
            rendered = _render_value(value)
            if rendered is not None:
                decorators.append(rendered)
        # Remove both the decorator values and the sentinel itself.
        del self.stack[deco_start:]

        class_def = _build_class(
            candidate_code, store_name or class_name or "Anon", bases, keywords
        )
        class_def.decorators = decorators + class_def.decorators
        self.body.append(class_def)
        return True

    def _try_function_or_call(self) -> bool:
        if self.ins[self.pos].opname != "MAKE_FUNCTION":
            return False
        if not (
            self.stack
            and isinstance(self.stack[-1], _Literal)
            and isinstance(self.stack[-1].value, CodeType)
        ):
            return False
        code_obj = self.stack[-1].value

        # PEP 695 generic syntax (``def f[T](x): ...`` / ``class C[T]: ...``)
        # wraps the real definition in a synthetic ``<generic parameters of
        # X>`` closure. Recognise that and unpack: walk the wrapper's
        # bytecode to find the underlying class or function code object.
        if code_obj.co_name.startswith("<generic parameters of "):
            return self._unwrap_generic_params(code_obj)
        code_obj = self.stack[-1].value
        self.stack.pop()
        self._consume(1)
        self.pos += 1

        defaults: tuple | None = None
        kwdefaults: dict | None = None
        has_annotations = False
        while (
            self.pos < len(self.ins)
            and self.ins[self.pos].opname == "SET_FUNCTION_ATTRIBUTE"
        ):
            flag = self.ins[self.pos].arg or 0
            top = self.stack.pop() if self.stack else None
            if (
                flag == SFA_DEFAULTS
                and isinstance(top, _Literal)
                and isinstance(top.value, tuple)
            ):
                defaults = top.value
            elif flag == SFA_KWDEFAULTS:
                if isinstance(top, _Mapping):
                    unwrapped: dict[str, Any] = {}
                    for k, v in top.items:
                        key = _unwrap_literal(k)
                        if isinstance(key, str):
                            unwrapped[key] = _unwrap_literal(v)
                    kwdefaults = unwrapped
                elif isinstance(top, _Literal) and isinstance(top.value, dict):
                    kwdefaults = top.value
            elif flag == SFA_ANNOTATE:
                has_annotations = True
            elif flag == SFA_CLOSURE:
                pass
            self._consume(1)
            self.pos += 1

        # decorator chain
        decorator_count = 0
        while self.pos < len(self.ins) and self.ins[self.pos].opname == "CALL":
            decorator_count += 1
            self._consume(1)
            self.pos += 1
            if self.pos < len(self.ins) and self.ins[self.pos].opname == "PUSH_NULL":
                self._consume(1)
                self.pos += 1

        if self.pos >= len(self.ins) or self.ins[self.pos].opname not in {
            "STORE_NAME",
            "STORE_FAST",
            "STORE_DEREF",
            "STORE_GLOBAL",
        }:
            self.stack.append(
                _FunctionValue(code_obj, defaults, kwdefaults, has_annotations)
            )
            return True
        name = self.ins[self.pos].argval
        self._consume(1)
        self.pos += 1

        decorators: list[str] = []
        for _ in range(decorator_count):
            if not self.stack:
                break
            value = self.stack.pop()
            if isinstance(value, _Name):
                decorators.append(value.name)
            elif isinstance(value, _CallExpr):
                decorators.append(value.text)
            elif isinstance(value, _Sentinel):
                # Opaque value (e.g. nested call we couldn't render):
                # drop silently rather than emitting a placeholder.
                continue
            else:
                rendered = _render_value(value)
                if rendered is not None:
                    decorators.append(rendered)
        decorators.reverse()

        func_def = _build_function(
            code_obj,
            name=name,
            defaults=defaults,
            kwdefaults=kwdefaults,
            has_annotations=has_annotations,
            decorators=decorators,
        )
        if self.is_class and name in _CLASS_IMPLICIT_NAMES:
            return True
        # PEP 749: a synthetic ``__annotate__`` closure is created for
        # every annotated module-level scope. Suppress it from output —
        # users never wrote ``def __annotate__``.
        if name == "__annotate__":
            return True
        self.body.append(func_def)
        return True

    def _unwrap_generic_params(self, wrapper_code: CodeType) -> bool:
        """Recognise PEP 695 ``<generic parameters of X>`` wrappers.

        The wrapper's bytecode contains either ``LOAD_BUILD_CLASS …`` (for
        a generic class) or ``LOAD_CONST <inner_code>; MAKE_FUNCTION …``
        (for a generic function). We re-extract the underlying definition
        and emit it as if PEP 695 had not been used; type parameter
        recovery itself is out of scope for v1 (the names live in the
        wrapper's locals).
        """
        target_name = wrapper_code.co_name[len("<generic parameters of ") : -1]
        type_params = _extract_pep695_type_params(wrapper_code)
        inner_code: CodeType | None = None
        for c in wrapper_code.co_consts:
            if isinstance(c, CodeType) and c.co_name == target_name:
                inner_code = c
                break
        # Consume MAKE_FUNCTION ; PUSH_NULL ; CALL ; STORE_NAME.
        # Pop the code-object literal we peeked at first.
        self.stack.pop()
        self._consume(1)
        self.pos += 1
        while self.pos < len(self.ins) and self.ins[self.pos].opname in {
            "PUSH_NULL",
            "CALL",
            "CALL_KW",
        }:
            self._consume(1)
            self.pos += 1
        store_name = target_name
        if self.pos < len(self.ins) and self.ins[self.pos].opname in {
            "STORE_NAME",
            "STORE_FAST",
            "STORE_DEREF",
            "STORE_GLOBAL",
        }:
            store_name = self.ins[self.pos].argval
            self._consume(1)
            self.pos += 1
        if inner_code is None:
            return True
        # CO_NEWLOCALS (0x02) is *clear* on a class body and *set* on a
        # function. We previously wrote ``& 0x02 == 0`` without parens,
        # which Python parses as ``& (0x02 == 0)`` → always ``& 0`` →
        # always falsy. The heuristic below was therefore the only
        # discriminator in practice. Parenthesising restores the
        # intended bitwise test as a fast path, with the qualname
        # heuristic as fallback.
        if (inner_code.co_flags & 0x02) == 0 and any(
            isinstance(c, CodeType) and c.co_name == "__build_class__"
            for c in wrapper_code.co_consts
        ):
            inner_is_class = True
        else:
            # Heuristic: class code objects have STORE_NAME __qualname__
            # near the top; functions don't.
            inner_is_class = any(
                ins.opname == "STORE_NAME" and ins.argval == "__qualname__"
                for ins in dis.Bytecode(inner_code)
            )
        if inner_is_class:
            cls = _build_class(inner_code, store_name, bases=[], keywords=[])
            if type_params:
                cls.name = cls.name + "[" + ", ".join(type_params) + "]"
            self.body.append(cls)
        else:
            func = _build_function(
                inner_code,
                name=store_name,
                defaults=None,
                kwdefaults=None,
                has_annotations=False,
                decorators=[],
            )
            if type_params:
                func.name = func.name + "[" + ", ".join(type_params) + "]"
            self.body.append(func)
        return True

    def _try_annotation_store(self) -> bool:
        if self.pos + 3 >= len(self.ins):
            return False
        a, b, c, d = self.ins[self.pos : self.pos + 4]
        if not (
            a.opname == "LOAD_CONST"
            and b.opname == "LOAD_NAME"
            and b.argval == "__annotations__"
            and c.opname == "LOAD_CONST"
            and d.opname == "STORE_SUBSCR"
        ):
            return False
        annotation_value = a.argval
        var_name = c.argval
        if self.stack and isinstance(self.stack[-1], _Literal):
            self.stack.pop()
        if self.stack and isinstance(self.stack[-1], _Name):
            self.stack.pop()
        if self.stack and isinstance(self.stack[-1], _Literal):
            self.stack.pop()
        self._consume(4)
        self.pos += 4

        if self.body and isinstance(self.body[-1], ir.Assign):
            prev = self.body[-1]
            if prev.target == var_name:
                prev.annotation = (
                    annotation_value if isinstance(annotation_value, str) else None
                )
                return True
        if isinstance(annotation_value, str):
            self.body.append(
                ir.AnnotationOnly(target=var_name, annotation=annotation_value)
            )
        return True

    def _try_simple_store(self) -> bool:
        ins = self.ins[self.pos]
        if ins.opname not in {
            "STORE_NAME",
            "STORE_FAST",
            "STORE_DEREF",
            "STORE_GLOBAL",
        }:
            return False
        target = ins.argval
        # Skip STORE_FAST in module/class scope — those are loop locals
        # from inlined comprehensions (Python 3.12+ inlines list/set/dict
        # comprehensions). They're not module-visible names.
        if ins.opname == "STORE_FAST":
            self._consume(1)
            self.pos += 1
            return True
        if (
            self.is_class and target in _CLASS_IMPLICIT_NAMES
        ) or target in _MODULE_IMPLICIT_NAMES:
            if self.stack:
                self.stack.pop()
            self._consume(1)
            self.pos += 1
            return True
        # Lost stack frame (complex RHS we don't track exactly, e.g.
        # inlined comprehensions, BUILD_SLICE, generator subexpressions):
        # still emit a placeholder so the *name* survives. The metric
        # cares about presence, not RHS fidelity.
        if not self.stack:
            # Detect inlined comprehensions: the preceding instruction
            # window typically ends with END_FOR / POP_TOP after a
            # MAP_ADD / LIST_APPEND / SET_ADD loop. In that case the
            # accumulator literal is what was *supposed* to be on the
            # stack — guess the type from the trailing MAP/LIST/SET
            # opcode so the assign at least carries a same-shape RHS.
            kind_hint = self._guess_comprehension_kind()
            placeholder = {
                "list": "[]",
                "set": "set()",
                "dict": "{}",
            }.get(kind_hint, "...")
            self.body.append(ir.Assign(target=target, value=placeholder))
            self._consume(1)
            self.pos += 1
            return True
        value = self.stack.pop()
        rendered = _render_value(value)
        self._consume(1)
        self.pos += 1
        if rendered is None:
            self.body.append(ir.Assign(target=target, value="..."))
            return True
        self.body.append(ir.Assign(target=target, value=rendered))
        return True

    def _guess_comprehension_kind(self) -> str | None:
        """Look backwards from the current position for a comprehension
        accumulator opcode (``MAP_ADD`` / ``LIST_APPEND`` / ``SET_ADD``).

        We scan at most 50 instructions back — comprehensions don't get
        much larger than that without spilling to a real loop.
        Returns one of ``"dict"`` / ``"list"`` / ``"set"`` or ``None``
        when no comprehension pattern is detected.
        """
        for i in range(self.pos - 1, max(-1, self.pos - 50), -1):
            op = self.ins[i].opname
            if op == "MAP_ADD":
                return "dict"
            if op == "LIST_APPEND":
                return "list"
            if op == "SET_ADD":
                return "set"
            if op in {"STORE_NAME", "STORE_GLOBAL"}:
                # Hit the previous statement boundary — give up.
                return None
        return None

    def _try_pop_top(self) -> bool:
        if self.ins[self.pos].opname != "POP_TOP":
            return False
        if self.stack:
            self.stack.pop()
        self._consume(1)
        self.pos += 1
        return True

    def _try_copy(self) -> bool:
        """Handle ``COPY n`` (duplicate stack element n-from-top).

        Critical for multi-target chained assigns: ``a = b = expr`` is
        compiled as ``<expr> ; COPY 1 ; STORE_NAME a ; STORE_NAME b``.
        Without this, the second STORE_NAME finds the stack empty and
        the assignment is dropped.
        """
        if self.ins[self.pos].opname not in {"COPY", "DUP_TOP"}:
            return False
        arg = self.ins[self.pos].arg
        if self.stack and (arg is None or arg == 1):
            self.stack.append(self.stack[-1])
        elif self.stack and isinstance(arg, int) and arg <= len(self.stack):
            self.stack.append(self.stack[-arg])
        self._consume(1)
        self.pos += 1
        return True

    def _try_build(self) -> bool:
        ins = self.ins[self.pos]
        op = ins.opname
        if op == "BUILD_LIST":
            n = ins.arg or 0
            items = self._pop_n(n)
            self.stack.append(_Collection("list", items))
            self._consume(1)
            self.pos += 1
            return True
        if op == "BUILD_TUPLE":
            n = ins.arg or 0
            items = self._pop_n(n)
            self.stack.append(_Collection("tuple", items))
            self._consume(1)
            self.pos += 1
            return True
        if op == "BUILD_SET":
            n = ins.arg or 0
            items = self._pop_n(n)
            self.stack.append(_Collection("set", items))
            self._consume(1)
            self.pos += 1
            return True
        if op == "LIST_EXTEND":
            # Pattern: BUILD_LIST 0; LOAD_CONST <tuple>; LIST_EXTEND 1.
            # The compiler uses this for module-level pure-constant lists.
            if (
                len(self.stack) >= 2
                and isinstance(self.stack[-1], _Literal)
                and isinstance(self.stack[-2], _Collection)
                and self.stack[-2].kind == "list"
            ):
                lit = self.stack.pop()
                coll = self.stack.pop()
                if isinstance(lit.value, (tuple, list)):
                    coll.items.extend(_Literal(v) for v in lit.value)
                    self.stack.append(coll)
                else:
                    self.stack.append(coll)
            self._consume(1)
            self.pos += 1
            return True
        if op == "SET_UPDATE":
            # Pattern: BUILD_SET 0; LOAD_CONST <frozenset>; SET_UPDATE 1.
            if (
                len(self.stack) >= 2
                and isinstance(self.stack[-1], _Literal)
                and isinstance(self.stack[-2], _Collection)
                and self.stack[-2].kind == "set"
            ):
                lit = self.stack.pop()
                coll = self.stack.pop()
                values = lit.value
                if isinstance(values, (frozenset, set)):
                    coll.items.extend(_Literal(v) for v in sorted(values, key=repr))
                self.stack.append(coll)
            self._consume(1)
            self.pos += 1
            return True
        if op == "DICT_UPDATE" or op == "DICT_MERGE":
            # Pattern: BUILD_MAP 0; LOAD_CONST <dict>; DICT_UPDATE.
            if (
                len(self.stack) >= 2
                and isinstance(self.stack[-1], _Literal)
                and isinstance(self.stack[-2], _Mapping)
                and isinstance(self.stack[-1].value, dict)
            ):
                lit = self.stack.pop()
                mapping = self.stack.pop()
                for k, v in lit.value.items():
                    mapping.items.append((_Literal(k), _Literal(v)))
                self.stack.append(mapping)
            self._consume(1)
            self.pos += 1
            return True
        if op == "BUILD_MAP":
            n = ins.arg or 0
            pairs = self._pop_n(2 * n)
            keys = pairs[0::2]
            values = pairs[1::2]
            ordered: list[tuple[Any, Any]] = list(zip(keys, values))
            self.stack.append(_Mapping(ordered, {}))
            self._consume(1)
            self.pos += 1
            return True
        if op == "BUILD_CONST_KEY_MAP":
            n = ins.arg or 0
            if not self.stack or not isinstance(self.stack[-1], _Literal):
                return False
            keys_lit = self.stack.pop()
            keys = keys_lit.value if isinstance(keys_lit.value, tuple) else ()
            values = self._pop_n(n)
            ordered = list(zip(list(keys), values))
            self.stack.append(_Mapping(ordered, {}))
            self._consume(1)
            self.pos += 1
            return True
        if op == "MAP_ADD":
            # ``MAP_ADD i``: pop ``(k, v)`` from the top, look up the
            # accumulating dict ``i`` slots below the (now popped) pair,
            # and append ``(k, v)`` to it. CPython uses this both for
            # dict comprehensions *and* for plain module-level dict
            # literals whose keys are non-literal (e.g. tuple keys —
            # the ``_compat_pickle.NAME_MAPPING`` shape).
            if len(self.stack) < 2:
                self._consume(1)
                self.pos += 1
                return True
            v = self.stack.pop()
            k = self.stack.pop()
            i = ins.arg or 1
            dict_idx = len(self.stack) - i
            if 0 <= dict_idx < len(self.stack):
                target = self.stack[dict_idx]
                if isinstance(target, _Mapping):
                    target.items.append((k, v))
            self._consume(1)
            self.pos += 1
            return True
        if op == "LIST_APPEND":
            # ``LIST_APPEND i``: pop the top, append to list ``i`` slots
            # below (after the pop). Used by list comprehensions.
            if not self.stack:
                self._consume(1)
                self.pos += 1
                return True
            v = self.stack.pop()
            i = ins.arg or 1
            list_idx = len(self.stack) - i
            if 0 <= list_idx < len(self.stack):
                target = self.stack[list_idx]
                if isinstance(target, _Collection) and target.kind == "list":
                    target.items.append(v)
            self._consume(1)
            self.pos += 1
            return True
        if op == "SET_ADD":
            if not self.stack:
                self._consume(1)
                self.pos += 1
                return True
            v = self.stack.pop()
            i = ins.arg or 1
            set_idx = len(self.stack) - i
            if 0 <= set_idx < len(self.stack):
                target = self.stack[set_idx]
                if isinstance(target, _Collection) and target.kind == "set":
                    target.items.append(v)
            self._consume(1)
            self.pos += 1
            return True
        return False

    # ------------------------------------------------------------------
    # Module-level structured control flow
    # ------------------------------------------------------------------

    def _try_if_block(self) -> bool:
        """Recognise ``if <expr>:`` blocks at module scope.

        Two shapes are recognised:

        1. **Bare-name guard** (the historical case, e.g.
           ``if TYPE_CHECKING:``):
           ``LOAD_NAME X ; (TO_BOOL)? ; POP_JUMP_IF_FALSE T ; (NOT_TAKEN)?``.
        2. **Name-vs-string comparison** (the
           ``if __name__ == "__main__":`` pattern):
           ``LOAD_NAME X ; LOAD_CONST 'literal' ; COMPARE_OP == ;
           POP_JUMP_IF_FALSE T ; (NOT_TAKEN)?``.

        Anything more complex than these two shapes (boolean ``or`` /
        ``and``, attribute access, calls) is ignored — those rarely
        guard imports / main entry points and add disproportionate
        walker complexity. On a match we slice out the if-body
        instructions, run a fresh ``_Context`` over them, and emit a
        single ``ir.If`` node so the structure survives with correct
        indentation instead of being flattened to top level.
        """
        if self.pos >= len(self.ins):
            return False
        ins = self.ins[self.pos]
        if ins.opname not in {"LOAD_NAME", "LOAD_GLOBAL"}:
            return False
        test_name = str(ins.argval)
        cur = self.pos + 1
        test_expr = test_name
        # Shape 2: ``LOAD_NAME X ; LOAD_CONST 'literal' ; COMPARE_OP ==``.
        if (
            cur + 1 < len(self.ins)
            and self.ins[cur].opname == "LOAD_CONST"
            and self.ins[cur + 1].opname == "COMPARE_OP"
            and isinstance(self.ins[cur].argval, (str, int, float, bool, bytes))
        ):
            # CPython 3.12+ packs the comparison operator into the argval
            # text ("bool(==)" / "bool(!=)"); 3.11 reports the op name
            # directly. Accept either by string-matching the rendered
            # operator.
            comp_arg = str(self.ins[cur + 1].argrepr)
            if "==" in comp_arg or "!=" in comp_arg:
                op_symbol = "==" if "==" in comp_arg else "!="
                test_expr = f"{test_name} {op_symbol} {self.ins[cur].argval!r}"
                cur += 2
        # Optional TO_BOOL on 3.12+.
        if cur < len(self.ins) and self.ins[cur].opname == "TO_BOOL":
            cur += 1
        if cur >= len(self.ins) or self.ins[cur].opname != "POP_JUMP_IF_FALSE":
            return False
        jump = self.ins[cur]
        target_offset = jump.argval
        if target_offset is None:
            return False
        try:
            target_idx = next(
                i for i, x in enumerate(self.ins) if x.offset == target_offset
            )
        except StopIteration:
            return False
        body_start = cur + 1
        # ``NOT_TAKEN`` (3.14) sits between POP_JUMP_IF_FALSE and the
        # body to mark a constant-folded branch — skip it.
        if body_start < len(self.ins) and self.ins[body_start].opname == "NOT_TAKEN":
            body_start += 1
        if target_idx <= body_start:
            return False
        # The body slice is everything from body_start up to (but not
        # including) the target index. The target instruction itself is
        # typically ``NOP`` (CPython emits a NOP as the if-block exit
        # marker on 3.12+) which the outer walker will skip naturally.
        body_slice = self.ins[body_start:target_idx]
        sub_ctx = _Context(self.walker, list(body_slice), is_class=False)
        body_stmts = sub_ctx.run()
        # Advance past the entire if structure: jump op + optional
        # NOT_TAKEN + the body + the target NOP marker.
        self._consume(target_idx - self.pos)
        self.pos = target_idx
        if self.pos < len(self.ins) and self.ins[self.pos].opname == "NOP":
            self._consume(1)
            self.pos += 1
        self.body.append(ir.If(test=test_expr, body=body_stmts, orelse=[]))
        return True

    def _try_except_block(self) -> bool:
        """Recognise ``try: <single import> except <Exc>: <single import>``.

        Conservative shape match. The 3.11+ exception-table encoding
        makes it easy to find the *start* of a try body via the
        in-bytecode boundary, but cleanly finding the *end* of a
        handler is hard: the compiler inlines a re-raise scaffold and
        a forward jump back into the module's continuation, and a
        single-instruction overrun silently drops every subsequent
        module-level declaration.

        Rather than risk that regression, the matcher only fires on
        the narrow shape that motivated the work in the first place:

            try:
                from X import Y, Z    # or `import M`
            except ImportError:
                from A import Y, Z    # or `import M`

        Anything else falls through and is recovered by the existing
        flattening behaviour: imports survive at module scope (so
        ``signature_match`` is unaffected), only the ``if`` / ``try``
        indentation is lost.
        """
        import dis as _dis

        parser = getattr(_dis, "_parse_exception_table", None)
        if parser is None:
            return False
        entries = list(parser(self.walker.code))
        if not entries:
            return False
        cur_offset = self.ins[self.pos].offset
        match = next((e for e in entries if e.start == cur_offset), None)
        if match is None:
            return False
        try:
            body_end_idx = next(
                i for i, x in enumerate(self.ins) if x.offset >= match.end
            )
            handler_idx = next(
                i for i, x in enumerate(self.ins) if x.offset == match.target
            )
        except StopIteration:
            return False

        body_slice = self.ins[self.pos : body_end_idx]
        # Gate: the try body must look exactly like a single import.
        body_kinds = {ins.opname for ins in body_slice}
        allowed_import_only = {
            "LOAD_CONST",
            "LOAD_SMALL_INT",
            "IMPORT_NAME",
            "IMPORT_FROM",
            "STORE_NAME",
            "STORE_GLOBAL",
            "POP_TOP",
            "RESUME",
            "PUSH_NULL",
            "NOP",
            "JUMP_FORWARD",
        }
        if not body_kinds.issubset(allowed_import_only):
            return False
        if "IMPORT_NAME" not in body_kinds:
            return False

        body_ctx = _Context(self.walker, list(body_slice), is_class=False)
        body_stmts = body_ctx.run()
        if not body_stmts:
            return False

        # Walk the handler prologue: PUSH_EXC_INFO ; LOAD_NAME <Exc> ;
        # CHECK_EXC_MATCH ; POP_JUMP_IF_FALSE ; (NOT_TAKEN)? ; POP_TOP.
        exc_name: str = ""
        cursor = handler_idx
        if cursor < len(self.ins) and self.ins[cursor].opname == "PUSH_EXC_INFO":
            cursor += 1
        if cursor < len(self.ins) and self.ins[cursor].opname in {
            "LOAD_NAME",
            "LOAD_GLOBAL",
        }:
            exc_name = str(self.ins[cursor].argval)
            cursor += 1
        for expected in ("CHECK_EXC_MATCH", "POP_JUMP_IF_FALSE", "NOT_TAKEN"):
            if cursor < len(self.ins) and self.ins[cursor].opname == expected:
                cursor += 1
        if cursor < len(self.ins) and self.ins[cursor].opname == "POP_TOP":
            cursor += 1

        # Bound the handler by the first POP_EXCEPT. Gate again on the
        # same allow-list so any non-trivial handler falls through.
        handler_end = cursor
        while handler_end < len(self.ins):
            if self.ins[handler_end].opname == "POP_EXCEPT":
                break
            handler_end += 1
        if handler_end >= len(self.ins):
            return False
        handler_slice = self.ins[cursor:handler_end]
        handler_kinds = {ins.opname for ins in handler_slice}
        if not handler_kinds.issubset(allowed_import_only):
            return False
        if "IMPORT_NAME" not in handler_kinds:
            return False

        handler_ctx = _Context(self.walker, list(handler_slice), is_class=False)
        handler_stmts = handler_ctx.run()
        if not handler_stmts:
            return False

        # Advance past the entire try/handler region. After POP_EXCEPT
        # the compiler emits a JUMP_FORWARD to the module's
        # continuation and a small RERAISE scaffold — the outer walker
        # absorbs both via its noop dispatch.
        self.pos = handler_end
        self._consume(1)
        self.pos += 1
        self.body.append(ir.Try(body=body_stmts, handlers=[(exc_name, handler_stmts)]))
        return True

    def _try_binop_call(self) -> bool:
        op = self.ins[self.pos].opname
        if op == "CALL":
            argc = self.ins[self.pos].arg or 0
            popped = self._pop_n(argc + 1)
            rendered = _try_render_call(popped, kw_names=None)
            if rendered is not None:
                self.stack.append(_CallExpr(rendered))
            else:
                self.stack.append(_Sentinel("call_result"))
            self._consume(1)
            self.pos += 1
            return True
        if op == "CALL_KW":
            argc = self.ins[self.pos].arg or 0
            popped = self._pop_n(argc + 2)
            kw_names: tuple | None = None
            if (
                popped
                and isinstance(popped[-1], _Literal)
                and isinstance(popped[-1].value, tuple)
            ):
                kw_names = popped[-1].value
                popped = popped[:-1]
            rendered = _try_render_call(popped, kw_names=kw_names)
            if rendered is not None:
                self.stack.append(_CallExpr(rendered))
            else:
                self.stack.append(_Sentinel("call_result"))
            self._consume(1)
            self.pos += 1
            return True
        if op == "TO_BOOL":
            self._consume(1)
            self.pos += 1
            return True
        if op.startswith("POP_JUMP") or op in {
            "JUMP_FORWARD",
            "JUMP_BACKWARD",
            "JUMP_BACKWARD_NO_INTERRUPT",
        }:
            self._consume(1)
            self.pos += 1
            return True
        return False

    def _pop_n(self, n: int) -> list[Any]:
        if n <= 0:
            return []
        if n > len(self.stack):
            taken = list(self.stack)
            self.stack.clear()
            return taken
        out = self.stack[-n:]
        del self.stack[-n:]
        return out

    def _consume(self, n: int) -> None:
        self.walker.explained += n


def _build_function(
    code: CodeType,
    *,
    name: str,
    defaults: tuple | None,
    kwdefaults: dict | None,
    has_annotations: bool,
    decorators: list[str],
) -> ir.FunctionDef:
    is_async = bool(code.co_flags & CO_COROUTINE) or bool(
        code.co_flags & CO_ASYNC_GENERATOR
    )
    is_generator = bool(code.co_flags & CO_GENERATOR)
    args = _arguments_from_code(code, defaults=defaults, kwdefaults=kwdefaults)
    docstring = _extract_docstring(code)
    # `has_annotations` is captured today only as a marker — annotation
    # recovery from the lazy `__annotate__` closure is a v2 task.
    _ = has_annotations
    trivial = _try_recover_trivial_body(code, has_docstring=docstring is not None)
    if trivial is None:
        trivial = _try_recover_statement_body(code, has_docstring=docstring is not None)
    body: list[ir.Stmt]
    if trivial is not None:
        body = [ir.RawStatement(source=trivial)]
    else:
        body = [ir.UnknownBlock(disassembly=_dis_text(code), signature=f"def {name}")]
    return ir.FunctionDef(
        name=name,
        args=args,
        decorators=decorators,
        is_async=is_async,
        is_generator=is_generator,
        docstring=docstring,
        body=body,
        return_annotation=None,
    )


# Opcodes that introduce a value the trivial-body recogniser knows how to
# render symbolically. Anything outside this set bails to UnknownBlock.
_TRIVIAL_PROLOGUE = frozenset(
    {"RESUME", "MAKE_CELL", "COPY_FREE_VARS", "CACHE", "EXTENDED_ARG", "NOT_TAKEN"}
)


def _try_recover_trivial_body(code: CodeType, *, has_docstring: bool) -> str | None:
    """Recover the body of a function whose entire payload is one statement.

    Handles the common closed-form cases that account for the bulk of
    one-line definitions in real codebases:

    * ``return CONST`` — emitted as either ``RETURN_CONST x`` (Python
      3.12+) or ``LOAD_CONST x ; RETURN_VALUE``.
    * ``return name`` / ``return self.attr.attr2`` — a single
      ``LOAD_FAST`` / ``LOAD_NAME`` / ``LOAD_GLOBAL`` /
      ``LOAD_DEREF`` (possibly with ``LOAD_FAST_BORROW`` on 3.14)
      followed by zero or more ``LOAD_ATTR`` and ending in
      ``RETURN_VALUE``.
    * ``pass`` — the entire body collapses to ``LOAD_CONST None ;
      RETURN_VALUE`` once the docstring (if any) is skipped.

    Anything else returns ``None`` and the body becomes an
    ``UnknownBlock`` for the LLM pass to handle.

    The result is the *body source*, indented with 4 spaces and ready
    to splice into the rendered function definition.
    """
    if code.co_flags & (CO_GENERATOR | CO_COROUTINE | CO_ASYNC_GENERATOR):
        # Generators / coroutines have implicit prologue (RETURN_GENERATOR)
        # and the body is not simply ``return X``.
        return None
    if code.co_freevars:
        # A function with free variables closes over an outer scope.
        # A bare ``return name`` recovered here would be lexically wrong
        # — the rendered standalone function has no enclosing scope to
        # bind ``name`` to. Defer to the LLM body-fill rather than
        # emit code that parses but is semantically broken at runtime.
        return None

    instructions = [
        ins for ins in dis.Bytecode(code) if ins.opname not in _TRIVIAL_PROLOGUE
    ]
    if not instructions:
        return None

    # Skip the docstring's LOAD_CONST + POP_TOP, if any.
    if (
        has_docstring
        and len(instructions) >= 2
        and instructions[0].opname == "LOAD_CONST"
        and isinstance(instructions[0].argval, str)
        and instructions[1].opname == "POP_TOP"
    ):
        instructions = instructions[2:]
        if not instructions:
            return None

    # ``return CONST`` via the 3.12+ fused opcode.
    if len(instructions) == 1 and instructions[0].opname == "RETURN_CONST":
        return f"return {_format_literal(instructions[0].argval)}"

    # Locate the trailing RETURN_VALUE if present.
    if instructions[-1].opname != "RETURN_VALUE":
        return None
    head = instructions[:-1]
    if not head:
        return None

    # ``pass`` / no body: just ``LOAD_CONST None``.
    if (
        len(head) == 1
        and head[0].opname in {"LOAD_CONST", "LOAD_COMMON_CONSTANT"}
        and head[0].argval is None
    ):
        return "pass"

    # ``return <literal>`` — direct constant load.
    if len(head) == 1 and head[0].opname in {
        "LOAD_CONST",
        "LOAD_SMALL_INT",
        "LOAD_COMMON_CONSTANT",
    }:
        return f"return {_format_literal(head[0].argval)}"

    # ``return name(.attr)*`` — a single name push optionally extended by
    # attribute loads.
    expr = _try_render_name_chain(head)
    if expr is not None:
        return f"return {expr}"

    # Container / call / binary-op shapes — delegate to the
    # cross-version recogniser, which handles every CPython 3.x.
    # Importing here (rather than at module load) keeps the import
    # graph cycle-free.
    from pychd.cross_version import (
        _try_render_call,
        _try_render_expr,
    )

    call = _try_render_call(head)
    if call is not None:
        return f"return {call}"

    extended = _try_render_expr(head, code.co_consts)
    if extended is not None and not extended.startswith("__"):
        return f"return {extended}"

    return None


def _try_recover_statement_body(code: CodeType, *, has_docstring: bool) -> str | None:
    """Statement-shaped body recovery for ``raise X`` and
    ``self.attr = val; ...`` constructor patterns.

    Sibling to :func:`_try_recover_trivial_body` (which targets
    single-expression ``return …`` shapes). Lives alongside the rest
    of the rule pass so the native 3.14 walker gets the same body
    coverage the cross-version walker added.
    """
    if code.co_flags & (CO_GENERATOR | CO_COROUTINE | CO_ASYNC_GENERATOR):
        return None
    if code.co_freevars:
        return None
    from pychd.cross_version import (
        _try_render_init_assignments,
        _try_render_raise,
    )

    instructions = [
        ins for ins in dis.Bytecode(code) if ins.opname not in _TRIVIAL_PROLOGUE
    ]
    if (
        has_docstring
        and len(instructions) >= 2
        and instructions[0].opname == "LOAD_CONST"
        and isinstance(instructions[0].argval, str)
        and instructions[1].opname == "POP_TOP"
    ):
        instructions = instructions[2:]
    if not instructions:
        return None
    raise_body = _try_render_raise(instructions)
    if raise_body is not None:
        return raise_body
    init_body = _try_render_init_assignments(instructions)
    if init_body is not None:
        return init_body
    return None


def _try_render_name_chain(instructions: list[dis.Instruction]) -> str | None:
    """Render ``LOAD_X name; LOAD_ATTR a; LOAD_ATTR b`` as ``name.a.b``."""
    if not instructions:
        return None
    head = instructions[0]
    # ``LOAD_DEREF`` is intentionally excluded: a deref read targets a
    # closure-bound free variable, which a standalone rendered function
    # cannot resolve. ``_try_recover_trivial_body`` guards against
    # ``co_freevars`` to keep that path safe end-to-end.
    if head.opname not in {
        "LOAD_FAST",
        "LOAD_FAST_BORROW",
        "LOAD_NAME",
        "LOAD_GLOBAL",
    }:
        return None
    name = str(head.argval)
    # LOAD_GLOBAL's argval on 3.11+ is the actual name; on older versions
    # the high bit encoded a PUSH_NULL flag, but xdis normalises this.
    parts = [name]
    for ins in instructions[1:]:
        if ins.opname == "LOAD_ATTR":
            parts.append(str(ins.argval))
        else:
            return None
    return ".".join(parts)


def _unmangle(name: str, class_name: str) -> str:
    """Reverse CPython's class-private name mangling.

    Names starting with two underscores (and not ending with two) get
    rewritten by the compiler to ``_<ClassName><name>``. Reverse that
    when reading them back out of the bytecode.
    """
    stripped_class = class_name.lstrip("_") or class_name
    prefix = "_" + stripped_class + "__"
    if name.startswith(prefix):
        return "__" + name[len(prefix) :]
    return name


def _build_class(
    code: CodeType,
    name: str,
    bases: list[str],
    keywords: list[tuple[str, str]],
) -> ir.ClassDef:
    walker = _Walker(code)
    ctx = _Context(walker, list(dis.Bytecode(code)), is_class=True)
    stmts = ctx.run()
    # Unmangle ``_ClassName__name`` back to ``__name``.
    for stmt in stmts:
        if isinstance(stmt, (ir.FunctionDef, ir.ClassDef)):
            stmt.name = _unmangle(stmt.name, name)
        elif isinstance(stmt, (ir.Assign, ir.AnnotationOnly)):
            stmt.target = _unmangle(stmt.target, name)

    docstring: str | None = None
    filtered: list[ir.Stmt] = []
    for s in stmts:
        if (
            isinstance(s, ir.Assign)
            and s.target == "__doc__"
            and _is_string_literal(s.value)
        ):
            docstring = _strip_string_literal(s.value)
            continue
        if isinstance(s, ir.Assign) and s.target in _CLASS_IMPLICIT_NAMES:
            continue
        filtered.append(s)

    # PEP 749 lazy annotations: classes that declare typed attributes get
    # a synthetic ``__annotate__`` closure in their consts. Walk it to
    # recover ``attr: T`` declarations the rule pass otherwise misses.
    annotations = _extract_annotations_from_annotate(code)
    if annotations:
        filtered = _splice_annotations(filtered, annotations)

    return ir.ClassDef(
        name=name,
        bases=bases,
        keywords=keywords,
        docstring=docstring,
        body=filtered,
    )


def _extract_pep695_type_params(wrapper_code: CodeType) -> list[str]:
    """Pull ``T``, ``T1`` … names out of a PEP 695 wrapper code object.

    Pattern (per instance):

        LOAD_CONST '<name>'
        CALL_INTRINSIC_1 (INTRINSIC_TYPEVAR / INTRINSIC_PARAMSPEC / …)
        COPY 1
        STORE_FAST '<name>'

    We collect every ``STORE_FAST`` whose target is referenced from a
    preceding ``LOAD_CONST <str>`` to capture the user-visible name.
    """
    names: list[str] = []
    instructions = list(dis.Bytecode(wrapper_code))
    pending: str | None = None
    for ins in instructions:
        if ins.opname == "LOAD_CONST" and isinstance(ins.argval, str):
            pending = ins.argval
        elif ins.opname == "STORE_FAST" and pending is not None:
            if ins.argval != pending:
                # Only count names whose const matches storage target.
                pending = None
                continue
            names.append(pending)
            pending = None
    return names


def _find_annotate_code(code: CodeType) -> CodeType | None:
    """Return the scope-level ``__annotate__`` code object embedded in *code*.

    PEP 749 embeds two kinds of annotation closures in a class's
    ``co_consts``:

    1. **Class-level**: stored as ``__annotate_func__`` via
       ``STORE_NAME``. The matching code object's ``co_name`` is
       ``__annotate__``; it carries the class-body annotations
       (``x: int`` at class scope).
    2. **Per-method**: also named ``__annotate__``, attached to each
       method via ``SET_FUNCTION_ATTRIBUTE 0x10``. These describe the
       method's *parameter* annotations, not class fields.

    We need the **class-level** one. Distinguish by scanning the parent
    code's instructions for ``STORE_NAME '__annotate_func__'`` and
    capturing the ``LOAD_CONST`` immediately preceding it.
    """
    instructions = list(dis.Bytecode(code))
    for i, ins in enumerate(instructions):
        if (
            ins.opname in {"STORE_NAME", "STORE_GLOBAL"}
            and ins.argval == "__annotate_func__"
        ):
            for j in range(i - 1, max(-1, i - 6), -1):
                if instructions[j].opname == "LOAD_CONST" and isinstance(
                    instructions[j].argval, CodeType
                ):
                    if instructions[j].argval.co_name == "__annotate__":
                        return instructions[j].argval
        # Module-level annotations: STORE_NAME __annotate__ directly.
        if (
            ins.opname in {"STORE_NAME", "STORE_GLOBAL"}
            and ins.argval == "__annotate__"
        ):
            for j in range(i - 1, max(-1, i - 6), -1):
                if instructions[j].opname == "LOAD_CONST" and isinstance(
                    instructions[j].argval, CodeType
                ):
                    if instructions[j].argval.co_name == "__annotate__":
                        return instructions[j].argval
    # Fall back: first __annotate__ in consts (works for non-class scopes
    # that don't use STORE_NAME for the annotate closure).
    for const in code.co_consts:
        if isinstance(const, CodeType) and const.co_name == "__annotate__":
            return const
    return None


def _extract_annotations_from_annotate(parent: CodeType) -> list[tuple[str, str]]:
    """Walk a ``__annotate__`` closure and return ``[(attr, annotation), …]``.

    PEP 749 emits one annotation entry per attribute, with the simplest
    shape being::

        LOAD_FROM_DICT_OR_GLOBALS <annotation_name>
        COPY 2
        LOAD_CONST <attr_name>
        STORE_SUBSCR

    More complex annotations build a subscripted / piped expression on
    the stack before the ``LOAD_CONST <attr_name> ; STORE_SUBSCR``
    closing pair. We run a small symbolic interpreter over the segment
    between each declaration's leading boundary marker and its
    ``STORE_SUBSCR`` to recover the full expression source:

    * ``Dict[str, list[int]]`` → ``Dict[str, list[int]]``
    * ``str | None`` → ``str | None``
    * unrecognised shapes still emit the attribute name with ``...``
      as the annotation so the ``AnnAssign`` node survives.
    """
    annotate = _find_annotate_code(parent)
    if annotate is None:
        return []
    instructions = list(dis.Bytecode(annotate))
    out: list[tuple[str, str]] = []

    # Locate every ``LOAD_CONST <attr> ; STORE_SUBSCR`` pair — each
    # marks the *end* of one annotation declaration.
    boundary = 0
    for i in range(len(instructions) - 1):
        if not (
            instructions[i].opname == "LOAD_CONST"
            and isinstance(instructions[i].argval, str)
            and instructions[i + 1].opname == "STORE_SUBSCR"
        ):
            continue
        attr = instructions[i].argval
        # The annotation expression occupies the slice ``[boundary, i)``;
        # we drop the trailing ``COPY 2`` (or COPY 1, COPY 3) that
        # duplicates the annotations dict, and the trailing
        # ``LOAD_NAME __annotations__`` that pushed the dict onto the
        # stack. Whatever remains is the expression we want to render.
        segment = _strip_annotation_envelope(instructions[boundary:i])
        rendered = _render_annotation_segment(segment)
        out.append((attr, rendered if rendered is not None else "..."))
        boundary = i + 2
    return out


_ANNOT_DICT_NAMES = frozenset({"__annotations__", "__classdict__"})


def _strip_annotation_envelope(
    segment: list[dis.Instruction],
) -> list[dis.Instruction]:
    """Drop scaffold opcodes that wrap each annotation declaration.

    PEP 749's ``__annotate__`` body opens with a
    ``format > 2: raise NotImplementedError`` prelude (the formatting
    mode check) and wraps every declaration with the boilerplate that
    pushes the annotations dict (``LOAD_NAME __annotations__`` /
    ``LOAD_DEREF __classdict__``) and ``COPY``-s it under the
    annotation value before the closing ``STORE_SUBSCR``. We strip
    every such scaffold opcode so the remaining instructions form
    exactly the user-written annotation expression.
    """
    out: list[dis.Instruction] = []
    for ins in segment:
        op = ins.opname
        if op in {
            "COPY",
            "COPY_FREE_VARS",
            "EXTENDED_ARG",
            "CACHE",
            "RESUME",
            "NOT_TAKEN",
            "MAKE_CELL",
            "BUILD_MAP",
            "POP_JUMP_IF_FALSE",
            "POP_JUMP_IF_TRUE",
            "POP_JUMP_FORWARD_IF_FALSE",
            "POP_JUMP_FORWARD_IF_TRUE",
            "POP_JUMP_BACKWARD_IF_FALSE",
            "POP_JUMP_BACKWARD_IF_TRUE",
            "COMPARE_OP",
            "RAISE_VARARGS",
            "LOAD_COMMON_CONSTANT",
        }:
            continue
        if (
            op
            in {
                "LOAD_NAME",
                "LOAD_DEREF",
                "LOAD_FAST",
                "LOAD_FAST_BORROW",
            }
            and ins.argval in _ANNOT_DICT_NAMES
        ):
            continue
        if op == "LOAD_FAST_BORROW" and ins.argval == "format":
            # The PEP 749 format flag pushed at the head of every
            # ``__annotate__`` closure — not part of the user expression.
            continue
        if op == "LOAD_SMALL_INT" and ins.argval == 2:
            # The literal ``2`` only ever appears as the rhs of the
            # ``format > 2`` prelude check; never inside annotations.
            continue
        out.append(ins)
    return out


def _render_annotation_segment(
    segment: list[dis.Instruction],
) -> str | None:
    """Render an annotation expression by symbolically executing *segment*."""
    if not segment:
        return None
    stack: list[str] = []
    for ins in segment:
        op = ins.opname
        if op in {"LOAD_FROM_DICT_OR_GLOBALS", "LOAD_GLOBAL", "LOAD_NAME"}:
            stack.append(str(ins.argval))
        elif op in {"LOAD_DEREF", "LOAD_FAST", "LOAD_FAST_BORROW"}:
            stack.append(str(ins.argval))
        elif op == "LOAD_CONST":
            stack.append(_format_literal(ins.argval))
        elif op == "LOAD_SMALL_INT":
            stack.append(_format_literal(ins.argval))
        elif op == "LOAD_ATTR":
            if not stack:
                return None
            stack[-1] = f"{stack[-1]}.{ins.argval}"
        elif op == "BINARY_SUBSCR":
            if len(stack) < 2:
                return None
            idx = stack.pop()
            base = stack.pop()
            stack.append(f"{base}[{idx}]")
        elif op == "BUILD_TUPLE":
            n = ins.arg or 0
            if n > len(stack):
                return None
            items = stack[-n:] if n else []
            del stack[-n:]
            if n == 0:
                stack.append("()")
            elif n == 1:
                stack.append(f"({items[0]},)")
            else:
                stack.append(", ".join(items))
        elif op == "BUILD_LIST":
            n = ins.arg or 0
            if n > len(stack):
                return None
            items = stack[-n:] if n else []
            del stack[-n:]
            stack.append("[" + ", ".join(items) + "]")
        elif op == "BUILD_SET":
            n = ins.arg or 0
            if n > len(stack):
                return None
            items = stack[-n:] if n else []
            del stack[-n:]
            stack.append("{" + ", ".join(items) + "}" if items else "set()")
        elif op == "BINARY_OP":
            if len(stack) < 2:
                return None
            rhs = stack.pop()
            lhs = stack.pop()
            # Python 3.14 emits subscription as ``BINARY_OP 26 ('[]')``
            # instead of the dedicated ``BINARY_SUBSCR`` opcode; render
            # accordingly. All other binary ops in annotations are
            # standard infix operators looked up by their argrepr.
            if (ins.argrepr or "") == "[]":
                stack.append(f"{lhs}[{rhs}]")
            else:
                symbol = _BINARY_OP_SYMBOLS.get(ins.argrepr or "", None)
                if symbol is None:
                    return None
                stack.append(f"{lhs} {symbol} {rhs}")
        elif op in {"PUSH_NULL", "RESUME", "MAKE_CELL", "RETURN_VALUE"}:
            continue
        else:
            return None
    if len(stack) != 1:
        return None
    return stack[0]


# Map ``BINARY_OP``'s ``argrepr`` strings (as exposed by dis) back to
# their source-level operator. Only the operators that appear in
# annotation expressions matter — arithmetic and ``|`` for PEP 604
# union types.
_BINARY_OP_SYMBOLS = {
    "|": "|",
    "&": "&",
    "^": "^",
    "+": "+",
    "-": "-",
    "*": "*",
    "/": "/",
    "//": "//",
    "%": "%",
    "**": "**",
    "<<": "<<",
    ">>": ">>",
    "@": "@",
}


def _splice_annotations(
    body: list[ir.Stmt], annotations: list[tuple[str, str]]
) -> list[ir.Stmt]:
    """Insert ``AnnotationOnly`` / promote ``Assign`` for each annotation.

    Order matches the original ``__annotate__`` walk. Existing
    assignments to the same target are upgraded in-place into annotated
    assigns; otherwise an ``AnnotationOnly`` is appended at the head of
    the class body, before any other declarations.
    """
    existing: dict[str, ir.Stmt] = {}
    for stmt in body:
        if isinstance(stmt, (ir.Assign, ir.AnnotationOnly)):
            existing[stmt.target] = stmt

    out: list[ir.Stmt] = []
    consumed: set[str] = set()
    for attr, ann in annotations:
        prev = existing.get(attr)
        if isinstance(prev, ir.Assign):
            prev.annotation = ann
            consumed.add(attr)
        elif prev is None:
            out.append(ir.AnnotationOnly(target=attr, annotation=ann))

    out.extend(body)
    return out


def _arguments_from_code(
    code: CodeType,
    *,
    defaults: tuple | None,
    kwdefaults: dict | None,
) -> ir.Arguments:
    pos_total = code.co_argcount
    pos_only = code.co_posonlyargcount
    kw_only = code.co_kwonlyargcount
    has_varargs = bool(code.co_flags & CO_VARARGS)
    has_varkw = bool(code.co_flags & CO_VARKEYWORDS)

    varnames = list(code.co_varnames)
    idx = 0
    posonly_args: list[ir.Arg] = []
    args: list[ir.Arg] = []
    for i in range(pos_total):
        name = varnames[idx]
        idx += 1
        if i < pos_only:
            posonly_args.append(ir.Arg(name=name))
        else:
            args.append(ir.Arg(name=name))
    kwonly_args: list[ir.Arg] = []
    for _ in range(kw_only):
        name = varnames[idx]
        idx += 1
        kwonly_args.append(ir.Arg(name=name))
    vararg = None
    if has_varargs:
        vararg = ir.Arg(name=varnames[idx])
        idx += 1
    kwarg = None
    if has_varkw:
        kwarg = ir.Arg(name=varnames[idx])

    if defaults:
        positional = posonly_args + args
        for offset, value in enumerate(reversed(list(defaults))):
            target_idx = len(positional) - 1 - offset
            if 0 <= target_idx < len(positional):
                positional[target_idx].default = _format_literal(value)
        posonly_args = positional[:pos_only]
        args = positional[pos_only:]

    if kwdefaults:
        items_iter = kwdefaults.items() if hasattr(kwdefaults, "items") else kwdefaults
        normalised: dict[str, Any] = {}
        for k, v in items_iter:
            key = _unwrap_literal(k)
            val = _unwrap_literal(v)
            if isinstance(key, str):
                normalised[key] = val
        for a in kwonly_args:
            if a.name in normalised:
                a.default = _format_literal(normalised[a.name])

    return ir.Arguments(
        posonly=posonly_args,
        args=args,
        vararg=vararg,
        kwonly=kwonly_args,
        kwarg=kwarg,
    )


def _extract_docstring(code: CodeType) -> str | None:
    """Recover the docstring for a code object, if any.

    For modules and classes, CPython emits ``LOAD_CONST <doc>;
    STORE_NAME '__doc__'`` explicitly — we look for that pattern.

    For functions, ``co_consts[0]`` *might* be a docstring, or it might
    just be the first literal used in the body. We rule out the second
    case by checking whether any instruction references slot 0: if it
    does, the constant is real code data, not a docstring.
    """
    if not code.co_consts:
        return None
    first = code.co_consts[0]
    if not isinstance(first, str):
        return None
    instructions = list(dis.Bytecode(code))
    # Module/class case: explicit STORE_NAME __doc__.
    for i, ins in enumerate(instructions):
        if ins.opname == "STORE_NAME" and ins.argval == "__doc__":
            # Verify the preceding LOAD_CONST points at our string.
            if i > 0 and instructions[i - 1].opname == "LOAD_CONST":
                return (
                    instructions[i - 1].argval
                    if isinstance(instructions[i - 1].argval, str)
                    else None
                )
            return first
    # Function case: docstring lives at co_consts[0] only if no
    # instruction references slot 0 as a literal.
    for ins in instructions:
        if ins.opname == "LOAD_CONST" and ins.arg == 0:
            return None
    # A function with a docstring as the only top-level constant: trust it.
    return first


def _dis_text(code: CodeType) -> str:
    buf = io.StringIO()
    dis.dis(code, file=buf, depth=0)
    return buf.getvalue()


@dataclass
class _Literal:
    value: Any


@dataclass
class _Name:
    name: str


@dataclass
class _Sentinel:
    kind: str


@dataclass
class _Collection:
    kind: str
    items: list[Any]


@dataclass
class _Mapping:
    items: list[tuple[Any, Any]]
    raw: dict[Any, Any]


@dataclass
class _FunctionValue:
    code: CodeType
    defaults: tuple | None
    kwdefaults: dict | None
    has_annotations: bool


@dataclass
class _CallExpr:
    """A call expression already rendered as Python source.

    Produced by the ``CALL`` / ``CALL_KW`` dispatchers when both the
    callable and every argument can be rendered. Used by the class
    and function definition rules to recover decorator-with-args
    expressions like ``@dataclass(frozen=True)`` — when these
    expressions sit below the LOAD_BUILD_CLASS sentinel, they are
    captured as decorator strings before being consumed by the
    decorator-application ``CALL`` opcodes.
    """

    text: str


def _try_render_call(popped: list, *, kw_names: tuple | None) -> str | None:
    """Render ``callable(arg1, arg2, kw=val)`` from popped stack values.

    *popped* is in stack-bottom-first order: ``[callable, *positional]``.
    *kw_names* contains the trailing keyword names (for CALL_KW); the
    last ``len(kw_names)`` entries of *popped* are the keyword values.

    Returns ``None`` if any component cannot be rendered — the caller
    then falls back to the opaque ``_Sentinel`` model.
    """
    if not popped:
        return None
    callable_v = popped[0]
    if isinstance(callable_v, _Name):
        callable_str = callable_v.name
    elif isinstance(callable_v, _CallExpr):
        callable_str = callable_v.text
    else:
        return None
    raw_args = popped[1:]
    if kw_names:
        kw_count = len(kw_names)
        if kw_count > len(raw_args):
            return None
        kw_vals = raw_args[-kw_count:]
        pos_vals = raw_args[:-kw_count]
    else:
        kw_vals = []
        pos_vals = raw_args

    parts: list[str] = []
    for v in pos_vals:
        r = _render_value(v)
        if r is None:
            return None
        parts.append(r)
    for name, v in zip(kw_names or (), kw_vals):
        r = _render_value(v)
        if r is None:
            return None
        parts.append(f"{name}={r}")
    return f"{callable_str}({', '.join(parts)})"


def _render_value(v: Any) -> str | None:
    if isinstance(v, _Literal):
        return _format_literal(v.value)
    if isinstance(v, _Name):
        return v.name
    if isinstance(v, _CallExpr):
        return v.text
    if isinstance(v, _Collection):
        rendered: list[str] = []
        for item in v.items:
            r = _render_value(item)
            if r is None:
                return None
            rendered.append(r)
        joined = ", ".join(rendered)
        if v.kind == "list":
            return f"[{joined}]"
        if v.kind == "tuple":
            if len(rendered) == 1:
                return f"({joined},)"
            return f"({joined})"
        if v.kind == "set":
            if not rendered:
                return "set()"
            return f"{{{joined}}}"
    if isinstance(v, _Mapping):
        parts: list[str] = []
        for k, val in v.items:
            kr = _render_value(k)
            vr = _render_value(val)
            if kr is None or vr is None:
                return None
            parts.append(f"{kr}: {vr}")
        return "{" + ", ".join(parts) + "}"
    return None


def _format_literal(value: Any) -> str:
    if isinstance(value, str):
        return repr(value)
    if isinstance(value, bytes):
        return repr(value)
    if value is None or isinstance(value, (int, float, bool)):
        return repr(value)
    if isinstance(value, tuple):
        if not value:
            return "()"
        if len(value) == 1:
            return f"({_format_literal(value[0])},)"
        return "(" + ", ".join(_format_literal(x) for x in value) + ")"
    if isinstance(value, frozenset):
        if not value:
            return "frozenset()"
        return "frozenset({" + ", ".join(_format_literal(x) for x in value) + "})"
    try:
        return repr(value)
    except Exception:
        return "..."


def _unwrap_literal(v: Any) -> Any:
    if isinstance(v, _Literal):
        return v.value
    return v


def _is_string_literal(rendered: str) -> bool:
    if not rendered:
        return False
    first = rendered[0]
    if first not in ("'", '"'):
        return False
    return rendered.endswith(first)


def _strip_string_literal(rendered: str) -> str:
    """Reverse repr() for a Python string literal."""
    try:
        result = ast.literal_eval(rendered)
        return result if isinstance(result, str) else rendered
    except ValueError, SyntaxError:
        return rendered


def decompile_with_rules(code: CodeType) -> RuleResult:
    """Public entry point: extract a module from a code object."""
    return extract_module(code)
