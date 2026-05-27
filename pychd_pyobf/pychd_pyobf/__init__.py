"""pychd_pyobf — anonymise identifiers / constants / metadata in a .pyc.

Public API:

* :func:`obfuscate` — main entry point: ``obfuscate(in_path, out_path)``
  rewrites a .pyc in place and returns an :class:`ObfuscationReport`.
* :class:`ObfuscationReport` — the report dataclass (paths, writer
  version, identifier mapping, native vs subprocess flag).
* :class:`ObfuscationMapping` — the original→anonymised name table
  the report carries.

The CLI entry point is :func:`pychd_pyobf.cli.main` (registered as the
``pychd-pyobf`` console script).
"""

from __future__ import annotations

from .dispatch import ObfuscationReport, obfuscate
from .rewrite_native import ObfuscationMapping

__version__ = "0.1.0"

__all__ = [
    "ObfuscationMapping",
    "ObfuscationReport",
    "__version__",
    "obfuscate",
]
