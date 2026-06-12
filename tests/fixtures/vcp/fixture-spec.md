# Test fixtures — `vcp` module

**Last updated:** 2026-05-11 (round-1 answers integrated)
**Status:** **Round-1 signed off 2026-05-11.** Paired with `vcp-api.md` (round-1 answers integrated 2026-05-11) per T-005. Module #4 of 6.

---

## Round-1 review answers (2026-05-11)

| # | Topic | Resolution |
|---|---|---|
| Q1 | `comports()` snapshot format | **(a) minimal JSON** of the fields substrate consumes (`device` / `vid` / `pid` / `serial_number`). |
| Q2 | Serial-capture authoring | **(c) mixed** — recorded for F-CAP-PLAIN-ASCII + F-CAP-CRLF-TERMINATOR; synthetic for F-CAP-UTF8-MULTIBYTE + F-CAP-INVALID-BYTES + F-CAP-OVERFLOW. |
| Q3 | Transcript fixture shape | **(a) state-machine** JSON describing `{on_write, respond_after_ms, respond_with}` triples. |
| Q4 | Reconnect-fixture mechanism | **(b) event-trigger-based** — deterministic `mock.disconnect()` / `mock.reconnect_as(...)` between substrate calls in unit tests; real timing in hardware tests. |
| Q5 | Descriptor schema additions | **Superseded by RES-020 → (a) add now.** `firmware.uart_baud` / `firmware.uart_terminator` / `firmware.board` must land in `stm32-project.schema.json` in v1 — v1 code reads these fields, so jsonschema validation must accept them or it rejects valid descriptors. The original (b) defer answer was inconsistent with the API contract; RES-020 closed that gap. |

Inline question text + per-Q resolution lines preserved below for audit.
**Scope:** test inputs the vcp module's tests need across unit / smoke / hardware layers. (Eval-layer fixtures live under `tests/fixtures/eval/` when T3 lands — VCP-004 / VCP-005 are the relevant T3 prompts.)
**Build status:** spec only. Fixture artifacts supplied by the user during the code phase; tests gracefully skip on `[out]` items until artifacts arrive.

---

## How this fixture catalog works

(Same model as cubeide / debug fixture-specs ratified per RES-011 / RES-013.)

Each fixture requirement has: **ID** · **Status** (`[in]`/`[out]`) · **Description** · **Features required** · **Drop path** · **Multi-artifact** (yes — supply variants for broader coverage) · **Drives tests**.

Tests parametrize over the artifacts under the requirement's drop path; empty dir → `pytest.skip` with the path to populate.

**v1 fixture-authoring rules** (per M-020):
- LF line endings on all text files (serial captures, descriptors).
- Anonymized probe-SNs where artifacts come from a specific user host.
- Reference projects + workspaces are Linux-generated for v1; v2 will add Windows-generated variants.

---

## Catalog at a glance

| Group | Count | Path prefix | Used by |
|---|---|---|---|
| Reference projects (shared with cubeide / debug) | 1 reused (F-PROJ-NUCLEO-L476RG-VCP-ECHO) | `tests/fixtures/projects/F-PROJ-NUCLEO-L476RG-VCP-ECHO/` | hardware |
| pyserial `list_ports.ListPortInfo` snapshots | 5 | `tests/fixtures/vcp/list-ports/F-LP-*/` | substrate unit (discovery) |
| Serial-capture fixtures (recorded bytes + expected lines) | 5 | `tests/fixtures/vcp/captures/F-CAP-*/` | substrate unit (reader + tail) |
| Send-and-read transcript fixtures | 4 | `tests/fixtures/vcp/transcripts/F-TRX-*/` | substrate unit (send_and_read) |
| Reconnect-scenario fixtures | 3 | `tests/fixtures/vcp/reconnects/F-RC-*/` | substrate unit (reconnect + SB-002) |
| Descriptors (substrate-authored) | 3 | `descriptors/` | substrate unit |

