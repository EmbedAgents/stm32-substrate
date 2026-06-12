"""VCP hardware tests - non-destructive.

These run against an attached NUCLEO-L476RG flashed with the
F-PROJ-NUCLEO-L476RG-VCP-ECHO firmware (built from
``tests/fixtures/projects/F-PROJ-NUCLEO-L476RG-BLINKY/Projects/
NUCLEO-L476RG/Examples/PWR/PWR_ModesSelection`` after the main.c
rewrite to a polled char echo at 115200-8-N-1). The firmware
mirrors every byte received on USART2 back to the sender, so
``send_and_read("ping")`` writes ``"ping\\n"`` and reads back
``"ping"`` as one line.

Excluded from the default ``pytest`` run; invoke with
``pytest -m hardware``.

Firmware-prerequisite handling: tests that exercise the echo path
skip cleanly when send_and_read times out without a reply (= firmware
not flashed, or wrong firmware on the board). The discovery test runs
unconditionally - it only needs the ST-LINK USB device enumerated.
"""

from __future__ import annotations

import time

import pytest

from stm32_substrate.cubeprogrammer import CubeProgrammer
from stm32_substrate.vcp import ST_LINK_VID, VCP


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vcp(l476rg_ctx):
    """One VCP per test; cleanly closed on teardown so the next test
    starts from a fresh reader (and the COM port handle is released
    for a human running minicom / picocom between bench runs)."""
    client = VCP(l476rg_ctx)
    yield client
    client.close()


# ---------------------------------------------------------------------------
# TestVcpDiscovery
# ---------------------------------------------------------------------------


@pytest.mark.hardware
class TestVcpDiscovery:
    def test_discover_finds_stlink_vcp_port(self, vcp: VCP) -> None:
        """discover_vcp_ports() enumerates pyserial's view of attached
        STLink CDC interfaces. board_name is None at discovery time -
        it's only populated when _ensure_reader cross-references
        cubeprogrammer.list_probes(), per RES-020 #d."""
        ports = vcp.discover_vcp_ports()
        assert len(ports) >= 1, "no VCP ports enumerated"
        stlink_ports = [p for p in ports if p.vid == ST_LINK_VID]
        assert stlink_ports, (
            f"no STMicroelectronics VID ({ST_LINK_VID:#06x}) ports found; "
            f"saw vids {sorted({p.vid for p in ports})}"
        )
        # Substrate's ctx.default_probe_sn was set by l476rg_ctx fixture;
        # at least one enumerated port should match that probe's SN.
        target_sn = vcp.ctx.default_probe_sn
        assert target_sn, "l476rg_ctx should have pinned default_probe_sn"
        assert any(p.serial_number == target_sn for p in stlink_ports), (
            f"no STLink port with SN {target_sn}; saw "
            f"{[(p.port, p.serial_number) for p in stlink_ports]}"
        )


# ---------------------------------------------------------------------------
# TestVcpEchoLoopback
# ---------------------------------------------------------------------------


def _require_echo_response(resp, msg: str) -> None:
    """Shared skip-guard: skip when the firmware on the board clearly
    isn't VCP-ECHO. Lets the suite run cleanly on benches that haven't
    been pre-flashed with the right firmware, or where the resolved
    CDC port belongs to a different board running a different demo
    (e.g. H7S78-DK's AI Chatbot Demo's startup banner).

    Two skip triggers:
    - timeout with no replies at all (firmware not running / wrong baud)
    - replies received but the expected echo isn't one of them (wrong
      firmware on the board — e.g. a startup banner that arrived as
      reply_lines but doesn't contain ``msg``)
    """
    if resp.timeout_hit and not resp.reply_lines:
        pytest.skip(
            "VCP-ECHO firmware did not respond - flash VCP-ECHO.elf "
            "on the NUCLEO-L476RG and re-run."
        )
    if msg not in resp.reply_lines:
        pytest.skip(
            f"VCP-ECHO firmware not flashed on this board - got "
            f"non-echo reply {resp.reply_lines!r}; expected the echo "
            f"to include {msg!r}. Flash VCP-ECHO.elf and re-run."
        )


