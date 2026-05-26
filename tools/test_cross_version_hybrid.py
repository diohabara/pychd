"""Test hybrid-rewrite quality across Python versions."""

import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/jio/repo/pychd")
import ast

from pychd.decompile import Backend, Mode, decompile_pyc

original = open("/tmp/pychd-multiversion/sample.py").read()
original_ast = ast.dump(ast.parse(original))

results = []
for v in ["3.8", "3.9", "3.10", "3.11", "3.12", "3.13", "3.14"]:
    pyc = Path(f"/tmp/pychd-multiversion/sample-{v}.pyc")
    if not pyc.is_file():
        results.append((v, "no fixture", None, None))
        continue
    start = time.time()
    try:
        rep = decompile_pyc(pyc, mode=Mode.HYBRID_REWRITE, backend=Backend.CODEX)
        elapsed = time.time() - start
        recovered = rep.source
        try:
            recovered_ast = ast.dump(ast.parse(recovered))
            ast_match = ast.dump(
                ast.parse(ast.unparse(ast.parse(original)))
            ) == ast.dump(ast.parse(ast.unparse(ast.parse(recovered))))
            results.append((v, "ok", elapsed, ast_match))
        except SyntaxError as e:
            results.append((v, f"parse fail: {e}", elapsed, False))
    except Exception as e:
        elapsed = time.time() - start
        results.append((v, f"error: {e}", elapsed, False))

print()
print(f"{'Py ver':<8} {'status':<20} {'elapsed':<10} {'ast_match':<10}")
for v, status, t, m in results:
    et = f"{t:.1f}s" if t else "-"
    print(f"{v:<8} {status[:20]:<20} {et:<10} {m if m is not None else '-'}")
