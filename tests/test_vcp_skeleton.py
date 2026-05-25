"""Skeleton + result-type tests for the ``vcp`` module.

Verifies imports, dataclass shapes, and the error-subclass plumbing per
the VCP API spec. No serial I/O.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from stm32_substrate.context import SubstrateContext
from stm32_substrate.errors import (
    VCPAmbiguousProbe,
    VCPError,
    VCPNotEnumerated,
    VCPPortInUse,
    VCPReaderAlreadyActive,
)
from stm32_substrate.vcp import (
    VCP,
    PriorVCPState,
    ReconnectResult,
    RequestResponse,
    ST_LINK_PIDS,
    ST_LINK_VID,
    VCPPortCandidate,
    VCPProbeCandidate,
    discover_vcp_ports,
    is_stlink_vcp,
)


@pytest.fixture()
def ctx(tmp_path: Path) -> SubstrateContext:
    return SubstrateContext.from_environment(project_path=tmp_path)


class TestPublicSurface:
    def test_imports(self) -> None:
        # Sanity — the public re-exports are all callable / class-like.
        assert callable(discover_vcp_ports)
        assert callable(is_stlink_vcp)
        assert issubclass(VCP, object)
        assert ST_LINK_VID == 0x0483
        assert 0x374B in ST_LINK_PIDS

    def test_construct(self, ctx: SubstrateContext) -> None:
        client = VCP(ctx)
        assert client.ctx is ctx
        assert client._log.name == "stm32_substrate.vcp"

    def test_close_idempotent_with_no_reader(self, ctx: SubstrateContext) -> None:
        client = VCP(ctx)
        client.close()  # no reader yet → DEBUG log no-op
        client.close()  # still safe
        assert ctx.session_state.active_vcp_reader is None


class TestResultDataclasses:
    def test_request_response_frozen(self) -> None:
        rr = RequestResponse(
            sent_line="hi",
            reply_lines=("hello",),
            timeout_hit=False,
            duration_s=0.01,
            port="/dev/ttyACM0",
            baud=115200,
        )
        assert rr.reply_lines == ("hello",)
        with pytest.raises(dataclasses.FrozenInstanceError):
            rr.timeout_hit = True  # type: ignore[misc]

    def test_reconnect_result_status_literal(self) -> None:
        rr = ReconnectResult(
            port="/dev/ttyACM7",
            status="reconnected",
            prior_state=PriorVCPState(
                port="/dev/ttyACM0",
                baud=115200,
                last_byte_timestamp_s=None,
                open=True,
            ),
            duration_s=0.5,
        )
        assert rr.status == "reconnected"
        assert rr.prior_state.port == "/dev/ttyACM0"

    def test_vcp_probe_candidate_combined_shape(self) -> None:
        c = VCPProbeCandidate(
            port="/dev/ttyACM0", serial_number="ABCD", board_name="NUCLEO-L476RG"
        )
        assert c.port == "/dev/ttyACM0"
        assert c.board_name == "NUCLEO-L476RG"

    def test_vcp_port_candidate_default_board_name_none(self) -> None:
        c = VCPPortCandidate(
            port="/dev/ttyACM0", vid=0x0483, pid=0x374B, serial_number="ABCD"
        )
        assert c.board_name is None


class TestErrorSubclasses:
    def test_subclass_hierarchy(self) -> None:
        assert issubclass(VCPNotEnumerated, VCPError)
        assert issubclass(VCPAmbiguousProbe, VCPError)
        assert issubclass(VCPPortInUse, VCPError)
        assert issubclass(VCPReaderAlreadyActive, VCPError)

    def test_vcp_error_marker_field(self) -> None:
        err = VCPError(message="x", vcp_marker="no-vcp-enumerated", port="/dev/ttyACM0")
        assert err.vcp_marker == "no-vcp-enumerated"
        assert err.port == "/dev/ttyACM0"
        assert err.requested_probe_sn is None

    def test_ambiguous_probe_candidates(self) -> None:
        cands = (
            VCPProbeCandidate("/dev/ttyACM0", "AAA", "NUCLEO-L476RG"),
            VCPProbeCandidate("/dev/ttyACM1", "BBB", None),
        )
        err = VCPAmbiguousProbe(message="x", candidates=cands)
        assert len(err.candidates) == 2
        assert err.candidates[1].board_name is None