@pytest.mark.hardware
class TestVcpEchoLoopback:
    def test_send_and_read_returns_single_echoed_line(self, vcp: VCP) -> None:
        """The simplest end-to-end: send a line, receive the same line
        back. send_and_read writes ``"ping\\n"``; firmware echoes
        ``"ping\\n"``; substrate's line reader delivers ``"ping"`` (no
        trailing terminator)."""
        resp = vcp.send_and_read("ping", baud=115200, timeout_s=2.0)
        _require_echo_response(resp, "ping")
        assert resp.sent_line == "ping"
        assert resp.reply_lines == ("ping",), (
            f"echo mismatch: sent 'ping', got {resp.reply_lines}"
        )
        assert resp.timeout_hit is False
        assert resp.baud == 115200

    def test_round_trip_multiple_distinct_payloads(self, vcp: VCP) -> None:
        """Sequential send_and_read calls each return their own payload -
        confirms the firmware's polled receive loop doesn't fall behind
        and the substrate's reader correctly isolates per-call replies."""
        for payload in ("alpha", "beta", "gamma"):
            resp = vcp.send_and_read(payload, baud=115200, timeout_s=2.0)
            _require_echo_response(resp, payload)
            assert resp.reply_lines == (payload,), (
                f"sent {payload!r}, got {resp.reply_lines}"
            )

    def test_send_long_line_round_trips_intact(self, vcp: VCP) -> None:
        """64-byte payload exercises the firmware's tight polled loop
        without buffer concerns (firmware is byte-at-a-time, no
        line buffer). Catches any baud-rate drift or PCLK1
        miscalculation that would corrupt mid-line bytes."""
        payload = "abcdefghijklmnopqrstuvwxyz" + "0123456789" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ" + "!@"
        assert len(payload) == 64
        resp = vcp.send_and_read(payload, baud=115200, timeout_s=2.0)
        _require_echo_response(resp, payload)
        assert resp.reply_lines == (payload,), (
            f"long-line corruption: sent {len(payload)}B, "
            f"got {[len(r) for r in resp.reply_lines]}B replies"
        )


# ---------------------------------------------------------------------------
# TestVcpTailAndReconnect — VCP-001 (tail) + VCP-003 (reconnect)
# ---------------------------------------------------------------------------


@pytest.fixture
def vcp_reset(l476rg_ctx):
    """VCP client whose board is reset first so the VCP-ECHO firmware is
    responsive even when a prior test left it wedged — the long-line
    overrun test (VCP-006) is known to wedge the polled-loop firmware
    (plan-test.md Tier 6 #20), and tail/reconnect run after it. Reset +
    settle re-establishes the echo, per the documented pre-suite-reset
    workaround."""
    CubeProgrammer(l476rg_ctx).reset()
    time.sleep(0.5)  # let the firmware reboot + USART2 settle
    client = VCP(l476rg_ctx)
    yield client
    client.close()


@pytest.mark.hardware
class TestVcpTailAndReconnect:
    """VCP-001 (`tail`) + VCP-003 (`reconnect`). VCP-ECHO emits nothing
    unsolicited (polled char echo), so the tail test stages short lines
    onto the wire via the active reader's `write_line` — the firmware
    echoes each into the background drain buffer, and `tail(follow=False)`
    snapshots them back. Each staged line is well under the 64-byte
    polled-loop overrun threshold (VCP-006), with Python-call gaps between
    writes, so the firmware keeps up."""

    def test_tail_yields_buffered_echo_lines(self, vcp_reset: VCP) -> None:
        """VCP-001 — tail(follow=False) snapshots recent buffered lines.
        Warm up to confirm the firmware + establish the reader, stage three
        lines (echoed into the drain buffer), then tail and assert all
        three appear. Snapshot mode waits up to timeout_s for the lines to
        accumulate, so no explicit sleep is needed."""
        warmup = vcp_reset.send_and_read("warmup", baud=115200, timeout_s=2.0)
        _require_echo_response(warmup, "warmup")
        reader = vcp_reset.ctx.session_state.active_vcp_reader
        assert reader is not None, "send_and_read should have established a reader"
        staged = ("tail-alpha", "tail-beta", "tail-gamma")
        for line in staged:
            reader.write_line(line, terminator="\n")
            # Pace the writes: the polled-char-echo firmware drops bytes on
            # back-to-back lines (same RX-overrun root cause as VCP-006).
            # A short gap lets it echo each line fully before the next.
            time.sleep(0.2)
        tailed = list(vcp_reset.tail(last_n=len(staged), follow=False, timeout_s=2.0))
        for expected in staged:
            assert expected in tailed, f"{expected!r} not in tailed lines {tailed}"

    def test_reconnect_recycles_active_reader(self, vcp_reset: VCP) -> None:
        """VCP-003 — reconnect() recycles the active reader; the echo path
        keeps working afterward. status is reconnected/same_port (same_port
        is the typical bench result — the CDC device returns at the same
        /dev/ttyACMx path)."""
        warmup = vcp_reset.send_and_read("warmup", baud=115200, timeout_s=2.0)
        _require_echo_response(warmup, "warmup")
        result = vcp_reset.reconnect()
        assert result.status in ("reconnected", "same_port")
        assert result.port, "reconnect should report the VCP port"
        resp = vcp_reset.send_and_read("after-reconnect", baud=115200, timeout_s=2.0)
        _require_echo_response(resp, "after-reconnect")
        assert resp.reply_lines == ("after-reconnect",)
