from pathlib import Path

import pytest

# Sample sources used by parametrised tests in ``tests/test_decompile.py``
# and other places that want a representative Python module to compile +
# decompile. Kept intentionally tiny — these are smoke fixtures, not
# functional benchmarks (the benchmark corpora live under
# ``tools/build_corpora.py``).
_SAMPLE_SOURCES: dict[str, str] = {
    "variables.py": 'x = 1\ny = "hello"\nz = [1, 2, 3]\n',
    "imports.py": ("import os\nfrom os.path import join\n\n__all__ = ['join']\n"),
    "functions.py": (
        "def add(a, b=1):\n"
        "    return a + b\n"
        "\n"
        "def greet(name: str) -> str:\n"
        '    return f"hi {name}"\n'
    ),
    "classes.py": (
        "class Animal:\n"
        '    """An animal."""\n'
        '    kind: str = "unknown"\n'
        "    def speak(self):\n"
        '        return "..."\n'
    ),
    "exceptions.py": ("try:\n    x = 1 / 0\nexcept ZeroDivisionError:\n    x = 0\n"),
    "loops.py": (
        "result = [i * 2 for i in range(10)]\nfor x in result:\n    print(x)\n"
    ),
    "decorators.py": (
        "from functools import lru_cache\n"
        "\n"
        "@lru_cache\n"
        "def fib(n):\n"
        "    if n < 2:\n"
        "        return n\n"
        "    return fib(n - 1) + fib(n - 2)\n"
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
