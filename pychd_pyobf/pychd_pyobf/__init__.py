"""pychd_pyobf — anonymise identifiers / constants / metadata in a .pyc.

Public API (filled in by Phase C):

* ``obfuscate(in_path, out_path)``   — main entry point
* ``ObfuscationReport``              — dataclass holding mapping + stats
* ``rewrite_native``                 — 3.14 native rewriter
* ``rewrite_subprocess``             — 3.0–3.13 cross-version rewriter

The CLI entry point is ``pychd_pyobf.cli:main`` (registered as the
``pychd-pyobf`` console script).
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
