"""Generate the paper-quality benchmark section for the README.

This script is the **single source of truth** for every number, table,
and chart in the project README's *Evaluation* section. Running it
overwrites the placeholder block delimited by
``<!-- BEGIN: paper-generated -->`` … ``<!-- END: paper-generated -->``
inside `README.md` with freshly computed results.

Re-running the script is the only way the README's headline numbers
change. The script:

1. Builds every corpus (re-uses cached downloads under
   ``/tmp/pychd-corpora/``).
2. Runs pychd's rule-only pipeline against each corpus.
3. Computes signature / declaration / strict match rates plus
   identifier / import / docstring recall.
4. Renders a markdown block containing:

   - the per-corpus results table,
   - a Mermaid bar/line chart,
   - an aggregate row,
   - a small "what's still failing" breakdown,
   - the residual-failure attribution (``if False:`` blocks are called
     out as fundamentally unrecoverable per CPython's constant
     folding).

5. Splices the rendered block into the README.

Usage::

    uv run python tools/render_paper.py             # default
    uv run python tools/render_paper.py --dry-run   # print without writing
    uv run python tools/render_paper.py --json out.json   # also dump raw

Determinism: results depend on (a) the rule engine, (b) the running
Python's bytecode, (c) the corpus content. Re-runs on the same system
produce byte-identical output.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import warnings
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Recovered docstrings often contain invalid escape sequences (``\s``,
# ``\*``, ``\o``…) that Python 3.12+ flags via SyntaxWarning.
warnings.simplefilter("ignore", SyntaxWarning)

from tools.benchmark import ModuleMetrics, measure_module  # noqa: E402

CORPORA = [
    ("stdlib", "Curated stdlib (10 modules)"),
    ("stdlib-full", "Full Python 3.14 stdlib (single-file modules)"),
    ("pypi", "PyPI: requests, click, attrs, flask, httpx, rich"),
    ("pypi-top20", "PyPI top-20 pure-Python packages"),
    ("humaneval", "OpenAI HumanEval (164 problems)"),
    ("cursor-sdk", "cursor-sdk 0.1.5 (top-level modules)"),
]


def _ensure_corpora(root: Path) -> None:
    if all((root / name).exists() for name, _ in CORPORA):
        return
    subprocess.run(
        ["uv", "run", "python", "tools/build_corpora.py"],
        cwd=REPO_ROOT,
        check=True,
    )


def _gather_corpus(root: Path, name: str) -> list[ModuleMetrics]:
    base = root / name
    top_only = name in {"stdlib", "stdlib-full", "humaneval", "cursor-sdk"}
    files = sorted(base.glob("*.py")) if top_only else sorted(base.rglob("*.py"))
    files = [f for f in files if "_vendor" not in f.parts]
    rows: list[ModuleMetrics] = []
    for f in files:
        m = measure_module(f)
        if m is not None:
            rows.append(m)
    return rows


def _categorize_failure(src_path: Path) -> str:
    """Classify why a module fails signature/declaration match."""
    try:
        text = src_path.read_text()
    except OSError:
        return "io-error"
    if "if False:" in text or "if 0:" in text:
        return "if-False-block (CPython constant-folds — unrecoverable)"
    if "try:\n    import " in text or "try:\n    from " in text:
        return "try/except ImportError (control flow)"
    if "TYPE_CHECKING" in text:
        return "if TYPE_CHECKING block"
    return "other / complex RHS"


def _agg(rows: list[ModuleMetrics], attr: str) -> int:
    return sum(1 for r in rows if getattr(r, attr))


def _format_pct(num: int, total: int) -> str:
    return f"{num}/{total} ({100 * num / total:.1f}%)" if total else "0/0"


def render(root: Path) -> tuple[str, dict[str, Any]]:
    all_rows: dict[str, list[ModuleMetrics]] = {}
    for name, _label in CORPORA:
        all_rows[name] = _gather_corpus(root, name)

    # Per-corpus table
    table_lines = [
        "| Corpus | Modules | LoC | Parses | Signature | Declaration | Strict |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    grand_n = grand_loc = grand_par = grand_sig = grand_decl = grand_str = 0
    for name, label in CORPORA:
        rows = all_rows[name]
        n = len(rows)
        loc = sum(r.loc for r in rows)
        par = _agg(rows, "parses")
        sig = _agg(rows, "signature_match")
        decl = _agg(rows, "declaration_match")
        strict = _agg(rows, "strict_match")
        table_lines.append(
            f"| **{name}**<br/>_{label}_ | {n} | {loc:,} | "
            f"{_format_pct(par, n)} | {_format_pct(sig, n)} | "
            f"{_format_pct(decl, n)} | {_format_pct(strict, n)} |"
        )
        grand_n += n
        grand_loc += loc
        grand_par += par
        grand_sig += sig
        grand_decl += decl
        grand_str += strict
    table_lines.append(
        f"| **aggregate** | **{grand_n}** | **{grand_loc:,}** | "
        f"**{_format_pct(grand_par, grand_n)}** | "
        f"**{_format_pct(grand_sig, grand_n)}** | "
        f"**{_format_pct(grand_decl, grand_n)}** | "
        f"**{_format_pct(grand_str, grand_n)}** |"
    )

    # Pre-rendered SVG references. The figures themselves are generated
    # by ``tools/render_figures.py`` from the JSON file we write below.
    # Image-based charts render uniformly on GitHub / PyPI / IDE
    # previews — the previous Mermaid block did not.
    chart = [
        "![Recovery rate by corpus](assets/recovery_by_corpus.svg)",
        "",
        "Bars = signature match · declaration match · strict match per corpus.",
        "",
        "![Rule-pass coverage across CPython 3.x releases]"
        "(assets/version_coverage.svg)",
        "",
        "Every Python 3.x release routes through a rule pass: 3.14 hits"
        " the **native** walker for full-fidelity recovery, 3.0 – 3.13"
        " hit the **cross-version** walker for declaration-level"
        " recovery via xdis.",
    ]

    # Failure attribution
    failures: Counter[str] = Counter()
    for name, _ in CORPORA:
        base = root / name
        for r in all_rows[name]:
            if r.signature_match:
                continue
            # Find the offending file
            candidates = list(base.rglob(r.name)) or [base / r.name]
            if candidates:
                failures[_categorize_failure(candidates[0])] += 1
    fail_lines = [
        "**Residual failures** (signature match):",
        "",
        "| Cause | Count | Fundamentally recoverable? |",
        "|---|---:|---|",
    ]
    for cause, count in failures.most_common():
        recoverable = (
            "❌ no — constant-folded" if "if-False" in cause else "future work"
        )
        fail_lines.append(f"| {cause} | {count} | {recoverable} |")
    if not failures:
        fail_lines.append("| _(none)_ | 0 | every recoverable module recovered |")

    # Compose
    body = "\n".join(
        [
            "<!-- BEGIN: paper-generated -->",
            "",
            "> _This section is generated by `tools/render_paper.py` and_"
            " _committed alongside the code. Re-generate via `just paper`_"
            " _whenever rules.py or any corpus changes._",
            "",
            "**Headline:** rule-only recovery on **"
            f"{grand_n} modules / {grand_loc:,} LoC**:",
            "",
            f"- **Signature match: {_format_pct(grand_sig, grand_n)}**"
            " — every public class, function, import, and class-method"
            " name in the original survives in the recovered tree.",
            f"- **Declaration match: {_format_pct(grand_decl, grand_n)}**"
            " — signature match plus every module/class-level variable"
            " and annotated attribute by name.",
            f"- **Strict match: {_format_pct(grand_str, grand_n)}**"
            " — full stripped-AST equality (cosmetic regression telltale;"
            " bounded by CPython compiler normalisations).",
            "",
            "#### Per-corpus results",
            "",
            *table_lines,
            "",
            "#### Visualisation",
            "",
            *chart,
            "",
            "#### Residual failure attribution",
            "",
            *fail_lines,
            "",
            "<!-- END: paper-generated -->",
        ]
    )

    raw: dict[str, Any] = {
        "totals": {
            "modules": grand_n,
            "loc": grand_loc,
            "parses": grand_par,
            "signature_match": grand_sig,
            "declaration_match": grand_decl,
            "strict_match": grand_str,
        },
        "corpora": {
            name: {
                "modules": len(all_rows[name]),
                "loc": sum(r.loc for r in all_rows[name]),
                "parses": _agg(all_rows[name], "parses"),
                "signature_match": _agg(all_rows[name], "signature_match"),
                "declaration_match": _agg(all_rows[name], "declaration_match"),
                "strict_match": _agg(all_rows[name], "strict_match"),
                "rows": [asdict(r) for r in all_rows[name]],
            }
            for name, _ in CORPORA
        },
    }
    return body, raw


def splice_into_readme(readme: Path, block: str) -> None:
    text = readme.read_text()
    start_tag = "<!-- BEGIN: paper-generated -->"
    end_tag = "<!-- END: paper-generated -->"
    if start_tag not in text or end_tag not in text:
        # Append at end if not present.
        readme.write_text(text + "\n\n" + block + "\n")
        return
    before, _, rest = text.partition(start_tag)
    _, _, after = rest.partition(end_tag)
    readme.write_text(before + block + after)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--corpora-root",
        type=Path,
        default=Path("/tmp/pychd-corpora"),
    )
    ap.add_argument("--readme", type=Path, default=REPO_ROOT / "README.md")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument(
        "--render-figures",
        action="store_true",
        help=(
            "Also regenerate the SVG figures under assets/ via"
            " tools/render_figures.py. Requires the dev dependency"
            " group (plotly + kaleido)."
        ),
    )
    ap.add_argument(
        "--skip-comparison",
        action="store_true",
        help="Skip the comparative-benchmark step (uncompyle6 / decompyle3).",
    )
    args = ap.parse_args(argv)

    _ensure_corpora(args.corpora_root)
    block, raw = render(args.corpora_root)

    if args.dry_run:
        print(block)
    else:
        splice_into_readme(args.readme, block)
        print(f"updated {args.readme}")

    # Always cache the latest results next to the figures so re-rendering
    # is a no-op when nothing changed.
    cache_dir = REPO_ROOT / "assets"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "_results.json").write_text(json.dumps(raw, indent=2))

    if args.json:
        args.json.write_text(json.dumps(raw, indent=2))
        print(f"wrote {args.json}")

    if not args.skip_comparison:
        # Best-effort comparison; failure shouldn't break the paper render.
        try:
            subprocess.run(
                ["uv", "run", "python", "tools/compare_decompilers.py"],
                cwd=REPO_ROOT,
                check=False,
            )
        except Exception as e:  # pragma: no cover - defensive
            print(f"comparison step failed: {e}")

    if args.render_figures:
        subprocess.run(
            ["uv", "run", "python", "tools/render_figures.py"],
            cwd=REPO_ROOT,
            check=False,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
