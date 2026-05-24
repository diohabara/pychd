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
# is reserved for "out of scope" bars in comparative charts.
COLOR_SIGNATURE = "#1f77b4"  # blue
COLOR_DECLARATION = "#2ca02c"  # green
COLOR_STRICT = "#d62728"  # red
COLOR_NEUTRAL = "#999999"

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
    """Bar chart comparing pychd to other Python decompilers on a shared corpus."""
    tools = list(comparison.keys())
    sig = [
        100 * comparison[t]["signature_match"] / max(1, comparison[t]["modules"])
        for t in tools
    ]
    decl = [
        100 * comparison[t]["declaration_match"] / max(1, comparison[t]["modules"])
        for t in tools
    ]
    parses = [
        100 * comparison[t]["parses"] / max(1, comparison[t]["modules"]) for t in tools
    ]

    fig = go.Figure()
    fig.add_bar(
        name="Output parses",
        x=tools,
        y=parses,
        marker_color="#bbbbbb",
        hovertemplate="%{y:.1f}%<extra>output parses</extra>",
    )
    fig.add_bar(
        name="Signature match",
        x=tools,
        y=sig,
        marker_color=COLOR_SIGNATURE,
        hovertemplate="%{y:.1f}%<extra>signature match</extra>",
    )
    fig.add_bar(
        name="Declaration match",
        x=tools,
        y=decl,
        marker_color=COLOR_DECLARATION,
        hovertemplate="%{y:.1f}%<extra>declaration match</extra>",
    )
    fig.update_layout(
        title="pychd vs. uncompyle6 / decompyle3 on a shared Python 3.8 corpus",
        template=PLOTLY_TEMPLATE,
        barmode="group",
        yaxis=dict(title="Rate (%)", range=[0, 105]),
        xaxis=dict(title="Decompiler"),
        legend=dict(orientation="h", y=-0.15),
        margin=dict(l=60, r=20, t=80, b=80),
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
    if args.comparison_json.exists():
        cmp = json.loads(args.comparison_json.read_text())
        written.append(render_comparative_benchmark(cmp, png=args.png))
    for w in written:
        print(f"wrote {w.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
