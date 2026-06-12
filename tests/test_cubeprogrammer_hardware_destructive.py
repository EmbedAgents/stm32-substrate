"""Cubeprogrammer destructive hardware tests.

Gated by ``@pytest.mark.hardware_destructive`` — explicit opt-in only,
not run by ``pytest -m hardware``. These tests wipe the attached flash,
so the user must accept that the device's current firmware will be lost.

Invoke with:

    pytest -m hardware_destructive

Per RES-009 / M-019 (HIL principle): destructive ops require explicit
operator consent. The marker IS the consent mechanism.
"""

from __future__ import annotations

import pytest

from embedagents.stm32.cubeprogrammer import CubeProgrammer
from embedagents.stm32.cubeprogrammer.results import (
    Confirmation,
    EraseConfirmation,
    MemoryReadResult,
    OptionBytesDiff,
)
from embedagents.stm32.errors import ProtocolError


@pytest.mark.hardware_destructive
class TestEraseChip:
    def test_erase_chip_succeeds_and_flash_reads_all_ff(self, l476rg_ctx) -> None:
        """After ``erase_chip()`` the entire flash should read back as
        0xFF — substrate flags this as ``suspicious_unmapped=True`` per
        F-019/F-020 spec.

        Test sequence:
          1. erase_chip()
          2. read first 256 bytes of flash
          3. assert all bytes are 0xFF (suspicious_unmapped flag set)
        """
        client = CubeProgrammer(l476rg_ctx)
        erase_result = client.erase_chip()
        assert isinstance(erase_result, EraseConfirmation)
        assert erase_result.erase_complete is True

        # Post-erase read: flash is virgin, all 0xFF — substrate flags it.
        read = client.read_memory("0x08000000", size=256)
        assert isinstance(read, MemoryReadResult)
        assert read.suspicious_unmapped is True, (
            "post-erase flash must read all-0xFF and trip suspicious_unmapped"
        )


# ---------------------------------------------------------------------------
# TestOptionBytes (backlog #6) — F-021 + DIAG-018 destructive coverage
# ---------------------------------------------------------------------------


@pytest.mark.hardware_destructive
class TestOptionBytes:
    """F-021 (write_option_bytes) + DIAG-018 (verify_option_bytes) on
    real L476 hardware. **Safety:** this test only touches SRAM2_RST
    (a benign field controlling SRAM2 erase-on-reset behavior). It
    NEVER writes RDP — RDP=0x55 requires a chip erase to reverse,
    RDP=0xCC is irreversible (chip permanently locked). The
    irreversibility-gate test uses substrate's pre-CLI raise, so RDP
    is never actually sent to the chip.

    Test sequence:
      1. Read current OB state (capture baseline)
      2. Toggle SRAM2_RST to the opposite value via write_option_bytes
      3. Verify the toggle via read_option_bytes
      4. Restore original SRAM2_RST value
      5. Test irreversibility gate (substrate raise before CLI)"""

    def test_write_and_verify_sram2_rst_toggle(self, l476rg_ctx) -> None:
        """Read → toggle SRAM2_RST → verify → restore. Validates F-021
        end-to-end and DIAG-018 verify_option_bytes against real OB
        state changes."""
        client = CubeProgrammer(l476rg_ctx)

        # 1. Read baseline so we can restore exactly.
        baseline = client.read_option_bytes()
        assert "SRAM2_RST" in baseline.observed, (
            f"SRAM2_RST not in L476 OB dump; got: "
            f"{sorted(baseline.observed.keys())[:10]}..."
        )
        original = baseline.observed["SRAM2_RST"]
        # Coerce to int — substrate's read may return str like "0x1" or int 1.
        if isinstance(original, str):
            original_int = int(original, 0)
        else:
            original_int = int(original)
        toggled_int = 0 if original_int == 1 else 1

        try:
            # 2. Toggle.
            write_result = client.write_option_bytes(
                {"SRAM2_RST": toggled_int},
                confirm_destructive=True,
            )
            assert isinstance(write_result, Confirmation)
            assert write_result.operation == "write_option_bytes"
            # Substrate reads observed_after as part of the Confirmation.
            observed_after = write_result.data["observed_after"]
            assert "SRAM2_RST" in observed_after
            # 3. Verify via DIAG-018.
            diff = client.verify_option_bytes({"SRAM2_RST": toggled_int})
            assert isinstance(diff, OptionBytesDiff)
            assert diff.diffs == [], (
                f"verify_option_bytes mismatch after toggle: {diff.diffs}"
            )
        finally:
            # 4. Restore original value regardless of assertion outcomes.
            client.write_option_bytes(
                {"SRAM2_RST": original_int},
                confirm_destructive=True,
            )
            # Final verify to confirm restore landed.
            restore_diff = client.verify_option_bytes({"SRAM2_RST": original_int})
            assert restore_diff.diffs == [], (
                f"failed to restore SRAM2_RST to {original_int}; "
                f"diff: {restore_diff.diffs}"
            )

    def test_irreversibility_gate_blocks_rdp_level_2(self, l476rg_ctx) -> None:
        """Writing RDP=0xCC (level 2, irreversible) without
        confirm_irreversible=True must raise ProtocolError BEFORE
        invoking the CLI. Verified by checking the OB state didn't
        change after the raise — substrate's gate fires server-side
        with no subprocess call."""
        client = CubeProgrammer(l476rg_ctx)
        before = client.read_option_bytes()
        with pytest.raises(ProtocolError) as exc_info:
            client.write_option_bytes(
                {"RDP": "0xCC"},
                confirm_destructive=True,
                # confirm_irreversible deliberately omitted (defaults False)
            )
        # The raise message mentions irreversibility per the substrate spec.
        assert "irreversible" in str(exc_info.value).lower()
        # Confirm no OB state changed (the substrate raised before the CLI).
        after = client.read_option_bytes()
        assert before.observed.get("RDP") == after.observed.get("RDP"), (
            "RDP changed despite substrate raise — gate should fire BEFORE "
            "any CLI invocation"
        )
