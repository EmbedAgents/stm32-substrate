"""C4d gdb-MI parser tests.

Exercises the strict gdb-MI grammar plus the domain-specific reductions
(register dump, breakpoint, stopped notification, evaluate-expression,
stack list, memory read)."""

from __future__ import annotations

import pytest

from embedagents.stm32.debug.parsers import (
    _Parser,
    parse_breakpoint_insert,
    parse_evaluate_expression,
    parse_memory_read,
    parse_mi_record,
    parse_register_dump,
    parse_stack_list_frames,
    parse_stopped,
)
from embedagents.stm32.debug.results import (
    Breakpoint,
    CallStack,
    MIAsyncRecord,
    MIResultRecord,
    MIStreamRecord,
    RegisterDump,
    StoppedNotification,
    VariableValue,
)


# ---------------------------------------------------------------------------
# parse_mi_record — top-level dispatch
# ---------------------------------------------------------------------------


class TestRecordDispatch:
    def test_prompt_returns_none(self) -> None:
        assert parse_mi_record("(gdb)") is None
        assert parse_mi_record("(gdb) ") is None

    def test_blank_returns_none(self) -> None:
        assert parse_mi_record("") is None
        assert parse_mi_record("\n") is None

    def test_result_record_no_fields(self) -> None:
        r = parse_mi_record("^done")
        assert isinstance(r, MIResultRecord)
        assert r.class_ == "done"
        assert r.token is None
        assert r.fields == {}

    def test_result_record_with_token(self) -> None:
        r = parse_mi_record('42^done,value="hello"')
        assert isinstance(r, MIResultRecord)
        assert r.token == 42
        assert r.class_ == "done"
        assert r.fields["value"] == "hello"

    def test_error_record(self) -> None:
        r = parse_mi_record('^error,msg="No symbol \\"foo\\" in current context."')
        assert isinstance(r, MIResultRecord)
        assert r.class_ == "error"
        assert "foo" in r.fields["msg"]

    def test_async_stopped(self) -> None:
        r = parse_mi_record('*stopped,reason="breakpoint-hit",bkptno="1"')
        assert isinstance(r, MIAsyncRecord)
        assert r.kind == "exec"
        assert r.class_ == "stopped"
        assert r.fields["reason"] == "breakpoint-hit"
        assert r.fields["bkptno"] == "1"

    def test_async_running(self) -> None:
        r = parse_mi_record('*running,thread-id="all"')
        assert isinstance(r, MIAsyncRecord)
        assert r.class_ == "running"

    def test_status_record(self) -> None:
        r = parse_mi_record('+download,section=".text",section-size="1024"')
        assert isinstance(r, MIAsyncRecord)
        assert r.kind == "status"
        assert r.class_ == "download"
        assert r.fields["section"] == ".text"

    def test_notify_record(self) -> None:
        r = parse_mi_record('=thread-created,id="1"')
        assert isinstance(r, MIAsyncRecord)
        assert r.kind == "notify"
        assert r.class_ == "thread-created"

    def test_stream_console(self) -> None:
        r = parse_mi_record('~"Continuing.\\n"')
        assert isinstance(r, MIStreamRecord)
        assert r.stream == "console"
        assert r.text == "Continuing.\n"

    def test_stream_log(self) -> None:
        r = parse_mi_record('&"Quit\\n"')
        assert isinstance(r, MIStreamRecord)
        assert r.stream == "log"


# ---------------------------------------------------------------------------
# Value grammar
# ---------------------------------------------------------------------------


