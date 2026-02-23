import ast
import logging
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ValidationResult:
    match: bool
    details: str


def normalize_ast(source: str) -> ast.AST:
    """Parse source and strip line numbers, col offsets, and type comments."""
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
    return tree


def compare_ast(source_a: str, source_b: str) -> ValidationResult:
    """Compare two Python source strings by their normalised ASTs."""
    try:
        tree_a = normalize_ast(source_a)
    except SyntaxError as e:
        return ValidationResult(match=False, details=f"Failed to parse source A: {e}")
    try:
        tree_b = normalize_ast(source_b)
    except SyntaxError as e:
        return ValidationResult(match=False, details=f"Failed to parse source B: {e}")

    dump_a = ast.dump(tree_a)
    dump_b = ast.dump(tree_b)
    if dump_a == dump_b:
        return ValidationResult(match=True, details="ASTs match")
    return ValidationResult(match=False, details="ASTs differ")


def validate(original: Path, decompiled: Path) -> ValidationResult:
    """Validate a single pair of original and decompiled files."""
    src_a = original.read_text()
    src_b = decompiled.read_text()
    return compare_ast(src_a, src_b)


def validate_directory(
    orig_dir: Path, decomp_dir: Path
) -> list[tuple[str, ValidationResult]]:
    """Compare .py files in orig_dir against corresponding files in decomp_dir."""
    results = []
    for orig_file in sorted(orig_dir.glob("*.py")):
        decomp_file = decomp_dir / orig_file.name
        if not decomp_file.exists():
            details = f"Missing decompiled file: {decomp_file.name}"
            results.append(
                (
                    orig_file.name,
                    ValidationResult(match=False, details=details),
                )
            )
            continue
        result = validate(orig_file, decomp_file)
        status = "MATCH" if result.match else "DIFFER"
        logging.info(f"{orig_file.name}: {status} - {result.details}")
        results.append((orig_file.name, result))
    return results
