"""``stm32 vcp`` CLI argv parsing + dispatch.

Each subcommand: verify argparse routes the right kwargs to the
``VCP`` instance the CLI constructs. The VCP class itself is mocked so
no fake-pyserial threads spawn.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from stm32_substrate.cli import main
from stm32_substrate.errors import VCPError
from stm32_substrate.vcp.results import (
    PriorVCPState,
    ReconnectResult,
    RequestResponse,
)


@pytest.fixture()
def mock_vcp(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    instance = MagicMock(name="VCP-instance")
    factory = MagicMock(return_value=instance)
    # The CLI does `from stm32_substrate.vcp import VCP` at module load,
    # so the binding to patch is on `cli._vcp`.
    monkeypatch.setattr("stm32_substrate.cli._vcp.VCP", factory)
    return instance


def _run(argv: list[str], capsys: pytest.CaptureFixture) -> tuple[int, str, str]:
    code = main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


# ---------------------------------------------------------------------------
# Help discoverability
# ---------------------------------------------------------------------------


class TestHelpListsVcp:
    def test_top_level_help_includes_vcp(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        with pytest.raises(SystemExit):
            _run(["--help"], capsys)
        out = capsys.readouterr().out
        assert "vcp" in out

    def test_vcp_help_lists_four_subcommands(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        with pytest.raises(SystemExit):
            _run(["vcp", "--help"], capsys)
        out = capsys.readouterr().out
        for sub in ("tail", "send", "reconnect", "close"):
            assert sub in out


# ---------------------------------------------------------------------------
# tail
# ---------------------------------------------------------------------------


class TestTail:
    def test_snapshot_argv_default(
        self, mock_vcp: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        mock_vcp.tail.return_value = iter(["line-a", "line-b"])
        code, out, _ = _run(["vcp", "tail", "--last-n", "2", "--timeout", "1.0"], capsys)
        assert code == 0
        kwargs = mock_vcp.tail.call_args.kwargs
        assert kwargs["last_n"] == 2
        assert kwargs["follow"] is False
        assert kwargs["timeout_s"] == 1.0
        assert "line-a" in out
        assert "line-b" in out

    def test_follow_flag(
        self, mock_vcp: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        mock_vcp.tail.return_value = iter([])
        _run(["vcp", "tail", "--follow"], capsys)
        assert mock_vcp.tail.call_args.kwargs["follow"] is True

    def test_port_baud_override(
        self, mock_vcp: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        mock_vcp.tail.return_value = iter([])
        _run(["vcp", "tail", "--port", "/dev/ttyACM5", "--baud", "9600"], capsys)
        kwargs = mock_vcp.tail.call_args.kwargs
        assert kwargs["port"] == "/dev/ttyACM5"
        assert kwargs["baud"] == 9600

    def test_keyboard_interrupt_exits_clean(
        self, mock_vcp: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        def _raise():
            raise KeyboardInterrupt
            yield  # pragma: no cover - unreachable

        mock_vcp.tail.return_value = _raise()
        code, _, _ = _run(["vcp", "tail", "--follow"], capsys)
        assert code == 0


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------


class TestSend:
    def _result(self) -> RequestResponse:
        return RequestResponse(
            sent_line="hi",
            reply_lines=("hello",),
            timeout_hit=False,
            duration_s=0.1,
            port="/dev/ttyACM0",
            baud=115200,
        )

    def test_minimal_argv(
        self, mock_vcp: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        mock_vcp.send_and_read.return_value = self._result()
        code, out, _ = _run(["vcp", "send", "hi"], capsys)
        assert code == 0
        call = mock_vcp.send_and_read.call_args
        assert call.args[0] == "hi"
        assert call.kwargs["echo_filter"] is False
        payload = json.loads(out)
        assert payload["sent_line"] == "hi"
        assert payload["reply_lines"] == ["hello"]

    def test_full_argv(
        self, mock_vcp: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        mock_vcp.send_and_read.return_value = self._result()
        _run(
            [
                "vcp", "send", "hi",
                "--port", "/dev/ttyACM5",
                "--baud", "9600",
                "--terminator", "\r\n",
                "--timeout", "0.5",
                "--inter-line-idle-ms", "200",
                "--echo-filter",
            ],
            capsys,
        )
        call = mock_vcp.send_and_read.call_args
        assert call.kwargs["port"] == "/dev/ttyACM5"
        assert call.kwargs["baud"] == 9600
        assert call.kwargs["terminator"] == "\r\n"
        assert call.kwargs["timeout_s"] == 0.5
        assert call.kwargs["inter_line_idle_ms"] == 200
        assert call.kwargs["echo_filter"] is True


# ---------------------------------------------------------------------------
# reconnect
# ---------------------------------------------------------------------------


class TestReconnect:
    def test_default(
        self, mock_vcp: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        mock_vcp.reconnect.return_value = ReconnectResult(
            port="/dev/ttyACM0",
            status="reconnected",
            prior_state=PriorVCPState(
                port=None, baud=None, last_byte_timestamp_s=None, open=False
            ),
            duration_s=0.0,
        )
        code, out, _ = _run(["vcp", "reconnect"], capsys)
        assert code == 0
        kwargs = mock_vcp.reconnect.call_args.kwargs
        assert kwargs["port"] is None
        assert kwargs["max_wait_s"] is None
        payload = json.loads(out)
        assert payload["status"] == "reconnected"

    def test_max_wait_argv(
        self, mock_vcp: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        mock_vcp.reconnect.return_value = ReconnectResult(
            port="/dev/ttyACM0",
            status="same_port",
            prior_state=PriorVCPState(
                port="/dev/ttyACM0", baud=115200, last_byte_timestamp_s=None, open=True
            ),
            duration_s=0.05,
        )
        _run(["vcp", "reconnect", "--max-wait", "3.0"], capsys)
        assert mock_vcp.reconnect.call_args.kwargs["max_wait_s"] == 3.0


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


class TestClose:
    def test_calls_close(
        self, mock_vcp: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        code, _, _ = _run(["vcp", "close"], capsys)
        assert code == 0
        assert mock_vcp.close.call_count == 1


# ---------------------------------------------------------------------------
# Errors → stderr JSON envelope
# ---------------------------------------------------------------------------


class TestErrors:
    def test_substrate_error_yields_stderr_envelope(
        self, mock_vcp: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        mock_vcp.reconnect.side_effect = VCPError(
            message="probe did not re-enumerate",
            vcp_marker="reconnect-timeout",
            port="/dev/ttyACM0",
        )
        code, _, err = _run(["vcp", "reconnect"], capsys)
        assert code == 1
        payload = json.loads(err)
        assert payload["error_type"] == "VCPError"
        assert payload["vcp_marker"] == "reconnect-timeout"
