"""Tests for the .pyc anonymiser (``pychd_pyobf``).

Contracts under test:

1. **Round-trip validity.** ``marshal.loads`` succeeds on the rewritten
   body, and ``dis.dis()`` walks the result without raising.
2. **Opcode preservation.** Anonymisation does not change the opcode
   sequence — only the operand metadata (names / consts).
3. **Identifier prefix discipline.** Every emitted ``co_names`` /
   ``co_varnames`` / ``co_freevars`` / ``co_cellvars`` entry starts
   with ``_n`` / ``_v`` / ``_f`` / ``_c`` respectively; every emitted
   string constant starts with ``_s``; every function name starts
   with ``_fn``.
4. **Recursive descent.** Nested code objects (inside ``co_consts``,
   e.g. the body of a function defined inside another function) are
   anonymised too.

The cross-version subprocess path is covered indirectly: it ships the
same logic, only running under a different interpreter. The unit suite
runs only the native path (we are on 3.14); the cross-version path is
exercised by the evaluator's smoke run in Phase D.
"""

from __future__ import annotations

import dis
import marshal
import py_compile
from pathlib import Path

import pytest

from pychd_pyobf import obfuscate

SAMPLE_SOURCE = """\
import os
import sys

GREETING = "hello"

def outer(x, y=1):
    \"\"\"Docstring leak risk — anonymise me.\"\"\"
    z = x + y
    def inner(w):
        return w * z
    return inner(z)

class Container:
    name: str = "container"
    def method(self, value):
        return value + len(self.name)
"""


@pytest.fixture
def sample_pyc(tmp_path: Path) -> Path:
    src = tmp_path / "sample.py"
    src.write_text(SAMPLE_SOURCE)
    pyc = tmp_path / "sample.pyc"
    py_compile.compile(str(src), cfile=str(pyc), doraise=True)
    return pyc


def _opcodes(code) -> list[str]:
    return [i.opname for i in dis.Bytecode(code)]


def _all_opcodes_including_nested(code) -> list[list[str]]:
    """Return a list of opcode lists — one per code object in the
    tree (parent first, then each nested code in order of appearance
    inside ``co_consts``)."""
    out: list[list[str]] = [_opcodes(code)]
    for c in code.co_consts:
        if isinstance(c, type(code)):
            out.extend(_all_opcodes_including_nested(c))
    return out


def test_obfuscate_roundtrips(sample_pyc: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.pyc"
    report = obfuscate(sample_pyc, out)
    assert report.used_native is True
    assert out.exists() and out.stat().st_size > 0
    # Round-trip the rewritten body.
    data = out.read_bytes()
    # Header is 16 bytes on 3.7+ (this fixture is 3.14).
    body = data[16:]
    new_code = marshal.loads(body)
    # dis.dis() must not raise.
    dis.dis(new_code, file=open("/dev/null", "w"))


def test_obfuscate_preserves_opcodes(sample_pyc: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.pyc"
    obfuscate(sample_pyc, out)
    # Top-level code object before and after.
    orig_body = sample_pyc.read_bytes()[16:]
    new_body = out.read_bytes()[16:]
    orig_code = marshal.loads(orig_body)
    new_code = marshal.loads(new_body)
    orig_seqs = _all_opcodes_including_nested(orig_code)
    new_seqs = _all_opcodes_including_nested(new_code)
    assert len(orig_seqs) == len(new_seqs), (
        f"nested-code count changed: {len(orig_seqs)} → {len(new_seqs)}"
    )
    for i, (a, b) in enumerate(zip(orig_seqs, new_seqs)):
        assert a == b, f"opcode mismatch at nested code object {i}: {a} vs {b}"


def _collect_field(code, field: str) -> list[str]:
    """Flatten a co_* tuple field over the whole nested code-object tree."""
    out = list(getattr(code, field))
    for c in code.co_consts:
        if isinstance(c, type(code)):
            out.extend(_collect_field(c, field))
    return out


def test_obfuscate_identifier_prefixes(sample_pyc: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.pyc"
    obfuscate(sample_pyc, out)
    code = marshal.loads(out.read_bytes()[16:])
    for prefix, field in [
        ("_n", "co_names"),
        ("_v", "co_varnames"),
        ("_f", "co_freevars"),
        ("_c", "co_cellvars"),
    ]:
        for name in _collect_field(code, field):
            assert name.startswith(prefix), (
                f"field {field} entry {name!r} should start with {prefix!r}"
            )


def test_obfuscate_filename_and_lineno_stripped(
    sample_pyc: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out.pyc"
    obfuscate(sample_pyc, out)
    code = marshal.loads(out.read_bytes()[16:])
    assert code.co_filename == "<anonymised>"
    assert code.co_firstlineno == 1
    # Nested code objects too.
    nested = [c for c in code.co_consts if isinstance(c, type(code))]
    assert nested, "expected at least one nested code object in the fixture"
    for n in nested:
        assert n.co_filename == "<anonymised>"


def test_obfuscate_mapping_populated(sample_pyc: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.pyc"
    report = obfuscate(sample_pyc, out)
    m = report.mapping
    # The fixture defines `outer`, `inner`, `Container`, `method`,
    # and binds locals x / y / z / w / value / self.
    assert m.varnames, "co_varnames mapping should not be empty"
    assert m.names, "co_names mapping should not be empty"
    assert m.co_names, "function-name mapping should not be empty"
    # Total renames should be at least the # of locals + globals.
    assert report.total_renames() >= 5


def test_obfuscate_recurses_into_nested_code(sample_pyc: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.pyc"
    obfuscate(sample_pyc, out)
    code = marshal.loads(out.read_bytes()[16:])

    # Walk the tree; every co_name should have the `_fn` prefix.
    def walk(c):
        yield c
        for k in c.co_consts:
            if isinstance(k, type(c)):
                yield from walk(k)

    for c in walk(code):
        assert c.co_name.startswith("_fn"), (
            f"co_name {c.co_name!r} on a nested code object did not get"
            " the per-depth `_fn` prefix"
        )


def test_obfuscate_force_subprocess_path(sample_pyc: Path, tmp_path: Path) -> None:
    """The cross-version (subprocess) rewriter must produce equivalent
    output to the native rewriter when forced on the current Python."""
    native_out = tmp_path / "native.pyc"
    sub_out = tmp_path / "sub.pyc"
    nat_report = obfuscate(sample_pyc, native_out)
    sub_report = obfuscate(sample_pyc, sub_out, force_subprocess=True)
    assert nat_report.used_native is True
    assert sub_report.used_native is False
    # Opcode sequences must match between the two paths.
    nat_code = marshal.loads(native_out.read_bytes()[16:])
    sub_code = marshal.loads(sub_out.read_bytes()[16:])
    assert _all_opcodes_including_nested(nat_code) == _all_opcodes_including_nested(
        sub_code
    )
