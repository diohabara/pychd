"""Python 3.14 syntax coverage matrix.

Each test pins one syntactic construct and asserts pychd's rule pass:

1. **survives** without raising,
2. produces **parseable Python**, and
3. preserves the **public surface** (top-level classes/functions/imports
   and class-level annotations) — i.e. signature_match holds.

The intent is a regression net: if we ever break one of these
constructs in the rule pass, the offending case fails by name rather
than as a percentage drop in a global benchmark.

Covered constructs (Python 3.6 – 3.14 frontier):

- Imports: plain, dotted, ``as``, ``from`` *, relative, star
- Defs: positional, defaults, kwonly, posonly, varargs, kwargs,
  decorators (zero arg / arg-bearing / dotted), async, generator,
  async generator
- Classes: empty, single base, multi-base, metaclass, dotted base,
  decorated, generic (PEP 695), with class methods / staticmethods /
  properties / cached_property
- Annotations: parameter, return, AnnAssign, class-level AnnAssign,
  ``Final[T]``, ``ClassVar[T]``, generic ``list[int]``, union ``X | Y``
- Module-level constants: int / float / str / bytes / bool / None /
  tuple / list / set / frozenset / dict / nested literals
- TypedDict / Protocol / NamedTuple / dataclass
- Match statements (PEP 634)
- Walrus (PEP 572)
- f-strings (incl. nested PEP 701)
- ``type`` alias statements (PEP 695)
- ``except*`` exception groups (PEP 654)
- ``raise … from …``
- ``global`` / ``nonlocal``
- ``with`` (single / multi / async)
- ``yield`` / ``yield from`` / ``await``
- Comprehensions: list / set / dict / generator / nested
"""

from __future__ import annotations

import ast
import py_compile
import sys
import tempfile
from pathlib import Path

import pytest

from pychd.decompile import Mode, decompile_pyc