---

## Reference project (shared per T-004)

### F-PROJ-NUCLEO-L476RG-VCP-ECHO — UART echo firmware

**Status:** `[out]` (shared with cubeide / debug / cubeprogrammer modules; ownership for code-phase build is cubeide).
**Path:** `tests/fixtures/projects/F-PROJ-NUCLEO-L476RG-VCP-ECHO/`
**Description:** Already specified in cubeide's fixture-spec; reused here for vcp hardware tests. Echoes bytes back on the VCP within ~100ms.

**Drives vcp tests (hardware layer):**
- `tail(follow=True)` after flash + reset → captures boot banner.
- `send_and_read("hello\n")` → reply includes echoed `hello` (when echo_filter=False) or excludes it (echo_filter=True).
- `reconnect()` after a deliberate `cubeprogrammer.reset()` → port re-enumerates; status `reconnected` or `same_port`.

---

## pyserial `list_ports` snapshots (F-LP-*)

Captured outputs from `pyserial.tools.list_ports.comports()` covering the discovery edge cases. Each fixture is a list of `ListPortInfo` dicts (the fields substrate's `discover_vcp_ports()` consumes: `device`, `vid`, `pid`, `serial_number`).

Population strategy: **mostly recorded** from real `comports()` invocations on the user's host with various ST-LINK and non-ST-LINK USB devices plugged in. A couple synthetic fixtures cover edge cases hard to force.

### F-LP-SINGLE-STLINK — One ST-LINK probe, no other USB-serial

**Status:** `[out]`
**Path:** `tests/fixtures/vcp/list-ports/F-LP-SINGLE-STLINK/`
**Description:** Captured `comports()` output with exactly one ST-LINK VCP enumerated (the canonical hardware target's probe).

**Features required:**
- One entry with vid=0x0483 + pid in the ST-LINK VCP set.
- `device` = `/dev/ttyACMx` (Linux v1).
- `serial_number` = a real probe SN (anonymized OK; the substrate compares against `ctx.default_probe_sn`).

**Multi-artifact:** yes — variants per ST-LINK firmware version (V2 vs V3).

**Drives tests:** `discover_vcp_ports(probe_sn=<match>)` → returns the one candidate; `discover_vcp_ports(probe_sn=None)` → also returns the one candidate (single-probe ergonomic per Q8).

### F-LP-MULTI-STLINK — Two ST-LINK probes

**Status:** `[out]`
**Path:** `tests/fixtures/vcp/list-ports/F-LP-MULTI-STLINK/`
**Description:** Captured `comports()` with two ST-LINK probes enumerated (the user has two NUCLEOs plugged in, or one NUCLEO + one standalone ST-LINK).

**Features required:**
- Two entries with ST-LINK vid/pid.
- Different `serial_number` values.
- Different `device` paths.

**Drives tests:**
- `discover_vcp_ports(probe_sn=None)` → returns a **list of two candidates** (pure discovery; no raise per the corrected boundary in vcp-api.md).
- `discover_vcp_ports(probe_sn=<one of them>)` → returns only that one.
- `_ensure_reader()` called with `ctx.default_probe_sn=None` and `ctx.project.firmware.board=None` → **this** is where `VCPAmbiguousProbe` is raised (with `candidates: tuple[VCPProbeCandidate, ...]`). Test imports the resolver and asserts the raise.
- `_ensure_reader()` called with `ctx.project.firmware.board="nucleo-l476rg"` AND cubeprogrammer.list_probes() (mocked) returns one probe whose board_name matches → resolver auto-picks that probe; `ctx.default_probe_sn` updated for the session.
- `_ensure_reader()` called with `ctx.project.firmware.board="nucleo-l476rg"` AND neither candidate's board_name matches → falls through to `VCPAmbiguousProbe`.

Note: ambiguity is **not** the responsibility of `discover_vcp_ports()`; that helper stays a pure filter (per RES-020 / vcp-feedback #2).

### F-LP-NO-STLINK — No ST-LINK, other USB-serial present

**Status:** `[out]`
**Path:** `tests/fixtures/vcp/list-ports/F-LP-NO-STLINK/`
**Description:** Captured `comports()` with non-ST-LINK USB-serial devices (e.g., Arduino on `/dev/ttyACM0` with vid=0x2341).

**Features required:**
- No entry with ST-LINK vid.
- At least one non-ST-LINK USB-serial entry.

**Drives tests:** `discover_vcp_ports()` filters non-ST-LINK → returns `[]` (pure filter per RES-020); `_ensure_reader()` raises `VCPNotEnumerated` with the non-ST-LINK candidates seen, formatted into the loud-error message.

### F-LP-EMPTY — No serial devices at all

**Status:** `[out]`
**Path:** `tests/fixtures/vcp/list-ports/F-LP-EMPTY/`
**Description:** Captured (or synthesized) empty `comports()` output.

**Drives tests:** `discover_vcp_ports()` returns `[]` (pure filter per RES-020); `_ensure_reader()` raises `VCPNotEnumerated` with the "no probes seen at all" loud-error path.

### F-LP-PORT-RENUMERATED — Before / after reset, same probe, different port name

**Status:** `[out]` (synthetic — captures the reset-rename scenario)
**Path:** `tests/fixtures/vcp/list-ports/F-LP-PORT-RENUMERATED/`
**Description:** Two snapshots, "before" and "after", representing a `cubeprogrammer.reset()` cycle. Same probe SN, different `/dev/ttyACMx` number.

**Features required:**
- `before.json`: probe at `/dev/ttyACM0`, SN `<X>`.
- `after.json`: same probe SN `<X>`, now at `/dev/ttyACM7` (or any different number).

**Drives tests:** SB-002 lazy reconnect logic — substrate sees the cached port is stale, re-enumerates, finds the same probe SN at the new port, updates the handle. `ReconnectResult.status="reconnected"` (different port).

---

## Serial-capture fixtures (F-CAP-*)

Captured byte streams from real VCP reads, paired with the expected lines after decoding + line-splitting. Tests feed the bytes through `_VcpReader` (with the serial-port mocked to return these bytes) and assert the yielded lines match.

### F-CAP-PLAIN-ASCII — Simple ASCII line stream

**Status:** `[out]`
**Path:** `tests/fixtures/vcp/captures/F-CAP-PLAIN-ASCII/`
**Description:** A captured byte stream + sidecar expected-lines JSON. Plain ASCII, `\n`-terminated lines.

**Features required:**
- `bytes.bin` — raw bytes captured.
- `expected.json` — list of expected decoded lines.
- `params.json` — encoding (`"utf-8"`), terminator (`"\n"`), baud (`115200`).

**Multi-artifact:** yes — different firmwares' boot banners + log lines.

**Drives tests:** `_VcpReader.read_lines(last_n=...)` round-trip.

### F-CAP-CRLF-TERMINATOR — `\r\n`-terminated lines

**Status:** `[out]`
**Path:** `tests/fixtures/vcp/captures/F-CAP-CRLF-TERMINATOR/`
**Description:** Stream with `\r\n` terminator (Windows-style firmware or explicit firmware choice).

**Drives tests:** terminator-handling: `\r\n` correctly splits; trailing `\r` not left in line content.

### F-CAP-UTF8-MULTIBYTE — UTF-8 multi-byte sequences

**Status:** `[out]`
**Path:** `tests/fixtures/vcp/captures/F-CAP-UTF8-MULTIBYTE/`
**Description:** Stream containing legitimate UTF-8 multi-byte sequences (e.g., firmware printing degree-symbol °, em-dash —).

**Drives tests:** UTF-8 decode preserves the multi-byte characters (per Q7 default `"utf-8"`).

### F-CAP-INVALID-BYTES — Garbled bytes triggering `errors='replace'`

**Status:** `[out]`
**Path:** `tests/fixtures/vcp/captures/F-CAP-INVALID-BYTES/`
**Description:** Stream with invalid UTF-8 sequences (e.g., baud-mismatch simulating noise — randomly-flipped high bits).

**Features required:**
- Some bytes are valid UTF-8, some are not.
- `expected.json` contains the replace-char (`�`) where decode failed.

**Drives tests:** encoding-error fallback per Q7; `WARNING` log fires once for the session.

### F-CAP-OVERFLOW — High-rate stream triggering bounded-queue drop

**Status:** `[out]`
**Path:** `tests/fixtures/vcp/captures/F-CAP-OVERFLOW/`
**Description:** Synthetic stream of >1000 short lines simulating a fast-talking firmware that overruns the bounded queue (per Q6).

**Features required:**
- Bytes for ~1200 lines.
- `expected.json` contains the last ~1000 lines (oldest 200 dropped).
- `expected.json` also notes the expected WARNING-log line.

**Drives tests:** bounded-queue drop-oldest behavior + WARNING log.

---

## Send-and-read transcript fixtures (F-TRX-*)

Each fixture is a (sent-line, expected-bytes-from-device, expected-RequestResponse JSON) triple. The serial-port mock plays the device-side bytes after the substrate sends the line.

### F-TRX-SIMPLE-REPLY — One line in, one line out

**Status:** `[out]`
**Path:** `tests/fixtures/vcp/transcripts/F-TRX-SIMPLE-REPLY/`
**Description:** `send_and_read("hello\n")` → device responds with `"world\n"`.

**Drives tests:** baseline `send_and_read` → `RequestResponse(sent_line="hello", reply_lines=("world",), timeout_hit=False, ...)`.

### F-TRX-MULTI-LINE-REPLY — Multi-line response collected via idle timeout

**Status:** `[out]`
**Path:** `tests/fixtures/vcp/transcripts/F-TRX-MULTI-LINE-REPLY/`
**Description:** Device responds with 3 lines spread over ~50ms; `inter_line_idle_ms=100` should collect all three.

**Drives tests:** idle-timeout-driven multi-line collection.

### F-TRX-ECHO-FIRMWARE — Device echoes input + responds

**Status:** `[out]`
**Path:** `tests/fixtures/vcp/transcripts/F-TRX-ECHO-FIRMWARE/`
**Description:** Device echoes the sent line back + appends a response (F-PROJ-NUCLEO-L476RG-VCP-ECHO firmware behavior).

**Features required:**
- Two fixture pairs: `echo_filter=False` (reply includes echo + response) and `echo_filter=True` (reply excludes echoed line).

**Drives tests:** `echo_filter` flag behavior.

### F-TRX-TIMEOUT-NO-REPLY — Device never responds

**Status:** `[out]`
**Path:** `tests/fixtures/vcp/transcripts/F-TRX-TIMEOUT-NO-REPLY/`
**Description:** Substrate sends a line; device sends no bytes; substrate's `timeout_s=0.2` (short, for test speed) fires.

**Drives tests:** `RequestResponse(timeout_hit=True, reply_lines=())`.

---

## Reconnect-scenario fixtures (F-RC-*)

Each fixture is a sequence of `comports()` snapshots + serial-port events, simulating the reconnect cycle.

### F-RC-SAME-PORT — Device reappears at the same port

**Status:** `[out]`
**Path:** `tests/fixtures/vcp/reconnects/F-RC-SAME-PORT/`
**Description:** Pre-reset: `/dev/ttyACM0` open. Reset: port handle becomes stale (write fails with EBADF). Re-enumerate: same probe SN at `/dev/ttyACM0`.

**Drives tests:** `reconnect()` → `ReconnectResult(status="same_port", port="/dev/ttyACM0")`.

### F-RC-NEW-PORT — Device reappears at a different port

**Status:** `[out]`
**Path:** `tests/fixtures/vcp/reconnects/F-RC-NEW-PORT/`
**Description:** Pre-reset: `/dev/ttyACM0`. Re-enumerate: same probe SN now at `/dev/ttyACM7` (uses F-LP-PORT-RENUMERATED fixture above).

**Drives tests:** `reconnect()` → `ReconnectResult(status="reconnected", port="/dev/ttyACM7")`.

### F-RC-TIMEOUT — Device never reappears

**Status:** `[out]`
**Path:** `tests/fixtures/vcp/reconnects/F-RC-TIMEOUT/`
**Description:** Pre-reset: open. Re-enumerate: probe never returns (USB cable yanked). `max_wait_s=0.5` (short for test speed) elapses.

**Drives tests:** `reconnect()` → `VCPError(vcp_marker="reconnect-timeout")`; `ReconnectResult.status="failed"` if returned non-raising variant.

---

## Descriptors (substrate-authored)

| File | Drives |
|---|---|
| `descriptors/vcp-defaults.jsonc` | Baseline — no `firmware.uart_baud` / `uart_terminator` set; substrate uses defaults (115200, `"\n"`). |
| `descriptors/vcp-explicit-baud.jsonc` | `firmware.uart_baud: 9600` set; substrate honors it. |
| `descriptors/vcp-crlf.jsonc` | `firmware.uart_terminator: "\r\n"` set; substrate uses it for send_and_read + line-splitting. |

---

## Layer breakdown

### Unit-layer (T-001) — primary coverage

- Discovery: F-LP-* round-trips through `discover_vcp_ports()`.
- Reader byte → line: F-CAP-* fixtures fed to `_VcpReader.read_lines()`; assert output matches expected.
- Send-and-read transcripts: F-TRX-* fixtures.
- Reconnect scenarios: F-RC-* fixtures.
- Concurrent-collision: in-process check that `ctx.session_state.active_vcp_reader` non-None blocks a second open → `VCPReaderAlreadyActive`.

### Smoke-layer (T-002) — real CLI, no hardware

- `pyserial` importable.
- `pyserial.tools.list_ports.comports()` returns ≥0 entries.
- If an ST-LINK happens to be plugged in: `discover_vcp_ports()` finds it.
- Without ST-LINK: `discover_vcp_ports()` returns `[]` (per RES-020 — pure filter, never raises). `VCPNotEnumerated` is raised from `_ensure_reader()` when a public `VCP` method finds no candidates.

### Hardware-layer (T-003) — attached NUCLEO + F-PROJ-NUCLEO-L476RG-VCP-ECHO

- `tail(follow=True)` for 3s after flash + reset → captures bootup banner.
- `send_and_read("hello", echo_filter=False)` → reply contains echoed `hello` + firmware's response.
- `send_and_read("hello", echo_filter=True)` → reply excludes echoed line.
- `reconnect()` after `cubeprogrammer.reset()` → status `reconnected` or `same_port`; subsequent `tail()` reads from the new port transparently (SB-002 in action).
- Cross-module compound: cubeide builds → cubeprogrammer flashes → vcp.tail observes echo.

### Eval-layer (T-007) — placeholder

VCP-004 (config-mismatch diagnose) + VCP-005 (baud-raise) are T3-deferred. When they land, eval scenarios consume `vcp.send_and_read` + `debug.read_peripheral("USART1")` for the Claude-in-loop logic. Eval fixtures live under `tests/fixtures/eval/` then.

---

## Cross-tool sharing

| Shared fixture | Owners |
|---|---|
| `tests/fixtures/projects/F-PROJ-NUCLEO-L476RG-VCP-ECHO/` | cubeide (build), cubeprogrammer (flash), debug (breakpoint workflow), **vcp (round-trip — primary consumer)**, compound (CP-002/006) |
| `tests/fixtures/devicedb/` | cubemx (owner) — vcp doesn't consume directly |

---

## Build sequence

Per T-005:

1. **API-surface phase (now):** spec only.
2. **Code phase (incremental as user supplies):**
   - **User** captures `pyserial.tools.list_ports.comports()` outputs in various USB-plug configurations (single ST-LINK, multi-ST-LINK, no-ST-LINK, etc.) — saves as JSON sidecars.
   - **User** captures real serial byte streams from F-PROJ-NUCLEO-L476RG-VCP-ECHO firmware (booting, after `send_and_read` exchange, after invalid-baud noise) and writes the `expected.json` sidecars.
   - **User** captures the renumeration-on-reset scenario (F-RC-NEW-PORT) by intentionally plugging/unplugging an ST-LINK or running cubeprogrammer.reset() with a board that triggers VCP re-enumeration.
   - **Substrate-side** descriptors authored by Claude.
   - Tests run continuously against whatever's supplied; missing fixtures cleanly skip.
3. **Hardware phase:** real-board exercises of tail / send_and_read / reconnect against F-PROJ-NUCLEO-L476RG-VCP-ECHO.

---

## Round-1 review questions

Per the inline-explanation discipline.

---

### Q1. `comports()` snapshot format

**Context.** The F-LP-* fixtures capture `pyserial.tools.list_ports.comports()` output. pyserial returns a list of `ListPortInfo` objects with attributes (`device`, `vid`, `pid`, `serial_number`, `manufacturer`, `product`, etc.). Two ways to serialize:

- **(a) JSON of the fields substrate consumes** (proposal). Substrate's `discover_vcp_ports()` reads `device` / `vid` / `pid` / `serial_number` — that's the entire contract. Fixture JSON contains only those fields per `ListPortInfo`. Test deserializes into mock `ListPortInfo` objects.
- **(b) Pickle-of-real-`ListPortInfo`-objects**. Round-trips perfectly but is Python-version-fragile and opaque.
- **(c) Full JSON of every attribute pyserial exposes**. Future-proofs if substrate ever needs more fields; more authoring work.

**Trade-off.** (a) is minimal + auditable + version-stable. (b) is brittle. (c) is over-engineering; we can add fields when needed.

**Proposal:** (a) minimal JSON. **TODO(v1+):** widen if substrate consumes more `ListPortInfo` fields.

**Pick.** (a)

**Resolved (2026-05-11):** (a) minimal JSON of consumed fields.

---

### Q2. Serial-capture authoring — recorded vs synthetic

**Context.** F-CAP-* fixtures need raw byte streams paired with expected decoded lines. Authoring options:

- **(a) Recorded** — capture real bytes from F-PROJ-NUCLEO-L476RG-VCP-ECHO firmware running on the bench; write `expected.json` from the actually-observed lines.
- **(b) Synthetic** — hand-craft bytes + expected output. Faster to author for edge cases (UTF-8 multi-byte sequences, deliberate invalid bytes, overflow at exact 1001-line boundary).
- **(c) Mixed** — recorded for happy paths, synthetic for edge cases.

**Trade-off.** (a) is realistic; (b) is precise for tricky cases; (c) gets both. Same pattern as cubeprogrammer / debug fixture-spec choices.

**Proposal:** (c) mixed — recorded for F-CAP-PLAIN-ASCII + F-CAP-CRLF-TERMINATOR; synthetic for F-CAP-UTF8-MULTIBYTE + F-CAP-INVALID-BYTES + F-CAP-OVERFLOW (these are easier to hand-craft precisely).

**Pick.** (c)

**Resolved (2026-05-11):** (c) mixed authoring.

---

### Q3. Transcript fixtures — mock serial behavior

**Context.** F-TRX-* fixtures drive `send_and_read()` tests. The test mocks the serial port (pyserial Serial object) so that when substrate writes `"hello\n"`, the mock makes `"world\n"` available for read. Two ways to express the transcript:

- **(a) State-machine fixture** (proposal). Each F-TRX-NNN/ holds a small JSON describing the transcript: `{"on_write": "hello\n", "respond_after_ms": 50, "respond_with": "world\n"}`. Test reads the JSON and configures the mock accordingly.
- **(b) pre-recorded byte sequence** independent of substrate's writes. Mock just plays bytes back regardless of what substrate sent. Doesn't test the send → reply correlation.

**Trade-off.** (a) tests the full request-response cycle realistically. (b) is simpler but misses the "did the send actually happen" assertion.

**Proposal:** (a) state-machine. JSON shape documented in the fixture-spec.

**Pick.** (a)

**Resolved (2026-05-11):** (a) state-machine fixture shape.

---

### Q4. Reconnect-fixture concurrency-of-events

**Context.** F-RC-* fixtures simulate the sequence: reader open at port A → port becomes stale → re-enumerate → reattach to port A (or B). The serial-port mock needs to flip from "port A works" to "port A is gone" mid-test.

- **(a) Time-based** — `mock.set_port_state(open=True)` at t=0; `mock.set_port_state(open=False)` at t=200ms; `mock.set_port_state(open=True, new_port=...)` at t=400ms. Test uses small real sleeps.
- **(b) Event-trigger-based** — the test explicitly calls `mock.disconnect()` and `mock.reconnect_as(...)` between substrate calls. Deterministic; no sleeps.

**Trade-off.** (a) is more realistic; (b) is faster + deterministic. (b) doesn't test SB-002's actual polling/timing, which (a) would.

**Proposal:** (b) event-trigger-based for unit tests (deterministic); (a) for hardware tests (real boards have real timing).

**Pick.** (b)

**Resolved (2026-05-11):** (b) event-trigger-based for unit tests; real-timing in hardware tests.

---

### Q5. Descriptor schema additions (firmware.uart_baud / uart_terminator)

**Context.** vcp's defaults come from `ctx.project.firmware.uart_baud` and `uart_terminator`. These fields don't exist in `stm32-project.schema.json` yet. Decision: add them now (so descriptors fixtures can exercise them) or treat as TODO?

- **(a) Add now** (proposal). Update `stm32-project.schema.json` during the schema-cleanup pass (SC-004); fixture-spec already assumes the fields exist.
- **(b) Defer** — schema is "queued for code phase per M-005"; substrate's vcp module reads the fields if present (validates via schema once added), uses defaults if not.

**Trade-off.** (a) commits the schema additions now (one more entry in the SC-004 queue). (b) is consistent with other modules' approach of flagging schema deltas as round-2 candidates.

**Proposal:** (b) defer (consistent with cubeide / debug round-2 flags for their own schema additions).

**Confirm.** (b)

**Resolved (2026-05-11):** (b) defer schema additions to round-2. **Superseded by RES-020 (2026-05-12):** `firmware.uart_baud` / `firmware.uart_terminator` / `firmware.board` must land in `stm32-project.schema.json` in v1 — v1 code reads these fields, and jsonschema would otherwise reject valid descriptors. The deferred answer wasn't tenable once the API contract committed to reading them.

---

## State

- **Round-1 review answers integrated 2026-05-11.** All 5 questions resolved.
- **Build deferred** to code phase per T-005.
- **VCP module fully signed off** — both `vcp-api.md` and this fixture-spec ratified. Module #4 of 6 complete.
- **v1 in-scope substrate-unit-test fixtures**: F-LP-* (list_ports snapshots) + F-CAP-* (serial captures) + F-TRX-* (send/read transcripts) + F-RC-* (reconnect scenarios) + descriptors/.
- **v1 in-scope reference projects**: F-PROJ-NUCLEO-L476RG-VCP-ECHO (cubeide-owned, shared).
- **Next module after sign-off:** `signing` (F-013; may fold into cubeprogrammer per the original M-015 plan — single-prompt module).
