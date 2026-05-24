"""End-to-end recovery tests against handcrafted stdlib-style modules.

These tests intentionally avoid network or external fixtures — every
source string is inlined here. They cover the patterns we *expect* the
rule engine to handle today (Python 3.14):

- module docstrings
- `from __future__ import annotations`
- `import X`, `import X.Y`, `import X.Y as Z`, `from X import Y[, ...]`,
  `from X import *`, `from .x import Y` (relative)
- module-level constants and `__all__`
- class definitions with bases, docstrings, attributes, methods
- function definitions: positional, defaults, kwonly, *args, **kw,
  async, decorators
- nested classes
- multiple decorators
- attribute-style assignments

For each scenario we drive the full pipeline:

    source string → py_compile → rules-only decompile → AST parse

and assert that:

1. The recovered source parses as valid Python.
2. The top-level *named* surface (classes, functions, imports, vars)
   round-trips.
3. Specific structural claims hold (e.g. method names, decorators,
   docstring text).

These are deliberately written as black-box behavioural tests so they
remain stable across changes to the IR or rule implementation.
"""

from __future__ import annotations

import ast
import py_compile
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from pychd.decompile import Mode, decompile_pyc

SUPPORTED = sys.version_info[:2] == (3, 14)
pytestmark = pytest.mark.skipif(
    not SUPPORTED,
    reason="Rule engine targets Python 3.14",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compile_to_pyc(src: str, dst: Path) -> Path:
    py_path = dst / "src.py"
    py_path.write_text(src)
    pyc = dst / "out.pyc"
    py_compile.compile(str(py_path), cfile=str(pyc), doraise=True)
    return pyc


def _recover(src: str) -> str:
    """Compile *src* to a .pyc then run rules-only decompile.

    Returns the recovered source as a string. Always asserts that the
    result parses as valid Python before returning.
    """
    with tempfile.TemporaryDirectory() as tmp:
        pyc = _compile_to_pyc(src, Path(tmp))
        report = decompile_pyc(pyc, mode=Mode.RULES_ONLY)
    ast.parse(report.source)
    return report.source


def _public_surface(source: str) -> dict[str, Any]:
    """Extract a structural fingerprint suitable for round-trip comparison."""
    tree = ast.parse(source)
    classes: dict[str, dict[str, Any]] = {}
    functions: dict[str, dict[str, Any]] = {}
    imports: set[str] = set()
    from_imports: set[str] = set()
    variables: set[str] = set()

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            method_names = [
                m.name
                for m in node.body
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            classes[node.name] = {
                "bases": [ast.unparse(b) for b in node.bases],
                "methods": sorted(method_names),
            }
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[node.name] = {
                "is_async": isinstance(node, ast.AsyncFunctionDef),
                "args": [a.arg for a in node.args.args],
                "decorators": [ast.unparse(d) for d in node.decorator_list],
            }
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    variables.add(t.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                variables.add(node.target.id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = ("." * (node.level or 0)) + (node.module or "")
            for alias in node.names:
                from_imports.add(f"{mod}:{alias.name}")
    return {
        "classes": classes,
        "functions": functions,
        "imports": imports,
        "from_imports": from_imports,
        "variables": variables,
    }


def _assert_surface_subset(
    expected: dict[str, Any], actual: dict[str, Any], *, context: str
) -> None:
    """Every name in *expected* must appear in *actual*; details may relax."""
    missing_classes = set(expected["classes"]) - set(actual["classes"])
    assert not missing_classes, f"{context}: missing classes {missing_classes}"
    missing_funcs = set(expected["functions"]) - set(actual["functions"])
    assert not missing_funcs, f"{context}: missing functions {missing_funcs}"
    missing_imports = expected["imports"] - actual["imports"]
    assert not missing_imports, f"{context}: missing imports {missing_imports}"
    missing_from = expected["from_imports"] - actual["from_imports"]
    assert not missing_from, f"{context}: missing from-imports {missing_from}"
    missing_vars = expected["variables"] - actual["variables"]
    assert not missing_vars, f"{context}: missing vars {missing_vars}"


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


class TestReExportModule:
    """Modules whose sole job is to re-export names — rules should fully recover."""

    SRC = '''"""Public surface for the foo package."""

from .core import Bar, Baz
from .util import as_dict, parse
from .errors import FooError

__all__ = [
    "Bar",
    "Baz",
    "FooError",
    "as_dict",
    "parse",
]
'''

    def test_round_trips(self):
        recovered = _recover(self.SRC)
        expected = _public_surface(self.SRC)
        actual = _public_surface(recovered)
        _assert_surface_subset(expected, actual, context="re-export module")

    def test_no_unknown_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            pyc = _compile_to_pyc(self.SRC, Path(tmp))
            report = decompile_pyc(pyc, mode=Mode.RULES_ONLY)
        assert report.unknown_blocks == 0


class TestConfigStyleModule:
    """A small `argparse`-style entry-point module."""

    SRC = '''"""Entry point for the foo command."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

VERSION = "1.0.0"
DEFAULT_PORT = 8080
__all__ = ["main", "parse_args"]


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="foo")
    parser.add_argument("path", type=str)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level)
    return _run(Path(args.path), args.port)


def _run(path, port):
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

    def test_round_trips(self):
        recovered = _recover(self.SRC)
        expected = _public_surface(self.SRC)
        actual = _public_surface(recovered)
        _assert_surface_subset(expected, actual, context="argparse-style module")
        # Functions retain their argument names.
        for fname in ("parse_args", "main", "_run"):
            assert actual["functions"][fname]["args"], (
                f"{fname} signature should have positional args"
            )

    def test_constants_preserved(self):
        recovered = _recover(self.SRC)
        assert "VERSION = " in recovered
        assert "'1.0.0'" in recovered
        assert "DEFAULT_PORT = 8080" in recovered


class TestDataclassStyleModule:
    """A typical `dataclasses` module with decorated classes and methods."""

    SRC = '''"""Domain models for the foo package."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Point:
    """A 2D point."""

    x: int
    y: int

    def translate(self, dx, dy):
        return Point(self.x + dx, self.y + dy)


@dataclass
class Polygon:
    """A list of points with metadata."""

    points: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def add(self, p):
        self.points.append(p)

    @classmethod
    def empty(cls):
        return cls()

    @staticmethod
    def origin():
        return Point(0, 0)


def make_square(side):
    return Polygon(
        points=[Point(0, 0), Point(side, 0), Point(side, side), Point(0, side)]
    )
'''

    def test_class_skeletons_recovered(self):
        recovered = _recover(self.SRC)
        actual = _public_surface(recovered)
        assert "Point" in actual["classes"]
        assert "Polygon" in actual["classes"]
        # Methods on Polygon are all there even if their bodies aren't.
        assert "translate" in actual["classes"]["Point"]["methods"]
        for m in ("add", "empty", "origin"):
            assert m in actual["classes"]["Polygon"]["methods"]

    def test_module_docstring_preserved(self):
        recovered = _recover(self.SRC)
        assert "Domain models for the foo package." in recovered

    def test_class_docstrings_preserved(self):
        recovered = _recover(self.SRC)
        assert "A 2D point." in recovered
        assert "A list of points with metadata." in recovered

    def test_decorators_attached_to_methods(self):
        # We recover `classmethod`/`staticmethod` as decorators in v1.
        recovered = _recover(self.SRC)
        # classmethod / staticmethod should appear in the rendered text
        # of the Polygon class.
        assert "@classmethod" in recovered
        assert "@staticmethod" in recovered


class TestExceptionHierarchyModule:
    """A typical `errors.py` module — pure class hierarchy."""

    SRC = '''"""Exception types raised by the foo package."""

from __future__ import annotations


class FooError(Exception):
    """Base error for the foo package."""


class FooAuthError(FooError):
    """Authentication-related error."""


class FooConfigError(FooError):
    pass


class FooTimeoutError(FooError):
    def __init__(self, seconds):
        super().__init__(f"timed out after {seconds}s")
        self.seconds = seconds
'''

    def test_all_classes_present(self):
        recovered = _recover(self.SRC)
        actual = _public_surface(recovered)
        for cls in (
            "FooError",
            "FooAuthError",
            "FooConfigError",
            "FooTimeoutError",
        ):
            assert cls in actual["classes"]

    def test_docstrings_kept(self):
        recovered = _recover(self.SRC)
        assert "Exception types raised by the foo package." in recovered
        assert "Base error for the foo package." in recovered

    def test_init_method_recovered(self):
        recovered = _recover(self.SRC)
        # __init__ signature is rule-recoverable (body is LLM territory)
        assert "def __init__(self, seconds):" in recovered


class TestAsyncIOStyleModule:
    """Async-heavy module — coroutines, async generators, decorators."""

    SRC = '''"""Async API surface."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator


async def fetch(url):
    await asyncio.sleep(0)
    return url


async def stream(urls):
    for url in urls:
        yield await fetch(url)


def retry(fn):
    return fn


@retry
async def fetch_with_retry(url):
    return await fetch(url)


class AsyncClient:
    """An async client."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def request(self, path):
        return await fetch(path)
'''

    def test_async_functions_recovered_as_async(self):
        recovered = _recover(self.SRC)
        actual = _public_surface(recovered)
        for name in ("fetch", "stream", "fetch_with_retry"):
            assert name in actual["functions"]
            assert actual["functions"][name]["is_async"] is True

    def test_async_methods_recovered(self):
        recovered = _recover(self.SRC)
        assert "async def __aenter__" in recovered
        assert "async def __aexit__" in recovered
        assert "async def request" in recovered

    def test_decorator_on_async_function(self):
        recovered = _recover(self.SRC)
        actual = _public_surface(recovered)
        decos = actual["functions"]["fetch_with_retry"]["decorators"]
        assert "retry" in decos


class TestFunctionVariationsModule:
    """Stress signatures: posonly, kwonly, *args, **kw, defaults, lambdas."""

    SRC = '''"""Signature menagerie."""

from __future__ import annotations


def positional(a, b, c):
    return a + b + c


def with_defaults(a, b=1, c=2):
    return a + b + c


def with_kwonly(a, *, b, c=10):
    return a + b + c


def with_varargs(*args, **kw):
    return sum(args) + sum(kw.values())


def with_posonly(a, b, /, c, *, d=4):
    return a + b + c + d


def with_everything(a, b, /, c, d=4, *args, e=5, **kw):
    return a + b + c + d + e + sum(args) + sum(kw.values())


double = lambda x: x * 2
'''

    def test_signatures_recovered(self):
        recovered = _recover(self.SRC)
        actual = _public_surface(recovered)
        expected_names = {
            "positional",
            "with_defaults",
            "with_kwonly",
            "with_varargs",
            "with_posonly",
            "with_everything",
        }
        assert expected_names.issubset(actual["functions"])

    def test_default_values_preserved_for_simple_literals(self):
        recovered = _recover(self.SRC)
        # The rule engine renders defaults as literal repr; check a few.
        assert "b=1" in recovered
        assert "c=2" in recovered
        assert "d=4" in recovered

    def test_kwonly_marker_present(self):
        recovered = _recover(self.SRC)
        # `def with_kwonly(a, *, b, c=10):` — the `*` separator must survive.
        assert "with_kwonly(a, *," in recovered

    def test_posonly_marker_present(self):
        recovered = _recover(self.SRC)
        assert "with_posonly(a, b, /," in recovered


class TestNestedClassesModule:
    """A class containing nested classes — rule extraction must recurse."""

    SRC = '''"""Nested types."""

from __future__ import annotations


class Outer:
    """Outer class."""

    CONST = 42

    class Inner:
        """Inner class."""

        def hello(self):
            return "hi"

    class Sibling:
        pass

    def make_inner(self):
        return Outer.Inner()
'''

    def test_outer_recovered(self):
        recovered = _recover(self.SRC)
        actual = _public_surface(recovered)
        assert "Outer" in actual["classes"]
        # Methods on Outer are visible.
        assert "make_inner" in actual["classes"]["Outer"]["methods"]

    def test_nested_classes_appear_in_source(self):
        recovered = _recover(self.SRC)
        # Nested classes are inside Outer's body; check we kept the keywords.
        assert "class Inner" in recovered
        assert "class Sibling" in recovered


class TestRelativeImports:
    """Relative imports common in package internals."""

    SRC = """from . import sibling
from .util import helper
from ..parent import Thing
from .errors import FooError as Err
"""

    def test_all_relative_imports_recovered(self):
        recovered = _recover(self.SRC)
        # Sanity-check the rendered form for each pattern.
        assert "from . import sibling" in recovered
        assert "from .util import helper" in recovered
        assert "from ..parent import Thing" in recovered
        assert "from .errors import FooError as Err" in recovered


class TestComplexLiteralsModule:
    """Module-level data structures: nested lists, tuples, dicts, sets."""

    SRC = '''"""Constants."""

NUMBERS = [1, 2, 3, 4, 5]
NAMES = ("alpha", "beta", "gamma")
COLORS = {"red", "green", "blue"}
CONFIG = {
    "host": "localhost",
    "port": 8080,
    "debug": True,
    "options": None,
}
NESTED = [(1, "a"), (2, "b"), (3, "c")]
EMPTY_LIST = []
EMPTY_TUPLE = ()
EMPTY_DICT = {}
'''

    def test_literal_constants_round_trip(self):
        recovered = _recover(self.SRC)
        # We render Python repr() — values should be present verbatim.
        for needle in (
            "NUMBERS = [1, 2, 3, 4, 5]",
            "EMPTY_LIST = []",
            "EMPTY_TUPLE = ()",
        ):
            assert needle in recovered, f"missing: {needle}"

    def test_string_literals_preserved(self):
        recovered = _recover(self.SRC)
        assert "'localhost'" in recovered
        assert "8080" in recovered


# ---------------------------------------------------------------------------
# Directory-level pipeline
# ---------------------------------------------------------------------------


class TestProjectLevelDecompile:
    """The directory mode produces a mirrored output tree."""

    def test_project_tree(self, tmp_path):
        src = tmp_path / "project"
        src.mkdir()
        (src / "__init__.py").write_text('"""Package."""\nfrom .core import Foo\n')
        (src / "core.py").write_text(
            '"""Core."""\n\n\nclass Foo:\n    """Doc."""\n    X = 1\n'
        )
        sub = src / "sub"
        sub.mkdir()
        (sub / "__init__.py").write_text("")
        (sub / "util.py").write_text("def add(a, b):\n    return a + b\n")

        out_dir = tmp_path / "decompiled"
        from pychd.decompile import decompile

        decompile(
            to_decompile=src,
            output_path=out_dir,
            model=None,
            mode=Mode.RULES_ONLY,
        )

        # Mirror layout is preserved.
        assert (out_dir / "core.py").exists()
        assert (out_dir / "sub" / "util.py").exists()
        # Each output parses as Python.
        for f in out_dir.rglob("*.py"):
            ast.parse(f.read_text())

    def test_project_recovers_named_surface(self, tmp_path):
        src = tmp_path / "pkg"
        src.mkdir()
        (src / "api.py").write_text(
            '"""API."""\n'
            "from typing import Any\n"
            "\n"
            "__all__ = ['create', 'Thing']\n"
            "\n"
            "\n"
            "def create(name: str) -> 'Thing':\n"
            "    return Thing(name)\n"
            "\n"
            "\n"
            "class Thing:\n"
            "    def __init__(self, name):\n"
            "        self.name = name\n"
        )
        out_dir = tmp_path / "decompiled"
        from pychd.decompile import decompile

        decompile(
            to_decompile=src,
            output_path=out_dir,
            model=None,
            mode=Mode.RULES_ONLY,
        )
        recovered = (out_dir / "api.py").read_text()
        surface = _public_surface(recovered)
        assert "create" in surface["functions"]
        assert "Thing" in surface["classes"]
        assert "__all__" in surface["variables"]


# ---------------------------------------------------------------------------
# Validation round-trip
# ---------------------------------------------------------------------------


class TestValidatorIntegration:
    """validate.compare_ast modulo annotations should accept rule output."""

    def test_compare_ignore_annotations_matches(self):
        src = (
            '"""M."""\n'
            "from typing import Any\n"
            "\n"
            "X = 1\n"
            "\n"
            "\n"
            "def foo(a, b):\n"
            "    return a + b\n"
        )
        recovered = _recover(src)
        from pychd.validate import compare_ast

        # The original signature has no annotation here, and the rule
        # engine emits a `pass` for the body. We compare modulo bodies
        # by stripping them on both sides.
        def _strip_bodies(source: str) -> str:
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    node.body = [ast.Pass()]
            return ast.unparse(tree)

        a = _strip_bodies(src)
        b = _strip_bodies(recovered)
        result = compare_ast(a, b, ignore_annotations=True)
        assert result.match, f"AST diff: {result.details}"
