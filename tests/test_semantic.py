"""Tests for the three-axis semantic equivalence comparators.

The tests compile a known source under the *current* interpreter so
that the producing-interpreter-detection codepath collapses to the
in-process happy path (the cross-version subprocess codepath is
exercised by the comparison benchmark itself, which already runs
Python 3.8 alongside the test interpreter).
"""

from __future__ import annotations

import py_compile
import textwrap
from pathlib import Path

import pytest

from pychd.semantic import (
    SemanticResult,
    behavioral_smoke,
    bytecode_exact,
    bytecode_normalized,
    compare_all,
    edit_similarity,
    functional_correctness,
)


def _compile(tmp_path: Path, name: str, src: str) -> tuple[Path, Path]:
    """Write *src* to a .py and compile it to .pyc; return (py, pyc)."""
    py = tmp_path / f"{name}.py"
    pyc = tmp_path / f"{name}.pyc"
    py.write_text(src)
    py_compile.compile(str(py), cfile=str(pyc), doraise=True)
    return py, pyc


# ---------------------------------------------------------------------------
# bytecode_exact
# ---------------------------------------------------------------------------


class TestBytecodeExact:
    def test_identical_source_matches(self, tmp_path):
        src = textwrap.dedent("""\
            def foo(x, y):
                return x + y

            VALUE = 42
            """)
        _, pyc = _compile(tmp_path, "m", src)
        result = bytecode_exact(pyc, src)
        assert result.match is True
        assert "identical" in result.detail

    def test_different_source_differs(self, tmp_path):
        a = "def foo():\n    return 1\n"
        b = "def foo():\n    return 2\n"
        _, pyc = _compile(tmp_path, "m", a)
        result = bytecode_exact(pyc, b)
        assert result.match is False
        assert "differs" in result.detail

    def test_whitespace_drift_breaks_exact(self, tmp_path):
        a = "def foo():\n    return 1\n"
        b = "def  foo():\n    return  1\n"
        _, pyc = _compile(tmp_path, "m", a)
        result = bytecode_exact(pyc, b)
        # Whitespace differences shift lnotab/positions even when
        # semantics are identical — this is exactly the false-negative
        # behaviour the README warns about.
        assert result.match is False

    def test_recovered_source_unparseable_returns_no_match(self, tmp_path):
        _, pyc = _compile(tmp_path, "m", "x = 1\n")
        result = bytecode_exact(pyc, "def :")
        assert result.match is False
        assert "compile" in result.detail


# ---------------------------------------------------------------------------
# bytecode_normalized
# ---------------------------------------------------------------------------


class TestBytecodeNormalized:
    def test_identical_source_matches(self, tmp_path):
        src = textwrap.dedent("""\
            class C:
                def m(self, x):
                    return x

            VALUE = 3
            """)
        _, pyc = _compile(tmp_path, "m", src)
        result = bytecode_normalized(pyc, src)
        assert result.match is True

    def test_whitespace_drift_still_matches(self, tmp_path):
        """Canonicalisation strips lnotab/position bookkeeping, so two
        sources that differ only in whitespace produce the same
        instruction stream."""
        a = textwrap.dedent("""\
            def foo(x):
                return x + 1
            """)
        # Extra blank line — lnotab will differ, instruction stream
        # will not.
        b = textwrap.dedent("""\
            def foo(x):

                return x + 1
            """)
        _, pyc = _compile(tmp_path, "m", a)
        result = bytecode_normalized(pyc, b)
        assert result.match is True

    def test_real_semantic_change_caught(self, tmp_path):
        a = "def foo(x):\n    return x + 1\n"
        b = "def foo(x):\n    return x + 2\n"
        _, pyc = _compile(tmp_path, "m", a)
        result = bytecode_normalized(pyc, b)
        assert result.match is False
        assert "divergence" in result.detail or "mismatch" in result.detail

    def test_nested_function_recurses(self, tmp_path):
        a = textwrap.dedent("""\
            def outer():
                def inner(x):
                    return x + 1
                return inner
            """)
        b = textwrap.dedent("""\
            def outer():
                def inner(x):
                    return x + 2
                return inner
            """)
        _, pyc = _compile(tmp_path, "m", a)
        # Same outer shape, divergent inner — canonical recursion must
        # catch it.
        result = bytecode_normalized(pyc, b)
        assert result.match is False


# ---------------------------------------------------------------------------
# behavioral_smoke
# ---------------------------------------------------------------------------


