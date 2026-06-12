"""gdb-MI record parsers.

Per ``v1/debug-api.md`` § "parsers.py". Strict grammar per GDB Manual
§27.2: every line is one of:

- Result record: ``<token>^class[,kv-pairs]``
- Async record: ``<token>(* | + | =)class[,kv-pairs]``
  ('*' = exec, '+' = status, '=' = notify)
- Stream record: ``~"text"`` / ``@"text"`` / ``&"text"``
- Prompt: ``(gdb)`` — terminator, returns ``None``.

Pure functions; no I/O. The ``GDBClient`` (gdb.py) reads stdin/stdout
and feeds lines through here.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from stm32_substrate.debug.results import (
    Breakpoint,
    CallStack,
    MIAsyncRecord,
    MIResultRecord,
    MIStreamRecord,
    RegisterDump,
    StackFrame,
    StoppedNotification,
    ThreadInfo,
    VariableValue,
)
from stm32_substrate.errors import GDBError

_log = logging.getLogger("stm32_substrate.debug.parsers")


# ---------------------------------------------------------------------------
# Record dispatch
# ---------------------------------------------------------------------------


_TOKEN_RE = re.compile(r"^(\d*)([\^*+=~@&])(.*)$")


def parse_mi_record(
    line: str,
) -> MIResultRecord | MIAsyncRecord | MIStreamRecord | None:
    """Parse one gdb-MI output line.

    Returns ``None`` for the ``(gdb)`` prompt + blank lines + unrecognised
    shapes (defensive — caller continues consuming).
    """
    stripped = line.rstrip("\r\n")
    if not stripped or stripped == "(gdb)" or stripped.startswith("(gdb)"):
        return None
    m = _TOKEN_RE.match(stripped)
    if not m:
        return None
    token_s, sigil, rest = m.group(1), m.group(2), m.group(3)
    token = int(token_s) if token_s else None

    # IMP-12: a truncated/grammar-drifted line must not escape as a raw
    # stdlib ValueError through every DebugSession method. A result
    # record we can't parse is fatal for the in-flight command → typed
    # GDBError. An unparseable async record is skipped like any other
    # unrecognised shape (logged; the caller keeps consuming).
    if sigil == "^":
        try:
            klass, fields = _split_class_and_fields(rest)
        except ValueError as ex:
            raise GDBError(
                message=f"unparseable gdb-MI result record: {stripped!r}",
                gdb_marker="protocol-violation",
                hint=(
                    "gdb emitted a result record outside the MI grammar "
                    "substrate parses — possibly truncated output or an "
                    "MI version drift"
                ),
            ) from ex
        return MIResultRecord(token=token, class_=klass, fields=fields)
    if sigil in ("*", "+", "="):
        kind: str
        kind = {"*": "exec", "+": "status", "=": "notify"}[sigil]
        try:
            klass, fields = _split_class_and_fields(rest)
        except ValueError:
            _log.warning("skipping unparseable gdb-MI async record: %r", stripped)
            return None
        return MIAsyncRecord(kind=kind, class_=klass, fields=fields)  # type: ignore[arg-type]
    if sigil in ("~", "@", "&"):
        stream = {"~": "console", "@": "target", "&": "log"}[sigil]
        text = _unquote(rest)
        return MIStreamRecord(stream=stream, text=text)  # type: ignore[arg-type]
    return None


def _split_class_and_fields(rest: str) -> tuple[str, dict[str, Any]]:
    """Split ``"class,k=v,k=v,..."`` into ``("class", {kv-dict})``."""
    if "," not in rest:
        return rest.strip(), {}
    klass, _, kv_blob = rest.partition(",")
    return klass.strip(), _parse_kv_pairs(kv_blob)


# ---------------------------------------------------------------------------
# Value grammar — strings, lists, tuples, plain identifiers
# ---------------------------------------------------------------------------


def _parse_kv_pairs(text: str) -> dict[str, Any]:
    """Parse a comma-separated ``key=value`` blob into a dict.

    Values may be strings (``"..."``), tuples (``{...}``), or lists
    (``[...]``). Keys are bare identifiers.
    """
    return _Parser(text).parse_kv_pairs(end_chars="")


class _Parser:
    """Single-pass parser for gdb-MI value grammar.

    Position-based; ``_pos`` advances as ``parse_*`` methods consume.
    """

    __slots__ = ("text", "_pos")

    def __init__(self, text: str) -> None:
        self.text = text
        self._pos = 0

    def parse_kv_pairs(self, *, end_chars: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        while self._pos < len(self.text):
            self._skip_ws()
            if self._peek() in end_chars:
                break
            key = self._parse_identifier()
            self._expect("=")
            value = self._parse_value()
            # gdb-MI sometimes emits multiple entries with the same key
            # (e.g., multiple ``frame`` records in a stack list). Promote
            # the second occurrence into a list.
            if key in out:
                existing = out[key]
                if isinstance(existing, list):
                    existing.append(value)
                else:
                    out[key] = [existing, value]
            else:
                out[key] = value
            if self._peek() == ",":
                self._pos += 1
            else:
                break
        return out

    def _parse_value(self) -> Any:
        self._skip_ws()
        ch = self._peek()
        if ch == '"':
            return self._parse_string()
        if ch == "{":
            return self._parse_tuple()
        if ch == "[":
            return self._parse_list()
        # Bare token (rare — typically class name on async records).
        return self._parse_identifier()

    def _parse_string(self) -> str:
        assert self._peek() == '"'
        self._pos += 1
        out: list[str] = []
        while self._pos < len(self.text):
            ch = self.text[self._pos]
            if ch == "\\":
                self._pos += 1
                if self._pos >= len(self.text):
                    break
                esc = self.text[self._pos]
                self._pos += 1
                out.append(_ESCAPE_TABLE.get(esc, esc))
            elif ch == '"':
                self._pos += 1
                return "".join(out)
            else:
                out.append(ch)
                self._pos += 1
        return "".join(out)

    def _parse_tuple(self) -> dict[str, Any] | list[Any]:
        assert self._peek() == "{"
        self._pos += 1
        # Empty tuple
        self._skip_ws()
        if self._peek() == "}":
            self._pos += 1
            return {}
        # Could be a kv-list or (rare) value-list — peek to decide.
        if self._looks_like_kv():
            d = self.parse_kv_pairs(end_chars="}")
            self._expect("}")
            return d
        values: list[Any] = []
        while self._pos < len(self.text):
            self._skip_ws()
            if self._peek() == "}":
                break
            values.append(self._parse_value())
            if self._peek() == ",":
                self._pos += 1
            else:
                break
        self._expect("}")
        return values

    def _parse_list(self) -> list[Any]:
        assert self._peek() == "["
        self._pos += 1
        self._skip_ws()
        if self._peek() == "]":
            self._pos += 1
            return []
        items: list[Any] = []
        # Lists can be either ``[value, value, ...]`` or
        # ``[key=value, key=value, ...]`` per gdb-MI spec; the latter
        # really is "an ordered list of key-tagged values" — we promote
        # it into a list of single-entry dicts so consumers see ordering.
        if self._looks_like_kv():
            while self._pos < len(self.text):
                self._skip_ws()
                if self._peek() == "]":
                    break
                key = self._parse_identifier()
                self._expect("=")
                value = self._parse_value()
                items.append({key: value})
                if self._peek() == ",":
                    self._pos += 1
                else:
                    break
        else:
            while self._pos < len(self.text):
                self._skip_ws()
                if self._peek() == "]":
                    break
                items.append(self._parse_value())
                if self._peek() == ",":
                    self._pos += 1
                else:
                    break
        self._expect("]")
        return items

    def _looks_like_kv(self) -> bool:
        """Scan ahead for ``identifier=`` from current position; doesn't
        advance ``_pos``."""
        i = self._pos
        # Skip whitespace.
        while i < len(self.text) and self.text[i].isspace():
            i += 1
        # Match identifier head.
        if i >= len(self.text) or not (
            self.text[i].isalpha() or self.text[i] == "_"
        ):
            return False
        while i < len(self.text) and (
            self.text[i].isalnum() or self.text[i] in "_-"
        ):
            i += 1
        # Followed immediately (optional ws) by '='.
        while i < len(self.text) and self.text[i].isspace():
            i += 1
        return i < len(self.text) and self.text[i] == "="

    def _parse_identifier(self) -> str:
        self._skip_ws()
        start = self._pos
        while self._pos < len(self.text) and (
            self.text[self._pos].isalnum() or self.text[self._pos] in "_-"
        ):
            self._pos += 1
        return self.text[start : self._pos]

    def _expect(self, ch: str) -> None:
        self._skip_ws()
        if self._peek() != ch:
            raise ValueError(
                f"expected {ch!r} at position {self._pos} in {self.text!r}"
            )
        self._pos += 1

    def _peek(self) -> str:
        if self._pos >= len(self.text):
            return ""
        return self.text[self._pos]

    def _skip_ws(self) -> None:
        while self._pos < len(self.text) and self.text[self._pos] == " ":
            self._pos += 1


_ESCAPE_TABLE = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"'}


def _unquote(text: str) -> str:
    """Decode a gdb-MI quoted-string body. Forgiving on partial input."""
    text = text.strip()
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1]
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            esc = text[i + 1]
            out.append(_ESCAPE_TABLE.get(esc, esc))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Domain-specific reductions
# ---------------------------------------------------------------------------


def parse_register_dump(
    values_record: MIResultRecord, names_record: MIResultRecord
) -> RegisterDump:
    """Combine ``-data-list-register-names`` + ``-data-list-register-values``.

    Names record yields ``register-names=[…]``; values record yields
    ``register-values=[{number=N,value="…"}, …]``. Builds a name→int dict.
    """
    names = names_record.fields.get("register-names") or []
    values_raw = values_record.fields.get("register-values") or []
    values: dict[str, int] = {}
    for entry in values_raw:
        if not isinstance(entry, dict):
            continue
        num_s = entry.get("number")
        val_s = entry.get("value")
        if num_s is None or val_s is None:
            continue
        try:
            num = int(num_s)
        except (TypeError, ValueError):
            continue
        if num >= len(names):
            continue
        name = names[num]
        if not isinstance(name, str) or not name:
            continue
        try:
            values[name] = int(val_s, 0) if isinstance(val_s, str) else int(val_s)
        except (TypeError, ValueError):
            continue
    fpu_present = any(n.startswith(("d0", "d1", "s0", "s1", "fpscr")) for n in values)
    return RegisterDump(values=values, fpu_present=fpu_present, secure_world=None)


def parse_breakpoint_insert(record: MIResultRecord) -> Breakpoint:
    """Extract ``bkpt={...}`` from ``^done,bkpt={number=...,...}``."""
    bkpt = record.fields.get("bkpt") or {}
    if not isinstance(bkpt, dict):
        bkpt = {}
    number = int(bkpt.get("number", 0) or 0)
    location = str(bkpt.get("original-location") or bkpt.get("func") or "")
    address = bkpt.get("addr")
    file = bkpt.get("file") or bkpt.get("fullname")
    line_s = bkpt.get("line")
    return Breakpoint(
        number=number,
        location=location,
        address=str(address) if address else None,
        file=str(file) if file else None,
        line=int(line_s) if line_s and str(line_s).isdigit() else None,
    )


def parse_stopped(record: MIAsyncRecord) -> StoppedNotification:
    """Decode a ``*stopped,reason=...`` record."""
    raw_reason = str(record.fields.get("reason") or "unknown")
    reason: str = _STOP_REASON_MAP.get(raw_reason, "unknown")
    bp_num = record.fields.get("bkptno")
    sig = record.fields.get("signal-name")
    frame_raw = record.fields.get("frame")
    frame: StackFrame | None = None
    if isinstance(frame_raw, dict):
        frame = StackFrame(
            level=int(frame_raw.get("level", 0) or 0),
            pc=str(frame_raw.get("addr") or ""),
            function=frame_raw.get("func") if frame_raw.get("func") else None,
            file=frame_raw.get("file") if frame_raw.get("file") else None,
            line=int(frame_raw["line"]) if str(frame_raw.get("line", "")).isdigit() else None,
        )
    return StoppedNotification(
        reason=reason,  # type: ignore[arg-type]
        breakpoint_number=int(bp_num) if bp_num and str(bp_num).isdigit() else None,
        signal_name=str(sig) if sig else None,
        frame=frame,
        raw_fields=dict(record.fields),
    )


_STOP_REASON_MAP = {
    "breakpoint-hit": "breakpoint-hit",
    "signal-received": "signal-received",
    "exited-normally": "exited-normally",
    "exited-signalled": "exited-signalled",
    "exited": "exited",
    "watchpoint-trigger": "watchpoint-trigger",
    "end-stepping-range": "end-stepping-range",
    "function-finished": "function-finished",
}


def parse_evaluate_expression(record: MIResultRecord) -> VariableValue:
    """``-data-evaluate-expression`` yields ``^done,value="..."``."""
    raw = str(record.fields.get("value", "") or "")
    integer_value: int | None = None
    try:
        integer_value = int(raw, 0)
    except (ValueError, TypeError):
        # Try stripping common trailing tokens like " '\\n' (0xa)".
        stripped = raw.split()[0] if raw.split() else raw
        try:
            integer_value = int(stripped, 0)
        except (ValueError, TypeError):
            pass
    return VariableValue(
        name="",  # caller fills in
        type_name="",
        raw=raw,
        integer_value=integer_value,
        optimized_out="<optimized out>" in raw,
    )


def parse_stack_list_frames(
    record: MIResultRecord,
    threads_record: MIResultRecord | None = None,
    args_record: MIResultRecord | None = None,
) -> CallStack:
    """Build a ``CallStack`` from ``-stack-list-frames`` (+ optional
    ``-thread-info`` for the threads array, + optional
    ``-stack-list-arguments 1`` whose ``stack-args`` payload fills
    ``StackFrame.args`` for ``callstack(full=True)``)."""
    # Per-frame arguments from `-stack-list-arguments 1`:
    # ^done,stack-args=[frame={level="0",args=[{name=...,value=...},...]},...]
    args_by_level: dict[int, dict[str, str]] = {}
    if args_record is not None:
        raw_arg_frames = args_record.fields.get("stack-args") or []
        for item in raw_arg_frames if isinstance(raw_arg_frames, list) else []:
            if isinstance(item, dict) and "frame" in item and isinstance(item["frame"], dict):
                arg_frame = item["frame"]
            elif isinstance(item, dict):
                arg_frame = item
            else:
                continue
            try:
                arg_level = int(arg_frame.get("level", 0) or 0)
            except (TypeError, ValueError):
                continue
            frame_args: dict[str, str] = {}
            raw_args = arg_frame.get("args") or []
            for arg in raw_args if isinstance(raw_args, list) else []:
                if isinstance(arg, dict) and "name" in arg:
                    frame_args[str(arg["name"])] = str(arg.get("value", ""))
            args_by_level[arg_level] = frame_args

    raw_frames = record.fields.get("stack") or []
    frames: list[StackFrame] = []
    for item in raw_frames if isinstance(raw_frames, list) else []:
        # Each entry is ``{frame: {...}}`` per the list-of-kv-tuples shape.
        if isinstance(item, dict) and "frame" in item and isinstance(item["frame"], dict):
            frame_raw = item["frame"]
        elif isinstance(item, dict):
            frame_raw = item
        else:
            continue
        level = int(frame_raw.get("level", 0) or 0)
        frames.append(
            StackFrame(
                level=level,
                pc=str(frame_raw.get("addr") or ""),
                function=frame_raw.get("func") if frame_raw.get("func") else None,
                file=frame_raw.get("file") if frame_raw.get("file") else None,
                line=int(frame_raw["line"]) if str(frame_raw.get("line", "")).isdigit() else None,
                args=args_by_level.get(level) if args_record is not None else None,
            )
        )

    threads: list[ThreadInfo] = []
    active_idx = 0
    if threads_record is not None:
        raw_threads = threads_record.fields.get("threads") or []
        for entry in raw_threads if isinstance(raw_threads, list) else []:
            if not isinstance(entry, dict):
                continue
            state_raw = str(entry.get("state") or "unknown").lower()
            state = state_raw if state_raw in ("halted", "running", "stopped") else "unknown"
            # gdb-MI uses "stopped" rather than "halted".
            if state == "stopped":
                state = "halted"
            threads.append(
                ThreadInfo(
                    id=int(entry.get("id", 0) or 0),
                    name=entry.get("name") if entry.get("name") else None,
                    state=state,  # type: ignore[arg-type]
                )
            )
        active_id = threads_record.fields.get("current-thread-id")
        if active_id is not None:
            for i, t in enumerate(threads):
                if str(t.id) == str(active_id):
                    active_idx = i
                    break
    return CallStack(frames=frames, threads=threads, active_thread_index=active_idx)


def parse_memory_read(record: MIResultRecord) -> bytes:
    """``-data-read-memory-bytes`` yields ``^done,memory=[{begin=...,offset=...,contents="hex..."}]``.

    gdb may return *multiple* blocks when the requested range contains an
    unreadable hole (each block carries an ``offset`` relative to the
    request start). IMP-14: stitch every block at its declared offset and
    return the contiguous prefix — keeping only ``memory[0]`` mis-placed
    later blocks and silently dropped data. Data past the first hole is
    truncated (a plain ``bytes`` return can't represent gaps); callers see
    the short read via ``bytes_read``.
    """
    memory = record.fields.get("memory") or []
    if not isinstance(memory, list):
        return b""
    blocks: list[tuple[int, bytes]] = []
    for block in memory:
        if not isinstance(block, dict):
            continue
        contents = block.get("contents")
        if not isinstance(contents, str):
            continue
        try:
            data = bytes.fromhex(contents)
        except ValueError:
            continue
        try:
            offset = int(str(block.get("offset", "0")), 0)
        except ValueError:
            offset = 0
        blocks.append((offset, data))
    blocks.sort(key=lambda pair: pair[0])
    out = bytearray()
    for offset, data in blocks:
        if offset > len(out):
            _log.warning(
                "memory read has an unreadable hole at request offset "
                "0x%x; returning the %d contiguous bytes before it",
                len(out),
                len(out),
            )
            break
        if offset < len(out):
            # Overlapping block (shouldn't happen) — keep the new tail.
            data = data[len(out) - offset :]
        out.extend(data)
    return bytes(out)
