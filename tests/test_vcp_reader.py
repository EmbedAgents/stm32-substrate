"""``_VcpReader`` drain-thread + bounded-queue + reconnect behaviour.

Uses a fake pyserial backend (no real `/dev/ttyACM*` open) and a
file-touch trick so ``os.path.exists(port)`` returns True while the test
controls byte arrival.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import pytest

from stm32_substrate.context import SubstrateContext
from stm32_substrate.errors import VCPError, VCPPortInUse
from stm32_substrate.vcp.reader import _VcpReader


# ---------------------------------------------------------------------------
# Fake pyserial Serial
# ---------------------------------------------------------------------------


class _FakeSerial:
    """Minimal stand-in for pyserial.Serial.

    The drain thread calls ``read(n)`` in a loop with the configured
    ``timeout``. Tests push bytes via ``feed(...)``; the reader sees them
    on its next ``read()`` cycle. ``raise_on_open`` lets tests simulate
    ``PermissionError`` on construction.
    """

    raise_on_open: Exception | None = None

    def __init__(self, *, port: str, baudrate: int, timeout: float) -> None:
        if _FakeSerial.raise_on_open is not None:
            raise _FakeSerial.raise_on_open
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.is_open = True
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.written = bytearray()

    def feed(self, data: bytes) -> None:
        with self._lock:
            self._buf.extend(data)

    def read(self, n: int) -> bytes:
        # Simulate the blocking-with-timeout pyserial behaviour: poll up to
        # ``self.timeout`` for bytes, return whatever is available.
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


def _serial_factory_capture():
    """Returns ``(factory, holder)`` — ``holder['serial']`` set on open."""
    holder: dict[str, _FakeSerial] = {}

    def factory(**kwargs):
        ser = _FakeSerial(**kwargs)
        holder["serial"] = ser
        return ser

    return factory, holder


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def ctx(tmp_path: Path) -> SubstrateContext:
    return SubstrateContext.from_environment(project_path=tmp_path)


@pytest.fixture()
def port_path(tmp_path: Path) -> str:
    """File on disk so ``os.path.exists(port)`` reports True."""
    p = tmp_path / "ttyACM-fake"
    p.write_text("")
    return str(p)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_open_close_idempotent(
        self, ctx: SubstrateContext, port_path: str
    ) -> None:
        factory, holder = _serial_factory_capture()
        reader = _VcpReader(
            ctx=ctx,
            port=port_path,
            baud=115200,
            _serial_factory=factory,
        )
        reader.open()
        assert reader._open is True
        ser = holder["serial"]
        assert ser.is_open is True
        # Second open() is idempotent.
        reader.open()
        reader.close()
        assert ser.is_open is False
        # Second close() also safe.
        reader.close()

    def test_is_alive_false_when_port_disappears(
        self, ctx: SubstrateContext, port_path: str
    ) -> None:
        factory, _holder = _serial_factory_capture()
        reader = _VcpReader(
            ctx=ctx, port=port_path, baud=115200, _serial_factory=factory
        )
        reader.open()
        assert reader.is_alive() is True
        # Remove the file → port "disappeared" via USB unplug equivalent.
        os.remove(port_path)
        assert reader.is_alive() is False
        reader.close()

    def test_open_translates_permission_error_to_port_in_use(
        self, ctx: SubstrateContext, port_path: str
    ) -> None:
        _FakeSerial.raise_on_open = PermissionError("13: Permission denied")
        try:
            factory, _ = _serial_factory_capture()
            reader = _VcpReader(
                ctx=ctx, port=port_path, baud=115200, _serial_factory=factory
            )
            with pytest.raises(VCPPortInUse) as ex:
                reader.open()
            assert ex.value.vcp_marker == "port-in-use"
            assert ex.value.port == port_path
        finally:
            _FakeSerial.raise_on_open = None


# ---------------------------------------------------------------------------
# Byte-to-line decoding
# ---------------------------------------------------------------------------


class TestDecoding:
    def test_lf_terminator_splits_lines(
        self, ctx: SubstrateContext, port_path: str
    ) -> None:
        factory, holder = _serial_factory_capture()
        reader = _VcpReader(
            ctx=ctx, port=port_path, baud=115200, _serial_factory=factory
        )
        reader.open()
        try:
            holder["serial"].feed(b"alpha\nbravo\ncharlie\n")
            lines = list(reader.read_lines(last_n=3, timeout_s=1.0))
            assert lines == ["alpha", "bravo", "charlie"]
        finally:
            reader.close()

    def test_crlf_terminator_stripped(
        self, ctx: SubstrateContext, port_path: str
    ) -> None:
        factory, holder = _serial_factory_capture()
        reader = _VcpReader(
            ctx=ctx,
            port=port_path,
            baud=115200,
            terminator="\r\n",
            _serial_factory=factory,
        )
        reader.open()
        try:
            holder["serial"].feed(b"alpha\r\nbravo\r\n")
            lines = list(reader.read_lines(last_n=2, timeout_s=1.0))
            assert lines == ["alpha", "bravo"]
        finally:
            reader.close()

    def test_lf_terminator_strips_trailing_cr_too(
        self, ctx: SubstrateContext, port_path: str
    ) -> None:
        # Firmware that prints CRLF but substrate is configured for LF should
        # still emit clean lines — the trailing CR is stripped.
        factory, holder = _serial_factory_capture()
        reader = _VcpReader(
            ctx=ctx,
            port=port_path,
            baud=115200,
            terminator="\n",
            _serial_factory=factory,
        )
        reader.open()
        try:
            holder["serial"].feed(b"alpha\r\nbravo\r\n")
            lines = list(reader.read_lines(last_n=2, timeout_s=1.0))
            assert lines == ["alpha", "bravo"]
        finally:
            reader.close()

    def test_invalid_utf8_replaced_and_warned_once(
        self,
        ctx: SubstrateContext,
        port_path: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        factory, holder = _serial_factory_capture()
        reader = _VcpReader(
            ctx=ctx, port=port_path, baud=115200, _serial_factory=factory
        )
        reader.open()
        try:
            with caplog.at_level(logging.WARNING, logger="stm32_substrate.vcp"):
                holder["serial"].feed(b"bad\xffbyte\nworse\xfe\n")
                lines = list(reader.read_lines(last_n=2, timeout_s=1.0))
            assert "�" in "".join(lines)
            warn_msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
            encoding_warns = [
                r for r in warn_msgs if "encoding-error" in r.getMessage()
            ]
            # Exactly one warning even though two lines had bad bytes.
            assert len(encoding_warns) == 1
        finally:
            reader.close()

    def test_partial_line_buffered_until_terminator(
        self, ctx: SubstrateContext, port_path: str
    ) -> None:
        factory, holder = _serial_factory_capture()
        reader = _VcpReader(
            ctx=ctx, port=port_path, baud=115200, _serial_factory=factory
        )
        reader.open()
        try:
            holder["serial"].feed(b"hel")
            # No newline yet → empty.
            snapshot = list(reader.read_lines(last_n=1, timeout_s=0.2))
            assert snapshot == []
            holder["serial"].feed(b"lo\n")
            lines = list(reader.read_lines(last_n=1, timeout_s=1.0))
            assert lines == ["hello"]
        finally:
            reader.close()


# ---------------------------------------------------------------------------
# Bounded queue + drop-oldest
# ---------------------------------------------------------------------------


class TestBoundedQueue:
    def test_overflow_drops_oldest_and_warns(
        self,
        tmp_path: Path,
        port_path: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Build a context with a tiny line buffer for fast overflow.
        defaults_file = tmp_path / "stm32-runtime-defaults.jsonc"
        defaults_file.write_text(
            '{ "version": 1, '
            '  "vcp": { "line_buffer_capacity": 3, "drain_read_timeout_ms": 50 } }'
        )
        ctx = SubstrateContext.from_environment(
            project_path=tmp_path, defaults_config_path=defaults_file
        )
        factory, holder = _serial_factory_capture()
        reader = _VcpReader(
            ctx=ctx, port=port_path, baud=115200, _serial_factory=factory
        )
        reader.open()
        try:
            with caplog.at_level(logging.WARNING, logger="stm32_substrate.vcp"):
                holder["serial"].feed(b"a\nb\nc\nd\ne\n")
                # Wait for the drain thread to absorb the chunk.
                time.sleep(0.2)
            with reader._lock:
                buf = list(reader._buffer)
            assert len(buf) == 3
            # Oldest dropped → last three survive.
            assert buf == ["c", "d", "e"]
            overflow = [
                r
                for r in caplog.records
                if r.levelno == logging.WARNING
                and "line buffer overflow" in r.getMessage()
            ]
            assert overflow, "expected at least one overflow WARNING"
        finally:
            reader.close()


# ---------------------------------------------------------------------------
# Idle-timeout multi-line read
# ---------------------------------------------------------------------------


class TestIdleRead:
    def test_collects_multi_line_before_idle_expires(
        self, ctx: SubstrateContext, port_path: str
    ) -> None:
        factory, holder = _serial_factory_capture()
        reader = _VcpReader(
            ctx=ctx, port=port_path, baud=115200, _serial_factory=factory
        )
        reader.open()
        try:
            holder["serial"].feed(b"one\ntwo\nthree\n")
            lines, timeout_hit = reader.read_lines_with_idle(
                timeout_s=2.0, inter_line_idle_ms=200
            )
            assert lines == ("one", "two", "three")
            assert timeout_hit is False
        finally:
            reader.close()

    def test_returns_timeout_when_no_reply(
        self, ctx: SubstrateContext, port_path: str
    ) -> None:
        factory, _holder = _serial_factory_capture()
        reader = _VcpReader(
            ctx=ctx, port=port_path, baud=115200, _serial_factory=factory
        )
        reader.open()
        try:
            lines, timeout_hit = reader.read_lines_with_idle(
                timeout_s=0.2, inter_line_idle_ms=50
            )
            assert lines == ()
            assert timeout_hit is True
        finally:
            reader.close()


# ---------------------------------------------------------------------------
# Reconnect (uses discover_vcp_ports → injected via the module fn)
# ---------------------------------------------------------------------------


class TestReconnect:
    def test_reconnect_timeout_raises(
        self,
        ctx: SubstrateContext,
        port_path: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        factory, _holder = _serial_factory_capture()
        reader = _VcpReader(
            ctx=ctx, port=port_path, baud=115200, _serial_factory=factory
        )
        reader.open()
        # Force the re-enumeration helper to always return empty.
        from stm32_substrate.vcp import reader as reader_mod

        monkeypatch.setattr(reader_mod, "discover_vcp_ports", lambda *a, **k: [])
        with pytest.raises(VCPError) as ex:
            reader.reconnect(max_wait_s=0.2)
        assert ex.value.vcp_marker == "reconnect-timeout"
        # close() is idempotent — should still be callable in cleanup.
        reader.close()

    def test_reconnect_same_port(
        self,
        ctx: SubstrateContext,
        port_path: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        factory, _holder = _serial_factory_capture()
        reader = _VcpReader(
            ctx=ctx,
            port=port_path,
            baud=115200,
            probe_sn="ABC",
            _serial_factory=factory,
        )
        reader.open()
        from stm32_substrate.vcp import reader as reader_mod
        from stm32_substrate.vcp.results import VCPPortCandidate

        def _fake_discover(*, probe_sn=None):
            assert probe_sn == "ABC"
            return [
                VCPPortCandidate(
                    port=port_path, vid=0x0483, pid=0x374B, serial_number="ABC"
                )
            ]

        monkeypatch.setattr(reader_mod, "discover_vcp_ports", _fake_discover)
        result = reader.reconnect(max_wait_s=1.0)
        assert result.status == "same_port"
        assert result.port == port_path
        reader.close()

    def test_reconnect_new_port(
        self,
        ctx: SubstrateContext,
        port_path: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        new_port = str(tmp_path / "ttyACM-renamed")
        Path(new_port).write_text("")
        factory, _holder = _serial_factory_capture()
        reader = _VcpReader(
            ctx=ctx,
            port=port_path,
            baud=115200,
            probe_sn="ABC",
            _serial_factory=factory,
        )
        reader.open()
        from stm32_substrate.vcp import reader as reader_mod
        from stm32_substrate.vcp.results import VCPPortCandidate

        monkeypatch.setattr(
            reader_mod,
            "discover_vcp_ports",
            lambda *, probe_sn=None: [
                VCPPortCandidate(
                    port=new_port, vid=0x0483, pid=0x374B, serial_number="ABC"
                )
            ],
        )
        result = reader.reconnect(max_wait_s=1.0)
        assert result.status == "reconnected"
        assert result.port == new_port
        assert reader.port == new_port
        reader.close()


# ---------------------------------------------------------------------------
# write_line
# ---------------------------------------------------------------------------


class TestWriteLine:
    def test_appends_terminator(
        self, ctx: SubstrateContext, port_path: str
    ) -> None:
        factory, holder = _serial_factory_capture()
        reader = _VcpReader(
            ctx=ctx,
            port=port_path,
            baud=115200,
            terminator="\n",
            _serial_factory=factory,
        )
        reader.open()
        try:
            reader.write_line("hello")
            assert bytes(holder["serial"].written) == b"hello\n"
        finally:
            reader.close()

    def test_per_call_terminator_overrides(
        self, ctx: SubstrateContext, port_path: str
    ) -> None:
        factory, holder = _serial_factory_capture()
        reader = _VcpReader(
            ctx=ctx,
            port=port_path,
            baud=115200,
            terminator="\n",
            _serial_factory=factory,
        )
        reader.open()
        try:
            reader.write_line("hi", terminator="\r\n")
            assert bytes(holder["serial"].written) == b"hi\r\n"
        finally:
            reader.close()

    def test_write_before_open_raises(
        self, ctx: SubstrateContext, port_path: str
    ) -> None:
        factory, _holder = _serial_factory_capture()
        reader = _VcpReader(
            ctx=ctx, port=port_path, baud=115200, _serial_factory=factory
        )
        # Never opened — serial is None.
        with pytest.raises(VCPError):
            reader.write_line("hi")
