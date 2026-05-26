"""Generate the SVG/PNG figures embedded in the README's evaluation section.

Inputs come from the in-memory results that :mod:`tools.render_paper` has
already computed for every corpus, plus the optional cross-tool
comparison block produced by :mod:`tools.compare_decompilers`. Outputs
are written under ``assets/`` so the README can reference them as
relative image links.

Why a separate module
---------------------

* The mermaid charts that used to live inside the README rendered
  inconsistently across PyPI, GitHub, and IDE previews. Pre-rendered
  SVG is universally supported.
* Figure layout (per-corpus recovery, comparative bar chart, residual
  failure attribution) is reusable across the README, future papers
  / talks, and the ``--json`` benchmark export.
* Headless rendering avoids the Chrome-dependent ``kaleido`` v1
  toolchain by pinning ``kaleido==0.2.1`` in ``pyproject.toml``.

Usage::

    uv run python tools/render_figures.py             # default
    uv run python tools/render_figures.py --png       # also emit PNG
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Suppress kaleido v0 deprecation noise — the chromium-free renderer
# is the only one that works without a Chrome install.
warnings.filterwarnings(
    "ignore",
    message=".*Kaleido versions less than 1.0.0.*",
    category=DeprecationWarning,
)

import plotly.graph_objects as go  # noqa: E402

ASSET_DIR = REPO_ROOT / "assets"

# Brand-consistent palette. Three hues distinguish the three tiers of
# the match metric (signature / declaration / strict); a neutral grey
# is reserved for "out of scope" bars in comparative charts. The
# semantic-axis chart uses three orthogonal hues so it reads as a
# distinct chart family.
COLOR_SIGNATURE = "#1f77b4"  # blue
COLOR_DECLARATION = "#2ca02c"  # green
COLOR_STRICT = "#d62728"  # red
COLOR_NEUTRAL = "#999999"
COLOR_BX = "#9467bd"  # purple — bytecode_exact
COLOR_BN = "#ff7f0e"  # orange — bytecode_normalized
COLOR_BS = "#17becf"  # cyan — behavioral_smoke
COLOR_FC = "#e377c2"  # pink — functional_correctness (Pass@1)
COLOR_ED = "#8c564b"  # brown — edit_similarity

PLOTLY_TEMPLATE = "plotly_white"


def _write(fig: go.Figure, name: str, *, png: bool = False) -> Path:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    svg_path = ASSET_DIR / f"{name}.svg"
    fig.write_image(str(svg_path), format="svg", width=900, height=520, scale=1)
    if png:
        png_path = ASSET_DIR / f"{name}.png"
        fig.write_image(str(png_path), format="png", width=1800, height=1040, scale=2)
    return svg_path


def render_per_corpus_recovery(
    results: dict[str, dict[str, Any]],
    *,
    png: bool = False,
) -> Path:
    """Grouped bar chart: signature / declaration / strict match per corpus."""
    corpora = list(results.keys())
    sig = [
        100 * results[c]["signature_match"] / max(1, results[c]["modules"])
        for c in corpora
    ]
    decl = [
        100 * results[c]["declaration_match"] / max(1, results[c]["modules"])
        for c in corpora
    ]
    strict = [
        100 * results[c]["strict_match"] / max(1, results[c]["modules"])
        for c in corpora
    ]

    fig = go.Figure()
    fig.add_bar(
        name="Signature match",
        x=corpora,
        y=sig,
        marker_color=COLOR_SIGNATURE,
        hovertemplate="%{y:.1f}%<extra>signature match</extra>",
    )
    fig.add_bar(
        name="Declaration match",
        x=corpora,
        y=decl,
        marker_color=COLOR_DECLARATION,
        hovertemplate="%{y:.1f}%<extra>declaration match</extra>",
    )
    fig.add_bar(
        name="Strict match",
        x=corpora,
        y=strict,
        marker_color=COLOR_STRICT,
        hovertemplate="%{y:.1f}%<extra>strict match</extra>",
    )
    fig.update_layout(
        title="Rule-only recovery rate by corpus (no LLM)",
        template=PLOTLY_TEMPLATE,
        barmode="group",
        yaxis=dict(title="Recovery rate (%)", range=[0, 105]),
        xaxis=dict(title="Corpus"),
        legend=dict(orientation="h", y=-0.15),
        margin=dict(l=60, r=20, t=80, b=80),
    )
    return _write(fig, "recovery_by_corpus", png=png)


def render_semantic_by_corpus(
    results: dict[str, dict[str, Any]],
    *,
    png: bool = False,
) -> Path:
    """Bar chart: bytecode_exact / bytecode_normalized / behavioral_smoke per corpus.

    Companion to :func:`render_per_corpus_recovery` — that one shows
    the static AST axes (signature / declaration / strict). This one
    shows the semantic axes, which answer the *other* question: how
    close is the recovered source to the original *after* the compiler
    is done with it?
    """
    corpora = list(results.keys())
    bx = [
        100 * results[c].get("bytecode_exact", 0) / max(1, results[c]["modules"])
        for c in corpora
    ]
    bn = [
        100 * results[c].get("bytecode_normalized", 0) / max(1, results[c]["modules"])
        for c in corpora
    ]
    bs = [
        100 * results[c].get("behavioral_smoke", 0) / max(1, results[c]["modules"])
        for c in corpora
    ]

    fig = go.Figure()
    fig.add_bar(
        name="bytecode_exact (BX)",
        x=corpora,
        y=bx,
        marker_color=COLOR_BX,
        hovertemplate="%{y:.1f}%<extra>marshal payload identical</extra>",
    )
    fig.add_bar(
        name="bytecode_normalized (BN)",
        x=corpora,
        y=bn,
        marker_color=COLOR_BN,
        hovertemplate="%{y:.1f}%<extra>canonical instruction stream</extra>",
    )
    fig.add_bar(
        name="behavioral_smoke (BS)",
        x=corpora,
        y=bs,
        marker_color=COLOR_BS,
        hovertemplate="%{y:.1f}%<extra>import + public surface</extra>",
    )
    fig.update_layout(
        title="Semantic equivalence rate by corpus (rule-only, no LLM)",
        template=PLOTLY_TEMPLATE,
        barmode="group",
        yaxis=dict(title="Rate (%)", range=[0, 105]),
        xaxis=dict(title="Corpus"),
        legend=dict(orientation="h", y=-0.15),
        margin=dict(l=60, r=20, t=80, b=80),
    )
    return _write(fig, "semantic_by_corpus", png=png)


def render_paper_axes_by_corpus(
    results: dict[str, dict[str, Any]],
    *,
    png: bool = False,
) -> Path:
    """Paper-aligned axes: Pass@1 + mean Edit Similarity per corpus.

    Reports the two metrics that align pychd's evaluation with
    Decompile-Bench (arXiv 2505.12668) and PyLingual
    (USENIX Security 2025). Pass@1 appears only for corpora that ship
    a ``_tests.json`` oracle — currently HumanEval; other corpora show
    a hollow bar to make the gap explicit.

    Edit Similarity is rescaled to a percentage (mean ratio × 100) so
    both metrics share the same y-axis.
    """
    corpora = list(results.keys())
    fc_rate: list[float | None] = []
    for c in corpora:
        d = results[c]
        total = d.get("functional_total", 0)
        if total <= 0:
            fc_rate.append(None)
        else:
            fc_rate.append(100 * d.get("functional_correctness", 0) / total)
    edit_pct = [100 * results[c].get("edit_similarity", 0.0) for c in corpora]

    fig = go.Figure()
    fig.add_bar(
        name="Pass@1 (functional_correctness)",
        x=corpora,
        y=[v if v is not None else 0 for v in fc_rate],
        marker_color=COLOR_FC,
        # Highlight n/a bars by hatching via opacity.
        marker_opacity=[1.0 if v is not None else 0.2 for v in fc_rate],
        hovertemplate=(
            "%{y:.1f}%<extra>Pass@1 — recovered module passes the "
            "original check()</extra>"
        ),
    )
    fig.add_bar(
        name="Edit Similarity (mean × 100)",
        x=corpora,
        y=edit_pct,
        marker_color=COLOR_ED,
        hovertemplate=(
            "%{y:.1f}<extra>character-level Ratcliff-Obershelp ratio × 100</extra>"
        ),
    )
    fig.update_layout(
        title=(
            "Paper-aligned axes by corpus — Pass@1 (Decompile-Bench)"
            " and Edit Similarity"
        ),
        template=PLOTLY_TEMPLATE,
        barmode="group",
        yaxis=dict(title="Rate / similarity (%)", range=[0, 105]),
        xaxis=dict(title="Corpus"),
        legend=dict(orientation="h", y=-0.15),
        margin=dict(l=60, r=20, t=80, b=80),
    )
    return _write(fig, "paper_axes_by_corpus", png=png)


def render_version_coverage(*, png: bool = False) -> Path:
    """Stacked bar showing rule-pass coverage per Python minor release."""
    from pychd.versions import KNOWN_VERSIONS, rule_pass_for

    by_minor: dict[tuple[int, int], int] = {}
    for info in KNOWN_VERSIONS.values():
        by_minor[info.version] = max(by_minor.get(info.version, 0), info.magic_number)
    minors = sorted(by_minor)
    labels = [f"3.{m[1]}" for m in minors]
    native = [1 if rule_pass_for(m) == "native" else 0 for m in minors]
    cross = [1 if rule_pass_for(m) == "cross-version" else 0 for m in minors]
    llm = [1 if rule_pass_for(m) == "llm-only" else 0 for m in minors]

    fig = go.Figure()
    fig.add_bar(
        name="Native rule pass",
        x=labels,
        y=native,
        marker_color=COLOR_DECLARATION,
        hovertemplate="<extra>native pass (full fidelity)</extra>",
    )
    fig.add_bar(
        name="Cross-version rule pass",
        x=labels,
        y=cross,
        marker_color=COLOR_SIGNATURE,
        hovertemplate="<extra>cross-version pass (declarations)</extra>",
    )
    fig.add_bar(
        name="LLM-only",
        x=labels,
        y=llm,
        marker_color=COLOR_NEUTRAL,
        hovertemplate="<extra>LLM-only</extra>",
    )
    fig.update_layout(
        title="Rule-pass coverage across CPython 3.x releases",
        template=PLOTLY_TEMPLATE,
        barmode="stack",
        yaxis=dict(title="Pass available", showticklabels=False, range=[0, 1.15]),
        xaxis=dict(title="Python minor release"),
        legend=dict(orientation="h", y=-0.2),
        margin=dict(l=40, r=20, t=80, b=80),
    )
    return _write(fig, "version_coverage", png=png)


def render_comparative_benchmark(
    comparison: dict[str, dict[str, Any]],
    *,
    png: bool = False,
) -> Path:
    """Bar chart comparing pychd to other Python decompilers on a shared corpus.

    Renders all eight axes side-by-side: three static (declaration AST
    shape), three semantic (bytecode + runtime), Pass@1, and edit
    similarity. Tools that were skipped at run time (image not built /
    binary missing) are omitted entirely — drawing them as a zero
    column would imply they failed every axis, which isn't true.
    """
    # Drop tools that produced no scoring rows (skipped due to missing
    # binary / image). They appear in the run's JSON for bookkeeping
    # but plotting them flat at 0 is misleading.
    tools = [t for t in comparison if comparison[t].get("modules", 0) > 0]
    if not tools:
        # Nothing to compare; write an empty placeholder so the README
        # still has a valid <img> target.
        fig = go.Figure()
        fig.update_layout(title="No comparison data — run tools/compare_decompilers.py")
        return _write(fig, "comparison_decompilers", png=png)

    def rate(t: str, key: str) -> float:
        return 100 * comparison[t].get(key, 0) / max(1, comparison[t]["modules"])

    fig = go.Figure()
    fig.add_bar(
        name="Output parses",
        x=tools,
        y=[rate(t, "parses") for t in tools],
        marker_color="#bbbbbb",
        hovertemplate="%{y:.1f}%<extra>output parses</extra>",
    )
    fig.add_bar(
        name="Signature match",
        x=tools,
        y=[rate(t, "signature_match") for t in tools],
        marker_color=COLOR_SIGNATURE,
        hovertemplate="%{y:.1f}%<extra>signature match</extra>",
    )
    fig.add_bar(
        name="Declaration match",
        x=tools,
        y=[rate(t, "declaration_match") for t in tools],
        marker_color=COLOR_DECLARATION,
        hovertemplate="%{y:.1f}%<extra>declaration match</extra>",
    )
    fig.add_bar(
        name="bytecode_exact",
        x=tools,
        y=[rate(t, "bytecode_exact") for t in tools],
        marker_color=COLOR_BX,
        hovertemplate="%{y:.1f}%<extra>marshal payload identical</extra>",
    )
    fig.add_bar(
        name="bytecode_normalized",
        x=tools,
        y=[rate(t, "bytecode_normalized") for t in tools],
        marker_color=COLOR_BN,
        hovertemplate="%{y:.1f}%<extra>canonical instruction stream</extra>",
    )
    fig.add_bar(
        name="behavioral_smoke",
        x=tools,
        y=[rate(t, "behavioral_smoke") for t in tools],
        marker_color=COLOR_BS,
        hovertemplate="%{y:.1f}%<extra>import + public surface</extra>",
    )

    # Paper-aligned axes. FC may be n/a (no oracle) — show as 0 with
    # reduced opacity; ED is always defined and rescaled to a percentage.
    def fc_rate(t: str) -> tuple[float, float]:
        total = comparison[t].get("functional_total", 0)
        passes = comparison[t].get("functional_correctness", 0)
        return (100 * passes / max(1, total), total)

    fc_values = [fc_rate(t) for t in tools]
    fig.add_bar(
        name="Pass@1 (FC)",
        x=tools,
        y=[v[0] for v in fc_values],
        marker_color=COLOR_FC,
        marker_opacity=[1.0 if v[1] > 0 else 0.2 for v in fc_values],
        hovertemplate=("%{y:.1f}%<extra>Pass@1 — n/a where no oracle</extra>"),
    )
    fig.add_bar(
        name="Edit Similarity (×100)",
        x=tools,
        y=[
            100
            * comparison[t].get("edit_similarity_sum", 0.0)
            / max(1, comparison[t]["modules"])
            for t in tools
        ],
        marker_color=COLOR_ED,
        hovertemplate="%{y:.1f}<extra>mean Ratcliff-Obershelp ratio × 100</extra>",
    )

    fig.update_layout(
        title=(
            "pychd vs. uncompyle6 / decompyle3 on a shared Python 3.8 corpus"
            " — eight-axis comparison"
        ),
        template=PLOTLY_TEMPLATE,
        barmode="group",
        yaxis=dict(title="Rate (%)", range=[0, 105]),
        xaxis=dict(title="Decompiler"),
        legend=dict(orientation="h", y=-0.2),
        margin=dict(l=60, r=20, t=80, b=100),
    )
    return _write(fig, "comparison_decompilers", png=png)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--results-json",
        type=Path,
        default=REPO_ROOT / "assets" / "_results.json",
        help="Path to results.json emitted by render_paper.py.",
    )
    ap.add_argument(
        "--comparison-json",
        type=Path,
        default=REPO_ROOT / "assets" / "_comparison.json",
        help="Path to comparison.json emitted by compare_decompilers.py.",
    )
    ap.add_argument("--png", action="store_true", help="Also emit PNG variants.")
    args = ap.parse_args(argv)

    written: list[Path] = [render_version_coverage(png=args.png)]
    if args.results_json.exists():
        raw = json.loads(args.results_json.read_text())
        written.append(render_per_corpus_recovery(raw["corpora"], png=args.png))
        written.append(render_semantic_by_corpus(raw["corpora"], png=args.png))
        written.append(render_paper_axes_by_corpus(raw["corpora"], png=args.png))
    if args.comparison_json.exists():
        cmp = json.loads(args.comparison_json.read_text())
        # New versioned schema wraps per-version dicts under "versions";
        # render the most-supported version (lowest minor present) as
        # the canonical comparison chart so the README's single image
        # still makes sense at a glance.
        if isinstance(cmp, dict) and "versions" in cmp and cmp["versions"]:
            versions = sorted(
                cmp["versions"].keys(),
                key=lambda s: tuple(map(int, s.split("."))),
            )
            # The lowest minor is also the broadest tool overlap —
            # uncompyle6, decompyle3, and pycdc all read 3.8 .pyc but
            # only pychd + pylingual cover 3.9+.
            cmp = cmp["versions"][versions[0]]
        written.append(render_comparative_benchmark(cmp, png=args.png))
    for w in written:
        print(f"wrote {w.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
