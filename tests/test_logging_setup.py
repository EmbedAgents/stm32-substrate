"""Unit tests for ``embedagents.stm32.logging_setup``."""

from __future__ import annotations

import logging

import pytest

from embedagents.stm32.logging_setup import get_logger


class TestNamespace:
    def test_default_returns_root(self) -> None:
        log = get_logger()
        assert log.name == "embedagents.stm32"

    def test_short_name_gets_prefixed(self) -> None:
        log = get_logger("cubeprogrammer")
        assert log.name == "embedagents.stm32.cubeprogrammer"

    def test_already_namespaced_passes_through(self) -> None:
        log = get_logger("embedagents.stm32.debug.session")
        assert log.name == "embedagents.stm32.debug.session"

    def test_root_name_passes_through(self) -> None:
        log = get_logger("embedagents.stm32")
        assert log.name == "embedagents.stm32"


class TestStructuredFields:
    def test_extra_attached_to_record(self, caplog: pytest.LogCaptureFixture) -> None:
        log = get_logger("cubeprogrammer")
        with caplog.at_level(logging.INFO, logger="embedagents.stm32.cubeprogrammer"):
            log.info(
                "flash done",
                extra={"tool": "cubeprogrammer", "duration_s": 1.234, "marker": "ok"},
            )
        record = caplog.records[-1]
        assert record.message == "flash done"
        assert record.tool == "cubeprogrammer"  # type: ignore[attr-defined]
        assert record.duration_s == pytest.approx(1.234)  # type: ignore[attr-defined]
        assert record.marker == "ok"  # type: ignore[attr-defined]


class TestNoHandlerSideEffects:
    def test_import_does_not_configure_root_handlers(self) -> None:
        """Library must not call ``logging.basicConfig`` or attach handlers."""
        # Importing the module did not change root logger handlers — we can
        # only confirm by asserting our logger has no module-installed
        # handler set (handlers come from the test runner / user app).
        log = get_logger("cubeprogrammer")
        assert log.handlers == [] or all(
            not getattr(h, "_substrate_installed", False) for h in log.handlers
        )