class TestValueGrammar:
    def test_string_unescape(self) -> None:
        p = _Parser('msg="a\\nb"')
        result = p.parse_kv_pairs(end_chars="")
        assert result == {"msg": "a\nb"}

    def test_tuple_value(self) -> None:
        p = _Parser('frame={addr="0x100",func="main"}')
        result = p.parse_kv_pairs(end_chars="")
        assert result["frame"]["addr"] == "0x100"
        assert result["frame"]["func"] == "main"

    def test_list_of_values(self) -> None:
        p = _Parser('groups=["1","2","3"]')
        result = p.parse_kv_pairs(end_chars="")
        assert result["groups"] == ["1", "2", "3"]

    def test_list_of_kv_tuples(self) -> None:
        """gdb-MI sometimes emits ``stack=[frame={...},frame={...}]``;
        we preserve order by promoting each entry to a single-key dict."""
        p = _Parser('stack=[frame={level="0"},frame={level="1"}]')
        result = p.parse_kv_pairs(end_chars="")
        stack = result["stack"]
        assert isinstance(stack, list)
        assert stack[0]["frame"]["level"] == "0"
        assert stack[1]["frame"]["level"] == "1"

    def test_repeated_key_promoted_to_list(self) -> None:
        # When duplicate top-level keys appear (rare), promote.
        p = _Parser('x="a",x="b",x="c"')
        result = p.parse_kv_pairs(end_chars="")
        assert result["x"] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# parse_register_dump
# ---------------------------------------------------------------------------


class TestRegisterDump:
    def test_combines_names_and_values(self) -> None:
        names = parse_mi_record(
            '^done,register-names=["r0","r1","sp","lr","pc"]'
        )
        values = parse_mi_record(
            '^done,register-values=['
            '{number="0",value="0x0"},'
            '{number="2",value="0x20001000"},'
            '{number="4",value="0x08000234"}'
            ']'
        )
        assert isinstance(names, MIResultRecord)
        assert isinstance(values, MIResultRecord)
        rd = parse_register_dump(values, names)
        assert isinstance(rd, RegisterDump)
        assert rd.values["r0"] == 0
        assert rd.values["sp"] == 0x20001000
        assert rd.values["pc"] == 0x08000234
        assert rd.fpu_present is False

    def test_detects_fpu_registers(self) -> None:
        names = parse_mi_record(
            '^done,register-names=["r0","s0","s1","fpscr"]'
        )
        values = parse_mi_record(
            '^done,register-values=['
            '{number="0",value="0x0"},'
            '{number="1",value="0x3f800000"},'
            '{number="2",value="0x40000000"}'
            ']'
        )
        rd = parse_register_dump(values, names)
        assert rd.fpu_present is True


# ---------------------------------------------------------------------------
# parse_breakpoint_insert
# ---------------------------------------------------------------------------


class TestBreakpointInsert:
    def test_basic(self) -> None:
        r = parse_mi_record(
            '^done,bkpt={number="1",type="breakpoint",disp="keep",'
            'enabled="y",addr="0x08001234",func="main",file="main.c",'
            'fullname="/abs/main.c",line="42",original-location="main.c:42"}'
        )
        assert isinstance(r, MIResultRecord)
        bp = parse_breakpoint_insert(r)
        assert isinstance(bp, Breakpoint)
        assert bp.number == 1
        assert bp.address == "0x08001234"
        assert bp.file in ("/abs/main.c", "main.c")
        assert bp.line == 42

    def test_pending_breakpoint(self) -> None:
        # Pending bkpts have no addr / line — substrate keeps them.
        r = parse_mi_record(
            '^done,bkpt={number="2",type="breakpoint",pending="missing",'
            'original-location="late_loader.c:99"}'
        )
        assert isinstance(r, MIResultRecord)
        bp = parse_breakpoint_insert(r)
        assert bp.number == 2
        assert bp.address is None
        assert bp.line is None


# ---------------------------------------------------------------------------
# parse_stopped
# ---------------------------------------------------------------------------


