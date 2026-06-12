"""USB virtual COM port reader.

Public surface: ``VCP`` class + the result dataclasses + the candidate
records. Implements prompts VCP-001 (tail), VCP-002 (send_and_read),
VCP-003 (reconnect), plus SB-001 (auto-attach) and SB-002 (auto-reconnect)
as internal substrate defaults.

See ``v1/vcp-api.md`` for the full method signatures, descriptor fields,
multi-probe resolution flow, and CLI subcommand mapping.
"""

from __future__ import annotations

from stm32_substrate.vcp.client import VCP
from stm32_substrate.vcp.discovery import (
    ST_LINK_PIDS,
    ST_LINK_VID,
    discover_vcp_ports,
    is_stlink_vcp,
)
from stm32_substrate.vcp.results import (
    PriorVCPState,
    ReconnectResult,
    RequestResponse,
    VCPPortCandidate,
    VCPProbeCandidate,
)

__all__ = [
    "VCP",
    "PriorVCPState",
    "ReconnectResult",
    "RequestResponse",
    "ST_LINK_PIDS",
    "ST_LINK_VID",
    "VCPPortCandidate",
    "VCPProbeCandidate",
    "discover_vcp_ports",
    "is_stlink_vcp",
]
