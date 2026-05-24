"""Rule-based partial decompiler: bytecode → IR.

Operates on a code object produced by the *current* running interpreter
(`marshal.load` from a .pyc compiled for this Python version). The goal
is to recover the structural skeleton of a module — imports, function and
class signatures, docstrings, simple module-level constants — without
LLM assistance, while leaving function bodies as ``UnknownBlock`` IR
nodes for a downstream LLM pass to fill in.

Currently implements rules for **Python 3.14**. Other versions raise
``UnsupportedVersionError``; the caller should fall back to the
LLM-only pipeline.
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
    """Return True if the rule engine handles this Python version."""
    return version_tuple[:2] == (3, 14)


def extract_module(code: CodeType) -> RuleResult:
    """Extract an `ir.Module` from a module-level code object."""
    if sys.version_info[:2] != (3, 14):  # pragma: no cover - env guard
        raise UnsupportedVersionError(
            f"Rule engine targets Python 3.14, got {sys.version_info[:2]}"
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
        del self.stack[sent_pos_in_stack:]

        # Discard the decorator values that were sitting on the stack
        # below the LOAD_BUILD_CLASS sentinel. We can't reliably name
        # them yet (e.g. ``dataclass(frozen=True)`` is a call result),
        # so they're consumed silently — class structure still survives.
        for _ in range(decorator_calls):
            if self.stack:
                self.stack.pop()

        class_def = _build_class(
            candidate_code, store_name or class_name or "Anon", bases, keywords
        )
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
            if self.stack and isinstance(self.stack[-1], _Name):
                decorators.append(self.stack.pop().name)
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
        if inner_code.co_flags & 0x02 == 0 and any(
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
            self.body.append(ir.Assign(target=target, value="..."))
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
        return False

    def _try_binop_call(self) -> bool:
        op = self.ins[self.pos].opname
        if op == "CALL":
            argc = self.ins[self.pos].arg or 0
            self._pop_n(argc + 1)
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
    body: list[ir.Stmt] = [
        ir.UnknownBlock(disassembly=_dis_text(code), signature=f"def {name}")
    ]
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

    PEP 749 emits one annotation entry per attribute as the sequence:

        LOAD_FROM_DICT_OR_GLOBALS <annotation_name>
        COPY 2
        LOAD_CONST <attr_name>
        STORE_SUBSCR

    More complex annotations (``list[int]``, ``str | None``…) require
    deeper interpretation; for v1 we keep simple-name annotations and
    fall back to a stringified placeholder for the rest.
    """
    annotate = _find_annotate_code(parent)
    if annotate is None:
        return []
    instructions = list(dis.Bytecode(annotate))
    out: list[tuple[str, str]] = []

    # Each annotation declaration ends with ``LOAD_CONST <attr_name> ;
    # STORE_SUBSCR``. We scan for that pair and back up at most a few
    # instructions to identify the annotation value:
    #
    #   - simple name:        LOAD_FROM_DICT_OR_GLOBALS / LOAD_GLOBAL X ; COPY 2 …
    #   - quoted (PEP 563):   LOAD_CONST 'X' ; COPY 2 …
    #   - complex expression: anything else — recover attr name only,
    #                         annotation rendered as ``...`` so that the
    #                         AnnAssign node is still emitted.
    #
    # ``COPY 2`` may also be ``COPY 1`` / fused / absent in synthetic
    # cases; treat as optional.
    for i in range(len(instructions) - 1):
        if (
            instructions[i].opname == "LOAD_CONST"
            and isinstance(instructions[i].argval, str)
            and instructions[i + 1].opname == "STORE_SUBSCR"
        ):
            attr = instructions[i].argval
            ann = "..."
            # Look back up to 4 instructions for an annotation loader.
            for j in range(i - 1, max(-1, i - 5), -1):
                op = instructions[j].opname
                if op in {"LOAD_FROM_DICT_OR_GLOBALS", "LOAD_GLOBAL", "LOAD_NAME"}:
                    ann = str(instructions[j].argval)
                    break
                if op == "LOAD_CONST" and isinstance(instructions[j].argval, str):
                    ann = instructions[j].argval
                    break
                if op == "BINARY_SUBSCR" or op == "BINARY_OP":
                    # Complex annotation expression — we still want the
                    # attribute name in the output, just not the type.
                    break
            out.append((attr, ann))
    return out


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


def _render_value(v: Any) -> str | None:
    if isinstance(v, _Literal):
        return _format_literal(v.value)
    if isinstance(v, _Name):
        return v.name
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
