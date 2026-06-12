"""B8 tests — write_option_bytes (F-021) + verify_option_bytes (DIAG-018).

Covers the irreversibility gate, the destructive gate (bool + callable
flavours), OB value coercion, and verify diff semantics."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from stm32_substrate.context import SubstrateContext
from stm32_substrate.cubeprogrammer import CubeProgrammer
from stm32_substrate.cubeprogrammer.client import (
    _coerce_to_int,
    _is_rdp_level_2,
    _ob_values_equal,
    _render_ob_value,
)
from stm32_substrate.cubeprogrammer.results import (
    Confirmation,
    OptionByteDiffEntry,
    OptionBytesDiff,
)
from stm32_substrate.errors import (
    ProtocolError,
    UserAbortedError,
)
from stm32_substrate.subprocess_runner import ToolRunResult


OB = Path(__file__).resolve().parent / "fixtures" / "cubeprogrammer" / "option-bytes"


def _ob(name: str) -> str:
    return (OB / name).read_text(encoding="utf-8")


@pytest.fixture()
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SubstrateContext:
    fake_cli = tmp_path / "STM32_Programmer_CLI"
    fake_cli.write_text("#!/bin/sh\nexit 0\n")
    fake_cli.chmod(0o755)
    monkeypatch.setenv("STM32_PROGRAMMER_CLI", str(fake_cli))
    return SubstrateContext.from_environment(project_path=tmp_path)


def _ok(stdout: str = "") -> ToolRunResult:
    return ToolRunResult(
        exit_code=0, stdout=stdout, stderr="", duration_s=0.05, timed_out=False
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestIsRdpLevel2:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (0xCC, True),
            ("0xCC", True),
            ("0xcc", True),
            (204, True),
            ("204", True),
            (0xAA, False),
            ("0xAA", False),
            (0x55, False),
            (1, False),
            (True, False),
            (False, False),
            ("not-numeric", False),
        ],
    )
    def test_rdp_level_2_detection(self, value, expected: bool) -> None:
        assert _is_rdp_level_2(value) is expected


class TestRenderOBValue:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (True, "0x1"),
            (False, "0x0"),
            (0xAA, "0xaa"),
            (1, "0x1"),
            (0, "0x0"),
            ("0xAA", "0xAA"),  # string preserves user formatting
            ("Level 0", "Level 0"),  # non-hex string passthrough
        ],
    )
    def test_render(self, value, expected: str) -> None:
        assert _render_ob_value(value) == expected


class TestCoerceToInt:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, None),
            (True, 1),
            (False, 0),
            (170, 170),
            ("0xAA", 0xAA),
            ("0xaa", 0xAA),
            ("170", 170),
            ("not-numeric", None),
            ("0xZZ", None),
        ],
    )
    def test_coerce(self, value, expected) -> None:
        assert _coerce_to_int(value) == expected


class TestOBValuesEqual:
    @pytest.mark.parametrize(
        "observed,expected,result",
        [
            (0xAA, 0xAA, True),
            ("0xAA", 0xAA, True),
            (170, "0xAA", True),
            (True, 1, True),
            (False, 0, True),
            (0xAA, 0xCC, False),
            ("AA", "AA", True),
            ("AA", "BB", False),
            (None, 0xAA, False),
            (None, None, False),  # missing field always differs
        ],
    )
    def test_equal(self, observed, expected, result: bool) -> None:
        assert _ob_values_equal(observed, expected) is result


# ---------------------------------------------------------------------------
# write_option_bytes — irreversibility gate
# ---------------------------------------------------------------------------


class TestIrreversibilityGate:
    def test_rdp_level_2_without_flag_raises(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with pytest.raises(ProtocolError, match="RDP=0xCC"):
            client.write_option_bytes({"RDP": 0xCC}, confirm_destructive=True)

    def test_rdp_level_2_string_form_caught(self, ctx: SubstrateContext) -> None:
        """User passes ``"0xCC"`` as string — substrate normalises and
        still trips the gate."""
        client = CubeProgrammer(ctx)
        with pytest.raises(ProtocolError):
            client.write_option_bytes(
                {"RDP": "0xCC"}, confirm_destructive=True
            )

    def test_rdp_level_2_with_flag_proceeds(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            side_effect=[_ok(), _ok(_ob("stm32l4-rdp2.txt"))],
        ) as run:
            result = client.write_option_bytes(
                {"RDP": 0xCC},
                confirm_destructive=True,
                confirm_irreversible=True,
            )
        assert isinstance(result, Confirmation)
        assert result.operation == "write_option_bytes"
        # IMP-05: RDP level 2 permanently locks the debug port — the
        # verification reconnect is guaranteed to fail and previously
        # reported the SUCCESSFUL irreversible write as a failure. The
        # read-back is skipped (single CLI call) and the confirmation
        # says why.
        assert run.call_count == 1
        assert result.data["observed_after"] is None
        assert "read_back_skipped" in result.data
        assert result.data["pairs_written"] == {"RDP": 0xCC}

    def test_rdp_level_1_does_not_trip_irreversibility(
        self, ctx: SubstrateContext
    ) -> None:
        """0x55 is RDP level 1 (reversible by mass-erase); no gate."""
        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            side_effect=[_ok(), _ok(_ob("stm32l4-rdp1.txt"))],
        ):
            result = client.write_option_bytes(
                {"RDP": 0x55}, confirm_destructive=True
            )
        assert isinstance(result, Confirmation)

    def test_other_destructive_fields_do_not_trip_irreversibility(
        self, ctx: SubstrateContext
    ) -> None:
        client = CubeProgrammer(ctx)
        # No RDP key in the dict at all → irreversibility check passes.
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            side_effect=[_ok(), _ok(_ob("stm32l4-default.txt"))],
        ):
            result = client.write_option_bytes(
                {"BOR_LEV": 0x2}, confirm_destructive=True
            )
        assert result.data["pairs_written"] == {"BOR_LEV": 0x2}


# ---------------------------------------------------------------------------
# write_option_bytes — destructive gate
# ---------------------------------------------------------------------------


class TestDestructiveGate:
    def test_default_false_raises_user_aborted(
        self, ctx: SubstrateContext
    ) -> None:
        client = CubeProgrammer(ctx)
        with pytest.raises(UserAbortedError, match="declined"):
            client.write_option_bytes({"IWDG_SW": 0x1})

    def test_explicit_false_raises(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with pytest.raises(UserAbortedError):
            client.write_option_bytes(
                {"IWDG_SW": 0x1}, confirm_destructive=False
            )

    def test_explicit_true_proceeds(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            side_effect=[_ok(), _ok(_ob("stm32l4-default.txt"))],
        ):
            result = client.write_option_bytes(
                {"IWDG_SW": 0x1}, confirm_destructive=True
            )
        assert isinstance(result, Confirmation)
        assert result.data["destructive_ops_confirmed"] == ["IWDG_SW"]

    def test_callable_returning_true_proceeds(
        self, ctx: SubstrateContext
    ) -> None:
        captured: list[list[str]] = []

        def cb(fields: list[str]) -> bool:
            captured.append(list(fields))
            return True

        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            side_effect=[_ok(), _ok(_ob("stm32l4-default.txt"))],
        ):
            client.write_option_bytes(
                {"IWDG_SW": 0x1, "BOR_LEV": 0x0},
                confirm_destructive=cb,
            )
        assert captured == [["IWDG_SW", "BOR_LEV"]]

    def test_callable_returning_false_raises(
        self, ctx: SubstrateContext
    ) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool"
        ) as mocked:
            with pytest.raises(UserAbortedError):
                client.write_option_bytes(
                    {"IWDG_SW": 0x1},
                    confirm_destructive=lambda _: False,
                )
        # CLI never invoked when user declines.
        assert mocked.call_count == 0

    def test_irreversibility_check_runs_before_destructive(
        self, ctx: SubstrateContext
    ) -> None:
        """The irreversibility check on RDP=0xCC fires even when
        ``confirm_destructive=False`` — irreversibility is the more
        permanent failure mode and should be flagged first."""
        client = CubeProgrammer(ctx)
        with pytest.raises(ProtocolError):
            # confirm_destructive=False would normally raise UserAbortedError,
            # but RDP=0xCC's irreversibility check fires first.
            client.write_option_bytes({"RDP": 0xCC}, confirm_destructive=False)


# ---------------------------------------------------------------------------
# write_option_bytes — argv + observed_after
# ---------------------------------------------------------------------------


class TestWriteOptionBytesInvocation:
    def test_argv_includes_pairs(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            side_effect=[_ok(), _ok(_ob("stm32l4-default.txt"))],
        ) as mocked:
            client.write_option_bytes(
                {"RDP": 0xAA, "IWDG_SW": 0x1},
                confirm_destructive=True,
            )
        # First call is the OB write; second is the read-back.
        write_argv = mocked.call_args_list[0][0][1]
        assert "-ob" in write_argv
        # Values rendered as 0x<hex>
        assert "RDP=0xaa" in write_argv
        assert "IWDG_SW=0x1" in write_argv

    def test_bool_value_coerced_to_hex(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            side_effect=[_ok(), _ok(_ob("stm32l4-default.txt"))],
        ) as mocked:
            client.write_option_bytes(
                {"IWDG_SW": True, "WWDG_SW": False},
                confirm_destructive=True,
            )
        write_argv = mocked.call_args_list[0][0][1]
        assert "IWDG_SW=0x1" in write_argv
        assert "WWDG_SW=0x0" in write_argv

    def test_string_value_preserves_user_formatting(
        self, ctx: SubstrateContext
    ) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            side_effect=[_ok(), _ok(_ob("stm32l4-default.txt"))],
        ) as mocked:
            client.write_option_bytes(
                {"RDP": "0xAA"}, confirm_destructive=True
            )
        write_argv = mocked.call_args_list[0][0][1]
        # User passed uppercase; substrate preserves it.
        assert "RDP=0xAA" in write_argv

    def test_observed_after_pulled_from_read_back(
        self, ctx: SubstrateContext
    ) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            side_effect=[_ok(), _ok(_ob("stm32l4-default.txt"))],
        ):
            result = client.write_option_bytes(
                {"RDP": 0xAA}, confirm_destructive=True
            )
        assert "RDP" in result.data["observed_after"]
        assert result.data["observed_after"]["RDP"] == 0xAA

    def test_empty_pairs_rejected(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with pytest.raises(ValueError, match="at least one pair"):
            client.write_option_bytes({}, confirm_destructive=True)

    def test_no_cli_invocation_when_gated(self, ctx: SubstrateContext) -> None:
        """Neither the irreversibility nor the destructive path invokes
        the CLI when the gate fails — substrate never reaches subprocess."""
        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool"
        ) as mocked:
            with pytest.raises(UserAbortedError):
                client.write_option_bytes({"IWDG_SW": 1})
            with pytest.raises(ProtocolError):
                client.write_option_bytes(
                    {"RDP": 0xCC}, confirm_destructive=True
                )
        assert mocked.call_count == 0


# ---------------------------------------------------------------------------
# verify_option_bytes — DIAG-018
# ---------------------------------------------------------------------------


class TestVerifyOptionBytes:
    def test_all_match(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            return_value=_ok(_ob("stm32l4-default.txt")),
        ):
            result = client.verify_option_bytes(
                {"RDP": 0xAA, "IWDG_SW": 0x1, "WWDG_SW": 0x1}
            )
        assert isinstance(result, OptionBytesDiff)
        assert result.diffs == []

    def test_mismatch_reported(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            return_value=_ok(_ob("stm32l4-default.txt")),
        ):
            # RDP is 0xAA in the fixture; expecting 0x55 (level 1).
            result = client.verify_option_bytes({"RDP": 0x55})
        assert len(result.diffs) == 1
        entry = result.diffs[0]
        assert isinstance(entry, OptionByteDiffEntry)
        assert entry.field == "RDP"
        assert entry.observed_value == 0xAA
        assert entry.expected_value == 0x55

    def test_string_vs_int_normalised(self, ctx: SubstrateContext) -> None:
        """Caller passes ``"0xAA"`` while substrate parses to int 0xAA —
        normalisation reports no mismatch."""
        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            return_value=_ok(_ob("stm32l4-default.txt")),
        ):
            result = client.verify_option_bytes({"RDP": "0xAA"})
        assert result.diffs == []

    def test_bool_vs_int_normalised(self, ctx: SubstrateContext) -> None:
        """``True`` matches stored int ``1``."""
        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            return_value=_ok(_ob("stm32l4-default.txt")),
        ):
            result = client.verify_option_bytes({"IWDG_SW": True})
        assert result.diffs == []

    def test_missing_field_reported(self, ctx: SubstrateContext) -> None:
        """Caller expects a field that's not on the observed device."""
        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            return_value=_ok(_ob("stm32l4-default.txt")),
        ):
            result = client.verify_option_bytes(
                {"FUTURE_FIELD_X": 0x42}
            )
        assert len(result.diffs) == 1
        entry = result.diffs[0]
        assert entry.field == "FUTURE_FIELD_X"
        assert entry.observed_value == "<missing>"
        assert entry.expected_value == 0x42

    def test_full_dicts_preserved(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            return_value=_ok(_ob("stm32l4-default.txt")),
        ):
            result = client.verify_option_bytes(
                {"RDP": 0xAA, "BOR_LEV": 0x0}
            )
        # Full observed dict + expected dict survive on the result.
        assert "RDP" in result.observed
        assert "IWDG_SW" in result.observed  # also present though not asked about
        assert result.expected == {"RDP": 0xAA, "BOR_LEV": 0x0}

    def test_empty_expected_rejected(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with pytest.raises(ValueError, match="at least one expected"):
            client.verify_option_bytes({})

    def test_argv_uses_ob_displ(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            return_value=_ok(_ob("stm32l4-default.txt")),
        ) as mocked:
            client.verify_option_bytes({"RDP": 0xAA})
        argv = mocked.call_args[0][1]
        # verify_option_bytes routes through read_option_bytes → -ob displ.
        assert "-ob" in argv
        assert "displ" in argv
