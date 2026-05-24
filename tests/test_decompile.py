import ast
import py_compile
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from pychd.decompile import (
    Mode,
    decompile,
    decompile_disassembled_pyc,
    decompile_pyc,
    disassemble_pyc_file,
)


class TestDisassemble:
    def test_disassemble_produces_output(self, example_py):
        """Compile an example .py then disassemble the .pyc."""
        with tempfile.TemporaryDirectory() as tmp:
            pyc = Path(tmp) / "output.pyc"
            py_compile.compile(str(example_py), cfile=str(pyc))
            text, version_tuple, code = disassemble_pyc_file(pyc)
            assert len(text) > 0
            assert isinstance(version_tuple, tuple)
            assert len(version_tuple) >= 2
            # On the current interpreter we also get a native code object.
            assert code is not None


class TestLLMOnlyDecompile:
    def test_decompile_returns_mocked_source(self):
        mock_response = MagicMock()
        mock_response.choices[0].message.content = "x = 42"
        with patch("pychd.decompile.completion", return_value=mock_response):
            result = decompile_disassembled_pyc("LOAD_CONST 0", (3, 14), "gpt-4")
            assert result == "x = 42"

    def test_decompile_end_to_end_mocked(self, example_py):
        """Full LLM-only pipeline: compile -> decompile (mocked LLM)."""
        with tempfile.TemporaryDirectory() as tmp:
            pyc = Path(tmp) / "output.pyc"
            py_compile.compile(str(example_py), cfile=str(pyc))
            out = Path(tmp) / "result.py"
            mock_response = MagicMock()
            mock_response.choices[0].message.content = "# decompiled"
            with patch("pychd.decompile.completion", return_value=mock_response):
                decompile(
                    to_decompile=pyc,
                    output_path=out,
                    model="gpt-4",
                    mode=Mode.LLM_ONLY,
                )
            assert out.exists()
            assert out.read_text() == "# decompiled"


class TestRulesOnlyDecompile:
    def test_rules_only_produces_valid_python(self, example_py):
        with tempfile.TemporaryDirectory() as tmp:
            pyc = Path(tmp) / "output.pyc"
            py_compile.compile(str(example_py), cfile=str(pyc))
            report = decompile_pyc(pyc, mode=Mode.RULES_ONLY)
            assert report.mode == Mode.RULES_ONLY
            # The result must at least parse as Python.
            ast.parse(report.source)

    def test_rules_only_does_not_call_llm(self, example_py):
        with tempfile.TemporaryDirectory() as tmp:
            pyc = Path(tmp) / "output.pyc"
            py_compile.compile(str(example_py), cfile=str(pyc))
            with patch("pychd.decompile.completion") as mock_completion:
                decompile_pyc(pyc, mode=Mode.RULES_ONLY)
                assert mock_completion.call_count == 0


class TestHybridDecompile:
    def test_hybrid_invokes_llm_only_for_unknown_bodies(self):
        src = '''"""Doc."""
from typing import Any

X = 1


def foo(a, b):
    return a + b


def bar(x):
    return x * 2
'''
        with tempfile.TemporaryDirectory() as tmp:
            py_path = Path(tmp) / "src.py"
            py_path.write_text(src)
            pyc = Path(tmp) / "out.pyc"
            py_compile.compile(str(py_path), cfile=str(pyc))
            mock_response = MagicMock()
            mock_response.choices[0].message.content = "    return a + b"
            with patch(
                "pychd.decompile.completion", return_value=mock_response
            ) as mock_completion:
                report = decompile_pyc(pyc, mode=Mode.HYBRID, model="gpt-4")
                # Exactly two function bodies → exactly two LLM calls.
                assert mock_completion.call_count == 2
                assert report.llm_calls == 2
            # Result still parses.
            ast.parse(report.source)
            # Top-level skeleton came from rules, not the LLM.
            assert '"""Doc."""' in report.source
            assert "X = 1" in report.source

    def test_hybrid_no_llm_when_module_is_pure_skeleton(self):
        src = '''"""Re-exports."""
from os.path import join, exists

__all__ = ["join", "exists"]
'''
        with tempfile.TemporaryDirectory() as tmp:
            py_path = Path(tmp) / "src.py"
            py_path.write_text(src)
            pyc = Path(tmp) / "out.pyc"
            py_compile.compile(str(py_path), cfile=str(pyc))
            with patch("pychd.decompile.completion") as mock_completion:
                report = decompile_pyc(pyc, mode=Mode.HYBRID, model="gpt-4")
                assert mock_completion.call_count == 0
                assert report.llm_calls == 0
                assert report.unknown_blocks == 0


class TestDirectoryDecompile:
    def test_decompile_directory_rules_only(self, tmp_path):
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "a.py").write_text('"""A."""\nfrom os import path\n')
        (src_dir / "b.py").write_text('"""B."""\nx = 1\n')
        out_dir = tmp_path / "out"
        decompile(
            to_decompile=src_dir,
            output_path=out_dir,
            model=None,
            mode=Mode.RULES_ONLY,
        )
        assert (out_dir / "a.py").exists()
        assert (out_dir / "b.py").exists()
        # Both outputs parse as Python.
        ast.parse((out_dir / "a.py").read_text())
        ast.parse((out_dir / "b.py").read_text())
