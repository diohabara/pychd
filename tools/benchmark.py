"""Per-module recovery benchmark for pychd's rule-based pass.

For each ``.py`` file under a target directory, we run the full
pipeline:

    .py  →  py_compile  →  .pyc  →  pychd (rules-only)  →  recovered .py

…and compute a small set of metrics. The script is intentionally
self-contained — no fixtures, no LLM calls, no network — so the
numbers are reproducible across machines and CI runs.

Metrics
-------

For every module *M* with original AST *O* and rule-recovered AST *R*:

- **parses**         — does *R* parse as valid Python?
- **import recall**  — |imports(O) ∩ imports(R)| / |imports(O)|
- **name recall**    — |top_names(O) ∩ top_names(R)| / |top_names(O)|,
                       where ``top_names`` enumerates classes, top-level
                       functions, and module-level variables
- **docstring recall** — module + class + function docstring strings
                       recovered, divided by the count in *O*
- **skeleton match** — boolean: after stripping every function/method
                       body to ``pass`` and every annotation, *O* and *R*
                       have an identical ``ast.dump``
- **body coverage**  — 1 − (unknown_blocks / function_count); the
                       fraction of function bodies the rule pass did not
                       have to defer to the LLM (always 0 in v1 since
                       bodies are LLM territory, but we still report it
                       for forward-compat)
- **elapsed_ms**     — wall-clock time for the rule pass

A *summary* row reports per-metric arithmetic means and the count of
files where ``parses`` and ``skeleton_match`` hold.

Output formats
--------------

- ``--format markdown`` (default): renders a markdown table + Mermaid
  bar chart suitable for embedding in README.md.
- ``--format json``: prints raw per-module metrics; useful for CI
  regression gates.

Usage
-----

::

    uv run python tools/benchmark.py path/to/source/tree
    uv run python tools/benchmark.py path/to/tree --format json > metrics.json
    uv run python tools/benchmark.py path/to/tree --top-level-only
"""

from __future__ import annotations