class TestStopped:
    def test_breakpoint_hit(self) -> None:
        r = parse_mi_record(
            '*stopped,reason="breakpoint-hit",disp="keep",bkptno="1",'
            'frame={level="0",addr="0x08001234",func="main",'
            'file="main.c",line="42"}'
        )
        assert isinstance(r, MIAsyncRecord)
        s = parse_stopped(r)
        assert isinstance(s, StoppedNotification)
        assert s.reason == "breakpoint-hit"
        assert s.breakpoint_number == 1
        assert s.frame is not None
        assert s.frame.function == "main"
        assert s.frame.line == 42

    def test_signal_received(self) -> None:
        r = parse_mi_record(
            '*stopped,reason="signal-received",signal-name="SIGSEGV",'
            'frame={level="0",addr="0xdeadbeef"}'
        )
        assert isinstance(r, MIAsyncRecord)
        s = parse_stopped(r)
        assert s.reason == "signal-received"
        assert s.signal_name == "SIGSEGV"

    def test_unknown_reason_maps_to_unknown(self) -> None:
        r = parse_mi_record('*stopped,reason="something-new"')
        assert isinstance(r, MIAsyncRecord)
        s = parse_stopped(r)
        assert s.reason == "unknown"


# ---------------------------------------------------------------------------
# parse_evaluate_expression
# ---------------------------------------------------------------------------


class TestEvaluateExpression:
    def test_simple_integer(self) -> None:
        r = parse_mi_record('^done,value="42"')
        assert isinstance(r, MIResultRecord)
        v = parse_evaluate_expression(r)
        assert isinstance(v, VariableValue)
        assert v.raw == "42"
        assert v.integer_value == 42

    def test_hex_value(self) -> None:
        r = parse_mi_record('^done,value="0x42"')
        assert isinstance(r, MIResultRecord)
        v = parse_evaluate_expression(r)
        assert v.integer_value == 0x42

    def test_char_value_with_trailing(self) -> None:
        r = parse_mi_record(r'^done,value="10 \'\\n\'"')
        assert isinstance(r, MIResultRecord)
        v = parse_evaluate_expression(r)
        assert v.integer_value == 10

    def test_optimized_out(self) -> None:
        r = parse_mi_record('^done,value="<optimized out>"')
        assert isinstance(r, MIResultRecord)
        v = parse_evaluate_expression(r)
        assert v.optimized_out is True

    def test_unparseable_keeps_raw(self) -> None:
        r = parse_mi_record('^done,value="some struct"')
        assert isinstance(r, MIResultRecord)
        v = parse_evaluate_expression(r)
        assert v.raw == "some struct"
        assert v.integer_value is None


# ---------------------------------------------------------------------------
# parse_stack_list_frames
# ---------------------------------------------------------------------------


class TestStackListFrames:
    def test_frames_only(self) -> None:
        r = parse_mi_record(
            '^done,stack=['
            'frame={level="0",addr="0x08001000",func="main",file="main.c",line="42"},'
            'frame={level="1",addr="0x08000800",func="_start"}'
            ']'
        )
        assert isinstance(r, MIResultRecord)
        cs = parse_stack_list_frames(r)
        assert isinstance(cs, CallStack)
        assert len(cs.frames) == 2
        assert cs.frames[0].function == "main"
        assert cs.frames[0].line == 42
        assert cs.frames[1].function == "_start"

    def test_full_merges_args_by_level(self) -> None:
        """A-004: callstack(full=True) used to substitute the frames
        command with -stack-list-arguments, whose stack-args key the
        parser never read — frames came back empty."""
        stack = parse_mi_record(
            '^done,stack=['
            'frame={level="0",addr="0x08001000",func="main",file="main.c",line="42"},'
            'frame={level="1",addr="0x08000800",func="_start"}'
            ']'
        )
        args = parse_mi_record(
            '^done,stack-args=['
            'frame={level="0",args=[{name="argc",value="1"},{name="argv",value="0x20001000"}]},'
            'frame={level="1",args=[]}'
            ']'
        )
        assert isinstance(stack, MIResultRecord)
        assert isinstance(args, MIResultRecord)
        cs = parse_stack_list_frames(stack, args_record=args)
        assert len(cs.frames) == 2  # frames survive — never empty
        assert cs.frames[0].function == "main"
        assert cs.frames[0].args == {"argc": "1", "argv": "0x20001000"}
        assert cs.frames[1].args == {}  # full requested, frame has no args

    def test_without_args_record_args_stay_none(self) -> None:
        stack = parse_mi_record(
            '^done,stack=[frame={level="0",addr="0x100",func="main"}]'
        )
        assert isinstance(stack, MIResultRecord)
        cs = parse_stack_list_frames(stack)
        assert cs.frames[0].args is None

    def test_with_threads(self) -> None:
        stack = parse_mi_record(
            '^done,stack=[frame={level="0",addr="0x100",func="main"}]'
        )
        threads = parse_mi_record(
            '^done,threads=['
            '{id="1",name="cpu0",state="stopped"},'
            '{id="2",name="cpu1",state="running"}'
            '],current-thread-id="2"'
        )
        assert isinstance(stack, MIResultRecord)
        assert isinstance(threads, MIResultRecord)
        cs = parse_stack_list_frames(stack, threads)
        assert len(cs.threads) == 2
        assert cs.threads[0].state == "halted"  # stopped → halted
        assert cs.threads[1].state == "running"
        assert cs.active_thread_index == 1


