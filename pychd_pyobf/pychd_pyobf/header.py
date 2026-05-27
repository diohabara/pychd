"""CPython ``.pyc`` header parsing + reconstruction.

CPython has used two header layouts across the 3.x line:

* **3.0 – 3.6** (12-byte header): ``magic (4) | timestamp (4) | source_size (4)``.
* **3.7+** (PEP 552, 16-byte header):
  ``magic (4) | bit_field (4) | timestamp-or-hash (8) | source_size (8 if hash mode)``
  Concretely the layout is still 16 bytes total — the ``bit_field``
  decides whether the next 8 bytes are timestamp-based (timestamp(4) +
  source_size(4)) or hash-based (8-byte hash).

We reuse :func:`pychd.versions.read_magic` / :func:`pychd.versions.detect_version`
to identify the writer. ``header_length_for(version)`` then tells us
where the marshalled code object begins; ``split_pyc(pyc)`` returns
``(header_bytes, body_bytes)``.

We deliberately do not parse the bit_field — the obfuscator preserves
the original header verbatim, so re-serialising the rewritten code
object just needs to concatenate the original bytes with the new body.
The only field we ever consider rewriting is ``source_size``, which we
zero out (no source on disk for an anonymised .pyc), but only when the
writer is 3.7+ where that field is unambiguous.
"""

from __future__ import annotations

from pathlib import Path

from pychd.versions import VersionInfo, detect_version


def header_length_for(version: VersionInfo) -> int:
    """Return the byte length of the .pyc header for *version*'s writer.

    3.7 introduced the 16-byte PEP 552 header. Everything before that
    used a 12-byte layout (magic + timestamp + source_size, each 4
    bytes little-endian).
    """
    if version.version >= (3, 7):
        return 16
    return 12


def split_pyc(pyc_path: Path) -> tuple[VersionInfo, bytes, bytes]:
    """Read *pyc_path* and return (version, header_bytes, body_bytes).

    The body is the marshalled top-level code object, ready to feed
    into :func:`marshal.loads` under the writer's Python interpreter.
    """
    data = pyc_path.read_bytes()
    version = detect_version(pyc_path)
    hlen = header_length_for(version)
    if len(data) < hlen:
        raise ValueError(
            f"{pyc_path}: truncated — only {len(data)} bytes but expected"
            f" at least {hlen} for Python {version.version[0]}."
            f"{version.version[1]}",
        )
    return version, data[:hlen], data[hlen:]


def merge_pyc(header: bytes, body: bytes) -> bytes:
    """Reassemble a .pyc from its (header, body) pair.

    This is a thin wrapper that exists so callers can match the
    :func:`split_pyc` mental model rather than concatenating raw
    bytes.
    """
    return header + body


__all__ = ["header_length_for", "split_pyc", "merge_pyc"]