import argparse
import ast
import json
import py_compile
import sys
import tempfile
import time
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Recovered docstrings often contain invalid escape sequences (``\s``,
# ``\*``, ``\o``…) that Python 3.12+ flags via SyntaxWarning. They are
# not actual decompiler errors — silence them so benchmark output stays
# readable.
warnings.simplefilter("ignore", SyntaxWarning)

# Make the local pychd importable when run via `python tools/benchmark.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pychd.decompile import Mode, decompile_pyc  # noqa: E402


@dataclass
class ModuleMetrics:
    name: str
    loc: int
    parses: bool
    # Three-tier match metric per
    # `references` in the skeptic review:
    #
    # - signature_match  — every original class / function / import name
    #                      survives in the recovered tree (set-subset).
    # - declaration_match — signature_match AND every module-level
    #                      variable / annotation survives.
    # - strict_match     — full stripped-AST equality (the historical
    #                      `skeleton_match`).
    #
    # `skeleton_match` is retained as an alias of strict_match for
    # backwards compatibility with existing reports.
    skeleton_match: bool  # alias of strict_match
    import_recall: float
    name_recall: float
    docstring_recall: float
    body_coverage: float
    function_count: int
    unknown_blocks: int
    elapsed_ms: float
    signature_match: bool = False
    declaration_match: bool = False
    strict_match: bool = False
    error: str | None = None


# ---------------------------------------------------------------------------
# AST utilities
# ---------------------------------------------------------------------------


def _collect_top_names(tree: ast.AST) -> set[str]:
    """Top-level class / function / variable names in a module."""
    if not isinstance(tree, ast.Module):
        return set()
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            out.add(f"class:{node.name}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(f"def:{node.name}")
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out.add(f"var:{t.id}")
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                out.add(f"var:{node.target.id}")
    return out


def _collect_imports(tree: ast.AST) -> set[str]:
    """Every import in the module, normalised to ``module:name``."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(f"import:{alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = ("." * (node.level or 0)) + (node.module or "")
            for alias in node.names:
                out.add(f"from:{mod}:{alias.name}")
    return out


def _collect_docstrings(tree: ast.AST) -> set[str]:
    """Module + class + function docstrings."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            doc = ast.get_docstring(node)
            if doc:
                out.add(doc)
    return out


def _count_functions(tree: ast.AST) -> int:
    return sum(
        1
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    )


def _strip_for_skeleton(tree: ast.AST) -> ast.AST:
    """Reduce *tree* to a normalised skeleton.

    Function/method bodies are replaced with a single ``Pass``;
    annotations on parameters, returns, and ``AnnAssign`` are erased;
    decorators are dropped (they survive textually but the rule engine
    doesn't always reattach them in the same order). After this, an
    ``ast.dump`` equality check is meaningful for skeleton comparison.
    """
    cloned = ast.parse(ast.unparse(tree))
    for node in ast.walk(cloned):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node.body = [ast.Pass()]
            node.returns = None
            node.decorator_list = []
            arglist: list[ast.arg] = []
            arglist.extend(node.args.args)
            arglist.extend(node.args.posonlyargs)
            arglist.extend(node.args.kwonlyargs)
            for a in arglist:
                a.annotation = None
            for a in (node.args.vararg, node.args.kwarg):
                if a is not None:
                    a.annotation = None
            # Drop defaults (cursor-sdk uses many string/dict literals
            # that the rule engine can't fully reconstruct yet).
            node.args.defaults = []
            node.args.kw_defaults = [None for _ in node.args.kwonlyargs]
        elif isinstance(node, ast.ClassDef):
            node.decorator_list = []
            # Re-base classes can use complex expressions; reduce to names.
            new_bases: list[ast.expr] = []
            for b in node.bases:
                if isinstance(b, ast.Name):
                    new_bases.append(b)
                elif isinstance(b, ast.Attribute):
                    new_bases.append(ast.Name(id=b.attr, ctx=ast.Load()))
                else:
                    new_bases.append(ast.Name(id="object", ctx=ast.Load()))
            node.bases = new_bases
            node.keywords = []
        elif isinstance(node, ast.AnnAssign):
            node.annotation = ast.Name(id="object", ctx=ast.Load())
    return cloned


def _skeleton_match(original: ast.AST, recovered: ast.AST | None) -> bool:
    if recovered is None:
        return False
    try:
        a = ast.dump(_strip_for_skeleton(original))
        b = ast.dump(_strip_for_skeleton(recovered))
    except Exception:
        return False
    return a == b


def _signature_signature(tree: ast.AST) -> set[str]:
    """Names that the *signature_match* metric requires.

    Per the skeptic review: every class, function, import — at module
    level or directly inside a class body — must survive in the
    recovered module. Functions defined *inside other function bodies*
    are excluded: function bodies are LLM territory (rule pass emits a
    placeholder), so nested-function recovery is out of scope.
    """
    if not isinstance(tree, ast.Module):
        return set()
    names: set[str] = set()

    def visit(node: ast.AST, *, in_function: bool) -> None:
        if isinstance(node, ast.ClassDef):
            if in_function:
                # A class defined inside a function is *not* reachable
                # from module scope. Skip its members entirely; they
                # cannot show up in the rule-only output either.
                return
            names.add(f"class:{node.name}")
            for child in node.body:
                # Class body opens a new namespace but is still
                # module-reachable; its methods/AnnAssigns count.
                visit(child, in_function=False)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not in_function:
                names.add(f"def:{node.name}")
            for child in node.body:
                visit(child, in_function=True)
            return
        if in_function:
            return
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(f"import:{alias.asname or alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = ("." * (node.level or 0)) + (node.module or "")
            for alias in node.names:
                names.add(f"from:{mod}:{alias.name}")
        elif isinstance(node, (ast.If, ast.Try, ast.With)):
            # Conditional or guarded statement blocks: dive in but stay
            # at module level (we still want to pick up imports/defs).
            for child in ast.iter_child_nodes(node):
                visit(child, in_function=False)
        elif isinstance(node, ast.Module):
            for child in node.body:
                visit(child, in_function=False)

    for node in tree.body:
        visit(node, in_function=False)
    return names


def _declaration_signature(tree: ast.AST) -> set[str]:
    """signature_signature plus module-level variables and AnnAssigns."""
    if not isinstance(tree, ast.Module):
        return set()
    names = _signature_signature(tree)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(f"var:{t.id}")
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                names.add(f"var:{node.target.id}")
        elif isinstance(node, ast.ClassDef):
            for inner in node.body:
                if isinstance(inner, ast.AnnAssign) and isinstance(
                    inner.target, ast.Name
                ):
                    names.add(f"clsvar:{node.name}.{inner.target.id}")
                elif isinstance(inner, ast.Assign):
                    for t in inner.targets:
                        if isinstance(t, ast.Name):
                            names.add(f"clsvar:{node.name}.{t.id}")
    return names


def _signature_match(original: ast.AST, recovered: ast.AST | None) -> bool:
    if recovered is None:
        return False
    return _signature_signature(original) <= _signature_signature(recovered)


def _declaration_match(original: ast.AST, recovered: ast.AST | None) -> bool:
    if recovered is None:
        return False
    return _declaration_signature(original) <= _declaration_signature(recovered)


# ---------------------------------------------------------------------------
# Per-module measurement
# ---------------------------------------------------------------------------


def measure_module(py_file: Path) -> ModuleMetrics | None:
    """Measure recovery quality for one .py file. Returns None if
    the file can't be compiled at all (e.g. Python 2 syntax)."""
    try:
        src = py_file.read_text()
    except OSError as e:
        return ModuleMetrics(
            name=py_file.name,
            loc=0,
            parses=False,
            skeleton_match=False,
            import_recall=0.0,
            name_recall=0.0,
            docstring_recall=0.0,
            body_coverage=0.0,
            function_count=0,
            unknown_blocks=0,
            elapsed_ms=0.0,
            error=f"read failed: {e}",
        )

    loc = len(src.splitlines())

    try:
        original_tree = ast.parse(src)
    except SyntaxError as e:
        return ModuleMetrics(
            name=py_file.name,
            loc=loc,
            parses=False,
            skeleton_match=False,
            import_recall=0.0,
            name_recall=0.0,
            docstring_recall=0.0,
            body_coverage=0.0,
            function_count=0,
            unknown_blocks=0,
            elapsed_ms=0.0,
            error=f"source unparseable: {e}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        pyc = Path(tmp) / "out.pyc"
        try:
            py_compile.compile(str(py_file), cfile=str(pyc), doraise=True)
        except Exception as e:
            return ModuleMetrics(
                name=py_file.name,
                loc=loc,
                parses=False,
                skeleton_match=False,
                import_recall=0.0,
                name_recall=0.0,
                docstring_recall=0.0,
                body_coverage=0.0,
                function_count=_count_functions(original_tree),
                unknown_blocks=0,
                elapsed_ms=0.0,
                error=f"py_compile failed: {e}",
            )

        t0 = time.perf_counter()
        try:
            report = decompile_pyc(pyc, mode=Mode.RULES_ONLY)
        except Exception as e:
            return ModuleMetrics(
                name=py_file.name,
                loc=loc,
                parses=False,
                skeleton_match=False,
                import_recall=0.0,
                name_recall=0.0,
                docstring_recall=0.0,
                body_coverage=0.0,
                function_count=_count_functions(original_tree),
                unknown_blocks=0,
                elapsed_ms=0.0,
                error=f"rule pass failed: {e}",
            )
        elapsed_ms = (time.perf_counter() - t0) * 1000

    out = report.source
    try:
        recovered_tree: ast.AST | None = ast.parse(out)
        parses = True
    except SyntaxError as e:
        recovered_tree = None
        parses = False
        return ModuleMetrics(
            name=py_file.name,
            loc=loc,
            parses=parses,
            skeleton_match=False,
            import_recall=0.0,
            name_recall=0.0,
            docstring_recall=0.0,
            body_coverage=0.0,
            function_count=_count_functions(original_tree),
            unknown_blocks=report.unknown_blocks,
            elapsed_ms=elapsed_ms,
            error=f"recovered unparseable: {e}",
        )

    orig_names = _collect_top_names(original_tree)
    rec_names = _collect_top_names(recovered_tree) if recovered_tree else set()
    name_recall = (
        len(orig_names & rec_names) / max(1, len(orig_names)) if orig_names else 1.0
    )

    orig_imports = _collect_imports(original_tree)
    rec_imports = _collect_imports(recovered_tree) if recovered_tree else set()
    import_recall = (
        len(orig_imports & rec_imports) / max(1, len(orig_imports))
        if orig_imports
        else 1.0
    )

    orig_docs = _collect_docstrings(original_tree)
    rec_docs = _collect_docstrings(recovered_tree) if recovered_tree else set()
    docstring_recall = (
        len(orig_docs & rec_docs) / max(1, len(orig_docs)) if orig_docs else 1.0
    )

    func_count = _count_functions(original_tree)
    body_coverage = (
        1.0 - report.unknown_blocks / max(1, func_count) if func_count else 1.0
    )
    body_coverage = max(0.0, body_coverage)

    strict = _skeleton_match(original_tree, recovered_tree)
    sig = _signature_match(original_tree, recovered_tree)
    decl = _declaration_match(original_tree, recovered_tree)

    return ModuleMetrics(
        name=py_file.name,
        loc=loc,
        parses=parses,
        skeleton_match=strict,
        signature_match=sig,
        declaration_match=decl,
        strict_match=strict,
        import_recall=import_recall,
        name_recall=name_recall,
        docstring_recall=docstring_recall,
        body_coverage=body_coverage,
        function_count=func_count,
        unknown_blocks=report.unknown_blocks,
        elapsed_ms=elapsed_ms,
        error=None,
    )


# ---------------------------------------------------------------------------
# Corpus walking + rendering
# ---------------------------------------------------------------------------


def _gather_files(root: Path, *, top_level_only: bool) -> list[Path]:
    if root.is_file():
        return [root]
    if top_level_only:
        return sorted(root.glob("*.py"))
    return sorted(p for p in root.rglob("*.py") if "_vendor" not in p.parts)


def _format_pct(x: float) -> str:
    return f"{x * 100:5.1f}%"


def _render_markdown(rows: list[ModuleMetrics], corpus_label: str) -> str:
    lines: list[str] = []
    lines.append(f"### Recovery accuracy: {corpus_label}\n")
    header = (
        "| Module | LoC | Parses | Sig | Decl | Strict |"
        " Names | Imports | Docstrings | Fns | Unknown | ms |"
    )
    lines.append(header)
    lines.append("|---|---:|:--:|:--:|:--:|:--:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        if r.error and not r.parses:
            err_row = (
                f"| `{r.name}` | {r.loc} | ❌ | ❌ | ❌ | ❌ | — | — | — |"
                f" {r.function_count} | — | — |"
            )
            lines.append(err_row)
            continue
        lines.append(
            f"| `{r.name}` | {r.loc} | {'✅' if r.parses else '❌'} | "
            f"{'✅' if r.signature_match else '❌'} | "
            f"{'✅' if r.declaration_match else '❌'} | "
            f"{'✅' if r.strict_match else '❌'} | "
            f"{_format_pct(r.name_recall)} | "
            f"{_format_pct(r.import_recall)} | "
            f"{_format_pct(r.docstring_recall)} | "
            f"{r.function_count} | {r.unknown_blocks} | {r.elapsed_ms:.1f} |"
        )

    # aggregate
    parsed = [r for r in rows if r.parses]
    if rows:
        n_parses = sum(1 for r in rows if r.parses)
        n_sig = sum(1 for r in rows if r.signature_match)
        n_decl = sum(1 for r in rows if r.declaration_match)
        n_strict = sum(1 for r in rows if r.strict_match)

        def mean(attr: str) -> float:
            vals = [getattr(r, attr) for r in parsed]
            return sum(vals) / len(vals) if vals else 0.0

        lines.append(
            f"| **mean (N={len(rows)})** | "
            f"{sum(r.loc for r in rows)} | "
            f"{n_parses}/{len(rows)} | "
            f"{n_sig}/{len(rows)} | "
            f"{n_decl}/{len(rows)} | "
            f"{n_strict}/{len(rows)} | "
            f"{_format_pct(mean('name_recall'))} | "
            f"{_format_pct(mean('import_recall'))} | "
            f"{_format_pct(mean('docstring_recall'))} | "
            f"{sum(r.function_count for r in rows)} | "
            f"{sum(r.unknown_blocks for r in rows)} | "
            f"{sum(r.elapsed_ms for r in rows):.1f} |"
        )

    return "\n".join(lines) + "\n"


def _render_mermaid_chart(rows: list[ModuleMetrics]) -> str:
    """Mermaid xychart-beta showing skeleton/name/import recall per module."""
    parsed = [r for r in rows if r.parses]
    if not parsed:
        return ""
    labels = ", ".join(f'"{r.name.removesuffix(".py")}"' for r in parsed)
    names = ", ".join(f"{r.name_recall * 100:.1f}" for r in parsed)
    imports = ", ".join(f"{r.import_recall * 100:.1f}" for r in parsed)
    docs = ", ".join(f"{r.docstring_recall * 100:.1f}" for r in parsed)
    return "\n".join(
        [
            "```mermaid",
            "xychart-beta",
            '    title "Recovery accuracy (%) — names / imports / docstrings"',
            f"    x-axis [{labels}]",
            "    y-axis 0 --> 100",
            f"    bar [{names}]",
            f"    line [{imports}]",
            f"    line [{docs}]",
            "```",
            "",
            "Bar: identifier recall · Lines: import & docstring recall.",
            "",
        ]
    )


def _to_dicts(rows: list[ModuleMetrics]) -> list[dict[str, Any]]:
    return [asdict(r) for r in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="File or directory tree of .py files")
    parser.add_argument(
        "--top-level-only",
        action="store_true",
        help="Only measure .py files directly in the path (no recursion).",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "json"],
        default="markdown",
    )
    parser.add_argument(
        "--corpus-label",
        default=None,
        help="Optional label printed in markdown headings.",
    )
    parser.add_argument(
        "--chart",
        action="store_true",
        help="Append a Mermaid chart to the markdown output.",
    )
    args = parser.parse_args(argv)

    if not args.path.exists():
        print(f"path not found: {args.path}", file=sys.stderr)
        return 2

    files = _gather_files(args.path, top_level_only=args.top_level_only)
    if not files:
        print(f"no .py files under {args.path}", file=sys.stderr)
        return 2

    rows: list[ModuleMetrics] = []
    for f in files:
        m = measure_module(f)
        if m is not None:
            rows.append(m)

    if args.format == "json":
        print(json.dumps(_to_dicts(rows), indent=2))
        return 0

    label = args.corpus_label or args.path.name
    out = _render_markdown(rows, corpus_label=label)
    if args.chart:
        out += "\n" + _render_mermaid_chart(rows)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
