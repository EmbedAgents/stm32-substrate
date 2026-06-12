"""Discovery / filtering rules for ``discover_vcp_ports``.

Per ``v1/vcp-api.md`` § "discovery.py — VCP port discovery": pure filter
over pyserial's ``list_ports.comports()`` keyed on ST-LINK USB IDs. Tests
inject a fake ``_comports`` callable so no real pyserial enumeration runs.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from stm32_substrate.vcp.discovery import (
    ST_LINK_PIDS,
    ST_LINK_VID,
    discover_vcp_ports,
)


@dataclass
class _FakeInfo:
    device: str
    vid: int | None
    pid: int | None
    serial_number: str | None


def _make_comports(*entries: _FakeInfo):
    def _():
        return list(entries)

    return _


class TestDiscovery:
    def test_empty_input_returns_empty(self) -> None:
        result = discover_vcp_ports(_comports=_make_comports())
        assert result == []

    def test_single_stlink_no_filter(self) -> None:
        result = discover_vcp_ports(
            _comports=_make_comports(
                _FakeInfo("/dev/ttyACM0", ST_LINK_VID, 0x374B, "ABC123"),
            ),
        )
        assert len(result) == 1
        assert result[0].port == "/dev/ttyACM0"
        assert result[0].vid == ST_LINK_VID
        assert result[0].serial_number == "ABC123"

    def test_filters_out_non_stlink_vid(self) -> None:
        result = discover_vcp_ports(
            _comports=_make_comports(
                _FakeInfo("/dev/ttyACM0", 0x2341, 0x0043, "ARDUINO1"),  # Arduino
            ),
        )
        assert result == []

    def test_filters_out_unknown_pid_with_stlink_vid(self) -> None:
        # An ST-VID device that isn't in the ST-LINK PID set must be skipped.
        unknown_pid = 0x9999
        assert unknown_pid not in ST_LINK_PIDS
        result = discover_vcp_ports(
            _comports=_make_comports(
                _FakeInfo("/dev/ttyACM0", ST_LINK_VID, unknown_pid, "X"),
            ),
        )
        assert result == []

    def test_two_stlinks_returns_both(self) -> None:
        result = discover_vcp_ports(
            _comports=_make_comports(
                _FakeInfo("/dev/ttyACM0", ST_LINK_VID, 0x374B, "AAA"),
                _FakeInfo("/dev/ttyACM1", ST_LINK_VID, 0x374B, "BBB"),
            ),
        )
        ports = {c.port for c in result}
        assert ports == {"/dev/ttyACM0", "/dev/ttyACM1"}

    def test_probe_sn_filter_picks_one(self) -> None:
        result = discover_vcp_ports(
            probe_sn="BBB",
            _comports=_make_comports(
                _FakeInfo("/dev/ttyACM0", ST_LINK_VID, 0x374B, "AAA"),
                _FakeInfo("/dev/ttyACM1", ST_LINK_VID, 0x374B, "BBB"),
            ),
        )
        assert len(result) == 1
        assert result[0].port == "/dev/ttyACM1"
        assert result[0].serial_number == "BBB"

    def test_probe_sn_no_match_returns_empty(self) -> None:
        result = discover_vcp_ports(
            probe_sn="ZZZ",
            _comports=_make_comports(
                _FakeInfo("/dev/ttyACM0", ST_LINK_VID, 0x374B, "AAA"),
            ),
        )
        assert result == []

    def test_missing_vid_pid_treated_as_non_stlink(self) -> None:
        # ListPortInfo entries with no USB descriptor (e.g. bluetooth ports)
        # have vid/pid = None.
        result = discover_vcp_ports(
            _comports=_make_comports(
                _FakeInfo("/dev/rfcomm0", None, None, None),
            ),
        )
        assert result == []

    def test_missing_serial_number_passes_when_probe_sn_unset(self) -> None:
        # Some ST-LINK probes report empty serials; should still surface when
        # the caller does not require a specific SN.
        result = discover_vcp_ports(
            _comports=_make_comports(
                _FakeInfo("/dev/ttyACM0", ST_LINK_VID, 0x374B, None),
            ),
        )
        assert len(result) == 1
        assert result[0].serial_number == ""
