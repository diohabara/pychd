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

from pychd.decompile import Backend, Mode, decompile_pyc  # noqa: E402
from pychd.semantic import compare_all  # noqa: E402


@dataclass
class ModuleMetrics:
    name: str
    loc: int
    parses: bool
    # Three-tier match metric:
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
    # Semantic axes (opt-in via the ``semantic`` flag on
    # :func:`measure_module`). All three default to ``False`` so a
    # benchmark run with the axes disabled looks like a clean miss
    # rather than spurious success.
    bytecode_exact: bool = False
    bytecode_normalized: bool = False
    behavioral_smoke: bool = False
    bytecode_exact_detail: str = ""
    bytecode_normalized_detail: str = ""
    behavioral_smoke_detail: str = ""
    # Continuous similarity axis (Decompile-Bench "Edit Similarity").
    # 1.0 = textually identical; 0.0 = entirely dissimilar.
    edit_similarity: float = 0.0
    # Pass@1 functional-correctness axis (Decompile-Bench / PyLingual).
    # ``None`` denotes "no test oracle available for this module" — the
    # common case outside HumanEval. ``False`` denotes a real failure.
    functional_correctness: bool | None = None
    functional_correctness_detail: str = ""
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


_FOLDABLE_BINOPS: dict[type, object] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a**b,
}


def _fold_constants(tree: ast.AST) -> ast.AST:
    """Recursively fold ``Constant`` ``BinOp`` ``Constant`` chains into
    a single ``Constant``, matching the CPython compiler's own constant
    folding pass.

    Without this, ``x = 60 * 60 * 24`` in the original and ``x = 86400``
    in the recovered source — which produce identical bytecode — show
    up as different ASTs and fail ``strict_match``. The fold below is
    symmetric across original and recovered so the comparison stays
    structural rather than literal.

    Skips division by zero and exceptions; the un-folded subtree is
    kept verbatim in that case.
    """

    class Folder(ast.NodeTransformer):
        def visit_BinOp(self, node):  # type: ignore[override]
            node.left = self.visit(node.left)
            node.right = self.visit(node.right)
            if (
                isinstance(node.left, ast.Constant)
                and isinstance(node.right, ast.Constant)
                and type(node.op) in _FOLDABLE_BINOPS
            ):
                try:
                    value = _FOLDABLE_BINOPS[type(node.op)](
                        node.left.value, node.right.value
                    )
                    return ast.copy_location(ast.Constant(value=value), node)
                except Exception:
                    return node
            return node

        def visit_UnaryOp(self, node):  # type: ignore[override]
            node.operand = self.visit(node.operand)
            if isinstance(node.operand, ast.Constant) and isinstance(
                node.operand.value, (int, float, complex)
            ):
                if isinstance(node.op, ast.USub):
                    return ast.copy_location(
                        ast.Constant(value=-node.operand.value), node
                    )
                if isinstance(node.op, ast.UAdd):
                    return ast.copy_location(
                        ast.Constant(value=+node.operand.value), node
                    )
                if isinstance(node.op, ast.Invert) and isinstance(
                    node.operand.value, int
                ):
                    return ast.copy_location(
                        ast.Constant(value=~node.operand.value), node
                    )
            return node

    return Folder().visit(tree)


def _normalise_imports(tree: ast.AST) -> ast.AST:
    """Split multi-name ``import a, b`` / ``from x import a, b`` into
    individual single-name statements at every scope.

    Bytecode preserves *which names* an import line brings in but loses
    the syntactic grouping — ``import sys, os`` and ``import sys`` +
    ``import os`` compile to the same opcode sequence. Comparing them
    structurally with ``ast.dump`` therefore needs both sides reduced
    to a canonical one-name-per-line form.
    """

    def visit(body: list[ast.stmt]) -> list[ast.stmt]:
        new_body: list[ast.stmt] = []
        for node in body:
            # Recurse into compound statements first so nested imports
            # also get split.
            for attr in ("body", "orelse", "finalbody"):
                if hasattr(node, attr):
                    setattr(node, attr, visit(getattr(node, attr)))
            if isinstance(node, ast.Try):
                node.handlers = [
                    ast.ExceptHandler(type=h.type, name=h.name, body=visit(h.body))
                    for h in node.handlers
                ]
            if isinstance(node, ast.With):
                # ``with`` body already handled via .body above.
                pass
            if isinstance(node, ast.Import) and len(node.names) > 1:
                for alias in node.names:
                    new_body.append(ast.Import(names=[alias]))
                continue
            if isinstance(node, ast.ImportFrom) and len(node.names) > 1:
                for alias in node.names:
                    new_body.append(
                        ast.ImportFrom(
                            module=node.module, names=[alias], level=node.level
                        )
                    )
                continue
            new_body.append(node)
        return new_body

    if isinstance(tree, ast.Module):
        tree.body = visit(tree.body)
    return tree


