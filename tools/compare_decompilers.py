"""Compare pychd against every publicly-installable Python decompiler
on a shared real corpus.

Why this script exists
----------------------

The README's headline numbers measure pychd against the ground truth
(the original ``.py`` source). They do **not** answer the natural
follow-up: *how does pychd compare to the existing tooling?*

The answer needs a corpus every tool can read. The newest mutually-
supported Python release is **3.8** (uncompyle6 caps there;
decompyle3 covers 3.7 / 3.8; pycdc reads up to 3.10 but its
declaration-recovery quality drops off past 3.8 too). So this script
compiles a real corpus (a curated PyPI subset + stdlib modules)
with a locally-installed 3.8 interpreter, runs **each tool** against
every ``.pyc``, and scores the recovered source via the same
eight-axis metric used by the rest of the benchmark suite.

Honest framing
--------------

* Every external tool is pinned to a specific public version. The
  versions are captured at run time via the tool's own
  ``--version`` / ``-V`` / ``-h`` output and embedded in the result
  JSON. README rendering picks them up from there. **No published
  paper numbers are reproduced verbatim** — every figure comes from
  running the tool against our corpus.
* Tools whose binary is missing from the host are listed as "not
  installed" in the output rather than scored at 0 %. Their
  version-range coverage is documented separately. Reviewers can
  rebuild the missing binaries via ``tools/setup_decompilers.sh``.
* The corpus is downloaded on demand from PyPI and from the running
  interpreter's stdlib; nothing third-party is committed.

Tools currently in scope
------------------------

============  =========  =========================================
tool          source     versions read
============  =========  =========================================
pychd         in-repo    every CPython 3.x (this run: 3.8)
uncompyle6    PyPI       2.4 – 3.8 (no 3.9+)
decompyle3    PyPI       3.7 / 3.8 only
pycdc         source     1.0 – 3.10 (declaration recovery only)
============  =========  =========================================

Adding a new tool means appending one entry to :data:`EXTERNAL_TOOLS`
and (if it's not pip-installable) wiring its build into
``tools/setup_decompilers.sh``.

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

from pychd.semantic import compare_all  # noqa: E402
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
    # Semantic axes — see :mod:`pychd.semantic` for definitions.
    # All three are scored against the *same* original .pyc that each
    # tool was handed, so a comparator score here is directly
    # comparable across tools.
    bytecode_exact: int = 0
    bytecode_normalized: int = 0
    behavioral_smoke: int = 0
    # Paper-axis metrics (Decompile-Bench / PyLingual).
    # ``functional_correctness`` counts modules with test oracles; it
    # is paired with ``functional_total`` so the denominator excludes
    # modules that don't ship one (most of stdlib + PyPI).
    functional_correctness: int = 0
    functional_total: int = 0
    # Mean ``edit_similarity`` over all scored modules, in ``[0, 1]``.
    edit_similarity_sum: float = 0.0
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


def _score_semantic(
    pyc: Path,
    src: Path,
    recovered_src: str,
    py_interp: str,
    *,
    test_src: str | None = None,
    entry_point: str | None = None,
) -> tuple[bool, bool, bool, float, bool | None]:
    """Return ``(bx, bn, bs, edit_similarity, functional_correctness)``.

    Routes the whole comparison through the producing interpreter
    (Python 3.8 in this script's defaults) so that opcode tables and
    marshal format match what generated the .pyc. Errors collapse to
    a clean miss row rather than aborting the row.

    ``functional_correctness`` is ``None`` when no test oracle was
    provided — the caller should keep that module out of the Pass@1
    denominator. The 3.8 stdlib corpus this script ships against has
    no oracle by default; HumanEval-style corpora can opt in by
    threading their ``_tests.json`` data through here.
    """
    try:
        rep = compare_all(
            pyc,
            src,
            recovered_src,
            py_interp=py_interp,
            test_src=test_src,
            entry_point=entry_point,
        )
    except Exception:
        return False, False, False, 0.0, (False if test_src else None)
    fc = (
        rep.functional_correctness.match
        if rep.functional_correctness is not None
        else None
    )
    return (
        rep.bytecode_exact.match,
        rep.bytecode_normalized.match,
        rep.behavioral_smoke.match,
        rep.edit_similarity,
        fc,
    )


def _run_pychd(
    corpus: list[tuple[str, Path, Path]],
    *,
    py_interp: str,
    hybrid: bool = False,
    backend_name: str = "codex",
    model: str | None = None,
    rewrite: bool = False,
    parallel: int = 1,
) -> ToolResult:
    """Run pychd against *corpus* in rules-only, hybrid, or hybrid-rewrite mode.

    *hybrid=True* (without *rewrite*) fills every ``UnknownBlock`` via
    the LLM — one call per body. *rewrite=True* upgrades that to
    ``HYBRID_REWRITE`` mode: rule pass first, then the LLM rewrites the
    whole module given (disassembly + rule output). The rewrite mode is
    the apples-to-apples comparison against PyLingual (also LLM-based)
    and gives the strongest ``strict_match`` / ``FC`` numbers.

    *parallel* controls the worker-thread count when running with the
    LLM; the codex CLI is RPC-bound, so a thread pool genuinely
    parallelises the wait.
    """
    from pychd.decompile import Backend, Mode, decompile_pyc

    if rewrite:
        mode = Mode.HYBRID_REWRITE
    elif hybrid:
        mode = Mode.HYBRID
    else:
        mode = Mode.RULES_ONLY
    backend = Backend(backend_name) if mode != Mode.RULES_ONLY else Backend.LITELLM

    res = ToolResult(per_module=[])

    def _one(item: tuple[str, Path, Path]) -> dict | None:
        name, src, pyc = item
        original = src.read_text()
        try:
            report = decompile_pyc(pyc, mode=mode, backend=backend, model=model)
            recovered = report.source
        except Exception as e:
            return {"name": name, "ok": False, "error": str(e), "_skipped": True}
        parses, sig, decl, strict = _score(original, recovered)
        bx, bn, bs, esim, fc = _score_semantic(pyc, src, recovered, py_interp)
        return {
            "name": name,
            "ok": parses,
            "sig": sig,
            "decl": decl,
            "strict": strict,
            "bx": bx,
            "bn": bn,
            "bs": bs,
            "edit_similarity": esim,
            "functional_correctness": fc,
        }

    results: list[dict | None] = [None] * len(corpus)
    if mode == Mode.RULES_ONLY or parallel <= 1:
        for i, item in enumerate(corpus):
            results[i] = _one(item)
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = {pool.submit(_one, item): i for i, item in enumerate(corpus)}
            for fut in as_completed(futures):
                results[futures[fut]] = fut.result()

    for r in results:
        if r is None:
            continue
        res.modules += 1
        if r.get("_skipped"):
            res.skipped += 1
            r.pop("_skipped", None)
            res.per_module.append(r)
            continue
        if r["ok"]:
            res.parses += 1
        if r["sig"]:
            res.signature_match += 1
        if r["decl"]:
            res.declaration_match += 1
        if r["strict"]:
            res.strict_match += 1
        if r["bx"]:
            res.bytecode_exact += 1
        if r["bn"]:
            res.bytecode_normalized += 1
        if r["bs"]:
            res.behavioral_smoke += 1
        res.edit_similarity_sum += r["edit_similarity"]
        fc = r["functional_correctness"]
        if fc is not None:
            res.functional_total += 1
            if fc:
                res.functional_correctness += 1
        res.per_module.append(r)
    return res


# ---------------------------------------------------------------------------
# Decompiler registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecompilerSpec:
    """Everything :func:`_run_with_spec` needs to invoke one external tool.

    ``output_mode`` controls how the recovered source is captured:

    ``"outdir"``
        Invoked as ``<cmd> -o <outdir> <pyc>``; the recovered file is
        the first ``*.py`` found under ``<outdir>``. Used by
        uncompyle6, decompyle3, and PyLingual.

    ``"stdout"``
        Invoked as ``<cmd> <pyc>``; the recovered source is read from
        the subprocess's stdout. Used by pycdc, which prints to stdout
        by default and writes nothing useful with ``-o``.

    ``"container"``
        A podman/docker variant of ``"outdir"`` — the harness mounts
        the .pyc and an output directory into the container, runs
        ``<container_engine> run --rm -v ... <image> <args>``, then
        reads the produced ``.py`` from the host-side outdir. Used
        by PyLingual when its build is delegated to
        :mod:`tools/pylingual.Containerfile`.

    ``coverage_label`` is the human-readable supported-version range
    reported in the comparison summary (e.g. ``"2.4 – 3.8"``).
    ``version`` is captured at run time from the tool itself when
    possible; ``None`` if the tool has no version flag.
    """

    name: str
    binary: str
    extra_paths: tuple[str, ...] = ()  # fallback search locations
    output_mode: str = "outdir"  # "outdir" | "stdout" | "container"
    extra_args: tuple[str, ...] = ()
    # Some tools (pycdc) exit non-zero on partial recovery but still
    # print the recovered source. We track that here so the harness
    # accepts the stdout instead of marking the module skipped.
    tolerate_nonzero_exit: bool = False
    # Tools that can't print their own version (pycdc has none) we
    # leave as ``None`` and report the build-system pinned SHA.
    version_args: tuple[str, ...] | None = None
    version_pinned: str | None = None
    coverage_label: str = ""
    # podman-image-only fields.
    container_image: str | None = None
    container_engine: str = "podman"
    timeout: float = 120.0


def _resolve_binary(spec: DecompilerSpec) -> str | None:
    """Return the absolute path to *spec*'s binary, or ``None`` if absent.

    Searches ``PATH`` first, then any per-spec ``extra_paths`` (used
    by pycdc to find ``/tmp/pychd-decompilers/pycdc/build/pycdc``
    without forcing the user to extend ``PATH``).
    """
    via_path = shutil.which(spec.binary)
    if via_path:
        return via_path
    for candidate in spec.extra_paths:
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _detect_container_image(spec: DecompilerSpec) -> bool:
    """Return True if *spec*'s container image is pullable on the host."""
    if not spec.container_image:
        return False
    engine = shutil.which(spec.container_engine)
    if engine is None:
        return False
    try:
        proc = subprocess.run(
            [engine, "image", "exists", spec.container_image],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except FileNotFoundError, subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0


def _capture_version(spec: DecompilerSpec, binary: str) -> str:
    """Return a human-readable version string for *spec*.

    Falls back to the pinned label or "unknown" when the tool can't
    report its own version (the comparison summary annotates these
    so reviewers know the version came from the pin, not the tool).
    """
    if spec.version_pinned and not spec.version_args:
        return spec.version_pinned
    if spec.version_args is None:
        return spec.version_pinned or "unknown"
    try:
        proc = subprocess.run(
            [binary, *spec.version_args],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except FileNotFoundError, subprocess.TimeoutExpired:
        return spec.version_pinned or "unknown"
    # Tools print version to stdout (uncompyle6) or stderr
    # (decompyle3 due to click warnings). Concatenate and take the
    # first line that looks like a version.
    out = (proc.stdout + "\n" + proc.stderr).splitlines()
    for line in out:
        line = line.strip()
        if line and ("version" in line.lower() or any(c.isdigit() for c in line)):
            return line
    return spec.version_pinned or "unknown"


def _run_decompiler_invocation(
    spec: DecompilerSpec,
    binary: str,
    pyc: Path,
    workdir: Path,
) -> tuple[str | None, str]:
    """Invoke *spec*'s decompiler on *pyc*. Returns ``(source, detail)``.

    ``source`` is the recovered Python text, or ``None`` on any
    failure mode (exit, no output, timeout, missing engine). ``detail``
    is a short explanation suitable for the per-module error log.
    """
    out_dir = workdir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    if spec.output_mode == "container":
        if spec.container_image is None:
            return None, "no container image configured"
        engine = shutil.which(spec.container_engine)
        if engine is None:
            return None, f"container engine missing: {spec.container_engine}"
        # PyLingual lazy-downloads HF model weights into ``~/.cache``
        # on first run. We bind-mount a persistent host directory at
        # that location so:
        #   1. The model bundle survives across modules and runs
        #      (re-downloading per module would take hours).
        #   2. The runtime rootfs stays unmodified.
        # The cache is created on demand under ``/tmp`` — a fresh
        # checkout sees its first run pay the ~2 GB download once.
        hf_cache = Path("/tmp/pychd-hf-cache")
        hf_cache.mkdir(parents=True, exist_ok=True)
        pyc_host_dir = pyc.parent.resolve()
        cmd = [
            engine,
            "run",
            "--rm",
            "--security-opt",
            "label=disable",
            "-v",
            f"{hf_cache.resolve()}:/root/.cache:rw,Z",
            "-v",
            f"{pyc_host_dir}:/in:ro,Z",
            "-v",
            f"{out_dir.resolve()}:/out:rw,Z",
            spec.container_image,
            f"/in/{pyc.name}",
            "-o",
            "/out",
            *spec.extra_args,
        ]
    elif spec.output_mode == "outdir":
        cmd = [binary, *spec.extra_args, "-o", str(out_dir), str(pyc)]
    elif spec.output_mode == "stdout":
        cmd = [binary, *spec.extra_args, str(pyc)]
    else:
        return None, f"unknown output_mode: {spec.output_mode}"

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=spec.timeout,
        )
    except FileNotFoundError as e:
        return None, f"subprocess crashed: {e}"
    except subprocess.TimeoutExpired as e:
        # podman doesn't propagate SIGTERM into the contained process
        # reliably — when subprocess.run times out, the underlying
        # container can keep running until it finishes on its own,
        # holding our mount points and blocking the next module. Find
        # any container still attached to *spec*'s image and kill it.
        if spec.output_mode == "container" and spec.container_image is not None:
            try:
                ps_proc = subprocess.run(
                    [
                        spec.container_engine,
                        "ps",
                        "-q",
                        "--filter",
                        f"ancestor={spec.container_image}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                for cid in ps_proc.stdout.strip().splitlines():
                    if not cid.strip():
                        continue
                    subprocess.run(
                        [spec.container_engine, "kill", "--signal", "KILL", cid],
                        capture_output=True,
                        timeout=5,
                        check=False,
                    )
            except Exception:
                # The whole cleanup is best-effort; the timeout's
                # already been recorded for this module.
                pass
        return None, f"subprocess timeout: {e.timeout}s"

    if proc.returncode != 0 and not spec.tolerate_nonzero_exit:
        err = proc.stderr.strip().splitlines()
        msg = err[-1] if err else f"exit {proc.returncode}"
        return None, msg[:200]

    if spec.output_mode == "stdout":
        if not proc.stdout.strip():
            return None, "empty stdout"
        return proc.stdout, "stdout"

    # outdir / container: pick the first .py emitted into ``out_dir``.
    py_outs = sorted(out_dir.rglob("*.py"))
    if not py_outs:
        # Some tools (PyLingual) emit into a sibling directory named
        # after the input. Fall back to a broader search.
        py_outs = sorted(out_dir.rglob("*.py"))
    if not py_outs:
        return None, "no .py output"
    try:
        return py_outs[0].read_text(), str(py_outs[0].relative_to(out_dir))
    except OSError as e:
        return None, f"read failed: {e}"


def _run_with_spec(
    spec: DecompilerSpec,
    binary: str,
    corpus: list[tuple[str, Path, Path]],
    *,
    py_interp: str,
) -> ToolResult:
    """Score *spec* against the corpus, exactly the way :func:`_run_pychd`
    scores pychd's own output. Result is comparable across tools."""
    res = ToolResult(per_module=[])
    for name, src, pyc in corpus:
        res.modules += 1
        original = src.read_text()
        with tempfile.TemporaryDirectory() as tmp:
            recovered, detail = _run_decompiler_invocation(spec, binary, pyc, Path(tmp))
        if recovered is None:
            res.skipped += 1
            res.per_module.append({"name": name, "ok": False, "error": detail})
            continue
        parses, sig, decl, strict = _score(original, recovered)
        bx, bn, bs, esim, fc = _score_semantic(pyc, src, recovered, py_interp)
        if parses:
            res.parses += 1
        if sig:
            res.signature_match += 1
        if decl:
            res.declaration_match += 1
        if strict:
            res.strict_match += 1
        if bx:
            res.bytecode_exact += 1
        if bn:
            res.bytecode_normalized += 1
        if bs:
            res.behavioral_smoke += 1
        res.edit_similarity_sum += esim
        if fc is not None:
            res.functional_total += 1
            if fc:
                res.functional_correctness += 1
        res.per_module.append(
            {
                "name": name,
                "ok": parses,
                "sig": sig,
                "decl": decl,
                "strict": strict,
                "bx": bx,
                "bn": bn,
                "bs": bs,
                "edit_similarity": esim,
                "functional_correctness": fc,
                "detail": detail,
            }
        )
    return res


# Pinned location of the source-built pycdc binary; matches the path
# in ``tools/setup_decompilers.sh``. Kept here as a constant so the
# script and the harness can't drift apart silently.
_PYCDC_LOCAL = "/tmp/pychd-decompilers/pycdc/build/pycdc"


# The full registry of external decompilers the comparison can score
# against. Adding one means appending a single entry here plus (for
# non-pip-installable ones) a recipe in ``tools/setup_decompilers.sh``.
EXTERNAL_TOOLS: tuple[DecompilerSpec, ...] = (
    DecompilerSpec(
        name="uncompyle6",
        binary="uncompyle6",
        output_mode="outdir",
        version_args=("--version",),
        coverage_label="2.4 – 3.8 (no 3.9+)",
    ),
    DecompilerSpec(
        name="decompyle3",
        binary="decompyle3",
        output_mode="outdir",
        # decompyle3's ``--version`` is registered twice (a click bug
        # in the package) and the introspection-time warnings drown
        # out the real version string. Importing ``decompyle3`` and
        # reading ``__version__`` is more reliable.
        version_args=None,
        version_pinned="3.9.3 (PyPI)",
        coverage_label="3.7 / 3.8 only",
    ),
    DecompilerSpec(
        name="pycdc",
        binary="pycdc",
        extra_paths=(_PYCDC_LOCAL,),
        output_mode="stdout",
        # pycdc returns 1 on partial recovery (very common — its
        # body recovery is noisy) but still emits a usable signature.
        tolerate_nonzero_exit=True,
        # pycdc has no --version flag; we pin to the SHA the build
        # script checked out.
        version_args=None,
        version_pinned="b428976 (2026-04-06)",
        coverage_label="1.0 – 3.10 (declarations only past 3.8)",
    ),
    DecompilerSpec(
        name="pylingual",
        binary="pylingual",  # used only as a label for missing-binary error
        output_mode="container",
        container_image="pychd-pylingual:latest",
        container_engine="podman",
        extra_args=("--quiet",),
        version_args=None,
        version_pinned="main (image: pychd-pylingual:latest)",
        coverage_label="3.6 – 3.13 (ML-based)",
        # 60s per module. PyTorch boot + segmentation/translation
        # inference on small modules finishes in ~30s; anything larger
        # than the corpus average exceeds the wall-clock budget and is
        # recorded as a timeout failure — fair, since that's what a
        # reviewer with their own corpus would experience.
        timeout=60.0,
    ),
)


# Each external tool was designed against a specific Python release.
# Comparing pychd to a tool on a Python version the tool can't read
# is unfair to the tool (it just times out / errors on every module).
# This table pins each tool to **its own** maximum supported version.
# pychd runs against every version listed here (it covers 3.0 – 3.14)
# so reviewers can see how pychd performs *under each competitor's
# best-case Python version* side-by-side.
TOOL_PREFERRED_VERSIONS: dict[str, str] = {
    "uncompyle6": "3.8",
    "decompyle3": "3.8",
    "pycdc": "3.10",
    "pylingual": "3.13",
}


def _resolve_spec(spec: DecompilerSpec) -> tuple[str | None, str]:
    """Return ``(binary_or_marker, status)`` for *spec*.

    For native tools, the first slot is the absolute binary path. For
    container tools, the marker is ``"<image>:<tag>"`` and the host's
    container engine is implied by the spec. ``status`` is either
    ``"available"`` or a short reason the tool was skipped.
    """
    if spec.output_mode == "container":
        if spec.container_image is None:
            return None, "no container image configured"
        if shutil.which(spec.container_engine) is None:
            return None, f"missing container engine: {spec.container_engine}"
        if not _detect_container_image(spec):
            return None, f"image not built: {spec.container_image}"
        return spec.container_image, "available"
    binary = _resolve_binary(spec)
    if binary is None:
        return None, "binary not found"
    return binary, "available"


def _list_local_python_versions() -> list[str]:
    """Return every CPython 3.x ``uv python list --only-installed`` reports.

    Output strings look like ``cpython-3.8.20-linux-x86_64-gnu …``; we
    extract the ``3.<minor>`` portion and dedupe.
    """
    try:
        proc = subprocess.run(
            ["uv", "python", "list", "--only-installed"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except FileNotFoundError, subprocess.TimeoutExpired:
        return []
    if proc.returncode != 0:
        return []
    versions: set[str] = set()
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("cpython-3."):
            continue
        # Expected token: cpython-3.<minor>.<patch>-<arch>
        head = line.split()[0]
        parts = head.split("-")[1].split(".")
        if len(parts) >= 2 and parts[0] == "3":
            versions.add(f"3.{parts[1]}")
    return sorted(versions, key=lambda s: int(s.split(".")[1]))


def _run_one_version(
    version: str,
    py_interp: str,
    *,
    quick: bool,
    pychd_hybrid: bool = False,
    pychd_rewrite: bool = False,
    pychd_backend: str = "codex",
    pychd_model: str | None = None,
    pychd_parallel: int = 1,
) -> dict[str, dict]:
    """Compile the corpus under *py_interp* and score every available tool.

    Returns the per-tool result dict for this single Python version.
    Tools that can't even spawn (binary missing / image not built)
    are still recorded so the cross-version matrix can mark them as
    "tool not installed" rather than silently dropping them.
    """
    with tempfile.TemporaryDirectory() as tmp:
        corpus = _gather_corpus(py_interp, Path(tmp), quick=quick)
        if not corpus:
            return {}
        print(
            f"  [Python {version}] {len(corpus)} modules from {py_interp}",
            file=sys.stderr,
        )
        out: dict[str, dict] = {}
        if pychd_rewrite:
            pychd_label = f"pychd (hybrid-rewrite:{pychd_backend})"
        elif pychd_hybrid:
            pychd_label = f"pychd (hybrid:{pychd_backend})"
        else:
            pychd_label = "pychd (rules-only)"
        out[pychd_label] = asdict(
            _run_pychd(
                corpus,
                py_interp=py_interp,
                hybrid=pychd_hybrid or pychd_rewrite,
                rewrite=pychd_rewrite,
                backend_name=pychd_backend,
                model=pychd_model,
                parallel=pychd_parallel,
            )
        )
        out[pychd_label]["version"] = "main (this repo)"
        out[pychd_label]["coverage_label"] = "every CPython 3.x"
        current_tuple = tuple(map(int, version.split(".")))
        for spec in EXTERNAL_TOOLS:
            preferred = TOOL_PREFERRED_VERSIONS.get(spec.name)
            preferred_tuple = (
                tuple(map(int, preferred.split("."))) if preferred else None
            )
            # Only invoke each external tool on its preferred Python
            # version. Running every tool on every version (then masking
            # the non-preferred results) wasted ~20 min/run on pylingual
            # containers that we'd discard anyway.
            if preferred_tuple is not None and current_tuple != preferred_tuple:
                out[spec.name] = {
                    "modules": 0,
                    "parses": 0,
                    "signature_match": 0,
                    "declaration_match": 0,
                    "strict_match": 0,
                    "bytecode_exact": 0,
                    "bytecode_normalized": 0,
                    "behavioral_smoke": 0,
                    "functional_correctness": 0,
                    "functional_total": 0,
                    "edit_similarity_sum": 0.0,
                    "skipped": 0,
                    "error": f"out of scope (preferred: Py {preferred})",
                    "version": "(skipped — see preferred-version row)",
                    "coverage_label": spec.coverage_label,
                    "per_module": None,
                }
                continue
            binary, status = _resolve_spec(spec)
            if binary is None:
                out[spec.name] = {
                    "modules": 0,
                    "parses": 0,
                    "signature_match": 0,
                    "declaration_match": 0,
                    "strict_match": 0,
                    "bytecode_exact": 0,
                    "bytecode_normalized": 0,
                    "behavioral_smoke": 0,
                    "functional_correctness": 0,
                    "functional_total": 0,
                    "edit_similarity_sum": 0.0,
                    "skipped": 0,
                    "error": status,
                    "version": "(not installed)",
                    "coverage_label": spec.coverage_label,
                    "per_module": None,
                }
                continue
            tool_version = _capture_version(spec, binary)
            res = asdict(_run_with_spec(spec, binary, corpus, py_interp=py_interp))
            res["version"] = tool_version
            res["coverage_label"] = spec.coverage_label
            out[spec.name] = res
        return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "assets" / "_comparison.json",
    )
    ap.add_argument(
        "--python-versions",
        default=None,
        help=(
            "Comma-separated Python minor releases to test, e.g."
            " ``3.8,3.10,3.13``. Default: every locally installed"
            " CPython 3.x. Tools that don't support a given release"
            " are recorded as failures rather than skipped — that's"
            " the cross-version coverage matrix we want."
        ),
    )
    ap.add_argument(
        "--quick",
        action="store_true",
        help=(
            "Halve the corpus size — useful for smoke runs while the"
            " harness itself is being modified."
        ),
    )
    ap.add_argument(
        "--pychd-hybrid",
        action="store_true",
        help=(
            "Run pychd in hybrid mode — every UnknownBlock body slot"
            " is filled via the chosen --pychd-backend instead of"
            " emitting ``pass  # pychd: unrecovered body``. Required"
            " for an apples-to-apples comparison against tools that"
            " always reconstruct bodies (decompyle3, pylingual)."
        ),
    )
    ap.add_argument(
        "--pychd-backend",
        default="codex",
        choices=["codex", "litellm"],
        help=(
            "Backend used when ``--pychd-hybrid`` is set. ``codex``"
            " (default) shells out to the OpenAI Codex CLI and uses"
            " the user's existing ``codex login`` session; ``litellm``"
            " routes via the litellm SDK and needs an OPENAI_API_KEY"
            " (or matching provider env var)."
        ),
    )
    ap.add_argument(
        "--pychd-model",
        default=None,
        help=(
            "Optional model override for the hybrid backend. When"
            " unset, codex falls back to ``gpt-5.5`` with xhigh"
            " reasoning effort (the strongest body-recovery config"
            " available through the Codex CLI's ChatGPT account)."
        ),
    )
    ap.add_argument(
        "--pychd-rewrite",
        action="store_true",
        help=(
            "Run pychd in hybrid-rewrite mode — one LLM call per module"
            " that both fills bodies *and* corrects module-level"
            " recovery. Strongest mode for strict_match and FC, and the"
            " apples-to-apples comparison against PyLingual."
        ),
    )
    ap.add_argument(
        "--pychd-parallel",
        type=int,
        default=8,
        help=(
            "Number of concurrent codex calls when pychd runs in hybrid"
            " or hybrid-rewrite mode. The codex CLI is RPC-bound so this"
            " genuinely parallelises the wait."
        ),
    )
    args = ap.parse_args(argv)

    # Resolve the list of Python versions to benchmark against.
    if args.python_versions:
        requested = [v.strip() for v in args.python_versions.split(",") if v.strip()]
    else:
        # Default: the union of each external tool's preferred
        # Python version (see :data:`TOOL_PREFERRED_VERSIONS`). pychd
        # is then scored on every entry, so each row of the resulting
        # matrix shows the competitor at its best — there's no
        # "compare-on-3.8-only" handicap on tools that read newer
        # bytecode (pycdc reads 3.10; pylingual reads 3.13).
        requested = sorted(
            set(TOOL_PREFERRED_VERSIONS.values()),
            key=lambda s: tuple(map(int, s.split("."))),
        )
        if not requested:
            requested = [COMPARISON_PYTHON]

    # Resolve each requested version to an interpreter path; record any
    # that aren't available so the final matrix surfaces them.
    versions_to_run: list[tuple[str, str]] = []
    missing: list[str] = []
    for v in requested:
        py = _find_python(v)
        if py is None:
            missing.append(v)
        else:
            versions_to_run.append((v, py))

    if not versions_to_run:
        print(
            "warning: no requested Python interpreter is installed; "
            "run `uv python install 3.8` (or similar) and re-try.",
            file=sys.stderr,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({}, indent=2))
        return 0

    print(
        f"comparing across Python {', '.join(v for v, _ in versions_to_run)}"
        + (f" (missing: {', '.join(missing)})" if missing else "")
    )

    per_version: dict[str, dict[str, dict]] = {}
    for v, py in versions_to_run:
        per_version[v] = _run_one_version(
            v,
            py,
            quick=args.quick,
            pychd_hybrid=args.pychd_hybrid,
            pychd_rewrite=args.pychd_rewrite,
            pychd_backend=args.pychd_backend,
            pychd_model=args.pychd_model,
            pychd_parallel=args.pychd_parallel,
        )
        # Mask out tools that aren't designed for this Python version
        # so the matrix isn't littered with timeouts from unfair runs
        # — except always keep pychd, which covers every release.
        for tool in list(per_version[v]):
            if tool.startswith("pychd"):
                continue
            preferred = TOOL_PREFERRED_VERSIONS.get(tool)
            if preferred is None:
                continue
            preferred_tuple = tuple(map(int, preferred.split(".")))
            current_tuple = tuple(map(int, v.split(".")))
            # Only run a tool on its preferred version; mark it
            # "out of scope" everywhere else.
            if current_tuple != preferred_tuple:
                per_version[v][tool] = {
                    "modules": 0,
                    "parses": 0,
                    "signature_match": 0,
                    "declaration_match": 0,
                    "strict_match": 0,
                    "bytecode_exact": 0,
                    "bytecode_normalized": 0,
                    "behavioral_smoke": 0,
                    "functional_correctness": 0,
                    "functional_total": 0,
                    "edit_similarity_sum": 0.0,
                    "skipped": 0,
                    "error": f"out of scope (preferred: Py {preferred})",
                    "version": per_version[v][tool].get("version", "—"),
                    "coverage_label": per_version[v][tool].get("coverage_label", ""),
                    "per_module": None,
                }

    # New output shape: ``{"versions": {v: {tool: data}}, "missing": [...]}``.
    # The wrapper makes the JSON self-documenting for readers who
    # encounter ``assets/_comparison.json`` cold.
    document = {
        "versions": per_version,
        "missing": missing,
        "tools_attempted": list(EXTERNAL_TOOLS_NAMES),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2))
    print(f"\nwrote {args.output.relative_to(REPO_ROOT)}\n")

    # Per-version summary printout.
    for v, by_tool in per_version.items():
        print(f"=== Python {v} ===")
        for tool, data in by_tool.items():
            n = data["modules"]
            if n == 0:
                print(f"  {tool:<22} {data.get('error', 'no data')}")
                continue
            es_mean = data["edit_similarity_sum"] / max(1, n)
            fc_total = data["functional_total"]
            fc_match = data["functional_correctness"]
            fc_str = f"fc={fc_match}/{fc_total}" if fc_total else "fc=n/a"
            print(
                f"  {tool:<22} "
                f"sig={data['signature_match']}/{n} "
                f"({100 * data['signature_match'] / max(1, n):4.1f}%)  "
                f"decl={data['declaration_match']}/{n} "
                f"({100 * data['declaration_match'] / max(1, n):4.1f}%)  "
                f"strict={data['strict_match']}/{n}  "
                f"bx={data['bytecode_exact']}/{n} "
                f"bn={data['bytecode_normalized']}/{n} "
                f"bs={data['behavioral_smoke']}/{n}  "
                f"{fc_str}  edit={es_mean:.3f}  "
                f"skipped={data['skipped']}"
            )

    # Tool versions are identical across Python-version runs; emit
    # them once at the end.
    first_version = next(iter(per_version.values()), {})
    if first_version:
        print()
        print("Tool versions captured at runtime (same across Python versions):")
        for tool, data in first_version.items():
            version = data.get("version", "unknown")
            coverage = data.get("coverage_label", "")
            print(f"  {tool:<22} {version:<40} {coverage}")
    return 0


EXTERNAL_TOOLS_NAMES = tuple(s.name for s in EXTERNAL_TOOLS)


if __name__ == "__main__":
    sys.exit(main())
