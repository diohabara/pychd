from pathlib import Path

import pytest

EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "example" / "python"


def _example_files():
    return sorted(EXAMPLE_DIR.glob("*.py"))


@pytest.fixture(params=_example_files(), ids=lambda p: p.name)
def example_py(request):
    """Yield each example .py file as a Path."""
    return request.param