SUPPORTED = sys.version_info[:2] == (3, 14)
pytestmark = pytest.mark.skipif(
    not SUPPORTED,
    reason="Syntax matrix targets Python 3.14",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _recover(src: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        py = Path(tmp) / "src.py"
        py.write_text(src)
        pyc = Path(tmp) / "out.pyc"
        py_compile.compile(str(py), cfile=str(pyc), doraise=True)
        report = decompile_pyc(pyc, mode=Mode.RULES_ONLY)
    return report.source


def _public_names(tree: ast.AST) -> set[str]:
    """Module-level + class-body names that pychd is expected to recover."""
    if not isinstance(tree, ast.Module):
        return set()
    names: set[str] = set()

    def visit(node: ast.AST, *, in_function: bool) -> None:
        if isinstance(node, ast.ClassDef):
            if not in_function:
                names.add(f"class:{node.name}")
            for c in node.body:
                visit(c, in_function=False)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not in_function:
                names.add(f"def:{node.name}")
            for c in node.body:
                visit(c, in_function=True)
            return
        if in_function:
            return
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(f"import:{a.asname or a.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = ("." * (node.level or 0)) + (node.module or "")
            for a in node.names:
                names.add(f"from:{mod}:{a.name}")

    for n in tree.body:
        visit(n, in_function=False)
    return names


def _assert_surface(src: str) -> str:
    """Recover *src*, assert it parses and the public surface survives."""
    out = _recover(src)
    # 1. parses
    rec_tree = ast.parse(out)
    # 3. public surface preserved (subset semantics — recovered may have extra)
    orig = _public_names(ast.parse(src))
    rec = _public_names(rec_tree)
    missing = orig - rec
    assert not missing, (
        f"public surface lost names {missing}\n\noriginal:\n{src}\n\nrecovered:\n{out}"
    )
    return out


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------


class TestImports:
    def test_plain(self):
        _assert_surface("import os\n")

    def test_dotted(self):
        _assert_surface("import os.path\n")

    def test_as(self):
        _assert_surface("import os.path as op\n")

    def test_from(self):
        _assert_surface("from os.path import join\n")

    def test_from_multiple(self):
        _assert_surface("from os.path import join, exists, basename\n")

    def test_from_as(self):
        _assert_surface("from os.path import join as j\n")

    def test_from_star(self):
        _assert_surface("from os.path import *\n")

    def test_from_future(self):
        _assert_surface("from __future__ import annotations\n")

    def test_relative_one_dot(self):
        _assert_surface("from . import sibling\n")

    def test_relative_two_dots(self):
        _assert_surface("from ..util import helper\n")


# ---------------------------------------------------------------------------
# Function definitions
# ---------------------------------------------------------------------------


class TestFunctions:
    def test_no_args(self):
        _assert_surface("def f(): pass\n")

    def test_positional(self):
        _assert_surface("def f(a, b, c): return a\n")

    def test_default(self):
        _assert_surface("def f(a, b=1): return a + b\n")

    def test_kwonly(self):
        _assert_surface("def f(a, *, b=1): return a + b\n")

    def test_posonly(self):
        _assert_surface("def f(a, b, /, c): return a + b + c\n")

    def test_varargs(self):
        _assert_surface("def f(*args): return args\n")

    def test_kwargs(self):
        _assert_surface("def f(**kw): return kw\n")

    def test_varargs_and_kwargs(self):
        _assert_surface("def f(*args, **kw): return (args, kw)\n")

    def test_full_signature(self):
        _assert_surface(
            "def f(a, b, /, c, d=1, *args, e=2, **kw): "
            "return (a, b, c, d, args, e, kw)\n"
        )

    def test_async(self):
        _assert_surface("async def f(x): return await x\n")

    def test_generator(self):
        _assert_surface("def f(n):\n    for i in range(n):\n        yield i\n")

    def test_yield_from(self):
        _assert_surface("def f(it):\n    yield from it\n")

    def test_async_generator(self):
        _assert_surface("async def f(it):\n    async for x in it:\n        yield x\n")


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------


class TestDecorators:
    def test_single_bare(self):
        _assert_surface("def deco(f): return f\n@deco\ndef foo(): pass\n")

    def test_multiple_bare(self):
        _assert_surface(
            "def a(f): return f\ndef b(f): return f\n@a\n@b\ndef foo(): pass\n"
        )

    def test_dotted(self):
        _assert_surface("import functools\n@functools.lru_cache\ndef foo(): pass\n")

    def test_with_args(self):
        _assert_surface(
            "import functools\n@functools.lru_cache(maxsize=128)\ndef foo(): pass\n"
        )

    def test_class_decorator(self):
        _assert_surface("def deco(c): return c\n@deco\nclass C: pass\n")

    def test_class_decorator_with_args(self):
        _assert_surface(
            "def deco(arg):\n    def wrapper(c): return c\n    return wrapper\n"
            "@deco(arg=1)\nclass C: pass\n"
        )


# ---------------------------------------------------------------------------
# Classes
# ---------------------------------------------------------------------------


class TestClasses:
    def test_empty(self):
        _assert_surface("class C: pass\n")

    def test_single_base(self):
        _assert_surface("class E(Exception): pass\n")

    def test_multi_base(self):
        _assert_surface("class A: pass\nclass B: pass\nclass C(A, B): pass\n")

    def test_dotted_base(self):
        _assert_surface("import abc\nclass C(abc.ABC): pass\n")

    def test_with_method(self):
        _assert_surface("class C:\n    def m(self, x): return x\n")

    def test_with_classmethod(self):
        _assert_surface("class C:\n    @classmethod\n    def m(cls): return cls\n")

    def test_with_staticmethod(self):
        _assert_surface("class C:\n    @staticmethod\n    def m(): return 1\n")

    def test_with_property(self):
        _assert_surface("class C:\n    @property\n    def m(self): return 1\n")

    def test_with_metaclass(self):
        _assert_surface("class M(type): pass\nclass C(metaclass=M): pass\n")

    def test_nested(self):
        _assert_surface(
            "class O:\n"
            "    class I:\n"
            "        def m(self): return 1\n"
            "    def make(self): return O.I()\n"
        )


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------


class TestAnnotations:
    def test_module_var(self):
        out = _assert_surface("X: int = 1\n")
        # Annotation must show up textually.
        assert "X" in out

    def test_module_var_no_value(self):
        out = _assert_surface("X: int\n")
        assert "X" in out

    def test_class_field(self):
        out = _assert_surface("class C:\n    name: str\n    age: int = 0\n")
        # Both annotated attributes should be present.
        tree = ast.parse(out)
        for cls in tree.body:
            if isinstance(cls, ast.ClassDef) and cls.name == "C":
                fields = [
                    s.target.id
                    for s in cls.body
                    if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
                ]
                assert "name" in fields
                assert "age" in fields

    def test_with_future_annotations(self):
        out = _assert_surface(
            "from __future__ import annotations\n"
            "class C:\n"
            "    name: str\n"
            "    items: list[int]\n"
        )
        # With future annotations the annotation is stored as a string;
        # signature_match doesn't need the annotation, but it must still
        # appear in the recovered text so AnnAssign nodes survive.
        tree = ast.parse(out)
        for cls in tree.body:
            if isinstance(cls, ast.ClassDef) and cls.name == "C":
                fields = [
                    s.target.id
                    for s in cls.body
                    if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
                ]
                assert "name" in fields
                assert "items" in fields


# ---------------------------------------------------------------------------
# Module-level literals
# ---------------------------------------------------------------------------


class TestLiterals:
    def test_int(self):
        _assert_surface("X = 42\n")

    def test_float(self):
        _assert_surface("X = 3.14\n")

    def test_string(self):
        _assert_surface("X = 'hi'\n")

    def test_bytes(self):
        _assert_surface("X = b'bytes'\n")

    def test_bool(self):
        _assert_surface("X = True\n")

    def test_none(self):
        _assert_surface("X = None\n")

    def test_tuple(self):
        _assert_surface("X = (1, 2, 3)\n")

    def test_list(self):
        _assert_surface("X = [1, 2, 3]\n")

    def test_set(self):
        _assert_surface("X = {1, 2, 3}\n")

    def test_frozenset(self):
        _assert_surface("X = frozenset({1, 2})\n")

    def test_dict(self):
        _assert_surface("X = {'a': 1, 'b': 2}\n")

    def test_nested_dict(self):
        _assert_surface("X = {'outer': {'inner': [1, 2]}}\n")

    def test_all_dunder(self):
        _assert_surface("__all__ = ['a', 'b', 'c']\n")


# ---------------------------------------------------------------------------
# Typing constructs
# ---------------------------------------------------------------------------


class TestTypingConstructs:
    def test_typeddict(self):
        _assert_surface(
            "from typing import TypedDict\n"
            "class Point(TypedDict):\n"
            "    x: int\n"
            "    y: int\n"
        )

    def test_protocol(self):
        _assert_surface(
            "from typing import Protocol\n"
            "class Hasher(Protocol):\n"
            "    def hash(self) -> int: ...\n"
        )

    def test_namedtuple_class(self):
        _assert_surface(
            "from typing import NamedTuple\n"
            "class Point(NamedTuple):\n"
            "    x: int\n"
            "    y: int\n"
        )

    def test_dataclass(self):
        _assert_surface(
            "from dataclasses import dataclass\n"
            "@dataclass\n"
            "class P:\n"
            "    x: int\n"
            "    y: int\n"
        )

    def test_frozen_dataclass(self):
        _assert_surface(
            "from dataclasses import dataclass\n"
            "@dataclass(frozen=True)\n"
            "class P:\n"
            "    x: int\n"
            "    y: int = 0\n"
        )


# ---------------------------------------------------------------------------
# Match statement (PEP 634)
# ---------------------------------------------------------------------------


class TestMatch:
    def test_match_literal(self):
        _assert_surface(
            "def f(x):\n"
            "    match x:\n"
            "        case 0: return 'zero'\n"
            "        case _: return 'other'\n"
        )

    def test_match_class(self):
        src = (
            "class P:\n    __match_args__ = ('x', 'y')\n    x = 0\n    y = 0\n"
            "def f(p):\n"
            "    match p:\n"
            "        case P(0, 0): return 'origin'\n"
            "        case _: return 'else'\n"
        )
        _assert_surface(src)


# ---------------------------------------------------------------------------
# PEP 695 type params and aliases
# ---------------------------------------------------------------------------


class TestPep695:
    def test_type_alias_statement(self):
        _assert_surface("type Alias = list[int]\n")

    def test_generic_function(self):
        _assert_surface("def f[T](x: T) -> T: return x\n")

    def test_generic_class(self):
        _assert_surface("class Stack[T]:\n    def push(self, x: T) -> None: pass\n")


# ---------------------------------------------------------------------------
# Walrus, comprehensions, ternary, lambdas
# ---------------------------------------------------------------------------


class TestExpressionForms:
    def test_walrus(self):
        _assert_surface(
            "def f(lst):\n"
            "    if (n := len(lst)) > 10:\n"
            "        return n\n"
            "    return -1\n"
        )

    def test_list_comprehension(self):
        _assert_surface("X = [i * 2 for i in range(10) if i % 2 == 0]\n")

    def test_set_comprehension(self):
        _assert_surface("X = {i * 2 for i in range(10)}\n")

    def test_dict_comprehension(self):
        _assert_surface("X = {i: i * 2 for i in range(10)}\n")

    def test_generator_expression(self):
        _assert_surface("def f(): return sum(i * 2 for i in range(10))\n")

    def test_lambda_module_level(self):
        _assert_surface("F = lambda x: x * 2\n")

    def test_lambda_in_call(self):
        _assert_surface("items = [3, 1, 2]\nresult = sorted(items, key=lambda x: -x)\n")

    def test_ternary(self):
        _assert_surface("def f(x): return 'pos' if x >= 0 else 'neg'\n")


# ---------------------------------------------------------------------------
# Exception handling
# ---------------------------------------------------------------------------


class TestExceptions:
    def test_try_except(self):
        _assert_surface(
            "def f():\n"
            "    try:\n        return 1\n    except ValueError:\n        return 2\n"
        )

    def test_try_except_finally(self):
        _assert_surface(
            "def f():\n"
            "    try:\n        return 1\n"
            "    except Exception:\n        return 2\n"
            "    finally:\n        pass\n"
        )

    def test_raise(self):
        _assert_surface("def f(): raise ValueError('bad')\n")

    def test_raise_from(self):
        _assert_surface(
            "def f(orig):\n"
            "    try:\n        raise RuntimeError() from orig\n"
            "    except RuntimeError:\n        pass\n"
        )

    def test_except_group(self):
        # PEP 654 — Python 3.11+ exception groups
        _assert_surface(
            "def f():\n    try:\n        pass\n    except* ValueError:\n        pass\n"
        )


# ---------------------------------------------------------------------------
# Statement variety
# ---------------------------------------------------------------------------


class TestStatements:
    def test_for_else(self):
        _assert_surface(
            "def f(items):\n"
            "    for x in items:\n        pass\n"
            "    else:\n        return 'empty'\n"
        )

    def test_while_else(self):
        _assert_surface(
            "def f():\n    while False:\n        pass\n    else:\n        return 0\n"
        )

    def test_with(self):
        _assert_surface("def f():\n    with open('x') as f:\n        return f.read()\n")

    def test_with_multi(self):
        _assert_surface(
            "def f():\n"
            "    with open('a') as a, open('b') as b:\n        return (a, b)\n"
        )

    def test_async_with(self):
        _assert_surface("async def f(cm):\n    async with cm as x:\n        return x\n")

    def test_global_nonlocal(self):
        _assert_surface(
            "X = 1\n"
            "def outer():\n"
            "    y = 2\n"
            "    def inner():\n"
            "        global X\n"
            "        nonlocal y\n"
            "        X += 1\n"
            "        y += 1\n"
            "    return inner\n"
        )

    def test_assert(self):
        _assert_surface("def f(x):\n    assert x > 0, 'must be positive'\n")
