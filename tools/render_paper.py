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


def _agg_fc(rows: list[ModuleMetrics]) -> tuple[int, int]:
    """Pass@1 aggregate: ``(passes, oracles_available)``.

    Modules without a test oracle (``functional_correctness is None``)
    drop out of *both* numerator and denominator, so the rate reflects
    only modules where Pass@1 is actually measurable.
    """
    total = sum(1 for r in rows if r.functional_correctness is not None)
    passes = sum(1 for r in rows if r.functional_correctness is True)
    return passes, total


def _agg_edit(rows: list[ModuleMetrics]) -> float:
    """Mean ``edit_similarity`` over all rows; 0.0 on empty input."""
    if not rows:
        return 0.0
    return sum(r.edit_similarity for r in rows) / len(rows)


def _format_pct(num: int, total: int) -> str:
    return f"{num}/{total} ({100 * num / total:.1f}%)" if total else "0/0"


def _format_fc(passes: int, total: int) -> str:
    if total == 0:
        return "n/a"
    return f"{passes}/{total} ({100 * passes / total:.1f}%)"


def _format_edit(value: float) -> str:
    return f"{value:.3f}"


def render(root: Path) -> tuple[str, dict[str, Any]]:
    all_rows: dict[str, list[ModuleMetrics]] = {}
    for name, _label in CORPORA:
        all_rows[name] = _gather_corpus(root, name)

    # Per-corpus table — eight metric columns total:
    #
    # static AST axes (Sig / Decl / Strict),
    # bytecode/behavioral axes (BX / BN / BS),
    # paper-aligned axes (FC = Pass@1, ED = mean edit similarity).
    #
    # FC is only meaningful on corpora that ship a test oracle
    # (HumanEval). Other corpora show "n/a" in that column.
    table_lines = [
        "| Corpus | Modules | LoC | Parses | Sig | Decl | Strict |"
        " BX | BN | BS | FC (Pass@1) | ED |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    grand_n = grand_loc = grand_par = grand_sig = grand_decl = grand_str = 0
    grand_bx = grand_bn = grand_bs = 0
    grand_fc_pass = grand_fc_total = 0
    grand_edit_sum = 0.0
    for name, label in CORPORA:
        rows = all_rows[name]
        n = len(rows)
        loc = sum(r.loc for r in rows)
        par = _agg(rows, "parses")
        sig = _agg(rows, "signature_match")
        decl = _agg(rows, "declaration_match")
        strict = _agg(rows, "strict_match")
        bx = _agg(rows, "bytecode_exact")
        bn = _agg(rows, "bytecode_normalized")
        bs = _agg(rows, "behavioral_smoke")
        fc_pass, fc_total = _agg_fc(rows)
        edit_mean = _agg_edit(rows)
        table_lines.append(
            f"| **{name}**<br/>_{label}_ | {n} | {loc:,} | "
            f"{_format_pct(par, n)} | {_format_pct(sig, n)} | "
            f"{_format_pct(decl, n)} | {_format_pct(strict, n)} | "
            f"{_format_pct(bx, n)} | {_format_pct(bn, n)} | "
            f"{_format_pct(bs, n)} | {_format_fc(fc_pass, fc_total)} | "
            f"{_format_edit(edit_mean)} |"
        )
        grand_n += n
        grand_loc += loc
        grand_par += par
        grand_sig += sig
        grand_decl += decl
        grand_str += strict
        grand_bx += bx
        grand_bn += bn
        grand_bs += bs
        grand_fc_pass += fc_pass
        grand_fc_total += fc_total
        grand_edit_sum += edit_mean * n
    table_lines.append(
        f"| **aggregate** | **{grand_n}** | **{grand_loc:,}** | "
        f"**{_format_pct(grand_par, grand_n)}** | "
        f"**{_format_pct(grand_sig, grand_n)}** | "
        f"**{_format_pct(grand_decl, grand_n)}** | "
        f"**{_format_pct(grand_str, grand_n)}** | "
        f"**{_format_pct(grand_bx, grand_n)}** | "
        f"**{_format_pct(grand_bn, grand_n)}** | "
        f"**{_format_pct(grand_bs, grand_n)}** | "
        f"**{_format_fc(grand_fc_pass, grand_fc_total)}** | "
        f"**{_format_edit(grand_edit_sum / max(1, grand_n))}** |"
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
            f"- **Behavioral smoke: {_format_pct(grand_bs, grand_n)}**"
            " — recovered module imports under the producing interpreter"
            " and exposes the same public name + signature surface as the"
            " original. The semantic axis that tolerates the most compiler"
            " normalisations; see"
            " [Why not naïve pyc → py → pyc?](#why-not-naïve-pyc--py--pyc)"
            " for what `BX`/`BN`/`BS` measure and what each one catches.",
            f"- **Pass@1 (functional correctness):"
            f" {_format_fc(grand_fc_pass, grand_fc_total)}**"
            " — Decompile-Bench's re-executability oracle, scored on"
            " corpora that ship a `check(candidate)` test (HumanEval is"
            " currently the only one). The recovered module is imported"
            " under the producing interpreter and its entry-point"
            " function is fed to the original test suite. A pure"
            " rules-only baseline necessarily scores near 0 here because"
            " bodies are stubbed; future LLM-assisted or simple-body"
            " matcher work shows up directly in this number.",
            f"- **Edit similarity (mean):"
            f" {_format_edit(grand_edit_sum / max(1, grand_n))}**"
            " — Decompile-Bench-style character-level"
            " Ratcliff-Obershelp ratio averaged over the corpus."
            " 1.0 means byte-identical, 0.0 means entirely dissimilar."
            " A continuous metric that surfaces incremental rule-pass"
            " improvements which haven't yet flipped any boolean axis.",
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

    def _corpus_block(rows: list[ModuleMetrics]) -> dict[str, Any]:
        fc_pass, fc_total = _agg_fc(rows)
        return {
            "modules": len(rows),
            "loc": sum(r.loc for r in rows),
            "parses": _agg(rows, "parses"),
            "signature_match": _agg(rows, "signature_match"),
            "declaration_match": _agg(rows, "declaration_match"),
            "strict_match": _agg(rows, "strict_match"),
            "bytecode_exact": _agg(rows, "bytecode_exact"),
            "bytecode_normalized": _agg(rows, "bytecode_normalized"),
            "behavioral_smoke": _agg(rows, "behavioral_smoke"),
            "functional_correctness": fc_pass,
            "functional_total": fc_total,
            "edit_similarity": _agg_edit(rows),
            "rows": [asdict(r) for r in rows],
        }

    raw: dict[str, Any] = {
        "totals": {
            "modules": grand_n,
            "loc": grand_loc,
            "parses": grand_par,
            "signature_match": grand_sig,
            "declaration_match": grand_decl,
            "strict_match": grand_str,
            "bytecode_exact": grand_bx,
            "bytecode_normalized": grand_bn,
            "behavioral_smoke": grand_bs,
            "functional_correctness": grand_fc_pass,
            "functional_total": grand_fc_total,
            "edit_similarity_mean": grand_edit_sum / max(1, grand_n),
        },
        "corpora": {name: _corpus_block(all_rows[name]) for name, _ in CORPORA},
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


def _render_per_tool_table(comparison: dict[str, dict[str, Any]]) -> str:
    """Render a single-version comparison table — one row per tool."""
    if not comparison:
        return ""
    lines: list[str] = []
    lines.append("| Tool | Version | Sig | Decl | Strict | BX | BN | BS | ED |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for tool, d in comparison.items():
        n = d.get("modules", 0)
        version = d.get("version", "unknown")
        if n == 0:
            error = d.get("error") or "skipped"
            lines.append(
                f"| **{tool}** | {version} | _({error})_ | — | — | — | — | — | — |"
            )
            continue
        edit_mean = d.get("edit_similarity_sum", 0.0) / max(1, n)
        cells = [
            f"{d.get('signature_match', 0)}/{n}",
            f"{d.get('declaration_match', 0)}/{n}",
            f"{d.get('strict_match', 0)}/{n}",
            f"{d.get('bytecode_exact', 0)}/{n}",
            f"{d.get('bytecode_normalized', 0)}/{n}",
            f"{d.get('behavioral_smoke', 0)}/{n}",
            f"{edit_mean:.3f}",
        ]
        lines.append(f"| **{tool}** | {version} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _render_coverage_matrix(per_version: dict[str, dict[str, dict]]) -> str:
    """Render the cross-version coverage matrix: rows = tools, columns =
    Python versions, cell = ``sig/n`` for that (tool, version) pair, or
    ``failed`` when every module errored out, or ``not installed`` when
    the tool's binary was unavailable.

    Every tool we *attempted* shows up in the matrix even if it
    succeeded on zero modules — that's the point of the user's
    "show failures as failures" framing.
    """
    if not per_version:
        return ""
    versions = sorted(per_version.keys(), key=lambda s: tuple(map(int, s.split("."))))
    # Collect the union of tool names across all versions (tool registry
    # is the same per call so this is just a defensive intersection).
    tool_names: list[str] = []
    for v in versions:
        for t in per_version[v]:
            if t not in tool_names:
                tool_names.append(t)

    header = "| Tool | " + " | ".join(f"Py {v}" for v in versions) + " |"
    sep = "|---|" + ":---:|" * len(versions)
    lines = [header, sep]
    for tool in tool_names:
        cells: list[str] = []
        for v in versions:
            data = per_version[v].get(tool)
            if data is None:
                cells.append("—")
                continue
            n = data.get("modules", 0)
            if n == 0:
                err = data.get("error") or "no data"
                if "not installed" in err.lower() or "not built" in err.lower():
                    cells.append("not installed")
                else:
                    cells.append(f"failed ({err[:25]})")
                continue
            sig = data.get("signature_match", 0)
            pct = 100 * sig / max(1, n)
            if sig == 0:
                cells.append(f"❌ 0/{n}")
            elif pct >= 90:
                cells.append(f"✅ {sig}/{n}")
            else:
                cells.append(f"⚠ {sig}/{n}")
        lines.append(f"| **{tool}** | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _render_comparison_block(comparison: dict[str, Any]) -> str:
    """Render the cross-decompiler comparison block.

    Supports two JSON shapes:

    * **Versioned (current)**: ``{"versions": {v: {tool: data}}, ...}``
      — emits a cross-version coverage matrix and per-version detail
      tables under collapsible ``<details>`` sections.
    * **Legacy (pre-multi-version)**: ``{tool: data}`` — emits a
      single-table view, the same as before this refactor. Kept so
      stale JSONs still render something useful.

    Tools and versions the harness *attempted* but couldn't run are
    surfaced explicitly (``failed`` / ``not installed``) rather than
    silently dropped.
    """
    if not comparison:
        return ""
    # Versioned shape: the new one.
    if "versions" in comparison and isinstance(comparison["versions"], dict):
        per_version = comparison["versions"]
        if not per_version:
            return ""
        out: list[str] = []
        out.append("#### Cross-version coverage matrix\n")
        out.append(_render_coverage_matrix(per_version))
        out.append("")
        out.append(
            "Each cell shows the ``signature_match`` count for that"
            " (tool, Python version) pair against the same .pyc corpus,"
            " or `❌ 0/N` when the tool ran but recovered no"
            " signatures, or `failed (…)` when every module raised, or"
            " `not installed` when the tool's binary / podman image"
            " wasn't available on this host. Per-version detail tables"
            " (all eight axes) follow below.\n"
        )

        # One <details> block per Python version so the long form
        # doesn't dominate the README on first read.
        for v in sorted(per_version, key=lambda s: tuple(map(int, s.split(".")))):
            out.append(f"<details><summary>Python {v} — all eight axes</summary>\n")
            out.append("")
            out.append(_render_per_tool_table(per_version[v]))
            out.append("")
            out.append("</details>")
            out.append("")
        return "\n".join(out)

    # Legacy shape: {tool: data}.
    return _render_per_tool_table(comparison)


def splice_comparison_into_readme(
    readme: Path,
    comparison: dict[str, dict[str, Any]],
) -> bool:
    """Splice ``_render_comparison_block`` between the comparison
    delimiters. Returns False (no-op) if the delimiters are absent
    or the comparison JSON is empty."""
    text = readme.read_text()
    start_tag = "<!-- BEGIN: comparison-generated -->"
    end_tag = "<!-- END: comparison-generated -->"
    if start_tag not in text or end_tag not in text:
        return False
    if not comparison:
        return False
    block = (
        f"{start_tag}\n\n"
        "> _This table is generated by `tools/render_paper.py` from_"
        " _`assets/_comparison.json`. Re-run via `just bench-compare`_"
        " _or `uv run python tools/compare_decompilers.py`._\n\n"
        f"{_render_comparison_block(comparison)}\n\n"
        f"{end_tag}"
    )
    before, _, rest = text.partition(start_tag)
    _, _, after = rest.partition(end_tag)
    readme.write_text(before + block + after)
    return True


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
        # Comparison block — best-effort, only updates if the JSON
        # exists and the delimiters are present in the README.
        cmp_path = REPO_ROOT / "assets" / "_comparison.json"
        if cmp_path.is_file():
            try:
                cmp_data = json.loads(cmp_path.read_text())
            except OSError, json.JSONDecodeError:
                cmp_data = {}
            if cmp_data and splice_comparison_into_readme(args.readme, cmp_data):
                print(f"updated {args.readme} (comparison block)")
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
