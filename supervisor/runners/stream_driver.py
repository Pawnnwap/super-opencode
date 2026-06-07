"""Shared subprocess event-stream driver for the opencode and codex runners.

Both engines stream one JSON event per stdout line. This module owns the
*mechanics* of consuming that stream safely; each engine supplies only its own
pure ``classify_line`` (see ``opencode_support.stream`` / ``codex_support.stream``).

Why a dedicated reader thread + queue instead of iterating ``proc.stdout``
directly: on a timeout we must kill the child, but ``proc.kill()`` does NOT
reliably deliver EOF to the read end of the pipe on Windows when the child has
spawned its own grandchildren (they inherit the write handle). A direct
``for line in proc.stdout`` then blocks forever. Reading in a daemon thread and
pulling from a ``queue.Queue`` with a deadline means the consumer never blocks
past the timeout; on timeout we kill the whole process tree and abandon the
(daemon) reader.
"""

from __future__ import annotations

import logging
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass

from supervisor.runners.opencode_support.stream import LineEvent, build_output

logger = logging.getLogger(__name__)

_STDOUT_DONE = object()  # sentinel: reader thread finished


@dataclass
class StreamOutcome:
    """Aggregate result of consuming one engine's json event stream."""

    stdout: str  # compacted output (prose + tool markers, tool I/O dropped)
    stderr: str
    returncode: int
    timed_out: bool
    tokens_total: int  # real context tokens, 0 if unseen
    session_id: str | None = None  # captured session id, None if unseen


def _drain_stderr(proc, sink: list[str]) -> None:
    """Read all of the child's stderr into *sink* (run in a worker thread).

    Draining stderr concurrently with stdout avoids the classic pipe-buffer
    deadlock. Only str payloads are appended so a mocked process (whose
    ``.read()`` returns a sentinel) never corrupts the join.
    """
    try:
        stream = proc.stderr
        if stream is None:
            return
        data = stream.read()
    except Exception:
        return
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    if isinstance(data, str):
        sink.append(data)


def _read_stdout(stream, out_q: "queue.Queue") -> None:
    """Push every stdout line onto *out_q*, then a sentinel (daemon thread)."""
    try:
        if stream is not None:
            for line in stream:
                out_q.put(line)
    except Exception:
        pass
    finally:
        out_q.put(_STDOUT_DONE)


def _kill_process_tree(proc) -> None:
    """Kill *proc* and any children. Tree-kill on Windows so grandchildren that
    inherited the stdout handle die too (otherwise the pipe never closes)."""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
        else:
            proc.kill()
    except Exception as exc:
        logger.warning("Error killing process tree: %s", exc)
        try:
            proc.kill()
        except Exception:
            pass


def consume_process_stream(
    proc,
    *,
    classify_line: Callable[[str | None], LineEvent | None],
    timeout: int,
) -> Generator[dict, None, StreamOutcome]:
    """Stream *proc* stdout via *classify_line*, yield live tool markers.

    Text events accumulate as the real output; tool events are emitted live as
    ``{"level":"tool","msg":"[tool] …"}`` events (own log level so the UI can
    style them distinctly) AND collapsed to markers (verbose I/O dropped);
    tokens/session/error events update the outcome. Returns a
    :class:`StreamOutcome`; never blocks past *timeout*.
    """
    text_parts: list[str] = []
    markers: list[str] = []
    raw_parts: list[str] = []
    saw_json = False
    tokens_total = 0
    session_id: str | None = None

    def _accumulate(event: LineEvent | None) -> str | None:
        """Fold one classified event into the running state.

        Returns the tool marker to live-yield (caller decides whether to), or
        ``None``. Mutates the enclosing lists/flags.
        """
        nonlocal saw_json, tokens_total, session_id
        if event is None:
            return None
        kind = event.kind
        if kind == "text":
            if event.text:
                text_parts.append(event.text)
                saw_json = True
        elif kind == "tool":
            markers.append(event.marker)
            saw_json = True
            return event.marker
        elif kind == "tokens":
            saw_json = True
            if event.tokens_total:
                tokens_total = event.tokens_total
        elif kind == "session":
            saw_json = True
            if event.session_id:
                session_id = event.session_id
        elif kind == "error":
            saw_json = True
            if event.text:
                text_parts.append(event.text)
        elif kind == "other":
            saw_json = True
        else:  # "raw" — non-JSON line
            raw_parts.append(event.raw)
        return None

    out_q: queue.Queue = queue.Queue()
    reader = threading.Thread(target=_read_stdout, args=(proc.stdout, out_q), daemon=True)
    stderr_sink: list[str] = []
    stderr_thread = threading.Thread(
        target=_drain_stderr,
        args=(proc, stderr_sink),
        daemon=True,
    )
    reader.start()
    stderr_thread.start()

    deadline = time.monotonic() + max(1, timeout)
    timed_out = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        try:
            item = out_q.get(timeout=min(remaining, 0.5))
        except queue.Empty:
            continue
        if item is _STDOUT_DONE:
            break
        marker = _accumulate(classify_line(item))
        if marker is not None:
            # Own "tool" level so the log UI can style it; msg stays ASCII
            # (it is also logged to cp1252 stderr on Windows).
            yield {"level": "tool", "msg": marker}

    if timed_out:
        _kill_process_tree(proc)
        # Best-effort: fold any lines already buffered before the kill, so a
        # timeout still preserves partial output. No live yields here.
        drain_deadline = time.monotonic() + 1.0
        while time.monotonic() < drain_deadline:
            try:
                item = out_q.get(timeout=0.1)
            except queue.Empty:
                break
            if item is _STDOUT_DONE:
                break
            _accumulate(classify_line(item))

    try:
        proc.wait(timeout=10)
    except Exception:
        pass
    stderr_thread.join(timeout=5)

    returncode = proc.returncode
    if returncode is None:
        returncode = -1

    stdout_text = build_output(
        text_parts=text_parts,
        markers=markers,
        saw_json=saw_json,
        raw_parts=raw_parts,
    )
    return StreamOutcome(
        stdout=stdout_text,
        stderr="".join(stderr_sink),
        returncode=returncode,
        timed_out=timed_out,
        tokens_total=tokens_total,
        session_id=session_id,
    )
