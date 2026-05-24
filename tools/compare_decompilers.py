"""Compare pychd against other Python decompilers on a shared corpus.

Why this script exists
----------------------

The README's headline numbers measure pychd against the ground truth
(the original ``.py`` source). They do **not** answer the natural
follow-up: *how does pychd compare to the existing tooling?*

To answer that, this script:

1. Picks a corpus we can compile to a version every comparison tool
   supports. ``uncompyle6`` only covers ≤ Python 3.8 and ``decompyle3``
   covers 3.7 / 3.8 — so the comparison corpus is compiled with
   Python 3.8 (the newest mutual ground).
2. Runs each tool against the same ``.pyc`` files.
3. Scores each tool's output against the original source using the
   same three-tier match metric (signature / declaration / strict).
4. Emits a JSON file consumed by :mod:`tools.render_figures` to
   produce the comparison bar chart embedded in the README.

What we **do not** claim
------------------------

* pychd is not faster than ``pycdc`` — we don't measure latency in the
  comparison chart, only fidelity. Cross-tool latency comparisons are
  unfair because the underlying engines target different problems
  (full body recovery vs. declaration recovery).
* The comparison is honest about the version mismatch: tools that
  don't support 3.8 are excluded rather than scored at 0%. Their
  version-range coverage is documented separately in the README.

Usage::

    uv run python tools/compare_decompilers.py
    uv run python tools/compare_decompilers.py --output /tmp/c.json
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.benchmark import (  # noqa: E402
    _declaration_match,
    _signature_match,
    _skeleton_match,
)

# Sources that each comparison tool can reasonably handle: we want
# small, syntactically simple modules so any failures are attributable
# to the decompiler rather than the corpus. The shapes match what
# we cover in the cross-version recovery test fixture.
COMPARISON_SOURCES: dict[str, str] = {
    "imports.py": (
        "import os.path\n"
        "from collections import OrderedDict\n"
        "from typing import List, Dict\n"
        "\n"
        "__all__ = ['OrderedDict']\n"
    ),
    "class.py": (
        '"""Greeter."""\n'
        "class Greeter:\n"
        '    """A trivial greeter."""\n'
        "    def __init__(self, name):\n"
        "        self.name = name\n"
        "    def greet(self):\n"
        "        return 'Hello, ' + self.name\n"
    ),
    "functions.py": (
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "def make_list():\n"
        "    return []\n"
        "\n"
        "def get_x(self):\n"
        "    return self.x\n"
    ),
}

# Python interpreter used to produce the .pyc files. 3.8 is the
# newest version that both ``uncompyle6`` and ``decompyle3`` can read.
COMPARISON_PYTHON = "3.8"


@dataclass
class ToolResult:
    modules: int
    parses: int
    signature_match: int
    declaration_match: int
    strict_match: int
    skipped: int
    error: str | None = None


def _find_python(version: str) -> str | None:
    try:
        proc = subprocess.run(
            ["uv", "python", "find", version],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except FileNotFoundError, subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip().splitlines()
    return out[0] if out else None


def _compile_corpus(py_interp: str, root: Path) -> dict[str, Path]:
    """Compile every source in :data:`COMPARISON_SOURCES` to .pyc."""
    src_dir = root / "src"
    pyc_dir = root / "pyc"
    src_dir.mkdir(parents=True, exist_ok=True)
    pyc_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {}
    for name, body in COMPARISON_SOURCES.items():
        src = src_dir / name
        src.write_text(body)
        pyc = pyc_dir / name.replace(".py", ".pyc")
        cmd = (
            "import py_compile; "
            f"py_compile.compile({str(src)!r}, cfile={str(pyc)!r}, doraise=True)"
        )
        proc = subprocess.run(
            [py_interp, "-c", cmd],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"py_compile failed for {name} under {py_interp}: {proc.stderr}"
            )
        files[name] = pyc
    return files


# ---- Per-tool runners ----------------------------------------------------


def _score(name: str, source: str, recovered: str) -> tuple[bool, bool, bool, bool]:
    """Return ``(parses, signature, declaration, strict)`` booleans."""
    try:
        original = ast.parse(source)
    except SyntaxError:
        return False, False, False, False
    try:
        rec = ast.parse(recovered)
        parses = True
    except SyntaxError:
        return False, False, False, False
    return (
        parses,
        _signature_match(original, rec),
        _declaration_match(original, rec),
        _skeleton_match(original, rec),
    )


def _run_pychd(pyc_files: dict[str, Path]) -> ToolResult:
    """Run pychd's rules-only pipeline on each compiled file."""
    from pychd.decompile import Mode, decompile_pyc

    res = ToolResult(
        modules=0,
        parses=0,
        signature_match=0,
        declaration_match=0,
        strict_match=0,
        skipped=0,
    )
    for name, pyc in pyc_files.items():
        res.modules += 1
        try:
            report = decompile_pyc(pyc, mode=Mode.RULES_ONLY)
        except Exception as e:
            res.error = (res.error or "") + f"\n{name}: {e}"
            continue
        parses, sig, decl, strict = _score(
            name, COMPARISON_SOURCES[name], report.source
        )
        if parses:
            res.parses += 1
        if sig:
            res.signature_match += 1
        if decl:
            res.declaration_match += 1
        if strict:
            res.strict_match += 1
    return res


