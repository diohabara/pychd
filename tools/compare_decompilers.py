"""Compare pychd against other Python decompilers on a shared real corpus.

Why this script exists
----------------------

The README's headline numbers measure pychd against the ground truth
(the original ``.py`` source). They do **not** answer the natural
follow-up: *how does pychd compare to the existing tooling?*

The answer needs a corpus all three tools can read. ``uncompyle6``
caps at Python 3.8; ``decompyle3`` covers 3.7 / 3.8 only. The newest
mutual ground is **Python 3.8** — so this script compiles a real
corpus (a curated PyPI subset + stdlib modules) with a locally
installed 3.8 interpreter, runs each tool against every ``.pyc``,
and scores the recovered source via the same three-tier metric used
by the rest of the benchmark suite.

Honest framing
--------------

* The comparison is **declaration-level** (signature_match,
  declaration_match) plus strict-AST equality. Tools are not scored
  on body recovery — every tool tries to recover bodies and they all
  succeed-or-fail in different ways; comparing those scores would
  mostly compare grammar coverage of the prior tools rather than
  pychd's design.
* Tools that don't support 3.8 are excluded rather than scored at
  0 %. Their version-range coverage is documented separately.
* The corpus is downloaded on demand from PyPI and from the running
  interpreter's stdlib; nothing third-party is committed.

Usage::

    uv run python tools/compare_decompilers.py
    uv run python tools/compare_decompilers.py --output /tmp/cmp.json
    uv run python tools/compare_decompilers.py --quick   # smaller corpus
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
import textwrap
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.benchmark import (  # noqa: E402
    _declaration_match,
    _signature_match,
    _skeleton_match,
)

# Python interpreter used to produce the .pyc files. 3.8 is the
# newest version that both ``uncompyle6`` and ``decompyle3`` can read.
COMPARISON_PYTHON = "3.8"

# Curated set of stdlib modules that compile cleanly under 3.8 and
# exercise a representative cross-section of declaration shapes: pure
# data (``calendar``, ``ipaddress``), inheritance hierarchies
# (``contextlib``, ``logging``), import-heavy facades
# (``traceback``, ``typing``), and decorator-heavy modules
# (``functools``).
STDLIB_MODULES = [
    "calendar",
    "contextlib",
    "copy",
    "dataclasses",
    "enum",
    "functools",
    "ipaddress",
    "logging",
    "queue",
    "socketserver",
    "string",
    "tempfile",
    "textwrap",
    "tomllib",  # may not exist on 3.8 — gracefully skip
    "traceback",
    "typing",
    "weakref",
]


@dataclass
class ToolResult:
    modules: int = 0
    parses: int = 0
    signature_match: int = 0
    declaration_match: int = 0
    strict_match: int = 0
    skipped: int = 0
    error: str | None = None
    per_module: list[dict] | None = None


def _find_python(version: str) -> str | None:
    """Locate the absolute path to ``python<version>`` via uv."""
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


def _stdlib_dir(py_interp: str) -> Path | None:
    """Resolve the stdlib directory of *py_interp* (e.g. ``/.../python3.8``)."""
    try:
        snippet = "import sysconfig; print(sysconfig.get_paths()['stdlib'])"
        proc = subprocess.run(
            [py_interp, "-c", snippet],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError, subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    p = Path(proc.stdout.strip())
    return p if p.is_dir() else None


def _gather_corpus(
    py_interp: str,
    workdir: Path,
    *,
    quick: bool,
) -> list[tuple[str, Path, Path]]:
    """Compile a real Python 3.8 corpus into *workdir*.

    Returns ``[(module_name, src_path, pyc_path), ...]``.

    The corpus combines two sources:

    1. The curated stdlib subset declared in :data:`STDLIB_MODULES`,
       resolved against *py_interp*'s actual stdlib directory.
    2. A curated PyPI subset (``six``, ``packaging``, ``certifi``,
       ``idna``, ``charset_normalizer``) — these are pure-Python,
       Python-3.8-compatible, and broadly representative.

    ``quick=True`` halves both lists for fast smoke runs.
    """
    src_root = workdir / "src"
    pyc_root = workdir / "pyc"
    src_root.mkdir(parents=True, exist_ok=True)
    pyc_root.mkdir(parents=True, exist_ok=True)

    out: list[tuple[str, Path, Path]] = []
    stdlib = _stdlib_dir(py_interp)
    if stdlib is None:
        print(
            f"warning: could not resolve stdlib for {py_interp}; "
            "stdlib subset will be skipped",
            file=sys.stderr,
        )
    else:
        modules = (
            STDLIB_MODULES[: len(STDLIB_MODULES) // 2] if quick else STDLIB_MODULES
        )
        for name in modules:
            src = stdlib / f"{name}.py"
            if not src.is_file():
                continue
            dst_src = src_root / f"stdlib_{name}.py"
            shutil.copy(src, dst_src)
            pyc = pyc_root / f"stdlib_{name}.pyc"
            if _compile(py_interp, dst_src, pyc):
                out.append((f"stdlib:{name}", dst_src, pyc))

    pypi_subset = ["six", "packaging", "certifi", "idna", "charset_normalizer"]
    if quick:
        pypi_subset = pypi_subset[:2]
    for pkg in pypi_subset:
        candidates = _resolve_pypi_modules(pkg)
        for label, src in candidates:
            dst_src = src_root / f"pypi_{label}.py"
            try:
                shutil.copy(src, dst_src)
            except OSError:
                continue
            pyc = pyc_root / f"pypi_{label}.pyc"
            if _compile(py_interp, dst_src, pyc):
                out.append((f"pypi:{label}", dst_src, pyc))

    return out


def _resolve_pypi_modules(pkg: str) -> list[tuple[str, Path]]:
    """Find top-level ``.py`` files from a cached PyPI corpus under /tmp.

    Re-uses ``/tmp/pychd-corpora/pypi-top20/<pkg>/`` when present (the
    same cache ``tools/build_corpora.py`` populates). Returns up to
    three files per package to keep the comparison corpus tractable.
    """
    base = Path("/tmp/pychd-corpora/pypi-top20") / pkg
    if not base.is_dir():
        return []
    py_files = sorted(base.glob("*.py"))[:3]
    # Skip dunder-init "stub" __init__.py that just re-exports.
    py_files = [p for p in py_files if p.stat().st_size > 200]
    out: list[tuple[str, Path]] = []
    for p in py_files:
        out.append((f"{pkg}_{p.stem}", p))
    return out


def _compile(py_interp: str, src: Path, pyc: Path) -> bool:
    """Compile *src* into *pyc* with *py_interp*; True on success."""
    snippet = textwrap.dedent(
        f"""\
        import py_compile, sys
        try:
            py_compile.compile({str(src)!r}, cfile={str(pyc)!r}, doraise=True)
        except Exception as e:
            sys.stderr.write(repr(e))
            sys.exit(1)
        """
    )
    try:
        proc = subprocess.run(
            [py_interp, "-c", snippet],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except FileNotFoundError, subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# Per-tool scoring
# ---------------------------------------------------------------------------


def _score(original_src: str, recovered_src: str) -> tuple[bool, bool, bool, bool]:
    """Return ``(parses, signature, declaration, strict)`` booleans."""
    try:
        original = ast.parse(original_src)
    except SyntaxError:
        return False, False, False, False
    try:
        rec = ast.parse(recovered_src)
        parses = True
    except SyntaxError:
        return False, False, False, False
    return (
        parses,
        _signature_match(original, rec),
        _declaration_match(original, rec),
        _skeleton_match(original, rec),
    )


def _run_pychd(corpus: list[tuple[str, Path, Path]]) -> ToolResult:
    """Run pychd's rules-only pipeline (cross-version pass for 3.8)."""
    from pychd.decompile import Mode, decompile_pyc

    res = ToolResult(per_module=[])
    for name, src, pyc in corpus:
        res.modules += 1
        original = src.read_text()
        try:
            report = decompile_pyc(pyc, mode=Mode.RULES_ONLY)
            recovered = report.source
        except Exception as e:
            res.skipped += 1
            res.per_module.append({"name": name, "ok": False, "error": str(e)})
            continue
        parses, sig, decl, strict = _score(original, recovered)
        if parses:
            res.parses += 1
        if sig:
            res.signature_match += 1
        if decl:
            res.declaration_match += 1
        if strict:
            res.strict_match += 1
        res.per_module.append(
            {
                "name": name,
                "ok": parses,
                "sig": sig,
                "decl": decl,
                "strict": strict,
            }
        )
    return res


