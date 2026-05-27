"""pychd_pyfuzz — random syntactically-valid Python source generator.

Public API (filled in by Phase B):

* ``Fuzzer(target, seed, ...)``      — main generator class
* ``Sample``                         — dataclass holding source + tags + metadata
* ``MIN_VERSIONS``                   — node-name → minimum Python version table

The CLI entry point is ``pychd_pyfuzz.cli:main`` (registered as the
``pychd-pyfuzz`` console script).
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
