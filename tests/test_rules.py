"""Unit tests for the rule-based bytecode → IR extractor."""

from __future__ import annotations

import ast
import sys

import pytest

from pychd import ir
from pychd.rules import RuleResult, extract_module, supported_version

SUPPORTED = sys.version_info[:2] == (3, 14)
pytestmark = pytest.mark.skipif(
    not SUPPORTED,
    reason="Rule engine targets Python 3.14",
)


def _run(src: str) -> RuleResult:
    code = compile(src, "<test>", "exec")
    return extract_module(code)


def _renders_to_valid_python(result: RuleResult) -> str:
    out = result.module.render()
    ast.parse(out)
    return out


class TestSupportedVersion:
    def test_current_version(self):
        assert supported_version(sys.version_info[:2]) is True

    def test_python_312(self):
        assert supported_version((3, 12)) is False


class TestModuleDocstring:
    def test_extracts_module_docstring(self):
        result = _run('"""Module doc."""\nx = 1\n')
        assert result.module.docstring == "Module doc."

    def test_no_docstring(self):
        result = _run("x = 1\n")
        assert result.module.docstring is None


class TestImports:
    def test_plain_import(self):
        result = _run("import os\n")
        imps = [s for s in result.module.body if isinstance(s, ir.Import)]
        assert imps == [ir.Import(names=[("os", None)])]

    def test_dotted_import(self):
        result = _run("import os.path\n")
        imps = [s for s in result.module.body if isinstance(s, ir.Import)]
        assert imps == [ir.Import(names=[("os.path", None)])]

    def test_import_as(self):
        result = _run("import os.path as op\n")
        imps = [s for s in result.module.body if isinstance(s, ir.Import)]
        assert imps == [ir.Import(names=[("os.path", "op")])]

    def test_from_import_single(self):
        result = _run("from os.path import join\n")
        froms = [s for s in result.module.body if isinstance(s, ir.FromImport)]
        assert froms == [
            ir.FromImport(module="os.path", level=0, names=[("join", None)])
        ]

    def test_from_import_multiple(self):
        result = _run("from os.path import join, exists\n")
        froms = [s for s in result.module.body if isinstance(s, ir.FromImport)]
        assert froms == [
            ir.FromImport(
                module="os.path",
                level=0,
                names=[("join", None), ("exists", None)],
            )
        ]

    def test_from_future(self):
        result = _run("from __future__ import annotations\n")
        froms = [s for s in result.module.body if isinstance(s, ir.FromImport)]
        assert froms == [
            ir.FromImport(module="__future__", level=0, names=[("annotations", None)])
        ]

    def test_from_import_as(self):
        result = _run("from os.path import join as j\n")
        froms = [s for s in result.module.body if isinstance(s, ir.FromImport)]
        assert froms == [
            ir.FromImport(module="os.path", level=0, names=[("join", "j")])
        ]

    def test_from_import_star(self):
        result = _run("from os.path import *\n")
        froms = [s for s in result.module.body if isinstance(s, ir.FromImport)]
        assert froms == [ir.FromImport(module="os.path", level=0, names=[("*", None)])]


class TestAssignments:
    def test_simple_int(self):
        result = _run("x = 42\n")
        assigns = [s for s in result.module.body if isinstance(s, ir.Assign)]
        assert assigns == [ir.Assign(target="x", value="42")]

    def test_simple_string(self):
        result = _run("name = 'Alice'\n")
        assigns = [s for s in result.module.body if isinstance(s, ir.Assign)]
        assert assigns == [ir.Assign(target="name", value="'Alice'")]

    def test_list_literal(self):
        result = _run('__all__ = ["a", "b"]\n')
        assigns = [s for s in result.module.body if isinstance(s, ir.Assign)]
        assert any(a.target == "__all__" and a.value == "['a', 'b']" for a in assigns)

    def test_tuple_literal(self):
        result = _run("data = (1, 2, 3)\n")
        assigns = [s for s in result.module.body if isinstance(s, ir.Assign)]
        assert any(a.target == "data" and a.value == "(1, 2, 3)" for a in assigns)


