"""Build benchmark corpora for pychd's accuracy evaluation.

This script *prepares* (it does not measure) the source-code corpora
used by ``tools/benchmark.py``. Output lives under
``/tmp/pychd-corpora/<corpus>/`` and is intentionally **not** tracked
in git — each run regenerates whatever is missing.

Corpora
-------

The selection mirrors what's used in the published Python-decompilation
literature:

- **stdlib** — a curated slice of the running Python's standard
  library. The Python stdlib is the de-facto baseline for "did your
  decompiler survive the language at all?" tests.
- **stdlib-full** — *every* single-file ``.py`` module directly under
  the running Python's stdlib path (excluding ``test*``, ``__pycache__``,
  and packages). Provides ~100–150 modules of breadth coverage.
- **pypi** — six popular pure-Python PyPI packages — the curated set we
  publish numbers for (``requests``, ``click``, ``attrs``, ``flask``,
  ``httpx``, ``rich``).
- **pypi-top20** — twenty additional popular pure-Python PyPI packages
  (``certifi``, ``urllib3``, ``packaging``, ``six``, …) for a
  statistically-meaningful PyPI sample, mirroring PyFET / PyLingual
  methodology.
- **humaneval** — the 164 ``canonical_solution`` reference solutions
  from OpenAI's HumanEval dataset (the de-facto Python code-completion
  benchmark, also used by Decompile-Bench arXiv 2505.12668 for
  re-executability testing).
- **cursor-sdk** — our own original motivating target.

Usage
-----

::

    uv run python tools/build_corpora.py                  # build all
    uv run python tools/build_corpora.py --only stdlib    # just stdlib
    uv run python tools/build_corpora.py --root /custom   # alternate root

The script is idempotent: existing corpora are reused unless ``--force``
is passed.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import shutil
import sys
import sysconfig
import tarfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOT = Path("/tmp/pychd-corpora")


@dataclass
class CorpusSpec:
    name: str
    description: str


# ---------------------------------------------------------------------------
# stdlib
# ---------------------------------------------------------------------------


STDLIB_MODULES = [
    # Single-file modules — most common interview targets.
    "argparse",
    "contextlib",
    "dataclasses",
    "dis",
    "enum",
    "pprint",
    "shutil",
    "textwrap",
    "tokenize",
    "typing",
    # Packages — we copy the top-level __init__.py only to keep the
    # corpus focused. ``json``'s submodules are pulled in explicitly.
]

STDLIB_PACKAGE_FILES = {
    "json": ["__init__.py", "encoder.py", "decoder.py", "scanner.py"],
    "pathlib": ["__init__.py"],  # pathlib became a package in 3.13
}


def build_stdlib(dst: Path) -> int:
    """Copy a curated slice of stdlib .py files into *dst*."""
    stdlib_root = Path(sysconfig.get_paths()["stdlib"])
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for mod in STDLIB_MODULES:
        src = stdlib_root / f"{mod}.py"
        if src.exists():
            shutil.copy(src, dst / f"{mod}.py")
            n += 1
            continue
        # Maybe it's a package
        pkg_init = stdlib_root / mod / "__init__.py"
        if pkg_init.exists():
            (dst / mod).mkdir(parents=True, exist_ok=True)
            for f in (stdlib_root / mod).glob("*.py"):
                shutil.copy(f, dst / mod / f.name)
                n += 1
    for pkg, files in STDLIB_PACKAGE_FILES.items():
        pkg_dir = stdlib_root / pkg
        if not pkg_dir.exists():
            continue
        target = dst / pkg
        target.mkdir(parents=True, exist_ok=True)
        for f in files:
            src = pkg_dir / f
            if src.exists():
                shutil.copy(src, target / f)
                n += 1
    return n


def build_stdlib_full(dst: Path, *, force: bool) -> int:
    """Copy every single-file .py module directly under the stdlib root.

    Excludes ``test*``, ``__pycache__``, anything starting with ``__``
    (e.g. ``__phello__``), and packages (any directory). The result is a
    flat directory of ~100-150 modules suitable for breadth-coverage
    benchmarking. Files that fail to parse on the running interpreter
    (rare; usually OS-specific stubs) are skipped silently.
    """
    stdlib_root = Path(sysconfig.get_paths()["stdlib"])
    if dst.exists() and not force and any(dst.glob("*.py")):
        return sum(1 for _ in dst.glob("*.py"))
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)

    n = 0
    for src in sorted(stdlib_root.glob("*.py")):
        name = src.name
        if name.startswith("test"):
            continue
        if name.startswith("__") and name not in {"__future__.py"}:
            # Skip __phello__.py and similar test scaffolding, but keep
            # __future__.py which is a real module.
            continue
        try:
            shutil.copy(src, dst / name)
            n += 1
        except OSError as e:
            print(f"  stdlib-full: skip {name}: {e}", file=sys.stderr)
    return n


# ---------------------------------------------------------------------------
# PyPI sdist + wheel fetching
# ---------------------------------------------------------------------------


def _fetch_pypi_metadata(name: str, timeout: float = 30.0) -> dict:
    url = f"https://pypi.org/pypi/{name}/json"
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310
        return json.loads(r.read().decode("utf-8"))


def _download(url: str, timeout: float = 90.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310
        return r.read()


def _find_sdist_url(meta: dict) -> str | None:
    for entry in meta.get("urls", []):
        if entry.get("packagetype") == "sdist":
            return entry["url"]
    return None


def _find_pure_wheel_url(meta: dict) -> str | None:
    for entry in meta.get("urls", []):
        if entry.get("packagetype") != "bdist_wheel":
            continue
        fn = entry.get("filename", "")
        # Prefer pure-Python (`py3-none-any.whl`) wheels — they contain
        # exactly the .py files we care about, no compiled extensions.
        if "-py3-none-any.whl" in fn or "-py2.py3-none-any.whl" in fn:
            return entry["url"]
    return None


def _extract_archive_to(buf: bytes, target_root: Path) -> None:
    target_root.mkdir(parents=True, exist_ok=True)
    if buf[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(buf)) as zf:
            zf.extractall(target_root)
    else:
        with tarfile.open(fileobj=io.BytesIO(buf), mode="r:*") as tf:
            tf.extractall(target_root)


def _find_package_dirs(extracted_root: Path, package_hint: str) -> list[Path]:
    """Return likely top-level package directories inside an extracted dist."""
    hint = package_hint.lower().replace("-", "_")
    candidates: list[Path] = []
    for p in extracted_root.rglob("*"):
        if not p.is_dir():
            continue
        if "_vendor" in p.parts or "vendor" in p.parts:
            continue
        if p.name.lower() == hint:
            candidates.append(p)
    # If we found nothing, fall back to any directory containing .py files.
    if not candidates:
        for p in extracted_root.rglob("*.py"):
            parent = p.parent
            if parent not in candidates:
                candidates.append(parent)
    return candidates


PYPI_PACKAGES = [
    "requests",
    "click",
    "attrs",
    "flask",
    "httpx",
    "rich",
]


# Twenty additional popular pure-Python PyPI packages, deliberately
# *non-overlapping* with PYPI_PACKAGES so the two corpora can be
# measured independently. Selected by approximate download count from
# PyPI Stats; all expose ``py3-none-any.whl`` or ``py2.py3-none-any.whl``
# wheels at the time of writing.
PYPI_TOP20_PACKAGES = [
    "certifi",
    "charset-normalizer",
    "idna",
    "urllib3",
    "packaging",
    "six",
    "python-dateutil",
    "PyYAML",
    "jinja2",
    "MarkupSafe",
    "werkzeug",
    "itsdangerous",
    "blinker",
    "beautifulsoup4",
    "tqdm",
    "pytz",
    "soupsieve",
    "sniffio",
    "anyio",
    # ``rich`` from the original candidate list is already in the
    # ``pypi`` corpus, so we substitute ``pygments`` — another widely
    # used pure-Python package (~340 modules) that fills out the
    # statistical sample without overlap.
    "pygments",
]


def fetch_pypi_package(name: str, dst: Path, *, force: bool) -> int:
    """Download a single PyPI package's source files into *dst*."""
    target = dst / name
    if target.exists() and not force and any(target.rglob("*.py")):
        return sum(1 for _ in target.rglob("*.py"))
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    try:
        meta = _fetch_pypi_metadata(name)
    except Exception as e:
        print(f"  {name}: metadata fetch failed: {e}", file=sys.stderr)
        return 0

    # Prefer pure-Python wheels (smaller, no test/benchmark folders).
    url = _find_pure_wheel_url(meta) or _find_sdist_url(meta)
    if not url:
        print(f"  {name}: no suitable distribution on PyPI", file=sys.stderr)
        return 0

    try:
        buf = _download(url)
    except Exception as e:
        print(f"  {name}: download failed: {e}", file=sys.stderr)
        return 0
    _extract_archive_to(buf, target)

    pkg_dirs = _find_package_dirs(target, name)
    if not pkg_dirs:
        print(f"  {name}: no python files found after extraction", file=sys.stderr)
        return 0

    # Move the canonical package directory up so the corpus is flat:
    # /tmp/pychd-corpora/pypi/<name>/<files...>
    main = pkg_dirs[0]
    if main != target:
        tmp_move = target.with_name(target.name + ".staged")
        if tmp_move.exists():
            shutil.rmtree(tmp_move)
        shutil.move(str(main), str(tmp_move))
        shutil.rmtree(target)
        shutil.move(str(tmp_move), str(target))
    return sum(1 for _ in target.rglob("*.py"))


