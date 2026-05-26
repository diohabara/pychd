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


def _normalize_svg(svg_text: str, name: str) -> str:
    """Rewrite every SVG ``id`` and corresponding reference to a stable
    figure-derived identifier so re-renders are byte-identical.

    The previous implementation scanned for any 6+ hex-digit run in
    the file and replaced it; that was disastrously over-eager because
    valid SVG numeric attributes like ``width="251.4666666666667"``
    contain stretches of decimal digits that happen to be valid hex
    (``66666666``), and Plotly emits these freely. The fallout was a
    structurally broken SVG (clip paths got their widths overwritten
    with random hex, rendering whole panels as blank rectangles —
    which is exactly the "真っ白" failure the user just hit).

    This rewrite only touches values that are unambiguously SVG ids:
    the contents of ``id="..."`` attributes and references to them
    via ``url(#...)`` / ``xlink:href="#..."`` / ``href="#..."`` /
    inline ``fill: url('#...')``. Numeric attributes are never
    matched. Each old id is mapped to ``svgid-<12-hex-hash>`` so the
    output is deterministic across runs.
    """
    import hashlib
    import re

    # Collect ids in first-appearance order so the deterministic
    # rename does not depend on the random string Plotly happened to
    # pick this run.
    seen_in_order: list[str] = []
    seen_set: set[str] = set()
    # Scan once over the whole text and grab any id-shaped match from
    # any of the four patterns.
    combined = re.compile(
        r'\bid="([^"]+)"'
        r"|url\(#([^)]+)\)"
        r'|\b(?:xlink:href|href)="#([^"]+)"'
        r"|url\(['\"]?#([^)'\"]+)['\"]?\)"
    )
    for m in combined.finditer(svg_text):
        for grp in m.groups():
            if grp and grp not in seen_set:
                seen_set.add(grp)
                seen_in_order.append(grp)

    mapping: dict[str, str] = {}
    for rank, old in enumerate(seen_in_order):
        h = hashlib.sha1(f"{name}:{rank}".encode()).hexdigest()[:12]
        mapping[old] = f"svgid-{h}"

    out = svg_text
    # Replace longest-first so a token that is a prefix of another
    # token (e.g. `clipFoo` ⊂ `clipFooxy`) does not get partially
    # rewritten and corrupt the longer one.
    for old in sorted(mapping, key=len, reverse=True):
        new = mapping[old]
        out = out.replace(f'id="{old}"', f'id="{new}"')
        out = out.replace(f"url(#{old})", f"url(#{new})")
        out = out.replace(f"url(#{old}')", f"url(#{new}')")
        out = out.replace(f'url(#{old}")', f'url(#{new}")')
        out = out.replace(f"url('#{old}')", f"url('#{new}')")
        out = out.replace(f'url("#{old}")', f'url("#{new}")')
        out = out.replace(f'xlink:href="#{old}"', f'xlink:href="#{new}"')
        out = out.replace(f'href="#{old}"', f'href="#{new}"')

    if not out.endswith("\n"):
        out += "\n"
    return out


def _write(
    fig: go.Figure,
    name: str,
    *,
    png: bool = False,
    height: int = 520,
) -> Path:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    svg_path = ASSET_DIR / f"{name}.svg"
    fig.write_image(str(svg_path), format="svg", width=900, height=height, scale=1)
    # Normalise non-deterministic plotly IDs so re-renders are no-ops
    # when the chart data hasn't changed (the pre-commit hook depends
    # on this to avoid an infinite "files modified" loop).
    normalised = _normalize_svg(svg_path.read_text(), name)
    svg_path.write_text(normalised)
    if png:
        png_path = ASSET_DIR / f"{name}.png"
        fig.write_image(
            str(png_path), format="png", width=1800, height=height * 2, scale=2
        )
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


def render_version_coverage(*, png: bool = False) -> Path | None:
    """Intentionally a no-op.

    The textual table in README §Cross-version support already lists
    every minor with its rule-pass type, latest magic number, and
    bytecode-change note. A single-row strip showing the same
    information reduces to a sentence ("3.6 – 3.13 use cross-version,
    3.14 uses native"), which is the Wilke-canonical case for *not*
    having a figure (codex review fix #2; Wilke, *Fundamentals of
    Data Visualization* — balance data and context). Kept as a stub
    so existing callers don't break.
    """
    _ = png  # silence unused-arg lint
    return None


