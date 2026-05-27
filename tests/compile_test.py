import tempfile
from pathlib import Path

from pychd.compile import compile


class TestCompileSingleFile:
    def test_compile_creates_pyc(self, example_py):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output.pyc"
            compile(to_compile=example_py, output_path=output)
            assert output.exists()
            assert output.stat().st_size > 0


class TestCompileDirectory:
    def test_compile_dir(self, tmp_path):
        # Create a small .py file in a temp directory
        src = tmp_path / "src"
        src.mkdir()
        (src / "hello.py").write_text("x = 1\n")
        out = tmp_path / "out"
        compile(to_compile=src, output_path=out)
        # compileall creates __pycache__ inside the source dir
        pycache = src / "__pycache__"
        assert pycache.exists()
        pyc_files = list(pycache.glob("*.pyc"))
        assert len(pyc_files) >= 1