def build_pypi(dst: Path, *, force: bool) -> int:
    dst.mkdir(parents=True, exist_ok=True)
    total = 0
    for pkg in PYPI_PACKAGES:
        n = fetch_pypi_package(pkg, dst, force=force)
        print(f"  {pkg}: {n} .py files")
        total += n
    return total


def build_pypi_top20(dst: Path, *, force: bool) -> int:
    """Download 20 additional popular pure-Python PyPI packages."""
    dst.mkdir(parents=True, exist_ok=True)
    total = 0
    for pkg in PYPI_TOP20_PACKAGES:
        n = fetch_pypi_package(pkg, dst, force=force)
        print(f"  {pkg}: {n} .py files")
        total += n
    return total


# ---------------------------------------------------------------------------
# HumanEval (OpenAI code-completion benchmark)
# ---------------------------------------------------------------------------


HUMANEVAL_URL = (
    "https://raw.githubusercontent.com/openai/human-eval/master/data/HumanEval.jsonl.gz"
)


def build_humaneval(dst: Path, *, force: bool) -> int:
    """Download the HumanEval dataset and emit each canonical solution.

    HumanEval is a JSONL file (gzipped) of 164 hand-written Python
    programming problems. We extract the ``canonical_solution`` (the
    reference implementation) for each ``task_id`` and write it as
    ``<task_id>.py``. Used by Decompile-Bench (arXiv 2505.12668) as the
    re-executability oracle.

    The canonical solution is just the body of a function; we prefix it
    with the ``prompt`` (the signature + docstring) so the resulting
    file is standalone and importable.

    Two artefacts come out of this builder:

    * ``<task_id>.py`` — the importable canonical solution. Compiled
      to ``.pyc`` by the benchmark and handed to pychd for recovery.
    * ``_tests.json`` — a sidecar mapping ``{file.py: {test, entry_point}}``
      consumed by :func:`pychd.semantic.functional_correctness` to run
      the Pass@1 oracle on the *recovered* file. We keep the test out
      of the importable .py so pychd's recovery target stays clean —
      otherwise the benchmark would be scoring how well pychd recovers
      the *test* function, which isn't what we want to measure.
    """
    if dst.exists() and not force and any(dst.glob("*.py")):
        # If the .py files exist but _tests.json doesn't, we still
        # want to populate the sidecar — older corpus builds predate
        # the Pass@1 metric and ship without test data.
        sidecar = dst / "_tests.json"
        if sidecar.exists():
            return sum(1 for _ in dst.glob("*.py"))
        # Fall through to rebuild; the .py files will be overwritten
        # with identical content, plus the sidecar will be created.
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)

    try:
        buf = _download(HUMANEVAL_URL)
    except Exception as e:
        print(f"  humaneval: download failed: {e}", file=sys.stderr)
        return 0

    try:
        text = gzip.decompress(buf).decode("utf-8")
    except OSError as e:
        print(f"  humaneval: gunzip failed: {e}", file=sys.stderr)
        return 0

    n = 0
    tests: dict[str, dict[str, str]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            problem = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"  humaneval: skip malformed line: {e}", file=sys.stderr)
            continue
        task_id = problem.get("task_id", "")
        prompt = problem.get("prompt", "")
        canonical = problem.get("canonical_solution", "")
        test_src = problem.get("test", "")
        entry_point = problem.get("entry_point", "")
        if not task_id or not canonical:
            continue
        # Sanitise ``HumanEval/0`` -> ``HumanEval_0.py``.
        safe = task_id.replace("/", "_")
        filename = f"{safe}.py"
        path = dst / filename
        # ``prompt`` typically ends with the function header and an
        # empty body; ``canonical_solution`` is the indented body. Join
        # them so the file is a complete, importable Python module.
        content = prompt
        if not content.endswith("\n"):
            content += "\n"
        content += canonical
        if not content.endswith("\n"):
            content += "\n"
        try:
            path.write_text(content, encoding="utf-8")
            n += 1
        except OSError as e:
            print(f"  humaneval: {task_id}: write failed: {e}", file=sys.stderr)
            continue
        if test_src and entry_point:
            tests[filename] = {
                "test": test_src,
                "entry_point": entry_point,
            }

    # Always overwrite the sidecar so it stays in sync with the .py files.
    try:
        (dst / "_tests.json").write_text(
            json.dumps(tests, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as e:
        print(f"  humaneval: _tests.json write failed: {e}", file=sys.stderr)
    return n


# ---------------------------------------------------------------------------
# numpy (single-corpus case study)
# ---------------------------------------------------------------------------


def build_numpy(dst: Path, *, force: bool) -> int:
    dst.mkdir(parents=True, exist_ok=True)
    return fetch_pypi_package("numpy", dst.parent, force=force)


# ---------------------------------------------------------------------------
# cursor-sdk
# ---------------------------------------------------------------------------


def build_synthetic(dst: Path, *, force: bool) -> int:
    """Copy the in-repo synthetic corpus into *dst*.

    The synthetic corpus is hand-written contamination-resistant code
    that ships in ``tools/synthetic_corpus/`` so the rule pass and the
    Codex rewrite have to recover modules that **were not** in any
    public training corpus before 2026-05-26. The originals are vendored
    in this repo (not downloaded) so reviewers always build against the
    same hash. See README §Contamination resistance.
    """
    src_root = Path(__file__).resolve().parent / "synthetic_corpus"
    if not src_root.is_dir():
        raise FileNotFoundError(
            f"synthetic corpus source missing: {src_root}. "
            "It ships with the repo; check your checkout."
        )
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for src in sorted(src_root.glob("*.py")):
        dest = dst / src.name
        if force or not dest.is_file() or dest.read_bytes() != src.read_bytes():
            dest.write_bytes(src.read_bytes())
        n += 1
    return n


def build_cursor_sdk(dst: Path, *, force: bool) -> int:
    """Download cursor-sdk's macOS arm64 wheel and extract just the .py files."""
    dst.mkdir(parents=True, exist_ok=True)
    if not force and any(dst.glob("*.py")):
        return sum(1 for _ in dst.glob("*.py"))
    try:
        meta = _fetch_pypi_metadata("cursor-sdk")
    except Exception as e:
        print(f"  cursor-sdk: metadata fetch failed: {e}", file=sys.stderr)
        return 0
    wheel_url = None
    for entry in meta.get("urls", []):
        fn = entry.get("filename", "")
        if fn.endswith(".whl") and "macosx_11_0_arm64" in fn:
            wheel_url = entry["url"]
            break
    if not wheel_url:
        # Pick any wheel.
        for entry in meta.get("urls", []):
            if entry.get("filename", "").endswith(".whl"):
                wheel_url = entry["url"]
                break
    if not wheel_url:
        print("  cursor-sdk: no wheel found", file=sys.stderr)
        return 0

    print(f"  cursor-sdk: downloading {wheel_url.split('/')[-1]}")
    buf = _download(wheel_url)
    tmp = dst.with_suffix(".staging")
    if tmp.exists():
        shutil.rmtree(tmp)
    _extract_archive_to(buf, tmp)
    # Copy top-level cursor_sdk/*.py only (skip _vendor).
    pkg = tmp / "cursor_sdk"
    if pkg.exists():
        for f in pkg.glob("*.py"):
            shutil.copy(f, dst / f.name)
    shutil.rmtree(tmp, ignore_errors=True)
    return sum(1 for _ in dst.glob("*.py"))


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


CORPORA = {
    "stdlib": ("Python stdlib (curated)", build_stdlib),
    "stdlib-full": ("Python stdlib (every single-file module)", build_stdlib_full),
    "pypi": ("Popular pure-Python PyPI packages", build_pypi),
    "pypi-top20": ("20 additional popular pure-Python PyPI packages", build_pypi_top20),
    "humaneval": ("HumanEval canonical solutions (164 problems)", build_humaneval),
    "cursor-sdk": ("cursor-sdk 0.1.5 (top-level modules)", build_cursor_sdk),
    "synthetic": (
        "Contamination-resistant synthetic modules (written 2026-05-26)",
        build_synthetic,
    ),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help="Where to write corpora (default: /tmp/pychd-corpora).",
    )
    parser.add_argument(
        "--only",
        choices=list(CORPORA),
        action="append",
        help="Build only these corpora (repeatable).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even when the corpus already exists.",
    )
    args = parser.parse_args(argv)

    args.root.mkdir(parents=True, exist_ok=True)
    selected = args.only or list(CORPORA)

    print(f"Writing corpora to {args.root}")
    for name in selected:
        label, builder = CORPORA[name]
        print(f"\n=== {name} — {label} ===")
        dst = args.root / name
        if name == "pypi":
            n = build_pypi(dst, force=args.force)
        elif name == "pypi-top20":
            n = build_pypi_top20(dst, force=args.force)
        elif name == "cursor-sdk":
            n = build_cursor_sdk(dst, force=args.force)
        elif name == "stdlib":
            n = build_stdlib(dst)
        elif name == "stdlib-full":
            n = build_stdlib_full(dst, force=args.force)
        elif name == "humaneval":
            n = build_humaneval(dst, force=args.force)
        elif name == "synthetic":
            n = build_synthetic(dst, force=args.force)
        else:  # pragma: no cover
            n = builder(dst)  # type: ignore[call-arg]
        print(f"  -> {n} .py files at {dst}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