def render_comparative_benchmark(
    versioned: dict[str, dict[str, dict[str, Any]]],
    *,
    png: bool = False,
) -> Path:
    """Faceted (small-multiples) grouped bar chart, one panel per
    Python version, with bar height = metric score.

    Design history:

    * v1 was a single grouped-bar panel with every tool forced to
      Python 3.8 — biased against modern decompilers (codex review).
    * v2 was a heatmap with all (tool, version) pairs as rows. Honest
      data, but cell colour is a *position-after-decoding* channel
      (Cleveland) and the cell text vanished into mid-range blues
      even with luminance-adaptive colours.
    * v3 (this version): facet by Python version. Each panel holds
      ≤3 tools × 8 metrics = 24 bars, which is in the Wilke "easy to
      read" range. Bar height carries the magnitude directly, panels
      stay scannable, and pychd-across-versions is recovered by
      reading panels left to right.
    """

    def version_key(v: str) -> tuple[int, int]:
        return tuple(int(p) for p in v.split("."))  # type: ignore[return-value]

    versions = sorted(versioned, key=version_key)

    rows: list[tuple[str, str, dict[str, Any]]] = []
    for v in versions:
        for tool, data in versioned[v].items():
            if data.get("modules", 0) > 0:
                rows.append((tool, v, data))

    if not rows:
        fig = go.Figure()
        fig.update_layout(title="No comparison data — run tools/compare_decompilers.py")
        return _write(fig, "comparison_decompilers", png=png)

    def short_name(tool: str) -> str:
        return tool.split(" (")[0]

    metric_specs: list[tuple[str, str, str]] = [
        ("Parses", "parses", "#bbbbbb"),
        ("Signature", "signature_match", COLOR_SIGNATURE),
        ("Declaration", "declaration_match", COLOR_DECLARATION),
        ("Strict", "strict_match", COLOR_STRICT),
        ("Bytecode exact", "bytecode_exact", COLOR_BX),
        ("Bytecode norm.", "bytecode_normalized", COLOR_BN),
        ("Behavioral smoke", "behavioral_smoke", COLOR_BS),
        ("Edit sim. ×100", "edit_similarity_sum", COLOR_ED),
    ]

    def cell_value(data: dict[str, Any], key: str) -> float:
        n = max(1, data.get("modules", 0))
        if key == "edit_similarity_sum":
            return 100 * data.get(key, 0.0) / n
        return 100 * data.get(key, 0) / n

    # Group rows by Python version into panel buckets.
    panels: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for tool, v, data in rows:
        panels.setdefault(v, []).append((tool, data))
    panel_versions = sorted(panels, key=version_key)

    from plotly.subplots import make_subplots  # noqa: E402

    fig = make_subplots(
        rows=1,
        cols=len(panel_versions),
        shared_yaxes=True,
        horizontal_spacing=0.04,
        subplot_titles=[f"Python {v}" for v in panel_versions],
    )

    # One trace per metric across the *whole* figure, but each metric
    # is added panel-by-panel with showlegend=True only on the first
    # panel so we end up with eight legend entries total instead of
    # eight per panel.
    for mi, (metric_name, key, color) in enumerate(metric_specs):
        for pi, v in enumerate(panel_versions, start=1):
            tools = panels[v]
            x_labels = [short_name(t) for t, _ in tools]
            y_values = [cell_value(d, key) for _, d in tools]
            # Replace pure zeros with a 1.2-unit floor so the bar is
            # visible as "explicitly zero" rather than "missing"
            # (codex review #1: zero bars vanish into the panel
            # background). The visible-stub trick is the *only*
            # visual cue we keep for low values; we deliberately do
            # NOT print per-bar value labels because mixing labelled
            # and unlabelled bars in the same panel reads as a chart
            # bug (user feedback). Magnitudes are read off the
            # y-axis instead.
            y_drawn = [max(v, 1.2) for v in y_values]
            fig.add_bar(
                name=metric_name,
                x=x_labels,
                y=y_drawn,
                customdata=y_values,
                marker_color=color,
                marker_line_color="#222222",
                marker_line_width=[0.6 if v < 1.0 else 0 for v in y_values],
                legendgroup=metric_name,
                showlegend=(pi == 1),
                hovertemplate=(
                    f"%{{x}} · {metric_name} = %{{customdata:.1f}}%<extra></extra>"
                ),
                row=1,
                col=pi,
            )

    # Y-axis label: "Score (0-100)" instead of "Rate (%)" — Edit
    # similarity is a Ratcliff-Obershelp ratio rescaled into [0, 100],
    # not literally a rate, and the previous label conflated the two
    # (codex review #1 — semantic muddiness).
    fig.update_yaxes(range=[0, 105], title="Score (0-100)", row=1, col=1)
    fig.update_xaxes(title="")
    fig.update_layout(
        title=(
            "Each tool at its preferred Python version"
            " — pychd vs competitors, side by side"
        ),
        template=PLOTLY_TEMPLATE,
        barmode="group",
        legend=dict(orientation="h", y=-0.18, x=0.5, xanchor="center"),
        margin=dict(l=60, r=20, t=80, b=120),
        height=480,
    )
    return _write(fig, "comparison_decompilers", png=png, height=480)


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

    written: list[Path] = []
    if (vc := render_version_coverage(png=args.png)) is not None:
        written.append(vc)
    if args.results_json.exists():
        raw = json.loads(args.results_json.read_text())
        written.append(render_per_corpus_recovery(raw["corpora"], png=args.png))
        written.append(render_semantic_by_corpus(raw["corpora"], png=args.png))
        written.append(render_paper_axes_by_corpus(raw["corpora"], png=args.png))
    if args.comparison_json.exists():
        cmp = json.loads(args.comparison_json.read_text())
        # The comparison figure now consumes the whole versioned dict
        # and plots each tool at its own preferred Python version.
        # Forcing all tools onto a shared release (the old 3.8-pinned
        # chart) excluded the modern decompilers entirely; see
        # render_comparative_benchmark() for the rationale.
        if isinstance(cmp, dict) and "versions" in cmp and cmp["versions"]:
            written.append(render_comparative_benchmark(cmp["versions"], png=args.png))
        # Legacy unversioned JSON: wrap as a synthetic "3.x" group so
        # the new renderer still has something to plot.
        elif isinstance(cmp, dict) and cmp:
            written.append(render_comparative_benchmark({"3.x": cmp}, png=args.png))
    for w in written:
        print(f"wrote {w.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
