"""Tests for pychd's bytecode-version detection across Python 3.0+.

Two layers of testing:

1. **Unit tests** against the in-memory ``KNOWN_VERSIONS`` table — no
   filesystem access required. These run everywhere.

2. **Integration tests** against real ``.pyc`` files compiled by
   each locally-installed Python interpreter (via
   ``tools/build_multiversion_fixtures.py``). These are skipped when
   the fixture directory is absent — the CI runner builds it once at
   the top of the workflow.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pychd.versions import (
    KNOWN_VERSIONS,
    compatibility_matrix,
    detect_version,
    read_magic,
    supported,
)

FIXTURE_ROOT = Path("/tmp/pychd-multiversion")


# ---------------------------------------------------------------------------
# Table-driven unit tests
# ---------------------------------------------------------------------------


class TestKnownVersions:
    def test_at_least_one_entry_per_minor(self):
        """Every Python 3.minor in 0..14 has at least one magic recorded."""
        minors = {info.version[1] for info in KNOWN_VERSIONS.values()}
        for m in range(0, 15):
            assert m in minors, f"no magic-number entry for Python 3.{m}"

    def test_3_14_is_rule_supported(self):
        info = KNOWN_VERSIONS[3627]
        assert info.version == (3, 14)
        assert info.rule_supported is True

    def test_pre_3_14_falls_through_to_llm(self):
        for magic, info in KNOWN_VERSIONS.items():
            if info.version[:2] < (3, 14):
                assert info.rule_supported is False, (
                    f"magic={magic} (Python {info.label}) unexpectedly claims rule "
                    "support — pychd's rule pass currently targets 3.14 only"
                )

    def test_supported_helper(self):
        assert supported((3, 14)) is True
        assert supported((3, 13)) is False
        assert supported((3, 0)) is False
        assert supported((2, 7)) is False


# ---------------------------------------------------------------------------
# Compatibility matrix rendering
# ---------------------------------------------------------------------------


class TestCompatibilityMatrix:
    def test_renders_markdown(self):
        text = compatibility_matrix()
        # Markdown table header and at least 15 rows (one per minor).
        assert "| Python |" in text
        assert text.count("\n") >= 15

    def test_contains_each_minor(self):
        text = compatibility_matrix()
        for minor in range(0, 15):
            assert f"**3.{minor}**" in text, f"3.{minor} row missing"


# ---------------------------------------------------------------------------
# Integration tests against real .pyc fixtures
# ---------------------------------------------------------------------------


_FIXTURES = sorted(FIXTURE_ROOT.glob("sample-*.pyc")) if FIXTURE_ROOT.is_dir() else []


@pytest.mark.skipif(
    not _FIXTURES,
    reason=(
        "no /tmp/pychd-multiversion/sample-*.pyc fixtures — run "
        "`uv run python tools/build_multiversion_fixtures.py` first"
    ),
)
class TestCrossVersionDetection:
    """Verify detect_version() correctly identifies each compiled .pyc."""

    @pytest.mark.parametrize("fixture", _FIXTURES, ids=lambda p: p.name)
    def test_detect_recognises_fixture(self, fixture: Path):
        info = detect_version(fixture)
        # Pull the version out of the filename: "sample-3.13.pyc" → (3, 13)
        version_label = fixture.stem.split("-", 1)[1]
        major, minor = (int(p) for p in version_label.split(".")[:2])
        assert info.version[:2] == (major, minor), (
            f"{fixture.name} expected Python {major}.{minor}, "
            f"detected {info.label} (magic={info.magic_number})"
        )

    @pytest.mark.parametrize("fixture", _FIXTURES, ids=lambda p: p.name)
    def test_magic_round_trips(self, fixture: Path):
        magic = read_magic(fixture)
        info = detect_version(fixture)
        assert info.magic_number == magic

    def test_3_14_fixture_is_rule_supported(self):
        for f in _FIXTURES:
            if f.stem.endswith("3.14"):
                assert detect_version(f).rule_supported is True
                return
        pytest.skip("no 3.14 fixture present")

    def test_older_fixtures_are_llm_only(self):
        for f in _FIXTURES:
            if "3.14" in f.stem:
                continue
            assert detect_version(f).rule_supported is False, (
                f"{f.name} should route to LLM-only path"
            )
