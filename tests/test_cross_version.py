"""Integration tests for :mod:`pychd.cross_version`.

These tests exercise the declaration-only rule pass that walks
non-current Python releases via xdis. Each test compiles the canonical
multi-version sample (``tools/build_multiversion_fixtures.py``) with
every locally-installed interpreter and verifies the recovered output:

* parses as Python,
* contains every original top-level declaration name,
* survives a hybrid-mode dispatch through :func:`decompile_pyc`
  *without* a single LLM call (because the cross-version pass leaves
  function bodies as :class:`pychd.ir.UnknownBlock`, the LLM is needed
  for hybrid mode — but rules-only must succeed unconditionally).
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from pychd import cross_version
from pychd.decompile import Mode, decompile_pyc
from pychd.versions import detect_version, rule_pass_for

FIXTURE_ROOT = Path("/tmp/pychd-multiversion")
_FIXTURES = sorted(FIXTURE_ROOT.glob("sample-*.pyc")) if FIXTURE_ROOT.is_dir() else []


# Expected declarations from tools/build_multiversion_fixtures.py SAMPLE.
EXPECTED_TOP_LEVEL_NAMES = {
    "Greeter",
    "greet",
    "make_greeter",
}
EXPECTED_IMPORTS = {"os.path"}
EXPECTED_VARIABLES = {"VERSION", "__all__"}


@pytest.mark.skipif(
    not _FIXTURES,
    reason=(
        "no /tmp/pychd-multiversion/sample-*.pyc fixtures — run "
        "`uv run python tools/build_multiversion_fixtures.py` first"
    ),
)
class TestCrossVersionRecovery:
    @pytest.mark.parametrize("fixture", _FIXTURES, ids=lambda p: p.name)
    def test_rules_only_dispatch_succeeds(self, fixture: Path) -> None:
        """rules-only mode must not raise for any fixture version."""
        report = decompile_pyc(fixture, mode=Mode.RULES_ONLY)
        # Source always parses.
        ast.parse(report.source)
        # The pass kept its hands off the LLM.
        assert report.llm_calls == 0

    @pytest.mark.parametrize("fixture", _FIXTURES, ids=lambda p: p.name)
    def test_declarations_survive(self, fixture: Path) -> None:
        """Every top-level class / function / import name is recovered."""
        report = decompile_pyc(fixture, mode=Mode.RULES_ONLY)
        tree = ast.parse(report.source)
        names: set[str] = set()
        imports: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                names.add(node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name)
        missing = (EXPECTED_TOP_LEVEL_NAMES | EXPECTED_VARIABLES) - names
        assert not missing, (
            f"{fixture.name}: missing top-level names {missing} "
            f"(recovered={sorted(names)})"
        )
        assert EXPECTED_IMPORTS <= imports, (
            f"{fixture.name}: missing imports {EXPECTED_IMPORTS - imports} "
            f"(recovered={sorted(imports)})"
        )

    @pytest.mark.parametrize("fixture", _FIXTURES, ids=lambda p: p.name)
    def test_dispatcher_chose_expected_pass(self, fixture: Path) -> None:
        """Verify dispatch chose native for 3.14, cross-version otherwise."""
        info = detect_version(fixture)
        expected = "native" if info.version[:2] == (3, 14) else "cross-version"
        assert rule_pass_for(info.version) == expected


class TestCrossVersionDefaults:
    """Per-version default-argument recovery for the cross-version walker.

    Recovery covers MAKE_FUNCTION's flag-encoded layout (3.7 – 3.10
    with qualname, 3.11 – 3.12 without), and the SET_FUNCTION_ATTRIBUTE
    chain layout (3.13+). The fixture is compiled fresh under each
    locally-installed Python in :func:`tools.build_multiversion_fixtures`,
    so any version that materialises a ``sample-X.Y.pyc`` is exercised.
    """

    _SOURCE = textwrap.dedent(
        """\
        def f(a, b=10, c="hi", *, d=5, e="yo"):
            return a
        """
    )

    @pytest.mark.parametrize("fixture", _FIXTURES, ids=lambda p: p.name)
    def test_defaults_round_trip(self, fixture: Path, tmp_path: Path) -> None:
        import py_compile

        # We need the sample to be compiled by the *same* interpreter as
        # ``fixture`` — derive the path back from the filename.
        py_interp = _python_for_fixture(fixture)
        if py_interp is None:
            pytest.skip(f"no interpreter for {fixture.name}")
        src_path = tmp_path / "defaults.py"
        src_path.write_text(self._SOURCE)
        pyc_path = tmp_path / "defaults.pyc"
        import subprocess

        cmd = (
            "import py_compile; "
            f"py_compile.compile({str(src_path)!r}, "
            f"cfile={str(pyc_path)!r}, doraise=True)"
        )
        result = subprocess.run([py_interp, "-c", cmd], capture_output=True, text=True)
        if result.returncode != 0:
            pytest.skip(f"{py_interp} could not compile sample: {result.stderr}")
        del py_compile  # ensure we used the *external* compiler, not local

        from pychd.decompile import Mode, decompile_pyc

        report = decompile_pyc(pyc_path, mode=Mode.RULES_ONLY)
        tree = ast.parse(report.source)
        funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
        assert len(funcs) == 1
        f = funcs[0]
        # Positional defaults: b=10, c='hi'.
        assert len(f.args.defaults) == 2
        default_values = [
            d.value if isinstance(d, ast.Constant) else None for d in f.args.defaults
        ]
        assert default_values == [10, "hi"], (
            f"{fixture.name}: positional defaults wrong: {default_values}"
        )
        # Keyword-only defaults: d=5, e='yo'.
        kw_defaults = [
            d.value if isinstance(d, ast.Constant) else None for d in f.args.kw_defaults
        ]
        assert kw_defaults == [5, "yo"], (
            f"{fixture.name}: kw defaults wrong: {kw_defaults}"
        )


def _python_for_fixture(fixture: Path) -> str | None:
    """Resolve the Python interpreter used to compile *fixture*."""
    import subprocess

    version = fixture.stem.split("-", 1)[1]
    proc = subprocess.run(
        ["uv", "python", "find", version],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip().splitlines()
    return out[0] if out else None


class TestCrossVersionSupports:
    """Coverage of the lightweight ``supports`` helper."""

    def test_all_3_x_minor_releases_supported(self) -> None:
        for minor in range(0, 15):
            assert cross_version.supports((3, minor)), (
                f"3.{minor} unexpectedly missing from cross_version coverage"
            )

    def test_python_2_not_supported(self) -> None:
        assert not cross_version.supports((2, 7))

    def test_garbage_not_supported(self) -> None:
        assert not cross_version.supports(())
        assert not cross_version.supports((99, 99))
