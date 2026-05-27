"""pychd_pyfuzz — random syntactically-valid Python source generator.

Public API:

* :class:`Fuzzer` — construct with a target version + seed, call
  :meth:`Fuzzer.generate` or :meth:`Fuzzer.generate_batch`.
* :class:`Sample` — dataclass returned by ``generate`` carrying the
  source text, the syntactic-feature tags it exercised, the target
  version, the seed, and the batch index.
* :class:`TagSet` — lower-level: the mutable tag accumulator
  builders write into. End users rarely touch this directly.

The CLI entry point is :func:`pychd_pyfuzz.cli.main` (registered as
the ``pychd-pyfuzz`` console script).
"""

from __future__ import annotations

from .generator import Fuzzer, Sample
from .tags import TagSet

__version__ = "0.1.0"

__all__ = ["Fuzzer", "Sample", "TagSet", "__version__"]