class TestBehavioralSmoke:
    def test_identical_module_matches(self, tmp_path):
        src = textwrap.dedent("""\
            def greet(name):
                return f"hello {name}"

            class Counter:
                def __init__(self, start=0):
                    self.value = start
            """)
        py, _ = _compile(tmp_path, "m", src)
        result = behavioral_smoke(py, src)
        assert result.match is True

    def test_missing_public_name_caught(self, tmp_path):
        orig = textwrap.dedent("""\
            def alpha():
                return 1

            def beta():
                return 2
            """)
        recovered = textwrap.dedent("""\
            def alpha():
                return 1
            """)
        py, _ = _compile(tmp_path, "m", orig)
        result = behavioral_smoke(py, recovered)
        assert result.match is False
        assert "missing" in result.detail.lower()

    def test_signature_change_caught(self, tmp_path):
        orig = "def f(x, y, z=0):\n    return x + y + z\n"
        recovered = "def f(x, y):\n    return x + y\n"
        py, _ = _compile(tmp_path, "m", orig)
        result = behavioral_smoke(py, recovered)
        assert result.match is False
        assert "signature" in result.detail.lower()

    def test_body_difference_tolerated(self, tmp_path):
        """A recovered file with placeholder bodies still passes the
        smoke test as long as the surface matches — this is the
        intended pychd workflow (rules-only emits ``pass`` bodies)."""
        orig = textwrap.dedent("""\
            def add(a, b):
                return a + b
            """)
        recovered = textwrap.dedent("""\
            def add(a, b):
                pass  # pychd: unrecovered body
            """)
        py, _ = _compile(tmp_path, "m", orig)
        result = behavioral_smoke(py, recovered)
        # Surface (name + signature) matches → smoke passes.
        assert result.match is True

    def test_recovered_unimportable_caught(self, tmp_path):
        orig = "x = 1\n"
        recovered = "raise RuntimeError('boom')\n"
        py, _ = _compile(tmp_path, "m", orig)
        result = behavioral_smoke(py, recovered)
        assert result.match is False
        assert "import" in result.detail.lower()


# ---------------------------------------------------------------------------
# compare_all combiner
# ---------------------------------------------------------------------------


class TestCompareAll:
    def test_identical_passes_all_axes(self, tmp_path):
        src = textwrap.dedent("""\
            VERSION = "1.0"

            def hello():
                return "world"
            """)
        py, pyc = _compile(tmp_path, "m", src)
        report = compare_all(pyc, py, src)
        assert report.bytecode_exact.match is True
        assert report.bytecode_normalized.match is True
        assert report.behavioral_smoke.match is True

    def test_placeholder_body_passes_smoke_only(self, tmp_path):
        """The pychd rules-only output looks like the recovered source
        below: signatures intact, bodies stubbed. Bytecode-level axes
        must fail (semantics differ) while behavioral_smoke passes."""
        orig = "def add(a, b):\n    return a + b\n"
        recovered = "def add(a, b):\n    pass  # pychd: unrecovered body\n"
        py, pyc = _compile(tmp_path, "m", orig)
        report = compare_all(pyc, py, recovered)
        assert report.bytecode_exact.match is False
        assert report.bytecode_normalized.match is False
        assert report.behavioral_smoke.match is True


# ---------------------------------------------------------------------------
# SemanticResult dataclass surface
# ---------------------------------------------------------------------------


def test_semantic_result_repr_is_useful():
    r = SemanticResult(match=False, detail="first divergence at #3")
    assert r.match is False
    assert "divergence" in r.detail


@pytest.mark.parametrize("src", ["", "x = 1\n", "class C: pass\n"])
def test_normalized_matches_self(tmp_path, src):
    _, pyc = _compile(tmp_path, "m", src or "pass\n")
    result = bytecode_normalized(pyc, src or "pass\n")
    assert result.match is True


# ---------------------------------------------------------------------------
# edit_similarity
# ---------------------------------------------------------------------------


class TestEditSimilarity:
    def test_identical_returns_one(self):
        src = "def f(x):\n    return x + 1\n"
        assert edit_similarity(src, src) == 1.0

    def test_empty_pair_returns_one(self):
        assert edit_similarity("", "") == 1.0

    def test_completely_different_low_score(self):
        a = "def alpha(): return 1"
        b = "import os\nimport sys\nclass Z: pass"
        score = edit_similarity(a, b)
        assert 0.0 <= score < 0.5

    def test_small_change_high_score(self):
        a = "def f(x):\n    return x + 1\n"
        b = "def f(x):\n    return x + 2\n"
        score = edit_similarity(a, b)
        # One character changed in 30 → expect well above 0.9.
        assert score > 0.9

    def test_truncation_cap_applied(self):
        # Two 200K strings that diverge only past the 100K cap should
        # still compare as identical because both are truncated to the
        # same prefix.
        common = "x = 1\n" * 17_000  # ~100 KB
        a = common + "DIVERGE_A\n"
        b = common + "DIVERGE_B\n"
        score = edit_similarity(a, b)
        assert score == 1.0

    def test_continuous_score_monotonic(self):
        """Adding shared prefix should not lower the similarity."""
        a = "import os\ndef foo(): pass\n"
        b1 = "def foo(): pass\n"
        b2 = "import os\ndef foo(): pass\n"
        assert edit_similarity(a, b2) >= edit_similarity(a, b1)


