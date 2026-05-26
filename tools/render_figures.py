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

    Plotly's SVG output embeds a fresh 6-hex random suffix on every
    ``<defs>`` / ``<clipPath>`` element (e.g. ``clip2d6634xyplot``).
    That suffix changes on every render even when the data is
    byte-identical, which keeps the pre-commit hook in a perpetual
    "files were modified" loop. We rewrite the suffix to a stable
    hash derived from the figure name so re-running the renderer is
    a true no-op when nothing meaningful changed.
    """
    import hashlib
    import re

    pattern = re.compile(r"[0-9a-f]{6}")
    digest = hashlib.sha1(name.encode()).hexdigest()[:6]
    # Plotly's suffix sits between a known prefix ("defs-", "clip",
    # "legend", "topdefs-") and either end-of-token or a known
    # secondary suffix ("x", "y", "xy", "xyplot"). Replace only those
    # occurrences so we don't clobber colour hex codes elsewhere.
    out = svg_text
    for prefix in ("defs-", "clip", "legend", "topdefs-"):
        out = re.sub(
            re.escape(prefix) + pattern.pattern,
            prefix + digest,
            out,
        )
    # Trailing newline so end-of-file-fixer doesn't keep "fixing" it.
    if not out.endswith("\n"):
        out += "\n"
    return out


def _write(fig: go.Figure, name: str, *, png: bool = False) -> Path:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    svg_path = ASSET_DIR / f"{name}.svg"
    fig.write_image(str(svg_path), format="svg", width=900, height=520, scale=1)
    # Normalise non-deterministic plotly IDs so re-renders are no-ops
    # when the chart data hasn't changed (the pre-commit hook depends
    # on this to avoid an infinite "files modified" loop).
    normalised = _normalize_svg(svg_path.read_text(), name)
    svg_path.write_text(normalised)
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
    """Bar chart of magic-number revisions covered per Python minor release.

    Bar height = number of distinct CPython magic numbers (micro-release
    bytecode revisions) routed to a rule pass for that minor. Bar colour
    encodes which rule pass handles the minor (native / cross-version).
    The chart is limited to 3.6+ — the realistic deployment range for
    modern Python — since CPython 3.0-3.5 are EOL and including them
    flattens the chart with versions nobody compiles bytecode against.
    """
    from pychd.versions import KNOWN_VERSIONS, rule_pass_for

    counts: dict[tuple[int, int], int] = {}
    for info in KNOWN_VERSIONS.values():
        counts[info.version] = counts.get(info.version, 0) + 1
    minors = sorted(m for m in counts if m >= (3, 6))
    labels = [f"3.{m[1]}" for m in minors]
    passes = [rule_pass_for(m) for m in minors]
    heights = [counts[m] for m in minors]
    colors = {
        "native": COLOR_DECLARATION,
        "cross-version": COLOR_SIGNATURE,
        "llm-only": COLOR_NEUTRAL,
    }
    bar_colors = [colors[p] for p in passes]
    text_labels = [
        f"{n} rev{'s' if n != 1 else ''}<br>{p}" for n, p in zip(heights, passes)
    ]

    fig = go.Figure()
    fig.add_bar(
        x=labels,
        y=heights,
        marker_color=bar_colors,
        text=text_labels,
        textposition="outside",
        hovertemplate=(
            "Python %{x}<br>%{y} magic-number revision(s)<br>"
            "rule pass: %{customdata}<extra></extra>"
        ),
        customdata=passes,
        showlegend=False,
    )
    # Legend proxies — empty traces just so the colour key shows up.
    for label, key in [
        ("Native rule pass (3.14)", "native"),
        ("Cross-version rule pass (3.6 – 3.13)", "cross-version"),
    ]:
        fig.add_bar(
            x=[None],
            y=[None],
            name=label,
            marker_color=colors[key],
            showlegend=True,
        )
    fig.update_layout(
        title=(
            "Rule-pass coverage across CPython 3.6 – 3.14"
            " (bar = # of magic-number revisions per minor)"
        ),
        template=PLOTLY_TEMPLATE,
        yaxis=dict(title="Magic-number revisions covered", rangemode="tozero"),
        xaxis=dict(title="Python minor release", type="category"),
        legend=dict(orientation="h", y=-0.2),
        margin=dict(l=60, r=20, t=80, b=100),
        bargap=0.25,
    )
    return _write(fig, "version_coverage", png=png)


def render_comparative_benchmark(
    versioned: dict[str, dict[str, dict[str, Any]]],
    *,
    png: bool = False,
) -> Path:
    """Bar chart comparing pychd vs other decompilers at each tool's
    *own* preferred Python version.

    Why not pin every tool to a single Python release: uncompyle6 /
    decompyle3 cap out at 3.8, pycdc is best on 3.10, and pylingual
    targets 3.13. Forcing them all onto 3.8 (the old chart) artificially
    advantaged the legacy tools and excluded the modern ones outright.
    We now plot every (tool, Python version) pair that actually
    produced scoring rows in ``_comparison.json``, so each tool is
    judged at its strongest version and pychd appears once per version
    to make the cross-version coverage story visible.
    """

    def version_key(v: str) -> tuple[int, int]:
        return tuple(int(p) for p in v.split("."))  # type: ignore[return-value]

    versions = sorted(versioned, key=version_key)

    # Collect every (tool, version) pair that actually ran. Skipped
    # tools (modules == 0, error mentions "out of scope" or
    # "not installed") drop out — they appear elsewhere in the README's
    # cross-version matrix as "— (not run)" / "not installed".
    columns: list[tuple[str, str, dict[str, Any]]] = []
    for v in versions:
        for tool, data in versioned[v].items():
            if data.get("modules", 0) > 0:
                columns.append((tool, v, data))

    if not columns:
        fig = go.Figure()
        fig.update_layout(title="No comparison data — run tools/compare_decompilers.py")
        return _write(fig, "comparison_decompilers", png=png)

    labels = [f"{tool}<br>@ Py {v}" for tool, v, _ in columns]

    def rate(data: dict[str, Any], key: str) -> float:
        return 100 * data.get(key, 0) / max(1, data["modules"])

    fig = go.Figure()
    fig.add_bar(
        name="Output parses",
        x=labels,
        y=[rate(d, "parses") for _, _, d in columns],
        marker_color="#bbbbbb",
        hovertemplate="%{y:.1f}%<extra>output parses</extra>",
    )
    fig.add_bar(
        name="Signature match",
        x=labels,
        y=[rate(d, "signature_match") for _, _, d in columns],
        marker_color=COLOR_SIGNATURE,
        hovertemplate="%{y:.1f}%<extra>signature match</extra>",
    )
    fig.add_bar(
        name="Declaration match",
        x=labels,
        y=[rate(d, "declaration_match") for _, _, d in columns],
        marker_color=COLOR_DECLARATION,
        hovertemplate="%{y:.1f}%<extra>declaration match</extra>",
    )
    fig.add_bar(
        name="Strict match",
        x=labels,
        y=[rate(d, "strict_match") for _, _, d in columns],
        marker_color=COLOR_STRICT,
        hovertemplate="%{y:.1f}%<extra>stripped-AST equality</extra>",
    )
    fig.add_bar(
        name="bytecode_exact",
        x=labels,
        y=[rate(d, "bytecode_exact") for _, _, d in columns],
        marker_color=COLOR_BX,
        hovertemplate="%{y:.1f}%<extra>marshal payload identical</extra>",
    )
    fig.add_bar(
        name="bytecode_normalized",
        x=labels,
        y=[rate(d, "bytecode_normalized") for _, _, d in columns],
        marker_color=COLOR_BN,
        hovertemplate="%{y:.1f}%<extra>canonical instruction stream</extra>",
    )
    fig.add_bar(
        name="behavioral_smoke",
        x=labels,
        y=[rate(d, "behavioral_smoke") for _, _, d in columns],
        marker_color=COLOR_BS,
        hovertemplate="%{y:.1f}%<extra>import + public surface</extra>",
    )
    fig.add_bar(
        name="Edit Similarity (×100)",
        x=labels,
        y=[
            100 * d.get("edit_similarity_sum", 0.0) / max(1, d["modules"])
            for _, _, d in columns
        ],
        marker_color=COLOR_ED,
        hovertemplate="%{y:.1f}<extra>mean Ratcliff-Obershelp ratio × 100</extra>",
    )

    fig.update_layout(
        title=(
            "Per-tool comparison at each tool's preferred Python version"
            " — eight-axis (no shared-version handicap)"
        ),
        template=PLOTLY_TEMPLATE,
        barmode="group",
        yaxis=dict(title="Rate (%)", range=[0, 105]),
        xaxis=dict(title="Decompiler @ Python version", tickangle=0),
        legend=dict(orientation="h", y=-0.25),
        margin=dict(l=60, r=20, t=80, b=140),
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
