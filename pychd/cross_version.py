"""Declaration-only rule pass that works on every CPython 3.x release.

The :mod:`pychd.rules` walker targets the *running* interpreter's
bytecode (Python 3.14 today). It speaks 3.14-specific opcodes
(``LOAD_SMALL_INT``, ``SET_FUNCTION_ATTRIBUTE``, ``__annotate__``…)
and depends on the stdlib :mod:`dis` module being able to read the
code object — which requires the bytecode to match the running Python.

This module supplies a **second** rule pass that:

1. Iterates instructions via :mod:`xdis` rather than stdlib :mod:`dis`,
   so it works on any code object xdis can disassemble (every
   CPython release from 3.0 onwards).
2. Restricts itself to the *declaration-shaped* opcode patterns that
   have been stable across the entire Python 3 series:

   * ``LOAD_CONST <doc>; STORE_NAME __doc__`` — module / class
     docstrings.
   * ``LOAD_CONST <level>; LOAD_CONST <fromlist>; IMPORT_NAME ;
     (IMPORT_FROM ; STORE_NAME)* ; POP_TOP`` — ``import`` and
     ``from x import y`` statements.
   * ``LOAD_CONST <code>; LOAD_CONST <name>; MAKE_FUNCTION ;
     STORE_NAME`` — function definitions (the per-version layout
     variant for 3.7 – 3.10 also pops a separate qualname).
   * ``LOAD_BUILD_CLASS; LOAD_CONST <code>; …; CALL ; STORE_NAME``
     — class definitions, with their methods recovered by recursing
     into the class code object.
   * ``LOAD_* ; STORE_NAME`` — simple module-level assignments
     (the *name* survives even when the RHS expression is dropped).

3. Skips body recovery entirely: every function / method body is
   emitted as ``pass  # pychd: unrecovered body``. The rule-only
   contract is that bodies are LLM territory; in hybrid mode the
   downstream LLM pass fills them in using each body's disassembly.

What is **deliberately not** attempted by this pass:

* Default-argument recovery — the ``MAKE_FUNCTION`` flag layout
  differs across 3.7 / 3.10 / 3.13.
* Class-decorator argument recovery — the call patterns vary.
* Complex annotation expressions — PEP 749 (3.14) and earlier
  ``__annotations__`` dict construction are version-specific.
* Match-statement structure — relevant only to method bodies.

The trade-off is intentional: the cross-version pass aims for high
``signature_match`` / ``declaration_match`` on every Python release,
not strict-AST equality. The latter is reserved for the 3.14 native
walker where every opcode quirk is known.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

from xdis.bytecode import get_instructions_bytes
from xdis.op_imports import op_imports

from pychd import ir

_CO_VARARGS = 0x04
_CO_VARKEYWORDS = 0x08
_CO_GENERATOR = 0x20
_CO_COROUTINE = 0x80
_CO_ASYNC_GENERATOR = 0x200

_CLASS_IMPLICIT = frozenset(
    {
        "__module__",
        "__qualname__",
        "__firstlineno__",
        "__static_attributes__",
        "__classdictcell__",
        "__annotate_func__",
        "__annotate__",
        "__conditional_annotations__",
        "__type_params__",
    }
)


@dataclass
class CrossVersionResult:
    module: ir.Module
    recovered: bool
    confidence: float


def supports(version_tuple: tuple) -> bool:
    """Return True if this pass can walk *version_tuple* bytecode.

    Coverage matches xdis: every 3.x release with a registered opcode
    module. The current interpreter's version is also supported via
    this path (the native pass in :mod:`pychd.rules` is preferred for
    3.14 because it recovers more, but the cross-version walker is a
    valid lower-fidelity fallback).
    """
    if version_tuple[:1] != (3,):
        return False
    return _resolve_opc(version_tuple) is not None


def extract_module(code: Any, version_tuple: tuple) -> CrossVersionResult:
    """Walk *code* (an xdis Code3X object) and return an IR module.

    *version_tuple* is ``(major, minor)`` or ``(major, minor, micro)``;
    the per-version opcode module is selected by best match.
    """
    opc = _resolve_opc(version_tuple)
    if opc is None:
        raise ValueError(
            f"cross_version: no xdis opcode module for Python {version_tuple}"
        )

    walker = _Walker(opc, version_tuple)
    docstring, body = walker.walk_module(code)
    module = ir.Module(docstring=docstring, body=body)
    unknowns = module.unknown_blocks()
    return CrossVersionResult(
        module=module,
        recovered=not unknowns,
        confidence=walker.confidence(),
    )


def _resolve_opc(version_tuple: tuple) -> Any | None:
    """Pick the best-matching xdis opcode module for *version_tuple*.

    xdis indexes opc modules by the full ``"x.y.z"`` string. We look
    for the highest patch release of the requested minor; falling back
    to ``"x.y"`` then to the minor with no patch suffix.
    """
    if not version_tuple:
        return None
    major = version_tuple[0]
    minor = version_tuple[1] if len(version_tuple) >= 2 else 0
    candidates: list[str] = []
    for key in op_imports.keys():
        if not isinstance(key, str):
            continue
        parts = key.split(".")
        if len(parts) >= 2 and parts[0] == str(major) and parts[1] == str(minor):
            candidates.append(key)
    if not candidates:
        return None
    candidates.sort(key=_version_sort_key, reverse=True)
    return op_imports[candidates[0]]


def _version_sort_key(label: str) -> tuple:
    """Sort xdis opc keys naturally: 3.13.10 > 3.13.9 > 3.13 > 3.13a1."""
    parts = label.split(".")
    out: list[int] = []
    for p in parts:
        digits = "".join(ch for ch in p if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------


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
    items: list


class _Walker:
    def __init__(self, opc: Any, version_tuple: tuple) -> None:
        self.opc = opc
        self.version_tuple = version_tuple
        self.total = 0
        self.explained = 0

    def confidence(self) -> float:
        if not self.total:
            return 1.0
        return min(1.0, self.explained / self.total)

    def walk_module(self, code: Any) -> tuple[str | None, list[ir.Stmt]]:
        ctx = _Context(self, code, is_class=False)
        stmts = ctx.run()
        docstring: str | None = None
        body: list[ir.Stmt] = []
        for s in stmts:
            if (
                isinstance(s, ir.Assign)
                and s.target == "__doc__"
                and _is_string_literal(s.value)
            ):
                docstring = _strip_string_literal(s.value)
                continue
            body.append(s)
        return docstring, body


class _Context:
    def __init__(self, walker: _Walker, code: Any, *, is_class: bool) -> None:
        self.walker = walker
        self.code = code
        self.is_class = is_class
        self.ins = _iter_instructions(code, walker.opc)
        self.walker.total += len(self.ins)
        self.pos = 0
        self.stack: list[Any] = []
        self.body: list[ir.Stmt] = []

    # The opname-based dispatch table. Order matters: annotation stores
    # must be checked before plain pushes so the LOAD_CONST that holds
    # the annotation value is intercepted before being pushed as a
    # literal. Class definitions must be checked before plain
    # MAKE_FUNCTION dispatch (LOAD_BUILD_CLASS sits below the code
    # object in the stack).
    def run(self) -> list[ir.Stmt]:
        while self.pos < len(self.ins):
            if self._match_noop():
                continue
            if self._match_return():
                continue
            if self._match_push():
                continue
            if self._match_import():
                continue
            if self._match_class_def():
                continue
            if self._match_function_def():
                continue
            if self._match_simple_store():
                continue
            if self._match_pop_top():
                continue
            if self._match_build_collection():
                continue
            if self._match_call_like():
                continue
            # Unrecognised opcode — skip without crediting it as explained.
            self.pos += 1
        return self.body

    # ---- micro-helpers -------------------------------------------------

    def _consume(self, n: int) -> None:
        self.walker.explained += n
        self.pos += n

    def _peek(self, offset: int = 0) -> Any:
        idx = self.pos + offset
        return self.ins[idx] if 0 <= idx < len(self.ins) else None

    def _pop_n(self, n: int) -> list[Any]:
        if n <= 0:
            return []
        if n > len(self.stack):
            out = list(self.stack)
            self.stack.clear()
            return out
        out = self.stack[-n:]
        del self.stack[-n:]
        return out

    # ---- pattern matchers ---------------------------------------------

    _NOOP_OPS = frozenset(
        {
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
            "EXTENDED_ARG",
            "PRECALL",
            "NOP",
            "RESERVED",
            "DELETE_NAME",
            "DELETE_GLOBAL",
            "DELETE_FAST",
            "DELETE_DEREF",
        }
    )

    def _match_noop(self) -> bool:
        cur = self._peek()
        if cur is None or cur.opname not in self._NOOP_OPS:
            return False
        self._consume(1)
        return True

    _RETURN_OPS = frozenset(
        {
            "RETURN_VALUE",
            "RETURN_CONST",
            "RETURN_GENERATOR",
        }
    )

    def _match_return(self) -> bool:
        cur = self._peek()
        if cur is None or cur.opname not in self._RETURN_OPS:
            return False
        self._consume(1)
        return True

    def _match_push(self) -> bool:
        cur = self._peek()
        if cur is None:
            return False
        op = cur.opname
        if op in {"LOAD_CONST", "LOAD_SMALL_INT", "LOAD_COMMON_CONSTANT"}:
            self.stack.append(_Literal(cur.argval))
            self._consume(1)
            return True
        if op in {"LOAD_NAME", "LOAD_GLOBAL", "LOAD_FAST", "LOAD_DEREF"}:
            self.stack.append(_Name(str(cur.argval)))
            self._consume(1)
            return True
        if op == "LOAD_ATTR":
            # Extend dotted name on top of stack: ``abc.ABC`` patterns.
            if self.stack and isinstance(self.stack[-1], _Name):
                self.stack[-1] = _Name(self.stack[-1].name + "." + str(cur.argval))
            else:
                self.stack.append(_Name(str(cur.argval)))
            self._consume(1)
            return True
        if op == "LOAD_BUILD_CLASS":
            self.stack.append(_Sentinel("LOAD_BUILD_CLASS"))
            self._consume(1)
            return True
        return False

    def _match_import(self) -> bool:
        cur = self._peek()
        if cur is None or cur.opname != "IMPORT_NAME":
            return False
        if len(self.stack) < 2:
            return False
        fromlist_val = self.stack[-1]
        level_val = self.stack[-2]
        if not (isinstance(fromlist_val, _Literal) and isinstance(level_val, _Literal)):
            return False
        fromlist = fromlist_val.value
        level = level_val.value if isinstance(level_val.value, int) else 0
        module = cur.argval or ""
        self.stack.pop()
        self.stack.pop()
        self._consume(1)
        if fromlist is None:
            return self._finalise_plain_import(module)
        if isinstance(fromlist, tuple):
            return self._finalise_from_import(module, level, [str(n) for n in fromlist])
        return True

    _STORE_OPS = frozenset({"STORE_NAME", "STORE_FAST", "STORE_DEREF", "STORE_GLOBAL"})

    def _finalise_plain_import(self, module: str) -> bool:
        store_ops = {"STORE_NAME", "STORE_GLOBAL"}
        cur = self._peek()
        if cur is not None and cur.opname in store_ops:
            target = cur.argval
            self._consume(1)
            top = module.split(".")[0]
            if target == top:
                self.body.append(ir.Import(names=[(module, None)]))
            else:
                self.body.append(ir.Import(names=[(module, target)]))
            return True
        # ``import a.b as c`` lowers to ``IMPORT_NAME a.b ; IMPORT_FROM b ;
        # STORE_NAME c ; POP_TOP`` on 3.7+.
        if (
            self._peek() is not None
            and self._peek().opname == "IMPORT_FROM"
            and self._peek(1) is not None
            and self._peek(1).opname in store_ops
        ):
            asname = self._peek(1).argval
            self._consume(2)
            if self._peek() is not None and self._peek().opname == "POP_TOP":
                self._consume(1)
            self.body.append(ir.Import(names=[(module, asname)]))
            return True
        return True

    def _finalise_from_import(
        self, module: str, level: int, fromlist: list[str]
    ) -> bool:
        store_ops = {"STORE_NAME", "STORE_GLOBAL"}
        if fromlist == ["*"]:
            cur = self._peek()
            if cur is not None:
                star_op = cur.opname
                is_star = star_op == "IMPORT_STAR" or (
                    star_op == "CALL_INTRINSIC_1" and cur.arg == 2
                )
                if is_star:
                    self._consume(1)
                    if self._peek() is not None and self._peek().opname == "POP_TOP":
                        self._consume(1)
                    self.body.append(
                        ir.FromImport(module=module, level=level, names=[("*", None)])
                    )
                    return True
        names: list[tuple[str, str | None]] = []
        for expected in fromlist:
            cur = self._peek()
            nxt = self._peek(1)
            if (
                cur is not None
                and cur.opname == "IMPORT_FROM"
                and nxt is not None
                and nxt.opname in store_ops
            ):
                stored = nxt.argval
                asname = stored if stored != expected else None
                names.append((expected, asname))
                self._consume(2)
            else:
                break
        if self._peek() is not None and self._peek().opname == "POP_TOP":
            self._consume(1)
        if names:
            self.body.append(ir.FromImport(module=module, level=level, names=names))
        return True

    def _match_class_def(self) -> bool:
        cur = self._peek()
        if cur is None or cur.opname != "MAKE_FUNCTION":
            return False
        # The LOAD_BUILD_CLASS sentinel must be somewhere on the stack.
        sent_pos = None
        for i in range(len(self.stack) - 1, -1, -1):
            v = self.stack[i]
            if isinstance(v, _Sentinel) and v.kind == "LOAD_BUILD_CLASS":
                sent_pos = i
                break
        if sent_pos is None:
            return False
        # On 3.7–3.10 a class's qualname LOAD_CONST sits on top of the
        # code object; pop it first when present.
        version = self.walker.version_tuple
        is_old_layout = version[:2] <= (3, 10)
        had_qualname = False
        if (
            is_old_layout
            and len(self.stack) >= 2
            and isinstance(self.stack[-1], _Literal)
            and isinstance(self.stack[-1].value, str)
            and isinstance(self.stack[-2], _Literal)
            and _is_code(self.stack[-2].value)
        ):
            had_qualname = True
        if not (
            self.stack
            and isinstance(self.stack[-1 if not had_qualname else -2], _Literal)
            and _is_code(self.stack[-1 if not had_qualname else -2].value)
        ):
            return False
        candidate_code = self.stack[-1 if not had_qualname else -2].value
        if sent_pos >= len(self.stack) - (2 if had_qualname else 1):
            return False
        if had_qualname:
            self.stack.pop()  # qualname
        self.stack.pop()  # code object
        self._consume(1)

        # Optional LOAD_CONST <class_name>.
        class_name: str | None = None
        while self._peek() is not None and self._peek().opname == "EXTENDED_ARG":
            self._consume(1)
        cur = self._peek()
        if (
            cur is not None
            and cur.opname == "LOAD_CONST"
            and isinstance(cur.argval, str)
        ):
            class_name = cur.argval
            self._consume(1)

        bases: list[str] = []
        # Consume bases + the CALL that actually builds the class.
        while self._peek() is not None:
            op = self._peek().opname
            if op == "EXTENDED_ARG":
                self._consume(1)
                continue
            if op in {"CALL", "CALL_FUNCTION", "CALL_FUNCTION_KW"}:
                self._consume(1)
                break
            if op in {"LOAD_NAME", "LOAD_GLOBAL", "LOAD_FAST", "LOAD_DEREF"}:
                bases.append(str(self._peek().argval))
                self._consume(1)
                continue
            if op == "LOAD_ATTR" and bases:
                bases[-1] = bases[-1] + "." + str(self._peek().argval)
                self._consume(1)
                continue
            if op == "LOAD_CONST":
                self._consume(1)
                continue
            if op == "KW_NAMES":
                self._consume(1)
                continue
            break

        # Decorator-with-args applied to a class produces extra
        # CALL opcodes between the build-class CALL and the STORE_NAME.
        # Eat them silently.
        while self._peek() is not None and self._peek().opname in {
            "CALL",
            "CALL_FUNCTION",
            "CALL_KW",
        }:
            self._consume(1)

        store_name = class_name
        if self._peek() is not None and self._peek().opname in self._STORE_OPS:
            store_name = self._peek().argval
            self._consume(1)
        del self.stack[sent_pos:]

        class_def = _build_class(
            self.walker,
            candidate_code,
            store_name or class_name or "Anon",
            bases,
        )
        self.body.append(class_def)
        return True

    def _match_function_def(self) -> bool:
        cur = self._peek()
        if cur is None or cur.opname != "MAKE_FUNCTION":
            return False
        # MAKE_FUNCTION's stack shape varies by Python version:
        #
        # * 3.7–3.10: pops ``qualname; code; [closure?]; [annotations?];
        #   [kw_defaults?]; [defaults?]`` — the trailing items depend
        #   on the 4-bit ``arg`` flag. The code object sits at
        #   ``stack[-2]`` (qualname is on top).
        # * 3.11–3.12: same flag-encoded layout but **no** qualname
        #   pop (PEP 657's split call removed it). Code is at ``-1``.
        # * 3.13+: MAKE_FUNCTION takes no arg; default / kwdefault /
        #   closure / annotate values are attached afterwards by a
        #   chain of ``SET_FUNCTION_ATTRIBUTE`` opcodes.
        version = self.walker.version_tuple
        is_old_layout = version[:2] <= (3, 10)
        is_split_layout = version[:2] in {(3, 11), (3, 12)}
        flag = cur.arg or 0
        n_flag_args = bin(flag).count("1") if flag else 0

        # Locate the code object based on the layout in use.
        code_idx = -1
        if (
            is_old_layout
            and len(self.stack) >= 2
            and isinstance(self.stack[-1], _Literal)
            and isinstance(self.stack[-1].value, str)
        ):
            code_idx = -2
        if not (
            len(self.stack) >= abs(code_idx)
            and isinstance(self.stack[code_idx], _Literal)
            and _is_code(self.stack[code_idx].value)
        ):
            return False

        code_obj = self.stack[code_idx].value
        # Sniff for the PEP 695 wrapper before destructively popping.
        name_for_check = getattr(code_obj, "co_name", "")
        if isinstance(name_for_check, str) and name_for_check.startswith(
            "<generic parameters of "
        ):
            return self._unwrap_generic(code_obj)

        # Drop qualname (old layout) and the code object itself.
        if is_old_layout and code_idx == -2:
            self.stack.pop()  # qualname
        self.stack.pop()  # code object
        self._consume(1)

        # Discard the flag-encoded attachments (defaults / kwdefaults /
        # annotations / closure) — the rule pass intentionally drops
        # defaults / annotations on the cross-version path.
        if (is_old_layout or is_split_layout) and n_flag_args:
            self._pop_n(n_flag_args)

        # 3.13+: ``SET_FUNCTION_ATTRIBUTE`` chain after MAKE_FUNCTION.
        while (
            self._peek() is not None and self._peek().opname == "SET_FUNCTION_ATTRIBUTE"
        ):
            if self.stack:
                self.stack.pop()
            self._consume(1)

        # Older versions: a name LOAD_CONST may follow MAKE_FUNCTION
        # before STORE_NAME — happens on 3.10 and below where the
        # qualified name is pushed separately and stored or used by
        # CALL. Some 3.8 / 3.9 code uses a separate LOAD_CONST for the
        # qualified name; consume it if present.
        cur = self._peek()
        if (
            cur is not None
            and cur.opname == "LOAD_CONST"
            and isinstance(cur.argval, str)
        ):
            # Heuristic: if the next op is STORE_NAME / CALL / decorator
            # CALL, this LOAD_CONST is the qualname and not the body.
            nxt = self._peek(1)
            if nxt is not None and nxt.opname in (
                self._STORE_OPS | {"CALL", "CALL_FUNCTION", "CALL_KW", "PUSH_NULL"}
            ):
                self._consume(1)

        # Decorator chain — consume CALLs (we don't reattach args).
        decorator_count = 0
        while self._peek() is not None and self._peek().opname in {
            "CALL",
            "CALL_FUNCTION",
            "CALL_KW",
        }:
            decorator_count += 1
            self._consume(1)
            if self._peek() is not None and self._peek().opname == "PUSH_NULL":
                self._consume(1)

        cur = self._peek()
        if cur is None or cur.opname not in self._STORE_OPS:
            # Function value used as an argument elsewhere; abandon.
            return True
        name = cur.argval
        self._consume(1)

        decorators: list[str] = []
        for _ in range(decorator_count):
            if self.stack and isinstance(self.stack[-1], _Name):
                decorators.append(self.stack.pop().name)
        decorators.reverse()

        func_def = _build_function(self.walker, code_obj, name, decorators)
        if self.is_class and name in _CLASS_IMPLICIT:
            return True
        if name == "__annotate__":
            return True
        self.body.append(func_def)
        return True

    def _unwrap_generic(self, wrapper_code: Any) -> bool:
        """PEP 695 ``<generic parameters of X>`` wrapper.

        Inner code object lives in the wrapper's ``co_consts`` with the
        user-visible name. We unwrap and emit the underlying def / class.
        Type-parameter recovery itself is out of scope here.
        """
        wrapper_name: str = getattr(wrapper_code, "co_name", "")
        target_name = wrapper_name[len("<generic parameters of ") : -1]
        inner = None
        for c in getattr(wrapper_code, "co_consts", ()):
            if _is_code(c) and getattr(c, "co_name", None) == target_name:
                inner = c
                break
        # Pop the wrapper code-object literal.
        self.stack.pop()
        self._consume(1)
        while self._peek() is not None and self._peek().opname in {
            "PUSH_NULL",
            "CALL",
            "CALL_FUNCTION",
            "CALL_KW",
            "LOAD_CONST",
        }:
            self._consume(1)
        store_name = target_name
        if self._peek() is not None and self._peek().opname in self._STORE_OPS:
            store_name = self._peek().argval
            self._consume(1)
        if inner is None:
            return True
        # Crude class-vs-function discriminator via __qualname__ store
        # in the inner code's instructions.
        is_class = False
        try:
            inner_ins = _iter_instructions(inner, self.walker.opc)
            for ins in inner_ins:
                if ins.opname == "STORE_NAME" and ins.argval == "__qualname__":
                    is_class = True
                    break
        except Exception:
            is_class = False
        if is_class:
            cls = _build_class(self.walker, inner, store_name, bases=[])
            self.body.append(cls)
        else:
            self.body.append(
                _build_function(self.walker, inner, store_name, decorators=[])
            )
        return True

    def _match_simple_store(self) -> bool:
        cur = self._peek()
        if cur is None or cur.opname not in self._STORE_OPS:
            return False
        target = cur.argval
        if cur.opname == "STORE_FAST":
            # Comprehension locals on 3.12+ — not a module-visible name.
            self._consume(1)
            return True
        if self.is_class and target in _CLASS_IMPLICIT:
            if self.stack:
                self.stack.pop()
            self._consume(1)
            return True
        if not self.stack:
            self.body.append(ir.Assign(target=target, value="..."))
            self._consume(1)
            return True
        value = self.stack.pop()
        rendered = _render_value(value)
        self._consume(1)
        if rendered is None:
            self.body.append(ir.Assign(target=target, value="..."))
            return True
        self.body.append(ir.Assign(target=target, value=rendered))
        return True

    def _match_pop_top(self) -> bool:
        cur = self._peek()
        if cur is None or cur.opname != "POP_TOP":
            return False
        if self.stack:
            self.stack.pop()
        self._consume(1)
        return True

    def _match_build_collection(self) -> bool:
        cur = self._peek()
        if cur is None:
            return False
        op = cur.opname
        if op == "BUILD_LIST":
            n = cur.arg or 0
            items = self._pop_n(n)
            self.stack.append(_Collection("list", items))
            self._consume(1)
            return True
        if op == "BUILD_TUPLE":
            n = cur.arg or 0
            items = self._pop_n(n)
            self.stack.append(_Collection("tuple", items))
            self._consume(1)
            return True
        if op == "BUILD_SET":
            n = cur.arg or 0
            items = self._pop_n(n)
            self.stack.append(_Collection("set", items))
            self._consume(1)
            return True
        if op == "BUILD_MAP":
            n = cur.arg or 0
            self._pop_n(2 * n)
            self.stack.append(_Sentinel("dict"))
            self._consume(1)
            return True
        if op == "BUILD_CONST_KEY_MAP":
            n = cur.arg or 0
            self._pop_n(n + 1)
            self.stack.append(_Sentinel("dict"))
            self._consume(1)
            return True
        if op in {"LIST_EXTEND", "SET_UPDATE", "DICT_UPDATE", "DICT_MERGE"}:
            if self.stack:
                self.stack.pop()
            self._consume(1)
            return True
        return False

    def _match_call_like(self) -> bool:
        cur = self._peek()
        if cur is None:
            return False
        op = cur.opname
        if op in {"CALL", "CALL_FUNCTION", "CALL_FUNCTION_KW", "CALL_METHOD"}:
            argc = cur.arg or 0
            self._pop_n(argc + 1)
            self.stack.append(_Sentinel("call_result"))
            self._consume(1)
            return True
        if op == "CALL_KW":
            argc = cur.arg or 0
            self._pop_n(argc + 2)
            self.stack.append(_Sentinel("call_result"))
            self._consume(1)
            return True
        if op == "CALL_INTRINSIC_1":
            # Intrinsic 2 = INTRINSIC_IMPORT_STAR. Already handled in
            # _finalise_from_import; here it's standalone, so drop.
            if self.stack:
                self.stack.pop()
            self._consume(1)
            return True
        if op == "TO_BOOL":
            self._consume(1)
            return True
        if op.startswith("POP_JUMP") or op in {
            "JUMP_FORWARD",
            "JUMP_BACKWARD",
            "JUMP_BACKWARD_NO_INTERRUPT",
            "JUMP_ABSOLUTE",
            "JUMP_IF_TRUE_OR_POP",
            "JUMP_IF_FALSE_OR_POP",
        }:
            self._consume(1)
            return True
        if op == "KW_NAMES":
            self._consume(1)
            return True
        return False


# ---------------------------------------------------------------------------
# Builders + helpers
# ---------------------------------------------------------------------------


def _build_class(
    walker: _Walker,
    code: Any,
    name: str,
    bases: list[str],
) -> ir.ClassDef:
    ctx = _Context(walker, code, is_class=True)
    stmts = ctx.run()
    # Unmangle ``_ClassName__name`` back to ``__name``.
    for stmt in stmts:
        if isinstance(stmt, (ir.FunctionDef, ir.ClassDef)):
            stmt.name = _unmangle(stmt.name, name)
        elif isinstance(stmt, (ir.Assign, ir.AnnotationOnly)):
            stmt.target = _unmangle(stmt.target, name)

    docstring: str | None = None
    body: list[ir.Stmt] = []
    for s in stmts:
        if (
            isinstance(s, ir.Assign)
            and s.target == "__doc__"
            and _is_string_literal(s.value)
        ):
            docstring = _strip_string_literal(s.value)
            continue
        if isinstance(s, ir.Assign) and s.target in _CLASS_IMPLICIT:
            continue
        body.append(s)

    return ir.ClassDef(
        name=name,
        bases=bases,
        keywords=[],
        docstring=docstring,
        body=body,
    )


def _build_function(
    walker: _Walker,
    code: Any,
    name: str,
    decorators: list[str],
) -> ir.FunctionDef:
    flags = getattr(code, "co_flags", 0) or 0
    is_async = bool(flags & _CO_COROUTINE) or bool(flags & _CO_ASYNC_GENERATOR)
    is_generator = bool(flags & _CO_GENERATOR)
    args = _args_from_code(code)
    docstring = _extract_docstring(walker, code)
    body: list[ir.Stmt] = [
        ir.UnknownBlock(
            disassembly=_dis_text(code, walker.version_tuple),
            signature=f"def {name}",
        )
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


def _args_from_code(code: Any) -> ir.Arguments:
    pos_total = getattr(code, "co_argcount", 0) or 0
    pos_only = getattr(code, "co_posonlyargcount", 0) or 0
    kw_only = getattr(code, "co_kwonlyargcount", 0) or 0
    flags = getattr(code, "co_flags", 0) or 0
    has_varargs = bool(flags & _CO_VARARGS)
    has_varkw = bool(flags & _CO_VARKEYWORDS)

    varnames = list(getattr(code, "co_varnames", ()))
    idx = 0
    posonly_args: list[ir.Arg] = []
    args: list[ir.Arg] = []
    for i in range(pos_total):
        if idx >= len(varnames):
            break
        name = varnames[idx]
        idx += 1
        if i < pos_only:
            posonly_args.append(ir.Arg(name=name))
        else:
            args.append(ir.Arg(name=name))
    kwonly_args: list[ir.Arg] = []
    for _ in range(kw_only):
        if idx >= len(varnames):
            break
        kwonly_args.append(ir.Arg(name=varnames[idx]))
        idx += 1
    vararg = None
    if has_varargs and idx < len(varnames):
        vararg = ir.Arg(name=varnames[idx])
        idx += 1
    kwarg = None
    if has_varkw and idx < len(varnames):
        kwarg = ir.Arg(name=varnames[idx])

    return ir.Arguments(
        posonly=posonly_args,
        args=args,
        vararg=vararg,
        kwonly=kwonly_args,
        kwarg=kwarg,
    )


def _extract_docstring(walker: _Walker, code: Any) -> str | None:
    consts = getattr(code, "co_consts", ())
    if not consts:
        return None
    first = consts[0]
    if not isinstance(first, str):
        return None
    instructions = _iter_instructions(code, walker.opc)
    for i, ins in enumerate(instructions):
        if (
            ins.opname == "STORE_NAME"
            and ins.argval == "__doc__"
            and i > 0
            and instructions[i - 1].opname == "LOAD_CONST"
            and isinstance(instructions[i - 1].argval, str)
        ):
            return instructions[i - 1].argval
    # Function: docstring at co_consts[0] only if no instruction
    # references slot 0 as a literal.
    for ins in instructions:
        if ins.opname == "LOAD_CONST" and ins.arg == 0:
            return None
    return first


def _iter_instructions(code: Any, opc: Any) -> list:
    """Materialise xdis instructions for *code* under *opc*."""
    varnames = getattr(code, "co_varnames", ())
    names = getattr(code, "co_names", ())
    consts = getattr(code, "co_consts", ())
    cellvars = getattr(code, "co_cellvars", ()) or ()
    freevars = getattr(code, "co_freevars", ()) or ()
    free = tuple(cellvars) + tuple(freevars)
    try:
        return list(
            get_instructions_bytes(code.co_code, opc, varnames, names, consts, free, {})
        )
    except Exception:
        return []


def _dis_text(code: Any, version_tuple: tuple) -> str:
    """Render *code*'s disassembly text — used by the LLM body-fill prompt.

    The version tuple is threaded through to :func:`xdis.disasm.disco`
    so that the rendered text uses the right opcode names for the
    bytecode at hand. A previous version hardcoded ``(3, 0, 0)`` which
    caused garbled disassembly for every other release — the LLM
    hybrid-mode prompt then saw nonsense.
    """
    buf = io.StringIO()
    try:
        from xdis.disasm import disco

        # disco signature varies between xdis releases; pass a permissive
        # set of kwargs and fall back to a minimal render on failure.
        disco(
            version_tuple,
            code,
            out=buf,
            timestamp=0,
            is_pypy=False,
        )
    except Exception:
        buf.write(f"<disassembly unavailable for {getattr(code, 'co_name', '?')}>\n")
    return buf.getvalue()


def _is_code(obj: Any) -> bool:
    """Detect both stdlib CodeType and xdis Code3X objects."""
    return type(obj).__name__.startswith("Code") and hasattr(obj, "co_consts")


def _unmangle(name: str, class_name: str) -> str:
    stripped_class = class_name.lstrip("_") or class_name
    prefix = "_" + stripped_class + "__"
    if name.startswith(prefix):
        return "__" + name[len(prefix) :]
    return name


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
            return "set()" if not rendered else f"{{{joined}}}"
    return None


def _format_literal(value: Any) -> str:
    if value is None or isinstance(value, (int, float, bool, bytes, str)):
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


def _is_string_literal(rendered: str) -> bool:
    if not rendered:
        return False
    first = rendered[0]
    if first not in ("'", '"'):
        return False
    return rendered.endswith(first)


def _strip_string_literal(rendered: str) -> str:
    import ast

    try:
        result = ast.literal_eval(rendered)
        return result if isinstance(result, str) else rendered
    except ValueError, SyntaxError:
        return rendered
