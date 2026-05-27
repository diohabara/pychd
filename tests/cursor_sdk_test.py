"""End-to-end test: recover cursor-sdk source code via pychd.

The wheel for ``cursor-sdk`` ships plain ``.py`` files (despite being
a large wheel — most of the size is a vendored Node.js binary). For
this test we treat those ``.py`` files as the **ground truth** and
exercise the full pychd pipeline:

    .py  →  py_compile  →  .pyc  →  pychd (rules-only)  →  recovered .py

We then compare the recovered output to the ground truth via AST
(modulo annotations and method bodies, which v1 of the rule engine
does not yet reconstruct).

The test runs only if the ``cursor-sdk`` wheel has been pre-extracted
into ``tests/fixtures/cursor_sdk/`` (a one-line ``conftest.py`` fixture
downloads + extracts it on demand).
"""

from __future__ import annotations

import ast
import py_compile
import sys
from pathlib import Path

import pytest

from pychd.decompile import Mode, decompile_pyc

SUPPORTED = sys.version_info[:2] == (3, 14)
pytestmark = pytest.mark.skipif(
    not SUPPORTED,
    reason="cursor-sdk recovery currently requires Python 3.14",
)


def _names_only(tree: ast.AST) -> set[str]:
    """Collect the names of top-level classes/functions/imports/assigns.

    Comparing exact ASTs is too brittle (we drop some statements like
    ``if TYPE_CHECKING:`` flattening, lose some annotation defaults
    etc.). Comparing the *names* recovered gives a meaningful smoke
    signal: did we keep the right public surface?
    """
    if not isinstance(tree, ast.Module):
        return set()
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            names.add(f"class:{node.name}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(f"def:{node.name}")
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(f"var:{t.id}")
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                names.add(f"var:{node.target.id}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(f"import:{alias.asname or alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = ("." * (node.level or 0)) + (node.module or "")
            for alias in node.names:
                names.add(f"from:{mod}:{alias.name}")
    return names


def _fixture_dir() -> Path | None:
    """Locate an extracted cursor-sdk source tree.

    First looks at ``tests/fixtures/cursor_sdk``. Falls back to
    ``/tmp/cursor-sdk-investigation/wheel-extracted/cursor_sdk`` which
    the development workflow populates. Returns None if neither exists.
    """
    here = Path(__file__).resolve().parent
    candidates = [
        here / "fixtures" / "cursor_sdk",
        Path("/tmp/cursor-sdk-investigation/wheel-extracted/cursor_sdk"),
    ]
    for c in candidates:
        if c.is_dir() and any(c.glob("*.py")):
            return c
    return None


FIXTURE = _fixture_dir()


@pytest.mark.skipif(FIXTURE is None, reason="cursor-sdk fixture not available")
class TestCursorSdkRecovery:
    """Sanity-check the rule engine against real cursor-sdk modules."""

    def test_events_module_fully_recovered(self, tmp_path):
        assert FIXTURE is not None
        src = FIXTURE / "events.py"
        pyc = tmp_path / "events.pyc"
        py_compile.compile(str(src), cfile=str(pyc), doraise=True)

        report = decompile_pyc(pyc, mode=Mode.RULES_ONLY)
        assert report.unknown_blocks == 0, (
            "events.py is a pure re-export module: rules alone should "
            f"recover it. Got {report.unknown_blocks} unknown blocks."
        )
        ast.parse(report.source)

        recovered_names = _names_only(ast.parse(report.source))
        original_names = _names_only(ast.parse(src.read_text()))
        # __all__ assignment is the key contract for a re-export module.
        assert "var:__all__" in recovered_names
        # Every `from X import Y` should round-trip.
        from_imports = {n for n in original_names if n.startswith("from:")}
        assert from_imports.issubset(recovered_names), (
            f"missing imports: {from_imports - recovered_names}"
        )

    def test_asyncio_module_fully_recovered(self, tmp_path):
        assert FIXTURE is not None
        src = FIXTURE / "asyncio.py"
        pyc = tmp_path / "asyncio.pyc"
        py_compile.compile(str(src), cfile=str(pyc), doraise=True)

        report = decompile_pyc(pyc, mode=Mode.RULES_ONLY)
        assert report.unknown_blocks == 0
        ast.parse(report.source)
        # The module docstring is multi-line; make sure we kept it.
        assert "Public async surface" in report.source

    def test_class_skeletons_recovered(self, tmp_path):
        """Even when bodies need the LLM, signatures should be intact."""
        assert FIXTURE is not None
        src = FIXTURE / "_cursor.py"
        pyc = tmp_path / "_cursor.pyc"
        py_compile.compile(str(src), cfile=str(pyc), doraise=True)

        report = decompile_pyc(pyc, mode=Mode.RULES_ONLY)
        # Method bodies are unknown but the file should still parse.
        ast.parse(report.source)
        recovered = _names_only(ast.parse(report.source))
        # All three classes survive rule extraction.
        assert "class:_CursorModels" in recovered
        assert "class:_CursorRepositories" in recovered
        assert "class:Cursor" in recovered

    @pytest.mark.parametrize(
        "filename",
        [
            "events.py",
            "asyncio.py",
            "_cursor.py",
            "errors.py",
            "_async_cursor.py",
        ],
    )
    def test_top_level_names_survive(self, tmp_path, filename):
        """The recovered output keeps the same top-level public names."""
        assert FIXTURE is not None
        src = FIXTURE / filename
        if not src.exists():
            pytest.skip(f"{filename} not present in fixture")
        pyc = tmp_path / (filename + "c")
        py_compile.compile(str(src), cfile=str(pyc), doraise=True)

        report = decompile_pyc(pyc, mode=Mode.RULES_ONLY)
        ast.parse(report.source)

        recovered = _names_only(ast.parse(report.source))
        original = _names_only(ast.parse(src.read_text()))
        # Classes and functions defined at module level must survive.
        for kind in ("class:", "def:"):
            orig_subset = {n for n in original if n.startswith(kind)}
            assert orig_subset.issubset(recovered), (
                f"{filename}: missing {kind}* names: {orig_subset - recovered}"
            )