class TestFunctions:
    def test_simple_function_signature(self):
        result = _run("def foo(a, b): return a + b\n")
        funcs = [s for s in result.module.body if isinstance(s, ir.FunctionDef)]
        assert len(funcs) == 1
        f = funcs[0]
        assert f.name == "foo"
        assert [a.name for a in f.args.args] == ["a", "b"]
        assert f.is_async is False

    def test_default_value(self):
        result = _run("def foo(a, b=10): return a + b\n")
        f = next(s for s in result.module.body if isinstance(s, ir.FunctionDef))
        assert [(a.name, a.default) for a in f.args.args] == [
            ("a", None),
            ("b", "10"),
        ]

    def test_keyword_only(self):
        result = _run("def foo(a, *, b=5): return a + b\n")
        f = next(s for s in result.module.body if isinstance(s, ir.FunctionDef))
        assert [a.name for a in f.args.args] == ["a"]
        assert [(a.name, a.default) for a in f.args.kwonly] == [("b", "5")]

    def test_async_function(self):
        result = _run("async def foo(x): return x\n")
        f = next(s for s in result.module.body if isinstance(s, ir.FunctionDef))
        assert f.is_async is True

    def test_varargs_kwargs(self):
        result = _run("def foo(*args, **kw): pass\n")
        f = next(s for s in result.module.body if isinstance(s, ir.FunctionDef))
        assert f.args.vararg is not None and f.args.vararg.name == "args"
        assert f.args.kwarg is not None and f.args.kwarg.name == "kw"

    def test_decorator_single(self):
        src = "def deco(f): return f\n@deco\ndef foo(): pass\n"
        result = _run(src)
        funcs = [s for s in result.module.body if isinstance(s, ir.FunctionDef)]
        f = next(f for f in funcs if f.name == "foo")
        assert f.decorators == ["deco"]

    def test_renders_unrecovered_body(self):
        result = _run("def foo(a): return a\n")
        out = _renders_to_valid_python(result)
        assert "def foo(a):" in out
        assert "pychd: unrecovered" in out


class TestClasses:
    def test_class_with_base(self):
        result = _run("class E(Exception): pass\n")
        cls = next(s for s in result.module.body if isinstance(s, ir.ClassDef))
        assert cls.name == "E"
        assert cls.bases == ["Exception"]

    def test_class_with_docstring(self):
        result = _run('class C:\n    """Doc."""\n    pass\n')
        cls = next(s for s in result.module.body if isinstance(s, ir.ClassDef))
        assert cls.docstring == "Doc."

    def test_class_with_method(self):
        src = "class C:\n    def m(self): return 1\n"
        result = _run(src)
        cls = next(s for s in result.module.body if isinstance(s, ir.ClassDef))
        methods = [m for m in cls.body if isinstance(m, ir.FunctionDef)]
        assert [m.name for m in methods] == ["m"]

    def test_class_attribute(self):
        src = "class C:\n    KIND = 1\n"
        result = _run(src)
        cls = next(s for s in result.module.body if isinstance(s, ir.ClassDef))
        attrs = [a for a in cls.body if isinstance(a, ir.Assign)]
        assert any(a.target == "KIND" and a.value == "1" for a in attrs)


class TestRecovery:
    def test_pure_reexport_module_fully_recovered(self):
        src = '"""Mod."""\nfrom os.path import join\n__all__ = ["join"]\n'
        result = _run(src)
        assert result.recovered is True
        out = _renders_to_valid_python(result)
        # Round-trip preserves docstring + __all__
        assert "Mod." in out
        assert "__all__" in out

    def test_module_with_function_has_unknown(self):
        result = _run("def foo(): pass\n")
        assert result.recovered is False
        assert len(result.module.unknown_blocks()) == 1


class TestRender:
    def test_class_with_methods_renders(self):
        src = (
            "class C(Exception):\n"
            '    """Doc."""\n'
            "    KIND = 1\n"
            "    def __init__(self, x): self.x = x\n"
        )
        result = _run(src)
        out = _renders_to_valid_python(result)
        assert "class C(Exception):" in out
        assert '"""Doc."""' in out
        assert "KIND = 1" in out
        assert "def __init__(self, x):" in out
