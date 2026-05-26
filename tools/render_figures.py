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
    """Replace Plotly's per-render random IDs with deterministic ones.

    Plotly stamps every ``<defs>`` / ``<clipPath>`` / heatmap group /
    gradient with a fresh hex suffix on every render — e.g.
    ``clip2d6634xyplot``, ``gebb666-45449b96``, ``hycbce8b``. Suffixes
    rotate even when the data is byte-identical, which used to trap the
    pre-commit hook in a "files modified" loop. We discover every
    distinct ≥6-hex token in the file (skipping CSS colour literals,
    which are always prefixed with ``#`` or wrapped in ``rgb(...)``)
    and rewrite each to a stable hash derived from the figure name +
    the token's *position rank* in the original SVG. Rank-based
    seeding keeps the mapping stable across runs even if Plotly picks
    a different random pool, as long as the *number* and *length* of
    tokens stay constant.
    """
    import hashlib
    import re

    # Tokens of ≥6 hex characters that are NOT immediately preceded by
    # `#` (which would mark them as a CSS colour) and NOT inside an
    # `rgb(...)` triplet. Plotly's random IDs sit in attribute values
    # like `id="clipebb666xy"` or `url(#gebb666-45449b96)`.
    token_re = re.compile(r"(?<![#0-9a-fA-F])[0-9a-f]{6,}")
    seen: list[str] = []
    for match in token_re.finditer(svg_text):
        tok = match.group(0)
        if tok not in seen:
            seen.append(tok)
    mapping: dict[str, str] = {}
    for rank, tok in enumerate(seen):
        seed = f"{name}:{rank}:{len(tok)}".encode()
        mapping[tok] = hashlib.sha1(seed).hexdigest()[: len(tok)]
    out = svg_text
    # Replace longer tokens first so a 6-hex prefix doesn't clobber an
    # 8-hex token of which it's the leading substring.
    for tok in sorted(mapping, key=len, reverse=True):
        out = out.replace(tok, mapping[tok])
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
    """Heatmap comparing pychd vs other decompilers across every metric.

    The previous design was a grouped bar chart with 7 tool-version
    columns × 8 metrics = 56 bars in a single panel — exactly the
    "complex" case Wilke's *Fundamentals of Data Visualization* (Ch. 6)
    flags ("seven groups of four data values can result in a figure
    that is complex"). The heatmap maps tool-version → row,
    metric → column, score → cell colour, and prints the percentage
    inside each cell so the matrix is both scannable for patterns and
    precise for individual lookups.

    Each tool runs at its *own* preferred Python version (uncompyle6 /
    decompyle3 → 3.8, pycdc → 3.10, pylingual → 3.13). pychd appears
    once per version so the cross-version coverage story stays visible
    side-by-side with each competitor's best-case Python.
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

    row_labels = [f"{short_name(tool)} @ Py {v}" for tool, v, _ in rows]

    # Metric layout: static-AST axes first (sig → strict), then the
    # semantic axes (bytecode + behavioural), then edit similarity. The
    # paper-aligned Pass@1 column is omitted: this corpus has no test
    # oracle, so every tool would print "n/a" and add noise.
    metric_specs: list[tuple[str, str]] = [
        ("Parses", "parses"),
        ("Signature", "signature_match"),
        ("Declaration", "declaration_match"),
        ("Strict", "strict_match"),
        ("Bytecode<br>exact", "bytecode_exact"),
        ("Bytecode<br>norm.", "bytecode_normalized"),
        ("Behavioral<br>smoke", "behavioral_smoke"),
        ("Edit<br>sim. ×100", "edit_similarity_sum"),
    ]
    col_labels = [m[0] for m in metric_specs]

    def cell_value(data: dict[str, Any], key: str) -> float:
        n = max(1, data.get("modules", 0))
        if key == "edit_similarity_sum":
            return 100 * data.get(key, 0.0) / n
        return 100 * data.get(key, 0) / n

    z: list[list[float]] = []
    for _, _, data in rows:
        z.append([cell_value(data, key) for _, key in metric_specs])

    # Sequential single-hue scale with a non-white floor — codex
    # review fix #6: the default Blues ramp makes 0 % cells visually
    # indistinguishable from "missing data" / page background, which
    # is the very confusion Wilke (§19) warns against. We anchor the
    # low end at a light blue-grey so 0 % is still clearly "a cell
    # with a score" rather than absence.
    colorscale = [
        [0.00, "#dde5ee"],
        [0.25, "#a8c1da"],
        [0.50, "#6e96bf"],
        [0.75, "#3a6da3"],
        [1.00, "#0d3b66"],
    ]

    fig = go.Figure(
        data=go.Heatmap(
            z=z,
            x=col_labels,
            y=row_labels,
            colorscale=colorscale,
            zmin=0,
            zmax=100,
            colorbar=dict(
                title=dict(text="Rate (%)", side="right"),
                thickness=14,
                len=0.75,
            ),
            xgap=2,
            ygap=2,
            hovertemplate="%{y} · %{x} = %{z:.1f}%<extra></extra>",
        )
    )

    # Per-cell annotations with luminance-based text colour (codex
    # review fix #7). Threshold derived empirically from the
    # colorscale above: cells above ~55 % land on a navy where
    # near-white text reads clearly; below that, dark text on the
    # pale-blue floor reads better.
    annotations = []
    for ri, row in enumerate(z):
        for ci, value in enumerate(row):
            annotations.append(
                dict(
                    x=col_labels[ci],
                    y=row_labels[ri],
                    text=f"{value:.0f}",
                    showarrow=False,
                    font=dict(
                        size=13,
                        color="#f8f9fb" if value >= 55 else "#1a1a1a",
                    ),
                )
            )

    # Light separator lines between rows that belong to *different*
    # Python versions, so the eye groups "pychd @ 3.8 / uncompyle6 /
    # decompyle3" as one block, "pychd @ 3.10 / pycdc" as another,
    # etc. (codex review fix #9 — split the two storylines visually.)
    shapes = []
    for i in range(1, len(rows)):
        prev_version = rows[i - 1][1]
        cur_version = rows[i][1]
        if prev_version != cur_version:
            shapes.append(
                dict(
                    type="line",
                    xref="paper",
                    yref="y",
                    x0=0,
                    x1=1,
                    y0=i - 0.5,
                    y1=i - 0.5,
                    line=dict(color="#222222", width=2),
                )
            )

    fig.update_layout(
        title="Each tool at its preferred Python version",
        template=PLOTLY_TEMPLATE,
        # No xaxis title: column headers already say "Metric".
        xaxis=dict(title="", side="top", tickfont=dict(size=12)),
        # Plotly heatmaps place y=0 at the bottom by default, so the
        # natural reading order (top = first row in the data) requires
        # an explicit reversal of the auto-range.
        yaxis=dict(title="", autorange="reversed", tickfont=dict(size=12)),
        annotations=annotations,
        shapes=shapes,
        margin=dict(l=180, r=20, t=90, b=40),
        height=440,
    )
    return _write(fig, "comparison_decompilers", png=png, height=440)


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