# ---------------------------------------------------------------------------
# functional_correctness (Pass@1)
# ---------------------------------------------------------------------------


class TestFunctionalCorrectness:
    def test_passing_check_returns_match(self):
        recovered = "def add(a, b):\n    return a + b\n"
        test = "def check(candidate):\n    assert candidate(2, 3) == 5\n"
        result = functional_correctness(recovered, test, "add")
        assert result.match is True
        assert "passed" in result.detail

    def test_assertion_failure_caught(self):
        # Recovered function returns wrong result; test asserts the
        # correct one.
        recovered = "def add(a, b):\n    return a - b\n"
        test = "def check(candidate):\n    assert candidate(2, 3) == 5\n"
        result = functional_correctness(recovered, test, "add")
        assert result.match is False
        assert "assertion" in result.detail.lower()

    def test_missing_entry_point_caught(self):
        recovered = "def something_else(): pass\n"
        test = "def check(candidate):\n    assert candidate() is None\n"
        result = functional_correctness(recovered, test, "add")
        assert result.match is False
        assert "entry point" in result.detail.lower()

    def test_recovered_import_failure_caught(self):
        recovered = "raise RuntimeError('boom')\n"
        test = "def check(candidate):\n    pass\n"
        result = functional_correctness(recovered, test, "add")
        assert result.match is False
        assert "import" in result.detail.lower()

    def test_exception_in_check_caught(self):
        recovered = "def f(x):\n    return x\n"
        # check raises a non-assertion exception
        test = "def check(candidate):\n    raise ValueError('intentional')\n"
        result = functional_correctness(recovered, test, "f")
        assert result.match is False
        assert "ValueError" in result.detail

    def test_multi_assertion_check(self):
        """Realistic HumanEval-style multi-assertion test."""
        recovered = "def is_palindrome(s):\n    return s == s[::-1]\n"
        test = (
            "def check(candidate):\n"
            "    assert candidate('') == True\n"
            "    assert candidate('a') == True\n"
            "    assert candidate('aba') == True\n"
            "    assert candidate('abc') == False\n"
        )
        result = functional_correctness(recovered, test, "is_palindrome")
        assert result.match is True

    def test_stub_body_fails_check(self):
        """A pychd rules-only stub (body = pass → returns None) fails
        any non-trivial check — the exact failure mode we want
        functional_correctness to surface."""
        recovered = "def add(a, b):\n    pass  # pychd: unrecovered body\n"
        test = "def check(candidate):\n    assert candidate(2, 3) == 5\n"
        result = functional_correctness(recovered, test, "add")
        assert result.match is False


# ---------------------------------------------------------------------------
# compare_all with new metrics
# ---------------------------------------------------------------------------


class TestCompareAllWithNewMetrics:
    def test_identical_full_score(self, tmp_path):
        src = "def add(a, b):\n    return a + b\n"
        py, pyc = _compile(tmp_path, "m", src)
        test = "def check(candidate):\n    assert candidate(2, 3) == 5\n"
        report = compare_all(pyc, py, src, test_src=test, entry_point="add")
        assert report.bytecode_exact.match is True
        assert report.bytecode_normalized.match is True
        assert report.behavioral_smoke.match is True
        assert report.edit_similarity == 1.0
        assert report.functional_correctness is not None
        assert report.functional_correctness.match is True

    def test_no_test_data_leaves_fc_none(self, tmp_path):
        src = "x = 1\n"
        py, pyc = _compile(tmp_path, "m", src)
        report = compare_all(pyc, py, src)
        assert report.functional_correctness is None
        # Other axes still computed.
        assert report.edit_similarity == 1.0
        assert report.bytecode_exact.match is True

    def test_stub_recovery_passes_only_similarity_and_smoke(self, tmp_path):
        """The pychd rules-only output: signatures intact, bodies
        stubbed. Verify each axis lands where the trade-off table
        promises it should."""
        orig = "def add(a, b):\n    return a + b\n"
        recovered = "def add(a, b):\n    pass  # pychd: unrecovered body\n"
        py, pyc = _compile(tmp_path, "m", orig)
        test = "def check(candidate):\n    assert candidate(2, 3) == 5\n"
        report = compare_all(pyc, py, recovered, test_src=test, entry_point="add")
        assert report.bytecode_exact.match is False
        assert report.bytecode_normalized.match is False
        assert report.behavioral_smoke.match is True  # surface matches
        assert 0.0 < report.edit_similarity < 1.0  # similar but not identical
        assert report.functional_correctness is not None
        assert report.functional_correctness.match is False  # stub fails check