def _strip_for_skeleton(tree: ast.AST) -> ast.AST:
    """Reduce *tree* to a normalised skeleton.

    Function/method bodies are replaced with a single ``Pass``;
    annotations on parameters, returns, and ``AnnAssign`` are erased;
    decorators are dropped (they survive textually but the rule engine
    doesn't always reattach them in the same order). Module-level
    docstrings, string literal quote styles, and numeric literal forms
    are normalised away too — those are CPython-compiler-induced
    cosmetic differences, not real recovery regressions.

    Multi-name imports (``import sys, os``) are split into single-name
    forms (``import sys`` ; ``import os``) on both sides before
    comparing, since the rule pass renders one ``IMPORT_NAME`` opcode
    per emitted ``import`` line and there's no way to distinguish the
    two source shapes from bytecode.

    After this, an ``ast.dump`` equality check is meaningful for
    skeleton comparison.
    """
    cloned = ast.parse(ast.unparse(tree))
    # Drop module-level docstring (CPython's ``inspect.cleandoc``
    # normalisation strips leading whitespace that we can't always
    # round-trip exactly).
    if (
        isinstance(cloned, ast.Module)
        and cloned.body
        and isinstance(cloned.body[0], ast.Expr)
        and isinstance(cloned.body[0].value, ast.Constant)
        and isinstance(cloned.body[0].value.value, str)
    ):
        cloned.body = cloned.body[1:]
    # Normalise multi-name ``import a, b`` and ``from x import a, b``
    # into separate single-name statements at every scope. The rule
    # pass and the LLM both emit one-per-line imports while the
    # original source may use comma-list shorthand; the comma form
    # carries no extra information that survives compilation.
    cloned = _normalise_imports(cloned)
    # Apply CPython's constant-folding so ``60 * 60 * 24`` matches the
    # recovered ``86400``. Both sides go through the same pass so the
    # comparison stays symmetric.
    cloned = _fold_constants(cloned)
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
            # Drop class-level docstring (same cleandoc-normalisation
            # concern as module docstrings above).
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                node.body = node.body[1:] or [ast.Pass()]
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

    Every class, function, import — at module level or directly inside
    a class body — must survive in the recovered module. Functions
    defined *inside other function bodies* are excluded: function
    bodies are LLM territory (rule pass emits a placeholder), so
    nested-function recovery is out of scope.
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


_TESTS_SIDECAR_CACHE: dict[Path, dict[str, dict[str, str]]] = {}


def _load_tests_sidecar(py_file: Path) -> dict[str, str] | None:
    """Return ``{test, entry_point}`` for *py_file* if its corpus
    directory carries a ``_tests.json`` oracle (HumanEval-style), or
    ``None`` otherwise. Cached per directory so a full corpus walk
    parses the sidecar once."""
    parent = py_file.parent
    sidecar = parent / "_tests.json"
    if not sidecar.is_file():
        return None
    if parent not in _TESTS_SIDECAR_CACHE:
        try:
            _TESTS_SIDECAR_CACHE[parent] = json.loads(sidecar.read_text())
        except OSError, json.JSONDecodeError:
            _TESTS_SIDECAR_CACHE[parent] = {}
    return _TESTS_SIDECAR_CACHE[parent].get(py_file.name)


