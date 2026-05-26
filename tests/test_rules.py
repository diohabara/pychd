"""Unit tests for the rule-based bytecode → IR extractor."""

from __future__ import annotations

import ast
import sys
import textwrap

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

    def test_python_312_via_cross_version(self):
        """3.12 is rule-supported via the cross-version xdis pass."""
        assert supported_version((3, 12)) is True

    def test_python_2_unsupported(self):
        assert supported_version((2, 7)) is False


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

    def test_renders_trivial_return_body(self):
        """``return arg`` is now recovered by the trivial-body rule."""
        result = _run("def foo(a): return a\n")
        out = _renders_to_valid_python(result)
        # Semantic assertion: the rendered module contains exactly one
        # FunctionDef named foo whose body is a single Return(Name 'a').
        # The earlier substring-based check would have happily passed
        # for "def foo(a): return a; def foo(a): return b" or for a
        # commented-out variant.
        tree = ast.parse(out)
        funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
        assert len(funcs) == 1
        f = funcs[0]
        assert f.name == "foo"
        assert len(f.body) == 1
        assert isinstance(f.body[0], ast.Return)
        assert isinstance(f.body[0].value, ast.Name)
        assert f.body[0].value.id == "a"
        assert "pychd: unrecovered" not in out

    def test_complex_body_still_uses_unknown_block(self):
        """Non-trivial bodies still fall through to the LLM placeholder."""
        result = _run("def foo(a, b): return a + b * 2\n")
        out = _renders_to_valid_python(result)
        tree = ast.parse(out)
        funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
        assert len(funcs) == 1
        assert funcs[0].name == "foo"
        assert "pychd: unrecovered" in out

    def test_closure_body_is_not_falsely_recovered(self):
        """Free-variable reads must not be rendered as a trivial body.

        The trivial-body rule sees ``LOAD_DEREF outer; RETURN_VALUE``
        and is tempted to emit ``return outer`` — but the rendered
        function is standalone, so the deref name is unbound. The
        guard in ``_try_recover_trivial_body`` keeps such closures as
        UnknownBlock instead.
        """
        src = (
            "def make():\n"
            "    outer = 1\n"
            "    def inner():\n"
            "        return outer\n"
            "    return inner\n"
        )
        # We extract just the ``inner`` code object and recover its
        # body in isolation: ``inner`` has ``co_freevars = ('outer',)``.
        code = compile(src, "<closure>", "exec")
        make_code = next(
            c for c in code.co_consts if hasattr(c, "co_name") and c.co_name == "make"
        )
        inner_code = next(
            c
            for c in make_code.co_consts
            if hasattr(c, "co_name") and c.co_name == "inner"
        )
        from pychd.rules import _try_recover_trivial_body

        recovered = _try_recover_trivial_body(inner_code, has_docstring=False)
        assert recovered is None, (
            f"trivial-body rule must defer closures to the LLM path; got {recovered!r}"
        )


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


