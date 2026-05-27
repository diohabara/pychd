"""Unit tests for the IR layer (`pychd.ir`)."""

from __future__ import annotations

import ast

from pychd import ir


def _parses(source: str) -> ast.AST:
    return ast.parse(source)


class TestArguments:
    def test_empty(self):
        assert ir.Arguments().render() == ""

    def test_positional(self):
        args = ir.Arguments(args=[ir.Arg("a"), ir.Arg("b")])
        assert args.render() == "a, b"

    def test_with_default(self):
        args = ir.Arguments(args=[ir.Arg("a"), ir.Arg("b", default="5")])
        assert args.render() == "a, b=5"

    def test_posonly(self):
        args = ir.Arguments(posonly=[ir.Arg("a")], args=[ir.Arg("b")])
        assert args.render() == "a, /, b"

    def test_kwonly_with_no_vararg(self):
        args = ir.Arguments(args=[ir.Arg("a")], kwonly=[ir.Arg("b")])
        assert args.render() == "a, *, b"

    def test_vararg_and_kwarg(self):
        args = ir.Arguments(
            args=[ir.Arg("a")],
            vararg=ir.Arg("args"),
            kwonly=[ir.Arg("b", default="1")],
            kwarg=ir.Arg("kw"),
        )
        assert args.render() == "a, *args, b=1, **kw"

    def test_annotation(self):
        args = ir.Arguments(args=[ir.Arg("a", annotation="int")])
        assert args.render() == "a: int"

    def test_annotation_with_default(self):
        args = ir.Arguments(args=[ir.Arg("a", annotation="int", default="0")])
        assert args.render() == "a: int = 0"


class TestImports:
    def test_simple(self):
        assert ir.Import(names=[("os", None)]).render() == "import os"

    def test_as(self):
        assert ir.Import(names=[("os.path", "op")]).render() == "import os.path as op"


class TestFromImport:
    def test_single(self):
        s = ir.FromImport(module="os.path", level=0, names=[("join", None)])
        assert s.render() == "from os.path import join"

    def test_multiple_with_alias(self):
        s = ir.FromImport(
            module="os.path",
            level=0,
            names=[("join", None), ("exists", "ex")],
        )
        assert s.render() == "from os.path import join, exists as ex"

    def test_relative(self):
        s = ir.FromImport(module="util", level=2, names=[("Foo", None)])
        assert s.render() == "from ..util import Foo"

    def test_star(self):
        s = ir.FromImport(module="os.path", level=0, names=[("*", None)])
        assert s.render() == "from os.path import *"


class TestAssign:
    def test_plain(self):
        assert ir.Assign(target="x", value="42").render() == "x = 42"

    def test_annotated(self):
        s = ir.Assign(target="x", value="42", annotation="int")
        assert s.render() == "x: int = 42"


class TestAnnotationOnly:
    def test_renders(self):
        s = ir.AnnotationOnly(target="name", annotation="str")
        assert s.render() == "name: str"


class TestFunctionDef:
    def test_minimal_renders_with_pass(self):
        f = ir.FunctionDef(name="foo", args=ir.Arguments())
        out = f.render()
        assert "def foo():" in out
        assert "pass" in out

    def test_async(self):
        f = ir.FunctionDef(name="foo", args=ir.Arguments(), is_async=True)
        assert "async def foo():" in f.render()

    def test_with_docstring(self):
        f = ir.FunctionDef(name="foo", args=ir.Arguments(), docstring="Hi.")
        out = f.render()
        assert '"""Hi."""' in out

    def test_decorator(self):
        f = ir.FunctionDef(name="foo", args=ir.Arguments(), decorators=["staticmethod"])
        assert "@staticmethod" in f.render()

    def test_renders_valid_python(self):
        f = ir.FunctionDef(
            name="foo",
            args=ir.Arguments(args=[ir.Arg("a")]),
            docstring="Doc.",
            body=[ir.RawStatement(source="return a")],
        )
        _parses(f.render())


class TestClassDef:
    def test_minimal(self):
        c = ir.ClassDef(name="C")
        out = c.render()
        assert "class C:" in out
        assert "pass" in out

    def test_with_bases(self):
        c = ir.ClassDef(name="C", bases=["A", "B"])
        assert "class C(A, B):" in c.render()

    def test_with_keywords(self):
        c = ir.ClassDef(name="C", bases=["A"], keywords=[("metaclass", "Meta")])
        assert "class C(A, metaclass=Meta):" in c.render()

    def test_renders_valid_python(self):
        c = ir.ClassDef(
            name="C",
            bases=["object"],
            docstring="Hi.",
            body=[
                ir.Assign(target="X", value="1"),
                ir.FunctionDef(
                    name="m",
                    args=ir.Arguments(args=[ir.Arg("self")]),
                    body=[ir.RawStatement("return 1")],
                ),
            ],
        )
        _parses(c.render())


class TestUnknownBlock:
    def test_renders_as_pass(self):
        u = ir.UnknownBlock(disassembly="LOAD_CONST 0\n", signature="def foo")
        assert "pass" in u.render()


class TestRawStatement:
    def test_single_line(self):
        s = ir.RawStatement(source="return 42")
        assert s.render(indent=1) == "    return 42"

    def test_multi_line(self):
        s = ir.RawStatement(source="x = 1\nreturn x")
        out = s.render(indent=1)
        assert out == "    x = 1\n    return x"


class TestModule:
    def test_empty(self):
        m = ir.Module()
        # An empty module renders to an empty (or blank-only) string —
        # which is still valid Python source.
        assert m.render().strip() == ""

    def test_docstring_only(self):
        m = ir.Module(docstring="Hi.")
        out = m.render()
        assert '"""Hi."""' in out

    def test_full_module_parses(self):
        m = ir.Module(
            docstring="Hi.",
            body=[
                ir.Import(names=[("os", None)]),
                ir.FromImport(module="typing", level=0, names=[("Any", None)]),
                ir.Assign(target="X", value="1"),
                ir.FunctionDef(
                    name="foo",
                    args=ir.Arguments(args=[ir.Arg("a")]),
                    body=[ir.RawStatement("return a")],
                ),
            ],
        )
        _parses(m.render())

    def test_unknown_blocks_recursion(self):
        m = ir.Module(
            body=[
                ir.FunctionDef(
                    name="f",
                    args=ir.Arguments(),
                    body=[ir.UnknownBlock(disassembly="", signature="def f")],
                ),
                ir.ClassDef(
                    name="C",
                    body=[
                        ir.FunctionDef(
                            name="g",
                            args=ir.Arguments(),
                            body=[ir.UnknownBlock(disassembly="", signature="def g")],
                        )
                    ],
                ),
            ]
        )
        unknowns = m.unknown_blocks()
        assert len(unknowns) == 2
