"""Deadline-respecting line reader over a subprocess text pipe.

``Popen.stdout.readline()`` blocks with no timeout, which made every
deadline above it dead code: the gdbserver handshake hung forever on a
live-but-silent server (IMP-13) and ``run_until_breakpoint`` hung
forever on a never-hit breakpoint (A-011). A daemon thread drains the
pipe into a queue; consumers poll the queue with a real timeout.

Plain ``threading`` / ``queue`` — cross-platform by construction, no
``platform/*`` wrapper needed (ADR-007 discipline applies to OS APIs,
not the stdlib concurrency primitives).
"""

from __future__ import annotations

import queue
import threading
from typing import IO

_EOF = object()


class PipeLineReader:
    """Single-consumer line reader backed by a daemon drain thread.

    The thread reads ``stream.readline()`` until EOF and pushes each
    line onto an unbounded queue (same retention semantics as the
    blocking design it replaces — nothing is dropped). Stream
    exceptions are forwarded to the consumer; EOF surfaces as
    ``EOFError`` once the queue is drained.
    """

    def __init__(self, stream: IO[str], *, name: str = "pipe-reader") -> None:
        self._stream = stream
        self._queue: queue.Queue = queue.Queue()
        self._eof = False  # consumer-side flag; set once the sentinel is seen
        self._thread = threading.Thread(
            target=self._drain, name=name, daemon=True
        )
        self._thread.start()

    def _drain(self) -> None:
        try:
            while True:
                line = self._stream.readline()
                if not line:
                    break
                self._queue.put(line)
        except (OSError, ValueError) as ex:
            # Pipe closed under us (process teardown) — forward to the
            # consumer, which maps it to its session-lost error type.
            self._queue.put(ex)
        finally:
            self._queue.put(_EOF)

    def read_line(self, *, timeout_s: float | None) -> str | None:
        """Return the next line, or ``None`` when ``timeout_s`` elapses
        with nothing available.

        Raises ``EOFError`` when the stream is exhausted (and the queue
        drained), or re-raises the drain thread's stream exception.
        ``timeout_s=None`` blocks until a line or EOF arrives.
        """
        if self._eof:
            raise EOFError("pipe stream exhausted")
        try:
            item = self._queue.get(timeout=timeout_s)
        except queue.Empty:
            return None
        if item is _EOF:
            self._eof = True
            raise EOFError("pipe stream exhausted")
        if isinstance(item, Exception):
            self._eof = True
            raise item
        return item