def _run_external(
    tool_cmd: list[str],
    corpus: list[tuple[str, Path, Path]],
) -> ToolResult:
    """Invoke an external decompiler via ``-o <tempdir>`` and score it.

    External tools write the recovered ``.py`` next to (or under) the
    output directory. We discover the produced file by extension and
    feed its contents to :func:`_score`. Any non-zero exit code, an
    empty output directory, or a timeout records a per-module skip
    rather than a 0-everything entry.
    """
    res = ToolResult(per_module=[])
    for name, src, pyc in corpus:
        res.modules += 1
        original = src.read_text()
        recovered: str | None = None
        try:
            with tempfile.TemporaryDirectory() as out_dir:
                proc = subprocess.run(
                    [*tool_cmd, "-o", out_dir, str(pyc)],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=60,
                )
                if proc.returncode != 0:
                    res.skipped += 1
                    res.per_module.append(
                        {"name": name, "ok": False, "error": "non-zero exit"}
                    )
                    continue
                py_outs = [
                    Path(out_dir) / f for f in os.listdir(out_dir) if f.endswith(".py")
                ]
                if not py_outs:
                    res.skipped += 1
                    res.per_module.append(
                        {"name": name, "ok": False, "error": "no .py output"}
                    )
                    continue
                recovered = py_outs[0].read_text()
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            res.skipped += 1
            res.per_module.append({"name": name, "ok": False, "error": str(e)})
            continue
        parses, sig, decl, strict = _score(original, recovered)
        if parses:
            res.parses += 1
        if sig:
            res.signature_match += 1
        if decl:
            res.declaration_match += 1
        if strict:
            res.strict_match += 1
        res.per_module.append(
            {
                "name": name,
                "ok": parses,
                "sig": sig,
                "decl": decl,
                "strict": strict,
            }
        )
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "assets" / "_comparison.json",
    )
    ap.add_argument("--python-version", default=COMPARISON_PYTHON)
    ap.add_argument(
        "--quick",
        action="store_true",
        help=(
            "Halve the corpus size — useful for smoke runs while the"
            " harness itself is being modified."
        ),
    )
    args = ap.parse_args(argv)

    py_interp = _find_python(args.python_version)
    if py_interp is None:
        print(
            f"warning: Python {args.python_version} not installed; "
            f"skipping comparison (run `uv python install {args.python_version}`).",
            file=sys.stderr,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({}, indent=2))
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        corpus = _gather_corpus(py_interp, Path(tmp), quick=args.quick)
        if not corpus:
            print(
                "warning: corpus assembly produced no files; nothing to do.",
                file=sys.stderr,
            )
            return 1
        print(
            f"comparing {len(corpus)} modules compiled with "
            f"{py_interp} (Python {args.python_version})..."
        )

        out: dict[str, dict] = {}
        out["pychd (rules-only)"] = asdict(_run_pychd(corpus))
        if shutil.which("uncompyle6"):
            out["uncompyle6"] = asdict(_run_external(["uncompyle6"], corpus))
        if shutil.which("decompyle3"):
            out["decompyle3"] = asdict(_run_external(["decompyle3"], corpus))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(out, indent=2))
    print(f"\nwrote {args.output.relative_to(REPO_ROOT)}\n")
    for tool, data in out.items():
        n = data["modules"]
        print(
            f"  {tool:<22} "
            f"sig={data['signature_match']}/{n} "
            f"({100 * data['signature_match'] / max(1, n):4.1f}%)  "
            f"decl={data['declaration_match']}/{n} "
            f"({100 * data['declaration_match'] / max(1, n):4.1f}%)  "
            f"strict={data['strict_match']}/{n}  "
            f"skipped={data['skipped']}"
        )

    print()
    print("Tool version-range coverage:")
    print("  pychd       — every CPython 3.x (this corpus: 3.8)")
    print("  uncompyle6  — 2.4 – 3.8 (no 3.9+)")
    print("  decompyle3  — 3.7 / 3.8 only")
    print(f"  Comparison corpus compiled with: {py_interp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
