"""Unit tests for the JSONC loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from embedagents.stm32._jsonc import load_jsonc, load_jsonc_file


class TestLineComments:
    def test_line_comment_stripped(self) -> None:
        text = '{"a": 1 // trailing\n}'
        assert load_jsonc(text) == {"a": 1}

    def test_full_line_comment(self) -> None:
        text = '// header\n{"a": 1}'
        assert load_jsonc(text) == {"a": 1}

    def test_line_comment_at_eof(self) -> None:
        text = '{"a": 1}\n// trailer'
        assert load_jsonc(text) == {"a": 1}

    def test_double_slash_inside_string_preserved(self) -> None:
        text = '{"url": "https://example.com"}'
        assert load_jsonc(text) == {"url": "https://example.com"}


class TestBlockComments:
    def test_block_comment_stripped(self) -> None:
        text = '{"a": /* note */ 1}'
        assert load_jsonc(text) == {"a": 1}

    def test_multiline_block_comment(self) -> None:
        text = '/*\n multi\n line\n*/\n{"a": 1}'
        assert load_jsonc(text) == {"a": 1}

    def test_block_open_inside_string_preserved(self) -> None:
        text = '{"glob": "/* keep */"}'
        assert load_jsonc(text) == {"glob": "/* keep */"}


class TestTrailingCommas:
    def test_trailing_comma_in_object(self) -> None:
        text = '{"a": 1, "b": 2,}'
        assert load_jsonc(text) == {"a": 1, "b": 2}

    def test_trailing_comma_in_array(self) -> None:
        text = "[1, 2, 3,]"
        assert load_jsonc(text) == [1, 2, 3]

    def test_trailing_comma_after_whitespace(self) -> None:
        text = '{"a": 1\n,\n}'
        assert load_jsonc(text) == {"a": 1}

    def test_comma_inside_string_preserved(self) -> None:
        text = '{"csv": "a,b,c,"}'
        assert load_jsonc(text) == {"csv": "a,b,c,"}


class TestEscapes:
    def test_escaped_quote_inside_string(self) -> None:
        text = r'{"s": "he said \"hi\""}'
        assert load_jsonc(text) == {"s": 'he said "hi"'}

    def test_backslash_inside_string(self) -> None:
        text = r'{"p": "C:\\Users\\anup"}'
        assert load_jsonc(text) == {"p": r"C:\Users\anup"}


class TestErrors:
    def test_truly_invalid_json_raises_decode_error(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            load_jsonc("{not json}")

    def test_unterminated_block_comment_yields_decode_error(self) -> None:
        # The stripper bails to EOF; json.loads then sees truncated content
        # and raises. Substrate callers wrap this into ConfigurationError.
        with pytest.raises(json.JSONDecodeError):
            load_jsonc('{"a": /* never closed')


class TestFileLoader:
    def test_load_file(self, tmp_path: Path) -> None:
        p = tmp_path / "cfg.jsonc"
        p.write_text('// note\n{"v": 42}\n')
        assert load_jsonc_file(p) == {"v": 42}


class TestRealisticConfig:
    """A representative blob mirroring stm32-tools.local.jsonc shape."""

    def test_full_blob(self) -> None:
        text = """
        {
            // Top-level marker
            "$schema": "schema.json",
            "version": 1,
            "programmer": {
                /* default_probe_sn loaded into ctx.default_probe_sn */
                "default_probe_sn": "066BFF...",
            },
            "tools": {
                "cube_programmer": {
                    "env_var": "STM32_PROGRAMMER_CLI",
                    "executable_name": "STM32_Programmer_CLI",
                    "candidates": {
                        "linux": [
                            "/opt/st/stm32cubeprogrammer/bin/STM32_Programmer_CLI",
                        ],
                    },
                },
            },
        }
        """
        parsed = load_jsonc(text)
        assert parsed["version"] == 1
        assert parsed["programmer"]["default_probe_sn"] == "066BFF..."
        assert parsed["tools"]["cube_programmer"]["env_var"] == "STM32_PROGRAMMER_CLI"
        assert parsed["tools"]["cube_programmer"]["candidates"]["linux"] == [
            "/opt/st/stm32cubeprogrammer/bin/STM32_Programmer_CLI"
        ]
