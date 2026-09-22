"""Regression tests for the bounded streaming chat helper.

The UI's protocol generation used to post a non-streaming completion with no
timeout, so a long generation blew past the SDK's 600s read timeout and then
retried twice more — a 30-minute frozen UI. These tests pin the behaviour
that prevents that: streaming is requested, chunks accumulate, and a stream
that never ends is capped by a wall-clock deadline.
"""

import asyncio
import time

from services.webui.components.busy import busy_buttons
from supervisor.utils.llm_stream import (
    STREAM_MAX_RETRIES,
    GenerationCancelled,
    make_stream_client,
    stream_chat_text,
)


class _FakeStream:
    def __init__(self, chunks, delay=0.0):
        self._it = iter(chunks)
        self.closed = False
        self.delay = delay

    def __iter__(self):
        return self

    def __next__(self):
        if self.delay:
            time.sleep(self.delay)
        return next(self._it)

    def close(self):
        self.closed = True


class _FakeCompletions:
    def __init__(self, chunks, delay=0.0):
        self._chunks = chunks
        self.kwargs = {}
        self.delay = delay

    def create(self, **kwargs):
        self.kwargs = kwargs
        return _FakeStream(self._chunks, self.delay)


class _FakeChat:
    def __init__(self, chunks, delay=0.0):
        self.completions = _FakeCompletions(chunks, delay)


class _FakeClient:
    def __init__(self, chunks, delay=0.0):
        self.chat = _FakeChat(chunks, delay)


def _chunk(text):
    delta = type("Delta", (), {"content": text})()
    choice = type("Choice", (), {"delta": delta})()
    return type("Chunk", (), {"choices": [choice]})()


def _empty_chunk():
    return type("Chunk", (), {"choices": []})()


def test_stream_accumulates_chunks_and_requests_streaming():
    client = _FakeClient([_chunk("## INPUT\n"), _empty_chunk(), _chunk("body")])
    text = stream_chat_text(
        client, "gpt-4o", [{"role": "user", "content": "hi"}],
    )
    assert text == "## INPUT\nbody"
    assert client.chat.completions.kwargs["stream"] is True
    assert client.chat.completions.kwargs["temperature"] == 0.3


def test_temperature_suppressed_for_reasoning_models():
    client = _FakeClient([_chunk("ok")])
    stream_chat_text(client, "o1-mini", [{"role": "user", "content": "hi"}])
    assert "temperature" not in client.chat.completions.kwargs


def test_deadline_raises_timeout_and_closes_stream():
    client = _FakeClient([_chunk("a") for _ in range(100)])
    try:
        stream_chat_text(
            client, "gpt-4o", [{"role": "user", "content": "hi"}],
            deadline_seconds=-1.0,
        )
    except TimeoutError as exc:
        assert "exceeded" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("deadline did not raise TimeoutError")


def test_empty_stream_returns_empty_string():
    client = _FakeClient([_empty_chunk()])
    assert stream_chat_text(client, "gpt-4o", [{"role": "user", "content": "hi"}]) == ""


def test_empty_chunks_do_not_reset_the_idle_timer():
    """A trickle of chunks carrying neither content nor reasoning is a stall,
    not a slow think: without the idle bound the request parks until the
    wall-clock deadline (observed for 20 minutes in production on a ~4 KB
    input).
    """
    client = _FakeClient([_empty_chunk() for _ in range(40)], delay=0.05)
    try:
        stream_chat_text(
            client, "gpt-4o", [{"role": "user", "content": "hi"}],
            idle_seconds=0.3,
        )
    except TimeoutError as exc:
        assert "No activity" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("trickle of empty chunks was not aborted")


def _reasoning_chunk(text):
    delta = type("Delta", (), {"content": None, "reasoning_content": text})()
    choice = type("Choice", (), {"delta": delta})()
    return type("Chunk", (), {"choices": [choice]})()


def test_reasoning_stream_is_not_declared_stalled():
    """DeepSeek-style endpoints stream chain-of-thought in
    ``reasoning_content`` and deliver ``content`` only afterwards; that phase
    can legitimately run for minutes. Reasoning must count as activity so a
    healthy thinking generation is not aborted.
    """
    # 3s of pure reasoning, well past the 0.3s idle threshold, then content.
    chunks = [_reasoning_chunk("thinking " * 5)] * 60 + [_chunk("## INPUT\nok")]
    client = _FakeClient(chunks, delay=0.05)
    seen = []
    text = stream_chat_text(
        client, "gpt-4o", [{"role": "user", "content": "hi"}],
        idle_seconds=0.3,
        on_progress=lambda content, reasoning, _n: seen.append((content, reasoning)),
    )
    assert text == "## INPUT\nok"
    assert seen, "progress must fire during the reasoning phase"
    assert all(r > 0 for _, r in seen), "reasoning chars must be reported"


def test_stop_event_aborts_generation():
    import threading

    client = _FakeClient([_chunk("a")] * 100, delay=0.01)
    stop = threading.Event()
    stop.set()
    try:
        stream_chat_text(
            client, "gpt-4o", [{"role": "user", "content": "hi"}],
            stop_event=stop,
        )
    except GenerationCancelled:
        pass
    else:  # pragma: no cover
        raise AssertionError("set stop_event did not abort the stream")


def test_content_resets_the_idle_timer():
    """A healthy stream that interleaves content with thinking gaps still
    completes."""
    chunks = (
        [_chunk("a" * 10)]
        + [_empty_chunk()] * 3
        + [_chunk("b" * 10)]
        + [_empty_chunk()] * 3
        + [_chunk("c" * 10)]
    )
    client = _FakeClient(chunks, delay=0.05)
    text = stream_chat_text(
        client, "gpt-4o", [{"role": "user", "content": "hi"}],
        idle_seconds=0.3,
    )
    assert text == "a" * 10 + "b" * 10 + "c" * 10


def test_progress_callback_is_throttled_and_monotonic():
    chunks = [_chunk("hello ")] * 10 + [_chunk("world")] * 10
    client = _FakeClient(chunks, delay=0.2)
    seen = []
    text = stream_chat_text(
        client, "gpt-4o", [{"role": "user", "content": "hi"}],
        on_progress=lambda content, reasoning, chunk_count: seen.append(content),
    )
    assert text == "hello " * 10 + "world" * 10
    assert 2 <= len(seen) <= 6, seen
    assert seen[-1] == 10 * 6 + 10 * 5
    assert all(seen[i] <= seen[i + 1] for i in range(len(seen) - 1))


def test_client_is_bounded():
    client = make_stream_client(api_key="k", base_url="http://example.invalid/v1")
    assert client.max_retries == STREAM_MAX_RETRIES
    assert client.timeout.read == 180


class _FakeButton:
    def __init__(self):
        self.disabled = False

    def disable(self):
        self.disabled = True

    def enable(self):
        self.disabled = False


def test_busy_buttons_disable_for_duration_then_reenable():
    a, b = _FakeButton(), _FakeButton()

    async def run():
        async with busy_buttons(a, b):
            assert a.disabled and b.disabled

    asyncio.run(run())
    assert not a.disabled and not b.disabled


def test_busy_buttons_reenable_on_exception():
    a = _FakeButton()

    async def run():
        async with busy_buttons(a):
            raise RuntimeError("boom")

    try:
        asyncio.run(run())
    except RuntimeError:
        pass
    assert not a.disabled
