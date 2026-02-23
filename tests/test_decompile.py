import py_compile
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from pychd.decompile import decompile, decompile_disassembled_pyc, disassemble_pyc_file


class TestDisassemble:
    def test_disassemble_produces_output(self, example_py):
        """Compile an example .py then disassemble the .pyc."""
        with tempfile.TemporaryDirectory() as tmp:
            pyc = Path(tmp) / "output.pyc"
            py_compile.compile(str(example_py), cfile=str(pyc))
            text, version_tuple = disassemble_pyc_file(pyc)
            assert len(text) > 0
            assert isinstance(version_tuple, tuple)
            assert len(version_tuple) >= 2


class TestDecompileWithMockedLLM:
    def test_decompile_returns_mocked_source(self):
        mock_response = MagicMock()
        mock_response.choices[0].message.content = "x = 42"
        with patch("pychd.decompile.completion", return_value=mock_response):
            result = decompile_disassembled_pyc("LOAD_CONST 0", (3, 14), "gpt-4")
            assert result == "x = 42"

    def test_decompile_end_to_end_mocked(self, example_py):
        """Full pipeline: compile -> disassemble -> decompile (mocked LLM)."""
        with tempfile.TemporaryDirectory() as tmp:
            pyc = Path(tmp) / "output.pyc"
            py_compile.compile(str(example_py), cfile=str(pyc))
            out = Path(tmp) / "result.py"
            mock_response = MagicMock()
            mock_response.choices[0].message.content = "# decompiled"
            with patch("pychd.decompile.completion", return_value=mock_response):
                decompile(to_decompile=pyc, output_path=out, model="gpt-4")
            assert out.exists()
            assert out.read_text() == "# decompiled"