def _run_external(
    tool_cmd: list[str],
    pyc_files: dict[str, Path],
    *,
    output_to_file: bool = False,
) -> ToolResult:
    """Invoke an external decompiler binary and score its output.

    The external tool is expected to take a single .pyc path as its
    argument and either write the recovered source to stdout, or — if
    ``output_to_file`` is set — accept ``-o <dir>`` and emit a sibling
    file next to the .pyc. Any non-zero exit code or empty output is
    treated as a per-module skip.

    ``decompyle3``'s default stdout includes a grammar dump that
    masks the actual decompiled source; for that tool we use ``-o``
    so the recovered file is isolated cleanly.
    """
    res = ToolResult(
        modules=0,
        parses=0,
        signature_match=0,
        declaration_match=0,
        strict_match=0,
        skipped=0,
    )
    for name, pyc in pyc_files.items():
        res.modules += 1
        recovered: str | None = None
        try:
            if output_to_file:
                with tempfile.TemporaryDirectory() as out_dir:
                    proc = subprocess.run(
                        [*tool_cmd, "-o", out_dir, str(pyc)],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=30,
                    )
                    if proc.returncode != 0:
                        res.skipped += 1
                        continue
                    out_files = [
                        Path(out_dir) / f
                        for f in os.listdir(out_dir)
                        if f.endswith((".py", ".dis"))
                    ]
                    py_files = [f for f in out_files if f.suffix == ".py"]
                    if not py_files:
                        res.skipped += 1
                        continue
                    recovered = py_files[0].read_text()
            else:
                proc = subprocess.run(
                    [*tool_cmd, str(pyc)],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
                if proc.returncode != 0 or not proc.stdout.strip():
                    res.skipped += 1
                    continue
                recovered = proc.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            res.error = (res.error or "") + f"\n{name}: {e}"
            res.skipped += 1
            continue
        if recovered is None:
            res.skipped += 1
            continue
        parses, sig, decl, strict = _score(name, COMPARISON_SOURCES[name], recovered)
        if parses:
            res.parses += 1
        if sig:
            res.signature_match += 1
        if decl:
            res.declaration_match += 1
        if strict:
            res.strict_match += 1
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "assets" / "_comparison.json",
    )
    ap.add_argument("--python-version", default=COMPARISON_PYTHON)
    args = ap.parse_args(argv)

    py_interp = _find_python(args.python_version)
    if py_interp is None:
        print(
            f"warning: Python {args.python_version} not installed; "
            f"skipping comparison (run `uv python install {args.python_version}`).",
            file=sys.stderr,
        )
        # Emit a stub so the rest of the pipeline still produces a file.
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({}, indent=2))
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        files = _compile_corpus(py_interp, Path(tmp))
        out: dict[str, dict] = {}
        out["pychd (rules-only)"] = asdict(_run_pychd(files))
        if shutil.which("uncompyle6"):
            out["uncompyle6"] = asdict(
                _run_external(["uncompyle6"], files, output_to_file=True)
            )
        if shutil.which("decompyle3"):
            out["decompyle3"] = asdict(
                _run_external(["decompyle3"], files, output_to_file=True)
            )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(out, indent=2))
    print(f"wrote {args.output.relative_to(REPO_ROOT)}")
    for tool, data in out.items():
        print(
            f"  {tool:<20} sig={data['signature_match']}/{data['modules']} "
            f"decl={data['declaration_match']}/{data['modules']} "
            f"strict={data['strict_match']}/{data['modules']} "
            f"skipped={data['skipped']}"
        )

    # Also surface the version-range matrix in the script's stdout so
    # CI logs document the comparison's limits explicitly.
    print()
    print("Tool version-range coverage:")
    print("  pychd          - every CPython 3.x (this corpus: 3.8)")
    print("  uncompyle6     - 2.4 - 3.8 (no 3.9+)")
    print("  decompyle3     - 3.7 / 3.8 only")
    print("  pycdc          - varies; not installed by default")
    print()
    print(f"Comparison corpus compiled with: {py_interp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
