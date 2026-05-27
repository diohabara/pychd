from unittest.mock import MagicMock, patch

from pychd.decompile import (
    _get_max_input_tokens,
    _split_disassembly,
    decompile_disassembled_pyc,
)


class TestGetMaxInputTokens:
    def test_returns_model_info(self):
        with patch("pychd.decompile.litellm") as mock_litellm:
            mock_litellm.get_model_info.return_value = {"max_input_tokens": 16000}
            assert _get_max_input_tokens("gpt-4") == 16000

    def test_fallback_on_error(self):
        with patch("pychd.decompile.litellm") as mock_litellm:
            mock_litellm.get_model_info.side_effect = Exception("unknown model")
            assert _get_max_input_tokens("unknown-model", default=4096) == 4096


class TestSplitDisassembly:
    def test_single_chunk_when_small(self):
        with patch("pychd.decompile.token_counter", return_value=100):
            chunks = _split_disassembly("small text", max_tokens=1000, model="gpt-4")
            assert len(chunks) == 1
            assert chunks[0] == "small text"

    def test_multiple_chunks_when_large(self):
        blocks = ["block1", "block2", "block3", "block4"]
        text = "\n\n".join(blocks)

        def fake_token_counter(model, text):
            # Each individual block is ~100 tokens, combined they exceed limit
            if "\n\n" in text:
                return len(text.split("\n\n")) * 100
            return 100

        with patch("pychd.decompile.token_counter", side_effect=fake_token_counter):
            chunks = _split_disassembly(text, max_tokens=250, model="gpt-4")
            assert len(chunks) > 1


class TestDecompileWithChunking:
    def test_single_chunk_no_part_info(self):
        with (
            patch("pychd.decompile._get_max_input_tokens", return_value=100000),
            patch("pychd.decompile.token_counter", return_value=100),
            patch("pychd.decompile.completion") as mock_completion,
        ):
            mock_response = MagicMock()
            mock_response.choices[0].message.content = "print('hello')"
            mock_completion.return_value = mock_response
            result = decompile_disassembled_pyc("LOAD_CONST 0", (3, 14), "gpt-4")
            assert result == "print('hello')"
            # Check that "Part" is NOT in the prompt
            call_args = mock_completion.call_args
            prompt = (
                call_args[1]["messages"][0]["content"]
                if "messages" in call_args[1]
                else call_args[0][0]
            )
            assert "Part" not in prompt

    def test_multi_chunk_concatenation(self):
        blocks = "\n\n".join([f"BLOCK_{i}" for i in range(10)])
        call_count = [0]

        def fake_token_counter(model, text):
            return len(text) * 2  # make tokens proportional to length

        def fake_completion(**kwargs):
            call_count[0] += 1
            mock_response = MagicMock()
            mock_response.choices[0].message.content = f"# chunk {call_count[0]}"
            return mock_response

        with (
            patch("pychd.decompile._get_max_input_tokens", return_value=200),
            patch("pychd.decompile.token_counter", side_effect=fake_token_counter),
            patch("pychd.decompile.completion", side_effect=fake_completion),
        ):
            result = decompile_disassembled_pyc(blocks, (3, 14), "gpt-4")
            assert call_count[0] > 1
            assert "# chunk 1" in result
