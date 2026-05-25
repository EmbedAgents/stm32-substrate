"""``VCP`` public-class behavior.

Covers ``_ensure_reader`` (SB-001 lazy attach + SB-002 lazy reconnect),
multi-probe descriptor auto-match + ambiguous fallback, HIL collision,
``send_and_read`` end-to-end with the fake serial, and ``close``.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from stm32_substrate.context import SubstrateContext
from stm32_substrate.cubeprogrammer.results import ProbeRecord
from stm32_substrate.errors import (
    VCPAmbiguousProbe,
    VCPNotEnumerated,
    VCPReaderAlreadyActive,
)
from stm32_substrate.vcp import VCP
from stm32_substrate.vcp.reader import _VcpReader
from stm32_substrate.vcp.results import VCPPortCandidate


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSerial:
    def __init__(self, *, port: str, baudrate: int, timeout: float) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.is_open = True
        self._lock = threading.Lock()
        self._buf = bytearray()
        self.written = bytearray()

    def feed(self, data: bytes) -> None:
        with self._lock:
            self._buf.extend(data)

    def read(self, n: int) -> bytes:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._buf:
                    chunk = bytes(self._buf[:n])
                    del self._buf[:n]
                    return chunk
            time.sleep(0.005)
        return b""

    def write(self, payload: bytes) -> int:
        self.written.extend(payload)
        return len(payload)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False


def _reader_factory_capture():
    """Build a reader factory that injects a fake pyserial + holds the
    serial reference so tests can feed bytes back."""
    holder: dict[str, _FakeSerial] = {}

    def _serial_factory(**kwargs):
        ser = _FakeSerial(**kwargs)
        holder["serial"] = ser
        return ser

    def factory(**kwargs):
        return _VcpReader(_serial_factory=_serial_factory, **kwargs)

    return factory, holder


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def port_path(tmp_path: Path) -> str:
    p = tmp_path / "ttyACM0"
    p.write_text("")
    return str(p)


@pytest.fixture()
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SubstrateContext:
    monkeypatch.delenv("STM32_PROGRAMMER_DEFAULT_SN", raising=False)
    return SubstrateContext.from_environment(project_path=tmp_path)


def _patch_discover(monkeypatch: pytest.MonkeyPatch, candidates: list) -> None:
    import stm32_substrate.vcp.client as client_mod

    def _fake(*, probe_sn=None):
        if probe_sn is None:
            return list(candidates)
        return [c for c in candidates if c.serial_number == probe_sn]

    monkeypatch.setattr(client_mod, "discover_vcp_ports", _fake)


# ---------------------------------------------------------------------------
# Single-probe ergonomic + SB-001
# ---------------------------------------------------------------------------


class TestLazyAttach:
    def test_first_tail_call_attaches(
        self,
        ctx: SubstrateContext,
        port_path: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        factory, holder = _reader_factory_capture()
        _patch_discover(
            monkeypatch,
            [VCPPortCandidate(port=port_path, vid=0x0483, pid=0x374B, serial_number="ABC")],
        )
        client = VCP(ctx, _reader_factory=factory)
        # Before any call: no reader.
        assert ctx.session_state.active_vcp_reader is None
        # Pre-load a couple of lines so snapshot mode returns immediately.
        # We do this by calling tail() with a tiny timeout — the lazy
        # attach happens inside tail().
        gen = client.tail(last_n=1, timeout_s=0.2)
        # Force the generator to advance to trigger attach.
        with pytest.raises(StopIteration):
            next(gen)
        assert ctx.session_state.active_vcp_reader is not None
        assert ctx.session_state.active_vcp_reader.port == port_path

    def test_no_candidates_raises_not_enumerated(
        self,
        ctx: SubstrateContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        factory, _ = _reader_factory_capture()
        _patch_discover(monkeypatch, [])
        client = VCP(ctx, _reader_factory=factory)
        with pytest.raises(VCPNotEnumerated) as ex:
            list(client.tail(timeout_s=0.1))
        assert ex.value.vcp_marker == "no-vcp-enumerated"


# ---------------------------------------------------------------------------
# Multi-probe resolution
# ---------------------------------------------------------------------------


class TestMultiProbeResolution:
    def test_ambiguous_without_descriptor_raises(
        self,
        ctx: SubstrateContext,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        port_a = str(tmp_path / "ttyACM0")
        port_b = str(tmp_path / "ttyACM1")
        Path(port_a).write_text("")
        Path(port_b).write_text("")

        factory, _ = _reader_factory_capture()
        _patch_discover(
            monkeypatch,
            [
                VCPPortCandidate(port_a, 0x0483, 0x374B, "AAA"),
                VCPPortCandidate(port_b, 0x0483, 0x374B, "BBB"),
            ],
        )
        # No descriptor.firmware.board → must surface as ambiguous.
        # No cubeprogrammer list_probes either (we inject one returning [])
        client = VCP(
            ctx,
            _reader_factory=factory,
            _list_probes_fn=lambda _ctx: [],
        )
        with pytest.raises(VCPAmbiguousProbe) as ex:
            list(client.tail(timeout_s=0.1))
        assert ex.value.vcp_marker == "ambiguous-probe"
        ports = {c.port for c in ex.value.candidates}
        assert ports == {port_a, port_b}

    def test_descriptor_board_auto_matches(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Build a project descriptor with firmware.board set.
        project = tmp_path / "stm32-project.jsonc"
        project.write_text(
            json.dumps(
                {
                    "version": 1,
                    "project_name": "blinky",
                    "firmware": {"board": "NUCLEO-L476RG"},
                }
            )
        )
        ctx_p = SubstrateContext.from_environment(project_path=tmp_path)

        port_a = str(tmp_path / "ttyACM0")
        port_b = str(tmp_path / "ttyACM1")
        Path(port_a).write_text("")
        Path(port_b).write_text("")

        factory, _ = _reader_factory_capture()
        _patch_discover(
            monkeypatch,
            [
                VCPPortCandidate(port_a, 0x0483, 0x374B, "AAA"),
                VCPPortCandidate(port_b, 0x0483, 0x374B, "BBB"),
            ],
        )

        # list_probes() maps BBB → NUCLEO-L476RG, AAA → some other board.
        def _list(_ctx):
            return [
                ProbeRecord("AAA", "V3.J7", "NUCLEO-F411RE"),
                ProbeRecord("BBB", "V3.J7", "NUCLEO-L476RG"),
            ]

        client = VCP(ctx_p, _reader_factory=factory, _list_probes_fn=_list)
        list(client.tail(timeout_s=0.1))  # drains nothing but triggers attach
        attached = ctx_p.session_state.active_vcp_reader
        assert attached is not None
        assert attached.port == port_b
        assert ctx_p.default_probe_sn == "BBB"

    def test_descriptor_board_no_match_falls_through_to_ambiguous(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project = tmp_path / "stm32-project.jsonc"
        project.write_text(
            json.dumps(
                {
                    "version": 1,
                    "project_name": "x",
                    "firmware": {"board": "NUCLEO-H745ZI-Q"},
                }
            )
        )
        ctx_p = SubstrateContext.from_environment(project_path=tmp_path)
        factory, _ = _reader_factory_capture()
        port_a = str(tmp_path / "ttyACM0")
        port_b = str(tmp_path / "ttyACM1")
        Path(port_a).write_text("")
        Path(port_b).write_text("")
        _patch_discover(
            monkeypatch,
            [
                VCPPortCandidate(port_a, 0x0483, 0x374B, "AAA"),
                VCPPortCandidate(port_b, 0x0483, 0x374B, "BBB"),
            ],
        )

        def _list(_ctx):
            return [
                ProbeRecord("AAA", "V3", "NUCLEO-F411RE"),
                ProbeRecord("BBB", "V3", "NUCLEO-L476RG"),
            ]

        client = VCP(ctx_p, _reader_factory=factory, _list_probes_fn=_list)
        with pytest.raises(VCPAmbiguousProbe) as ex:
            list(client.tail(timeout_s=0.1))
        # Both candidates surfaced with board_name attached.
        board_names = {c.board_name for c in ex.value.candidates}
        assert board_names == {"NUCLEO-F411RE", "NUCLEO-L476RG"}


# ---------------------------------------------------------------------------
# send_and_read
# ---------------------------------------------------------------------------


class TestSendAndRead:
    def test_simple_reply(
        self,
        ctx: SubstrateContext,
        port_path: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        factory, holder = _reader_factory_capture()
        _patch_discover(
            monkeypatch,
            [VCPPortCandidate(port_path, 0x0483, 0x374B, "X")],
        )
        client = VCP(ctx, _reader_factory=factory)

        # Pre-arm the device-side bytes BEFORE calling — the reader's
        # ``write_line`` clears the queue then the drain thread re-fills.
        # Easier path: install a hook that feeds bytes after write.
        def _send_in_background():
            time.sleep(0.05)
            holder["serial"].feed(b"world\n")

        threading.Thread(target=_send_in_background, daemon=True).start()
        result = client.send_and_read("hi", timeout_s=2.0, inter_line_idle_ms=100)
        assert result.sent_line == "hi"
        assert result.reply_lines == ("world",)
        assert result.timeout_hit is False
        assert bytes(holder["serial"].written) == b"hi\n"

    def test_echo_filter_drops_echoed_line(
        self,
        ctx: SubstrateContext,
        port_path: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        factory, holder = _reader_factory_capture()
        _patch_discover(
            monkeypatch,
            [VCPPortCandidate(port_path, 0x0483, 0x374B, "X")],
        )
        client = VCP(ctx, _reader_factory=factory)

        def _send_in_background():
            time.sleep(0.05)
            holder["serial"].feed(b"hello\nresponse\n")

        threading.Thread(target=_send_in_background, daemon=True).start()
        result = client.send_and_read(
            "hello", timeout_s=2.0, inter_line_idle_ms=100, echo_filter=True
        )
        assert result.reply_lines == ("response",)
        assert result.echo_filtered is True

    def test_timeout_hit(
        self,
        ctx: SubstrateContext,
        port_path: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        factory, _ = _reader_factory_capture()
        _patch_discover(
            monkeypatch,
            [VCPPortCandidate(port_path, 0x0483, 0x374B, "X")],
        )
        client = VCP(ctx, _reader_factory=factory)
        result = client.send_and_read("noop", timeout_s=0.2, inter_line_idle_ms=50)
        assert result.timeout_hit is True
        assert result.reply_lines == ()


# ---------------------------------------------------------------------------
# HIL collision + close idempotent
# ---------------------------------------------------------------------------


class TestCollision:
    def test_explicit_port_mismatch_raises_when_cached(
        self,
        ctx: SubstrateContext,
        port_path: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        factory, _ = _reader_factory_capture()
        _patch_discover(
            monkeypatch,
            [VCPPortCandidate(port_path, 0x0483, 0x374B, "X")],
        )
        client = VCP(ctx, _reader_factory=factory)
        list(client.tail(timeout_s=0.1))
        with pytest.raises(VCPReaderAlreadyActive) as ex:
            list(client.tail(port="/dev/different", timeout_s=0.1))
        assert ex.value.vcp_marker == "reader-already-active"
        assert ex.value.port == port_path

    def test_close_clears_session_slot(
        self,
        ctx: SubstrateContext,
        port_path: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        factory, _ = _reader_factory_capture()
        _patch_discover(
            monkeypatch,
            [VCPPortCandidate(port_path, 0x0483, 0x374B, "X")],
        )
        client = VCP(ctx, _reader_factory=factory)
        list(client.tail(timeout_s=0.1))
        assert ctx.session_state.active_vcp_reader is not None
        client.close()
        assert ctx.session_state.active_vcp_reader is None
        # Second close is harmless.
        client.close()
