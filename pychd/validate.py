import ast
import logging
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ValidationResult:
    match: bool
    details: str


def normalize_ast(source: str, *, strip_annotations: bool = False) -> ast.AST:
    """Parse source and strip line numbers, col offsets, and type comments.

    When ``strip_annotations`` is True, parameter/return/variable
    annotations are also dropped from both trees, so that two sources
    differing only in their annotations compare equal.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        attrs = (
            "lineno",
            "col_offset",
            "end_lineno",
            "end_col_offset",
            "type_comment",
        )
        for attr in attrs:
            if hasattr(node, attr):
                setattr(node, attr, None)
    if strip_annotations:
        _strip_annotations(tree)
    return tree


def _strip_annotations(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node.returns = None
            for a in (
                list(node.args.args)
                + list(node.args.posonlyargs)
                + list(node.args.kwonlyargs)
            ):
                a.annotation = None
            if node.args.vararg is not None:
                node.args.vararg.annotation = None
            if node.args.kwarg is not None:
                node.args.kwarg.annotation = None
        elif isinstance(node, ast.AnnAssign):
            node.annotation = ast.Name(id="object", ctx=ast.Load())


def compare_ast(
    source_a: str,
    source_b: str,
    *,
    ignore_annotations: bool = False,
) -> ValidationResult:
    """Compare two Python source strings by their normalised ASTs."""
    try:
        tree_a = normalize_ast(source_a, strip_annotations=ignore_annotations)
    except SyntaxError as e:
        return ValidationResult(match=False, details=f"Failed to parse source A: {e}")
    try:
        tree_b = normalize_ast(source_b, strip_annotations=ignore_annotations)
    except SyntaxError as e:
        return ValidationResult(match=False, details=f"Failed to parse source B: {e}")

    dump_a = ast.dump(tree_a)
    dump_b = ast.dump(tree_b)
    if dump_a == dump_b:
        return ValidationResult(match=True, details="ASTs match")
    return ValidationResult(match=False, details="ASTs differ")


def validate(
    original: Path,
    decompiled: Path,
    *,
    ignore_annotations: bool = False,
) -> ValidationResult:
    """Validate a single pair of original and decompiled files."""
    src_a = original.read_text()
    src_b = decompiled.read_text()
    return compare_ast(src_a, src_b, ignore_annotations=ignore_annotations)


def validate_directory(
    orig_dir: Path,
    decomp_dir: Path,
    *,
    ignore_annotations: bool = False,
) -> list[tuple[str, ValidationResult]]:
    """Compare .py files in orig_dir against corresponding files in decomp_dir."""
    results = []
    for orig_file in sorted(orig_dir.rglob("*.py")):
        rel = orig_file.relative_to(orig_dir)
        decomp_file = decomp_dir / rel
        if not decomp_file.exists():
            details = f"Missing decompiled file: {rel}"
            results.append(
                (
                    str(rel),
                    ValidationResult(match=False, details=details),
                )
            )
            continue
        result = validate(orig_file, decomp_file, ignore_annotations=ignore_annotations)
        status = "MATCH" if result.match else "DIFFER"
        logging.info(f"{rel}: {status} - {result.details}")
        results.append((str(rel), result))
    return results
