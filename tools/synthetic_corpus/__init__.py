"""Contamination-resistant synthetic corpus for pychd benchmarks.

Every module under this package was written from scratch for the
2026-05-26 pychd evaluation run. The code intentionally does *not*
mirror well-known open-source modules and uses fresh identifiers, so
no LLM trained on pre-2026 GitHub / PyPI / The-Stack has seen the
original ``.py`` source. The pychd hybrid-rewrite pipeline therefore
has to reconstruct identifiers and bodies from disassembly alone.

Each file exercises a distinct bytecode shape. See the per-corpus
breakdown in README §Benchmarks.
"""