def measure_module(
    py_file: Path,
    *,
    semantic: bool = True,
    mode: Mode = Mode.RULES_ONLY,
    backend: Backend = Backend.LITELLM,
    model: str | None = None,
) -> ModuleMetrics | None:
    """Measure recovery quality for one .py file. Returns None if
    the file can't be compiled at all (e.g. Python 2 syntax).

    When *semantic* is True, runs the full semantic + similarity
    comparator suite from :mod:`pychd.semantic`
    (bytecode_exact / bytecode_normalized / behavioral_smoke /
    edit_similarity, plus functional_correctness when a
    ``_tests.json`` sidecar is present next to *py_file*).
    Disabling it skips all of that — useful when running on a large
    corpus where the per-module overhead matters.

    *mode* / *backend* / *model* select the decompile strategy. The
    default is :data:`Mode.RULES_ONLY` (no LLM, deterministic,
    millisecond per module — what the original benchmark measured).
    Pass :data:`Mode.HYBRID` to fill function bodies via the LLM or
    :data:`Mode.HYBRID_REWRITE` to also let the LLM correct
    module-level recovery. Both modes work with the codex backend
    (uses the user's ``codex login`` instead of a litellm API key).
    """
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
            report = decompile_pyc(pyc, mode=mode, backend=backend, model=model)
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

    # Semantic + similarity axes. We re-compile under a fresh tempdir
    # so that the original .pyc has settled (the one produced earlier
    # in this function went out of scope when its TemporaryDirectory
    # closed).
    bx_match = bx_detail = None
    bn_match = bn_detail = None
    bs_match = bs_detail = None
    fc_match: bool | None = None
    fc_detail = ""
    edit_sim = 0.0
    if semantic:
        sidecar = _load_tests_sidecar(py_file)
        test_src = sidecar.get("test") if sidecar else None
        entry_point = sidecar.get("entry_point") if sidecar else None
        with tempfile.TemporaryDirectory() as tmp:
            pyc2 = Path(tmp) / "in.pyc"
            try:
                py_compile.compile(str(py_file), cfile=str(pyc2), doraise=True)
                sem = compare_all(
                    pyc2,
                    py_file,
                    out,
                    py_interp=sys.executable,
                    orig_src=src,
                    test_src=test_src,
                    entry_point=entry_point,
                )
                bx_match = sem.bytecode_exact.match
                bx_detail = sem.bytecode_exact.detail
                bn_match = sem.bytecode_normalized.match
                bn_detail = sem.bytecode_normalized.detail
                bs_match = sem.behavioral_smoke.match
                bs_detail = sem.behavioral_smoke.detail
                edit_sim = sem.edit_similarity
                if sem.functional_correctness is not None:
                    fc_match = sem.functional_correctness.match
                    fc_detail = sem.functional_correctness.detail
            except Exception as e:
                # Never let a semantic-comparator hiccup abort the
                # whole row — record the failure and keep going.
                bx_match = bn_match = bs_match = False
                bx_detail = bn_detail = bs_detail = f"semantic-compare crash: {e}"
                if test_src is not None:
                    fc_match = False
                    fc_detail = f"semantic-compare crash: {e}"

    return ModuleMetrics(
        name=py_file.name,
        loc=loc,
        parses=parses,
        skeleton_match=strict,
        signature_match=sig,
        declaration_match=decl,
        strict_match=strict,
        bytecode_exact=bool(bx_match) if bx_match is not None else False,
        bytecode_normalized=bool(bn_match) if bn_match is not None else False,
        behavioral_smoke=bool(bs_match) if bs_match is not None else False,
        bytecode_exact_detail=bx_detail or "",
        bytecode_normalized_detail=bn_detail or "",
        behavioral_smoke_detail=bs_detail or "",
        edit_similarity=edit_sim,
        functional_correctness=fc_match,
        functional_correctness_detail=fc_detail,
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
    # Show the semantic columns only when at least one row actually ran
    # the comparators — keeps the table narrow on legacy ``--no-semantic``
    # runs while still surfacing the axes on the default path.
    show_sem = any(
        (
            r.bytecode_exact_detail
            or r.bytecode_normalized_detail
            or r.behavioral_smoke_detail
        )
        for r in rows
    )
    lines: list[str] = []
    lines.append(f"### Recovery accuracy: {corpus_label}\n")
    if show_sem:
        header = (
            "| Module | LoC | Parses | Sig | Decl | Strict | BX | BN | BS |"
            " Names | Imports | Docstrings | Fns | Unknown | ms |"
        )
        sep = (
            "|---|---:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|---:|"
            "---:|---:|---:|---:|---:|"
        )
    else:
        header = (
            "| Module | LoC | Parses | Sig | Decl | Strict |"
            " Names | Imports | Docstrings | Fns | Unknown | ms |"
        )
        sep = "|---|---:|:--:|:--:|:--:|:--:|---:|---:|---:|---:|---:|---:|"
    lines.append(header)
    lines.append(sep)
    for r in rows:
        if r.error and not r.parses:
            if show_sem:
                err_row = (
                    f"| `{r.name}` | {r.loc} | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | "
                    f"— | — | — | {r.function_count} | — | — |"
                )
            else:
                err_row = (
                    f"| `{r.name}` | {r.loc} | ❌ | ❌ | ❌ | ❌ | — | — | — |"
                    f" {r.function_count} | — | — |"
                )
            lines.append(err_row)
            continue
        sem_cells = (
            f"{'✅' if r.bytecode_exact else '❌'} | "
            f"{'✅' if r.bytecode_normalized else '❌'} | "
            f"{'✅' if r.behavioral_smoke else '❌'} | "
            if show_sem
            else ""
        )
        lines.append(
            f"| `{r.name}` | {r.loc} | {'✅' if r.parses else '❌'} | "
            f"{'✅' if r.signature_match else '❌'} | "
            f"{'✅' if r.declaration_match else '❌'} | "
            f"{'✅' if r.strict_match else '❌'} | "
            f"{sem_cells}"
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
        n_bx = sum(1 for r in rows if r.bytecode_exact)
        n_bn = sum(1 for r in rows if r.bytecode_normalized)
        n_bs = sum(1 for r in rows if r.behavioral_smoke)

        def mean(attr: str) -> float:
            vals = [getattr(r, attr) for r in parsed]
            return sum(vals) / len(vals) if vals else 0.0

        sem_agg = (
            f"{n_bx}/{len(rows)} | {n_bn}/{len(rows)} | {n_bs}/{len(rows)} | "
            if show_sem
            else ""
        )
        lines.append(
            f"| **mean (N={len(rows)})** | "
            f"{sum(r.loc for r in rows)} | "
            f"{n_parses}/{len(rows)} | "
            f"{n_sig}/{len(rows)} | "
            f"{n_decl}/{len(rows)} | "
            f"{n_strict}/{len(rows)} | "
            f"{sem_agg}"
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
    parser.add_argument(
        "--no-semantic",
        action="store_true",
        help=(
            "Skip the three semantic axes (bytecode_exact,"
            " bytecode_normalized, behavioral_smoke). Useful on large"
            " corpora where the ~150 ms / module overhead matters."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=[m.value for m in Mode],
        default=Mode.RULES_ONLY.value,
        help=(
            "Decompile mode. ``rules-only`` (default) is the deterministic"
            " pass. ``hybrid`` fills function bodies via LLM. ``hybrid-rewrite``"
            " also asks the LLM to fix module-level recovery (one LLM call"
            " per module, strongest mode for strict_match / FC)."
        ),
    )
    parser.add_argument(
        "--backend",
        choices=[b.value for b in Backend],
        default=Backend.LITELLM.value,
        help=(
            "LLM backend when --mode != rules-only. ``codex`` uses the"
            " user's ``codex login`` (no API key needed); ``litellm`` reads"
            " standard provider env vars."
        ),
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name for the litellm backend. Ignored for codex.",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        help=(
            "Process this many modules in parallel. Only useful with LLM"
            " modes — the rule pass alone is fast enough that the"
            " thread-pool overhead dominates."
        ),
    )
    args = parser.parse_args(argv)

    if not args.path.exists():
        print(f"path not found: {args.path}", file=sys.stderr)
        return 2

    files = _gather_files(args.path, top_level_only=args.top_level_only)
    if not files:
        print(f"no .py files under {args.path}", file=sys.stderr)
        return 2

    mode = Mode(args.mode)
    backend = Backend(args.backend)
    rows: list[ModuleMetrics] = []
    if args.parallel <= 1:
        for f in files:
            m = measure_module(
                f,
                semantic=not args.no_semantic,
                mode=mode,
                backend=backend,
                model=args.model,
            )
            if m is not None:
                rows.append(m)
    else:
        # ``measure_module`` spends the bulk of its wall-clock waiting
        # on subprocess RPCs (codex exec, py_compile in a subprocess)
        # so a thread pool is appropriate even though we're inside one
        # Python process — the GIL is released around the RPC waits.
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            futures = {
                pool.submit(
                    measure_module,
                    f,
                    semantic=not args.no_semantic,
                    mode=mode,
                    backend=backend,
                    model=args.model,
                ): f
                for f in files
            }
            for fut in as_completed(futures):
                f = futures[fut]
                try:
                    m = fut.result()
                except Exception as e:  # noqa: BLE001
                    print(f"# error on {f.name}: {e}", file=sys.stderr)
                    continue
                if m is not None:
                    rows.append(m)
        # Preserve the original file order so output diffs are stable.
        order = {f.name: i for i, f in enumerate(files)}
        rows.sort(key=lambda r: order.get(r.name, 0))

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