class TestControlFlowRecovery:
    """Module-level if/try blocks survive with correct indentation."""

    def test_if_type_checking_guarded_imports(self):
        src = (
            "from typing import TYPE_CHECKING\n"
            "\n"
            "if TYPE_CHECKING:\n"
            "    from collections.abc import Iterable\n"
        )
        result = _run(src)
        out = _renders_to_valid_python(result)
        tree = ast.parse(out)
        ifs = [n for n in tree.body if isinstance(n, ast.If)]
        assert len(ifs) == 1
        assert isinstance(ifs[0].test, ast.Name)
        assert ifs[0].test.id == "TYPE_CHECKING"
        # Iterable import survives *inside* the if-block, not at top level.
        guarded_imports = [n for n in ifs[0].body if isinstance(n, ast.ImportFrom)]
        assert len(guarded_imports) == 1
        assert guarded_imports[0].module == "collections.abc"
        # And not also at the top level.
        top_iterable_imports = [
            n
            for n in tree.body
            if isinstance(n, ast.ImportFrom) and n.module == "collections.abc"
        ]
        assert not top_iterable_imports

    def test_try_except_importerror_is_flattened(self):
        """``try: import X except ImportError`` flattens to top-level imports.

        The ``_try_except_block`` matcher is implemented (see
        ``pychd/rules.py``) but not wired in — its handler-boundary
        heuristic regressed ~15 modules across the benchmark corpus
        from mis-bounded handler ranges. The fallback contract is
        that *every import inside a try/except still surfaces at
        module scope*, which keeps ``signature_match`` intact even
        though the original ``Try`` structure is lost.
        """
        src = textwrap.dedent(
            """\
            try:
                from _accelerated import fast_thing
            except ImportError:
                from _python_fallback import fast_thing
            """
        )
        result = _run(src)
        out = _renders_to_valid_python(result)
        tree = ast.parse(out)
        # Both imports survive at top level.
        modules_imported = {
            n.module for n in tree.body if isinstance(n, ast.ImportFrom)
        }
        assert {"_accelerated", "_python_fallback"}.issubset(modules_imported), (
            f"import flattening dropped a module: got {modules_imported}"
        )
        # And the names land too.
        names_imported: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                names_imported.update(a.name for a in node.names)
        assert "fast_thing" in names_imported


class TestRecovery:
    def test_pure_reexport_module_fully_recovered(self):
        src = '"""Mod."""\nfrom os.path import join\n__all__ = ["join"]\n'
        result = _run(src)
        assert result.recovered is True
        out = _renders_to_valid_python(result)
        # Round-trip preserves docstring + __all__
        assert "Mod." in out
        assert "__all__" in out

    def test_trivial_pass_body_is_fully_recovered(self):
        """A ``pass`` body is now recovered without needing the LLM."""
        result = _run("def foo(): pass\n")
        assert result.recovered is True
        assert len(result.module.unknown_blocks()) == 0

    def test_module_with_complex_function_has_unknown(self):
        """Non-trivial bodies stay as UnknownBlock for the hybrid path."""
        result = _run(
            "def foo(x):\n    if x > 0:\n        return x * 2\n    return -x\n"
        )
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


class TestModuleLevelRecovery:
    """Regression tests for module-level constructs the rule pass used
    to mis-recover (for-loop variable leaks, ``MAP_ADD`` dict literals,
    ``if __name__ == "__main__":``)."""

    def test_for_loop_variable_is_not_leaked_as_module_assign(self):
        """``for x in iterable: side_effect`` used to emit
        ``x = iterable`` as a top-level assign — exposing the iterator
        variable to module scope. The for-skip walker drops the loop
        entirely now."""
        import textwrap

        src = textwrap.dedent(
            """\
            VALUES = (1, 2, 3)
            REGISTRY = []
            for value in VALUES:
                REGISTRY.append(value * 2)
            """
        )
        result = _run(src)
        out = _renders_to_valid_python(result)
        assert "VALUES" in out
        assert "REGISTRY" in out
        # Loop variable must not appear as a module-level Assign.
        assert "value = " not in out

    def test_dict_literal_with_tuple_keys_recovered(self):
        """Non-literal keys force ``BUILD_MAP 0`` + iterative
        ``MAP_ADD`` — the shape ``_compat_pickle.NAME_MAPPING`` uses."""
        import textwrap

        src = textwrap.dedent(
            """\
            MAP = {('a', 1): ('x', 'X'), ('b', 2): ('y', 'Y')}
            """
        )
        result = _run(src)
        out = _renders_to_valid_python(result)
        assert "MAP" in out
        # The recovered dict should have both entries — not just the
        # last value, which was the rule-pass bug before MAP_ADD support.
        assert "'a'" in out and "'b'" in out

    def test_if_main_block_survives(self):
        """``if __name__ == "__main__":`` is now recognised as a
        ``ir.If`` with the comparison preserved verbatim."""
        import textwrap

        src = textwrap.dedent(
            """\
            def main():
                pass

            if __name__ == "__main__":
                main()
            """
        )
        result = _run(src)
        out = _renders_to_valid_python(result)
        assert "if __name__ == '__main__':" in out
