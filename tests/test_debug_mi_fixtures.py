"""F-MI fixture-corpus round-trip tests (TST-01).

Per ``v1/debug-api.md`` § "Test surface → For MI parsing": round-trip
every record type from ``tests/fixtures/debug/mi-records/`` through the
MI parser stack. The corpus is real arm-none-eabi-gdb MI3 output
captured on the bench (NUCLEO-L476RG / BLINKY.elf) via
``tools/capture-mi-records.py`` — the mock-fidelity counterpart to the
synthetic strings in ``test_debug_parsers.py`` (same risk class as
RES-028's `.cproject` superClass drift: synthetic fixtures can encode
shapes the real tool never emits).

File format: ``#`` comment lines (the MI command + capture provenance),
then one verbatim MI record per line.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from embedagents.stm32.debug.parsers import (
    parse_breakpoint_insert,
    parse_evaluate_expression,
    parse_memory_read,
    parse_mi_record,
    parse_register_dump,
    parse_stack_list_frames,
    parse_stopped,
)
from embedagents.stm32.debug.results import (
    MIAsyncRecord,
    MIResultRecord,
    MIStreamRecord,
)
from embedagents.stm32.errors import GDBError

MI_RECORDS = (
    Path(__file__).resolve().parent / "fixtures" / "debug" / "mi-records"
)

pytestmark = pytest.mark.skipif(
    not MI_RECORDS.is_dir(),
    reason="F-MI corpus not present (tools/capture-mi-records.py)",
)


def _records(fixture: str, name: str) -> list[str]:
    path = MI_RECORDS / fixture / f"{name}.mi"
    lines = path.read_text(encoding="utf-8").splitlines()
    return [l for l in lines if l and not l.startswith("#")]


def _all_files(fixture: str) -> list[Path]:
    return sorted((MI_RECORDS / fixture).glob("*.mi"))


# ---------------------------------------------------------------------------
# Corpus completeness — all 9 ratified fixture IDs populated
# ---------------------------------------------------------------------------


class TestCorpusComplete:
    def test_all_nine_fixture_dirs_populated(self) -> None:
        expected = {
            "F-MI-RESULT-DONE",
            "F-MI-RESULT-ERROR",
            "F-MI-ASYNC-STOPPED",
            "F-MI-ASYNC-RUNNING",
            "F-MI-STREAM-CONSOLE",
            "F-MI-INTERLEAVED",
            "F-MI-MALFORMED",
            "F-MI-DATA-EVALUATE",
            "F-MI-MEMORY-READ",
        }
        present = {d.name for d in MI_RECORDS.iterdir() if d.is_dir()}
        assert expected <= present, f"missing: {expected - present}"
        for fixture in expected:
            assert _all_files(fixture), f"{fixture} has no capture files"


# ---------------------------------------------------------------------------
# F-MI-RESULT-DONE — ^done records + domain reductions
# ---------------------------------------------------------------------------


class TestResultDone:
    def test_every_capture_parses_as_done_with_token(self) -> None:
        for path in _all_files("F-MI-RESULT-DONE"):
            for line in _records("F-MI-RESULT-DONE", path.stem):
                rec = parse_mi_record(line)
                assert isinstance(rec, MIResultRecord), path.name
                assert rec.class_ == "done", path.name
                assert rec.token is not None, path.name  # token-correlation

    def test_register_dump_reduction(self) -> None:
        names = parse_mi_record(
            _records("F-MI-RESULT-DONE", "register-names")[0]
        )
        values = parse_mi_record(
            _records("F-MI-RESULT-DONE", "register-values")[0]
        )
        dump = parse_register_dump(values, names)
        # Real Cortex-M4 capture: all core regs present and int-decoded;
        # the L476's FPU registers flip fpu_present.
        for reg in ("r0", "sp", "lr", "pc", "xpsr", "msp", "control"):
            assert reg in dump.values, sorted(dump.values)[:20]
        assert isinstance(dump.values["pc"], int)
        assert dump.fpu_present is True

    def test_breakpoint_insert_reduction(self) -> None:
        rec = parse_mi_record(_records("F-MI-RESULT-DONE", "break-insert")[0])
        bp = parse_breakpoint_insert(rec)
        assert bp.number >= 1
        assert "main" in (bp.location or "") or bp.address

    def test_stack_frames_reduction(self) -> None:
        stack = parse_mi_record(
            _records("F-MI-RESULT-DONE", "stack-list-frames")[0]
        )
        threads = parse_mi_record(_records("F-MI-RESULT-DONE", "thread-info")[0])
        cs = parse_stack_list_frames(stack, threads)
        assert cs.frames, "no frames parsed from the real capture"
        assert cs.frames[0].function
        assert cs.frames[0].pc


# ---------------------------------------------------------------------------
# F-MI-RESULT-ERROR — ^error records
# ---------------------------------------------------------------------------


class TestResultError:
    def test_every_capture_parses_as_error_with_msg(self) -> None:
        for path in _all_files("F-MI-RESULT-ERROR"):
            for line in _records("F-MI-RESULT-ERROR", path.stem):
                rec = parse_mi_record(line)
                assert isinstance(rec, MIResultRecord), path.name
                assert rec.class_ == "error", path.name
                assert rec.fields.get("msg"), path.name  # message preserved


# ---------------------------------------------------------------------------
# F-MI-ASYNC-STOPPED / F-MI-ASYNC-RUNNING
# ---------------------------------------------------------------------------


class TestAsyncStopped:
    @pytest.mark.parametrize(
        ("name", "reason"),
        [
            ("breakpoint-hit", "breakpoint-hit"),
            ("signal-received-sigint", "signal-received"),
            ("end-stepping-range", "end-stepping-range"),
            ("exited-normally-synthetic", "exited-normally"),
        ],
    )
    def test_stopped_reasons(self, name: str, reason: str) -> None:
        line = _records("F-MI-ASYNC-STOPPED", name)[0]
        rec = parse_mi_record(line)
        assert isinstance(rec, MIAsyncRecord)
        assert rec.class_ == "stopped"
        stop = parse_stopped(rec)
        assert stop.reason == reason

    def test_breakpoint_hit_carries_number(self) -> None:
        rec = parse_mi_record(_records("F-MI-ASYNC-STOPPED", "breakpoint-hit")[0])
        stop = parse_stopped(rec)
        assert stop.breakpoint_number is not None


class TestAsyncRunning:
    def test_running_records(self) -> None:
        lines = _records("F-MI-ASYNC-RUNNING", "exec-continue")
        parsed = [parse_mi_record(l) for l in lines]
        assert any(
            isinstance(r, MIResultRecord) and r.class_ == "running"
            for r in parsed
        )
        assert any(
            isinstance(r, MIAsyncRecord) and r.class_ == "running"
            for r in parsed
        )


# ---------------------------------------------------------------------------
# F-MI-STREAM-CONSOLE
# ---------------------------------------------------------------------------


class TestStreamConsole:
    def test_stream_records_decode(self) -> None:
        for path in _all_files("F-MI-STREAM-CONSOLE"):
            saw_stream = False
            for line in _records("F-MI-STREAM-CONSOLE", path.stem):
                rec = parse_mi_record(line)
                if isinstance(rec, MIStreamRecord):
                    saw_stream = True
                    assert rec.text  # c-string body decoded
            assert saw_stream, path.name


# ---------------------------------------------------------------------------
# F-MI-INTERLEAVED — async + result interleaving (queue routing)
# ---------------------------------------------------------------------------


class TestInterleaved:
    def test_transcript_routes_each_record_type(self) -> None:
        lines = _records("F-MI-INTERLEAVED", "continue-to-breakpoint")
        kinds = {"result": 0, "async": 0, "stream": 0}
        stopped = None
        for line in lines:
            rec = parse_mi_record(line)
            if isinstance(rec, MIResultRecord):
                kinds["result"] += 1
            elif isinstance(rec, MIAsyncRecord):
                kinds["async"] += 1
                if rec.class_ == "stopped":
                    stopped = parse_stopped(rec)
            elif isinstance(rec, MIStreamRecord):
                kinds["stream"] += 1
        # The real transcript carries all three shapes…
        assert kinds["result"] >= 1 and kinds["async"] >= 2 and kinds["stream"] >= 1, kinds
        # …and the trailing *stopped is the armed breakpoint firing.
        assert stopped is not None and stopped.reason == "breakpoint-hit"


# ---------------------------------------------------------------------------
# F-MI-MALFORMED — protocol violations (IMP-12 contract)
# ---------------------------------------------------------------------------


class TestMalformed:
    def test_malformed_result_records_raise_protocol_violation(self) -> None:
        for path in _all_files("F-MI-MALFORMED"):
            for line in _records("F-MI-MALFORMED", path.stem):
                with pytest.raises(GDBError) as excinfo:
                    parse_mi_record(line)
                assert excinfo.value.gdb_marker == "protocol-violation", path.name


# ---------------------------------------------------------------------------
# F-MI-DATA-EVALUATE — variable kinds
# ---------------------------------------------------------------------------


class TestDataEvaluate:
    def test_integer_variable(self) -> None:
        rec = parse_mi_record(_records("F-MI-DATA-EVALUATE", "integer")[0])
        v = parse_evaluate_expression(rec)
        assert v.integer_value is not None

    def test_struct_rendering_preserved_raw(self) -> None:
        rec = parse_mi_record(_records("F-MI-DATA-EVALUATE", "struct")[0])
        v = parse_evaluate_expression(rec)
        # Real GPIOA dump: gdb's {field = N, ...} rendering survives.
        assert v.raw.startswith("{") and "MODER" in v.raw

    def test_array_rendering(self) -> None:
        rec = parse_mi_record(_records("F-MI-DATA-EVALUATE", "array")[0])
        v = parse_evaluate_expression(rec)
        assert v.raw.startswith("{")

    def test_optimized_out_flagged(self) -> None:
        rec = parse_mi_record(
            _records("F-MI-DATA-EVALUATE", "optimized-out-synthetic")[0]
        )
        v = parse_evaluate_expression(rec)
        assert v.optimized_out is True

    def test_not_in_scope_is_error_record(self) -> None:
        rec = parse_mi_record(
            _records("F-MI-DATA-EVALUATE", "not-in-scope-error")[0]
        )
        assert isinstance(rec, MIResultRecord)
        assert rec.class_ == "error"


# ---------------------------------------------------------------------------
# F-MI-MEMORY-READ — byte round-trips
# ---------------------------------------------------------------------------


class TestMemoryRead:
    def test_word_aligned_4_bytes(self) -> None:
        rec = parse_mi_record(_records("F-MI-MEMORY-READ", "word-aligned-4b")[0])
        assert len(parse_memory_read(rec)) == 4

    def test_larger_block(self) -> None:
        rec = parse_mi_record(_records("F-MI-MEMORY-READ", "block-64b")[0])
        assert len(parse_memory_read(rec)) == 64

    def test_erased_flash_all_ff(self) -> None:
        # The suspicious_unmapped shape: erased high flash reads 0xFF.
        rec = parse_mi_record(_records("F-MI-MEMORY-READ", "erased-flash-ff")[0])
        data = parse_memory_read(rec)
        assert len(data) == 16
        assert all(b == 0xFF for b in data)
