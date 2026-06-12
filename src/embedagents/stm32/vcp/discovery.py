"""ST-LINK VCP port discovery via pyserial.

Per ``v1/vcp-api.md`` § "discovery.py — VCP port discovery". Pure filter:
``discover_vcp_ports`` returns the matching list and never raises for
ambiguity — ``VCP._ensure_reader()`` owns the 0 / 1 / 2+ branching.

The known ST-LINK USB descriptors are a frozen v1 set (no plugin / no
runtime override). Extend by editing ``ST_LINK_PIDS`` when a new ST-LINK
variant is encountered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from embedagents.stm32.vcp.results import VCPPortCandidate

if TYPE_CHECKING:  # pragma: no cover
    from serial.tools.list_ports_common import ListPortInfo


# USB ids for ST-LINK virtual COM ports. PIDs sourced from STSW-LINK007
# release notes. Edit the set to admit a new variant — no plugin surface.
ST_LINK_VID = 0x0483
ST_LINK_PIDS = frozenset(
    {0x3744, 0x3748, 0x374B, 0x374D, 0x374E, 0x374F, 0x3752, 0x3753, 0x3754}
)


def discover_vcp_ports(
    *,
    probe_sn: str | None = None,
    _comports: "callable | None" = None,
) -> list[VCPPortCandidate]:
    """Enumerate ST-LINK virtual COM ports.

    Filters ``pyserial.tools.list_ports.comports()`` to entries whose
    ``vid`` / ``pid`` match the ST-LINK set. When ``probe_sn`` is given,
    further filters to that serial number.

    Returns possibly-empty list. Ambiguity (multiple candidates with no
    selector) is the caller's responsibility — typically ``_ensure_reader``
    on the ``VCP`` class.

    ``_comports`` exists for tests to inject a fake enumerator without
    monkey-patching pyserial.
    """
    if _comports is None:
        # Imported lazily so that ``from embedagents.stm32.vcp import …`` does
        # not pay the pyserial cost when only the result types are needed.
        from serial.tools import list_ports

        _comports = list_ports.comports

    candidates: list[VCPPortCandidate] = []
    for info in _comports():
        vid = getattr(info, "vid", None)
        pid = getattr(info, "pid", None)
        if vid != ST_LINK_VID or pid not in ST_LINK_PIDS:
            continue
        sn = getattr(info, "serial_number", None) or ""
        if probe_sn is not None and sn != probe_sn:
            continue
        candidates.append(
            VCPPortCandidate(
                port=info.device,
                vid=vid,
                pid=pid,
                serial_number=sn,
            )
        )
    return candidates


def is_stlink_vcp(info: "ListPortInfo") -> bool:
    """Helper exposed for tests + loud-error messages.

    Returns True iff ``info`` looks like an ST-LINK virtual COM port.
    Equivalent to the filter ``discover_vcp_ports`` runs internally.
    """
    return (
        getattr(info, "vid", None) == ST_LINK_VID
        and getattr(info, "pid", None) in ST_LINK_PIDS
    )