# ---------------------------------------------------------------------------
# parse_memory_read
# ---------------------------------------------------------------------------


class TestMemoryRead:
    def test_decodes_hex_contents(self) -> None:
        r = parse_mi_record(
            '^done,memory=[{begin="0x20000000",end="0x20000010",'
            'offset="0x0",contents="deadbeefcafebabe1122334455667788"}]'
        )
        assert isinstance(r, MIResultRecord)
        data = parse_memory_read(r)
        assert data == bytes.fromhex("deadbeefcafebabe1122334455667788")

    def test_empty_memory(self) -> None:
        r = parse_mi_record('^done,memory=[]')
        assert isinstance(r, MIResultRecord)
        assert parse_memory_read(r) == b""

    def test_missing_memory_field(self) -> None:
        r = parse_mi_record('^done')
        assert isinstance(r, MIResultRecord)
        assert parse_memory_read(r) == b""


# ---------------------------------------------------------------------------
# IMP-12 — typed errors on grammar drift / truncation
# ---------------------------------------------------------------------------


class TestParseMiRecordHardening:
    def test_truncated_result_record_raises_typed_gdb_error(self) -> None:
        from embedagents.stm32.errors import GDBError

        with pytest.raises(GDBError) as excinfo:
            parse_mi_record('^done,memory=[{begin="0x0"')
        assert excinfo.value.gdb_marker == "protocol-violation"

    def test_truncated_async_record_skipped_not_raised(self) -> None:
        # An unparseable async record is dropped like any unrecognised
        # shape — the in-flight command must not die for it.
        assert parse_mi_record('*stopped,frame={addr="0x1"') is None


# ---------------------------------------------------------------------------
# IMP-14 — multi-block -data-read-memory-bytes results
# ---------------------------------------------------------------------------


class TestParseMemoryReadMultiBlock:
    def test_contiguous_blocks_stitched_in_order(self) -> None:
        rec = parse_mi_record(
            '^done,memory=['
            '{begin="0x20000000",offset="0x0",end="0x20000004",contents="aabbccdd"},'
            '{begin="0x20000004",offset="0x4",end="0x20000008",contents="11223344"}'
            ']'
        )
        assert parse_memory_read(rec) == bytes.fromhex("aabbccdd11223344")

    def test_out_of_order_blocks_sorted_by_offset(self) -> None:
        rec = parse_mi_record(
            '^done,memory=['
            '{begin="0x20000004",offset="0x4",end="0x20000008",contents="11223344"},'
            '{begin="0x20000000",offset="0x0",end="0x20000004",contents="aabbccdd"}'
            ']'
        )
        assert parse_memory_read(rec) == bytes.fromhex("aabbccdd11223344")

    def test_unreadable_hole_truncates_to_contiguous_prefix(self) -> None:
        # Block at offset 0x8 leaves a 4-byte hole — data past the hole
        # must not be silently glued to the wrong address.
        rec = parse_mi_record(
            '^done,memory=['
            '{begin="0x20000000",offset="0x0",end="0x20000004",contents="aabbccdd"},'
            '{begin="0x20000008",offset="0x8",end="0x2000000c",contents="11223344"}'
            ']'
        )
        assert parse_memory_read(rec) == bytes.fromhex("aabbccdd")
