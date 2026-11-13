"""A streamed LLM call must never hang silently.

Incident: Access & Protection's auto-generate sometimes appeared to freeze
forever with no log line and no error. Both limit checks (wall-clock deadline,
no-activity stall) lived INSIDE ``for chunk in stream``, so they needed a chunk
to arrive before they could fire. An endpoint that keeps the connection open
with SSE keep-alive comments - bytes DO arrive, so httpx's read timeout never
trips - while never emitting a choice left the iterator blocked indefinitely:
no deadline, no stall verdict, no progress update, nothing in the log.

Contract under test:
- the deadline fires on its own clock even when the iterator blocks forever;
- the idle guard fires on its own clock on a "connected but silent" stream;
- a healthy slow stream (reasoning flowing) is never declared stalled;
- the watchdog reports progress counters while waiting, so a live stall looks
  different from a silent hang in the UI;
- the closed stream surfaces as TimeoutError with an actionable message.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from supervisor.utils import llm_stream


class BlockingStream:
    """An iterator that blocks forever - the hung-endpoint shape.

    ``close()`` is what the watchdog uses to break the block; it mirrors a
    real httpx response closing the underlying connection.
    """

    def __init__(self):
        self.closed = threading.Event()
        self._released = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        # Block until closed, like a socket that keeps the connection open.
        while not self._released.is_set():
            if self.closed.is_set():
                self._released.set()
                raise RuntimeError("stream closed")
            time.sleep(0.02)
        raise StopIteration

    def close(self):
        self.closed.set()


class SilentChunkStream:
    """Delivers keep-alive chunks with no content and no reasoning."""

    def __init__(self, total: int = 1000):
        self.total = total
        self.count = 0
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed or self.count >= self.total:
            raise StopIteration
        self.count += 1
        time.sleep(0.02)
        # A chunk with no choices: exactly what a keep-alive comment yields.
        return SimpleNamespace(choices=[])

    def close(self):
        self.closed = True


class ReasoningStream:
    """A healthy slow stream: reasoning tokens arrive continuously."""

    def __init__(self, chunks: int = 6):
        self.chunks = chunks

    def __iter__(self):
        return self

    def __next__(self):
        if self.chunks <= 0:
            raise StopIteration
        self.chunks -= 1
        time.sleep(0.05)
        delta = SimpleNamespace(content="", reasoning_content="thinking…")
        return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])

    def close(self):
        pass


def _client(stream):
    class Completions:
        @staticmethod
        def create(**kwargs):
            return stream

    return SimpleNamespace(
        chat=SimpleNamespace(completions=Completions()),
    )


def test_deadline_fires_while_the_iterator_blocks_forever():
    """The hung-endpoint case: no chunk ever arrives, so an in-loop deadline
    check could never run. The watchdog must still break out."""
    stream = BlockingStream()
    started = time.monotonic()
    with pytest.raises(TimeoutError) as excinfo:
        llm_stream.stream_chat_text(
            _client(stream),
            "m",
            [{"role": "user", "content": "x"}],
            deadline_seconds=1.0,
            idle_seconds=60.0,
        )
    elapsed = time.monotonic() - started
    assert elapsed < 10, f"deadline did not fire promptly ({elapsed:.1f}s)"
    assert "exceeded" in str(excinfo.value).lower()
    assert stream.closed.is_set(), "the watchdog must close the stream to unblock it"


def test_idle_guard_fires_on_a_connected_but_silent_stream():
    """Keep-alive chunks with no choices are NOT activity: bytes arrive, but
    the model produced nothing. Without the watchdog this hung forever."""
    stream = SilentChunkStream()
    started = time.monotonic()
    with pytest.raises(TimeoutError) as excinfo:
        llm_stream.stream_chat_text(
            _client(stream),
            "m",
            [{"role": "user", "content": "x"}],
            deadline_seconds=60.0,
            idle_seconds=1.0,
        )
    elapsed = time.monotonic() - started
    assert elapsed < 10, f"idle guard did not fire promptly ({elapsed:.1f}s)"
    assert "no activity" in str(excinfo.value).lower()
    assert "empty" in str(excinfo.value).lower(), (
        "the message should say the chunks carried no content"
    )


def test_healthy_reasoning_stream_is_not_declared_stalled():
    """A thinking model streams reasoning continuously; that IS progress."""
    result = llm_stream.stream_chat_text(
        _client(ReasoningStream()),
        "m",
        [{"role": "user", "content": "x"}],
        deadline_seconds=5.0,
        idle_seconds=1.0,
    )
    assert isinstance(result, str)


def test_progress_is_reported_while_the_stream_is_silent(monkeypatch):
    """The UI needs counters even when nothing arrives, or a live stall is
    indistinguishable from a dead page."""
    # The stall re-report only kicks in after _STALL_QUIET of silence; in a
    # test that must finish fast, shorten it rather than sleeping 10 s.
    monkeypatch.setattr(llm_stream, "_STALL_QUIET", 0.2)
    reports: list[tuple[int, int, int]] = []
    silent = SilentChunkStream(total=400)

    with pytest.raises(TimeoutError):
        llm_stream.stream_chat_text(
            _client(silent),
            "m",
            [{"role": "user", "content": "x"}],
            deadline_seconds=60.0,
            idle_seconds=1.5,
            on_progress=lambda c, r, k: reports.append((c, r, k)),
        )
    assert reports, "the watchdog emitted no progress while waiting"
    assert len(reports) >= 2, (
        f"expected repeated progress reports during a stall, got {len(reports)}"
    )


def test_a_failing_progress_sink_never_kills_the_call():
    def boom(content, reasoning, chunks):
        raise RuntimeError("sink exploded")

    # Must complete normally; the sink's failure is logged, not propagated.
    llm_stream.stream_chat_text(
        _client(ReasoningStream()),
        "m",
        [{"role": "user", "content": "x"}],
        deadline_seconds=5.0,
        idle_seconds=10.0,
        on_progress=boom,
    )


def test_user_cancel_still_works_during_a_stall():
    """Cancel must interrupt a stalled stream, not wait for the deadline."""
    stop = threading.Event()
    threading.Timer(0.4, stop.set).start()
    started = time.monotonic()
    with pytest.raises(llm_stream.GenerationCancelled):
        llm_stream.stream_chat_text(
            _client(SilentChunkStream()),
            "m",
            [{"role": "user", "content": "x"}],
            deadline_seconds=60.0,
            idle_seconds=60.0,
            stop_event=stop,
        )
    assert time.monotonic() - started < 10
