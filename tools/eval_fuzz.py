"""End-to-end contamination-free evaluation harness for pychd.

The pipeline, per sample:

1. ``pychd_pyfuzz`` generates a random valid Python module (with a
   tag set describing the syntactic features it exercises).
2. ``py_compile`` (under the target Python; uv pulls the interpreter
   if absent) produces a ``.pyc``.
3. ``pychd_pyobf`` anonymises the ``.pyc`` (rename identifiers, strip
   strings / line table / filename).
4. The original ``.py`` is rewritten via the same mapping so we have
   a "what the recovered source should look like" reference.
5. ``pychd`` decompiles the anonymised ``.pyc`` (rule-only or
   hybrid-rewrite).
6. The recovered source is compared to the reference via pychd's
   existing AST / bytecode / behavioural metrics.

JSONL output: one line per sample with metric flags and the tag set,
so downstream analysis can break recovery rate out by syntactic
feature.

Usage::

    uv run python tools/eval_fuzz.py \\
        --target 3.14 --count 50 --seed 0 \\
        --mode rules-only \\
        --out /tmp/pychd-fuzz-eval/fuzz-3.14-rules.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pychd.decompile import Backend, Mode  # noqa: E402
from pychd_pyfuzz import Fuzzer  # noqa: E402
from pychd_pyobf import obfuscate  # noqa: E402
from tools._obf_source import apply_mapping_to_source  # noqa: E402
from tools.benchmark import measure_module  # noqa: E402
from tools.build_multiversion_fixtures import compile_with  # noqa: E402

logger = logging.getLogger("eval_fuzz")


def _python_for(target: tuple[int, int]) -> str:
    """Resolve a Python executable for the given minor.

    For the running interpreter's minor, return ``sys.executable``.
    For any other supported minor, ask uv to materialise it via
    ``uv python find <target>``; raise if uv is not on PATH or the
    minor is unavailable.
    """
    import shutil
    import subprocess

    if (sys.version_info.major, sys.version_info.minor) == target:
        return sys.executable
    if shutil.which("uv") is None:
        raise RuntimeError(
            "eval_fuzz: `uv` not on PATH — needed to materialise"
            f" Python {target[0]}.{target[1]} for cross-version eval."
        )
    label = f"{target[0]}.{target[1]}"
    proc = subprocess.run(
        ["uv", "python", "find", label],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(
            f"eval_fuzz: `uv python find {label}` failed: {proc.stderr!r}"
        )
    return proc.stdout.strip()


def _evaluate_one(
    sample_source: str,
    tags: list[str],
    target: tuple[int, int],
    *,
    mode: Mode,
    backend: Backend,
    work_dir: Path,
    sample_idx: int,
) -> dict[str, object]:
    """Run the full pipeline against one sample, return a JSONL row.

    Errors at any pipeline stage propagate as ``{"error": "<stage>: <msg>"}``
    in the row rather than crashing the whole run.
    """
    record: dict[str, object] = {
        "index": sample_idx,
        "target": f"{target[0]}.{target[1]}",
        "mode": mode.value,
        "tags": tags,
        "error": None,
    }
    t0 = time.perf_counter()
    src_path = work_dir / f"sample_{sample_idx:04d}.py"
    src_path.write_text(sample_source)
    pyc_path = work_dir / f"sample_{sample_idx:04d}.pyc"
    obf_pyc = work_dir / f"sample_{sample_idx:04d}.obf.pyc"

    # 2. Compile to .pyc under the target interpreter.
    py_exe = _python_for(target)
    if not compile_with(py_exe, src_path, pyc_path):
        record["error"] = "compile: py_compile failed under target Python"
        record["elapsed_ms"] = (time.perf_counter() - t0) * 1000
        return record

    # 3. Obfuscate .pyc.
    try:
        report = obfuscate(pyc_path, obf_pyc)
    except Exception as exc:  # noqa: BLE001
        record["error"] = f"obfuscate: {exc!r}"
        record["elapsed_ms"] = (time.perf_counter() - t0) * 1000
        return record

    # 4. Build the anonymised reference source.
    try:
        anon_source = apply_mapping_to_source(sample_source, report.mapping)
    except Exception as exc:  # noqa: BLE001
        record["error"] = f"anonymise_source: {exc!r}"
        record["elapsed_ms"] = (time.perf_counter() - t0) * 1000
        return record
    anon_py_path = work_dir / f"sample_{sample_idx:04d}.anon.py"
    anon_py_path.write_text(anon_source)

    # 5/6. Decompile + measure. ``measure_module`` itself handles the
    # decompile + AST diff + semantic comparison; we feed it the
    # anonymised .py path so it compiles → decompiles → compares
    # against the anonymised reference.
    try:
        metrics = measure_module(
            anon_py_path,
            mode=mode,
            backend=backend,
        )
    except Exception as exc:  # noqa: BLE001
        record["error"] = f"measure: {exc!r}"
        record["elapsed_ms"] = (time.perf_counter() - t0) * 1000
        return record

    if metrics is None:
        record["error"] = "measure: returned None"
        record["elapsed_ms"] = (time.perf_counter() - t0) * 1000
        return record

    # Flatten ModuleMetrics into the record.
    m = asdict(metrics)
    for key in (
        "parses",
        "signature_match",
        "declaration_match",
        "strict_match",
        "bytecode_exact",
        "bytecode_normalized",
        "behavioral_smoke",
        "edit_similarity",
        "functional_correctness",
    ):
        record[key] = m.get(key)
    record["loc"] = m.get("loc")
    record["elapsed_ms"] = (time.perf_counter() - t0) * 1000
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eval_fuzz", description=__doc__)
    parser.add_argument(
        "--target",
        type=lambda s: tuple(int(p) for p in s.split(".")),
        default=(3, 14),
        help="target Python minor for compile (default: 3.14)",
    )
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--mode",
        choices=[m.value for m in Mode],
        default=Mode.RULES_ONLY.value,
    )
    parser.add_argument(
        "--backend",
        choices=[b.value for b in Backend],
        default=Backend.LITELLM.value,
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--keep-work-dir",
        action="store_true",
        help="leave the intermediate .py / .pyc / .obf.pyc tree on disk",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    fuzzer = Fuzzer(target=args.target, seed=args.seed)
    samples = fuzzer.generate_batch(args.count)
    args.out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="eval_fuzz_") as raw_tmp:
        tmp = Path(raw_tmp)
        with args.out.open("w") as fh:
            for sample in samples:
                row = _evaluate_one(
                    sample.source,
                    sample.tags,
                    args.target,
                    mode=Mode(args.mode),
                    backend=Backend(args.backend),
                    work_dir=tmp,
                    sample_idx=sample.index,
                )
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
        if args.keep_work_dir:
            kept = args.out.with_suffix(".workdir")
            os.rename(tmp, kept)
            print(f"eval_fuzz: kept work tree → {kept}", file=sys.stderr)

    # Brief summary to stderr so reviewers can eyeball the run.
    sig = decl = strict = ok = 0
    total = args.count
    with args.out.open() as fh:
        for line in fh:
            row = json.loads(line)
            if row.get("error"):
                continue
            ok += 1
            sig += 1 if row.get("signature_match") else 0
            decl += 1 if row.get("declaration_match") else 0
            strict += 1 if row.get("strict_match") else 0
    print(
        f"eval_fuzz: {total} samples, {ok} measured | "
        f"sig {sig}/{ok} ({100 * sig / max(1, ok):.1f}%) | "
        f"decl {decl}/{ok} ({100 * decl / max(1, ok):.1f}%) | "
        f"strict {strict}/{ok} ({100 * strict / max(1, ok):.1f}%) → {args.out}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
