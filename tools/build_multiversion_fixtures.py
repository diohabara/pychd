"""Generate ``.pyc`` fixtures for every locally-installed Python 3.x.

For every Python interpreter ``uv`` can find on disk, compile the
canonical sample source (``tools/multiversion_sample.py``) into a
``.pyc`` and stash it under ``/tmp/pychd-multiversion/``. The
resulting files are used by:

- ``tests/test_versions.py`` — verifies pychd's version-detection
  module identifies each one correctly via the ``.pyc`` magic-number
  header.
- README's *Cross-version support* section — quoted directly to
  illustrate what changes between versions.

Why this script exists
----------------------

CPython's bytecode is **not** stable across releases. The ``.pyc``
header carries a magic number that pychd reads to choose a rule
pass; we want a reproducible way to materialise one ``.pyc`` per
version so we can demonstrate (and test) that detection works.

Usage::

    uv run python tools/build_multiversion_fixtures.py
    uv run python tools/build_multiversion_fixtures.py --versions 3.13 3.14
    uv run python tools/build_multiversion_fixtures.py --root /custom/dir
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_ROOT = Path("/tmp/pychd-multiversion")
ALL_VERSIONS = [
    "3.0",
    "3.1",
    "3.2",
    "3.3",
    "3.4",
    "3.5",
    "3.6",
    "3.7",
    "3.8",
    "3.9",
    "3.10",
    "3.11",
    "3.12",
    "3.13",
    "3.14",
]

# Canonical sample: every recoverable construct we want to show off
# across versions. Kept syntactically compatible with Python 3.6+; the
# script will skip versions whose interpreter can't compile it.
SAMPLE = '''"""Cross-version recovery sample.

This module is compiled by every locally-installed Python 3.x and
stashed as a fixture so pychd's version detection can be tested
against real .pyc files. Most constructs here are intentionally
boring; they exist to stress the rule pass uniformly across versions.
"""

import os.path

VERSION = "1.0"
__all__ = ["greet", "Greeter", "make_greeter"]


class Greeter:
    """A trivial greeter."""

    def __init__(self, name):
        self.name = name

    def greet(self):
        return "Hello, " + self.name + "!"


def greet(name):
    return Greeter(name).greet()


def make_greeter(name):
    return Greeter(name)
'''


def find_python(version: str) -> str | None:
    """Return the absolute path to the ``python<version>`` interpreter, or None."""
    try:
        result = subprocess.run(
            ["uv", "python", "find", version],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError, subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    path = result.stdout.strip().splitlines()
    return path[0] if path else None


def compile_with(python: str, src: Path, dst: Path) -> bool:
    """Compile *src* to *dst* using the given interpreter. Returns success."""
    snippet = (
        "import py_compile; "
        f"py_compile.compile({str(src)!r}, cfile={str(dst)!r}, doraise=True)"
    )
    try:
        result = subprocess.run(
            [python, "-c", snippet],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError, subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--versions", nargs="+", default=ALL_VERSIONS)
    args = ap.parse_args(argv)

    args.root.mkdir(parents=True, exist_ok=True)
    src_path = args.root / "sample.py"
    src_path.write_text(SAMPLE)

    print(f"Sample: {src_path}")
    print(f"Output root: {args.root}\n")
    print(f"{'Version':<10} {'Compiled?':<12} {'Output':<60}")
    print("-" * 82)
    successes = 0
    for v in args.versions:
        py = find_python(v)
        if py is None:
            print(
                f"{v:<10} {'NOT INSTALLED':<12} {'(install via uv python install)':<60}"
            )
            continue
        dst = args.root / f"sample-{v}.pyc"
        if compile_with(py, src_path, dst):
            print(f"{v:<10} {'OK':<12} {str(dst):<60}")
            successes += 1
        else:
            print(
                f"{v:<10} {'FAILED':<12} {'(interpreter could not compile sample)':<60}"
            )
    print()
    print(f"{successes} / {len(args.versions)} versions compiled.")
    return 0 if successes > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())


# Allow ``tests/`` to remove the cached fixture directory if needed.
def reset(root: Path = DEFAULT_ROOT) -> None:
    if root.exists():
        shutil.rmtree(root)
    print(f"removed {root}", file=sys.stderr)
