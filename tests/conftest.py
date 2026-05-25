"""Shared test fixtures for the pychd test suite."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

# Tiny sample sources used by parametrised tests in
# ``tests/test_decompile.py`` and other suites that need a
# representative Python module to compile + decompile. Kept
# intentionally small — these are smoke fixtures, not benchmark
# corpora (those live in ``tools/build_corpora.py``).
_SAMPLE_SOURCES: dict[str, str] = {
    "variables.py": textwrap.dedent(
        """\
        x = 1
        y = "hello"
        z = [1, 2, 3]
        """
    ),
    "imports.py": textwrap.dedent(
        """\
        import os
        from os.path import join

        __all__ = ['join']
        """
    ),
    "functions.py": textwrap.dedent(
        """\
        def add(a, b=1):
            return a + b

        def greet(name: str) -> str:
            return f"hi {name}"
        """
    ),
    "classes.py": textwrap.dedent(
        '''\
        class Animal:
            """An animal."""
            kind: str = "unknown"
            def speak(self):
                return "..."
        '''
    ),
    "exceptions.py": textwrap.dedent(
        """\
        try:
            x = 1 / 0
        except ZeroDivisionError:
            x = 0
        """
    ),
    "loops.py": textwrap.dedent(
        """\
        result = [i * 2 for i in range(10)]
        for x in result:
            print(x)
        """
    ),
    "decorators.py": textwrap.dedent(
        """\
        from functools import lru_cache

        @lru_cache
        def fib(n):
            if n < 2:
                return n
            return fib(n - 1) + fib(n - 2)
        """
    ),
}


@pytest.fixture(
    params=list(_SAMPLE_SOURCES.keys()),
    ids=list(_SAMPLE_SOURCES.keys()),
)
def example_py(request, tmp_path: Path) -> Path:
    name = request.param
    src = _SAMPLE_SOURCES[name]
    path = tmp_path / name
    path.write_text(src)
    return path
