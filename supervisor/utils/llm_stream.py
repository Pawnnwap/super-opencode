"""Bounded streaming helper for one-shot LLM text calls.

The UI's protocol-generation calls used to post a *non-streaming* chat
completion on an OpenAI client built with no timeout, so the SDK fell back
to its defaults (read=600s, max_retries=2). A long generation hit the read
timeout, and the SDK then retried the same over-long request two more times
— a 30-minute frozen UI from a single click (observed in
supervisor_app.err.log as io_bound workers stuck in 600s-spaced retry
loops).

Streaming fixes the retry loop, but the deeper issue is what makes
generation *long* in the first place: this model (DeepSeek-V4-Flash)
streams its entire chain-of-thought in a separate ``reasoning_content``
field, delivering ``content`` only after the reasoning finishes. A protocol
refine on a few KB of notes can legitimately produce tens of thousands of
reasoning characters over several minutes while ``content`` stays empty —
indistinguishable from a hung connection if you only look at ``content``.

So this helper: counts reasoning as activity (so a healthy thinking phase is
never declared stalled), reports both counters to the UI, accepts a
user-driven stop event, and keeps a generous wall-clock deadline as a last
resort.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import httpx

from supervisor.utils.text_utils import normalize_model_response

logger = logging.getLogger(__name__)

# Reasoning-first model families reject ``temperature``.
_TEMPERATURE_FREE_PREFIXES = ("o1", "o3")

# Per-chunk network timeouts. A stream of any length stays alive as long as
# *some* bytes arrive inside this window; a dropped connection fails here
# instead of blocking a UI thread-pool worker for ten minutes.
STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=180.0, pool=180.0)

# One retry only: the original failure mode was the SDK compounding a
# slow-generation timeout into three consecutive 10-minute attempts.
STREAM_MAX_RETRIES = 1

# Wall-clock safety net. A protocol refine that is genuinely working streams
# reasoning continuously; the UI shows progress and offers Cancel, so this
# only fires when something really has gone wrong. Reasoning on a few KB of
# notes has been observed to run well past ten minutes.
STREAM_DEADLINE_SECONDS = 30 * 60

# How long the endpoint may go with no activity *at all* — no content and no
# reasoning — before it is declared stalled. Real thinking streams reasoning
# continuously, so this is a stall detector, not a patience limit.
STREAM_IDLE_SECONDS = 120

ProgressFn = Callable[[int, int, int], None]


class GenerationCancelled(Exception):
    """Raised when the user cancels an in-flight generation."""


def make_stream_client(
    api_key: str | None = None,
    base_url: str | None = None,
    *,
    timeout: httpx.Timeout | None = None,
    max_retries: int | None = None,
):
    """Build an OpenAI client sized for streamed one-shot text calls."""
    from openai import OpenAI

    kwargs: dict[str, Any] = {}
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url
    kwargs["timeout"] = timeout or STREAM_TIMEOUT
    kwargs["max_retries"] = (
        STREAM_MAX_RETRIES if max_retries is None else max_retries
    )
    return OpenAI(**kwargs)


def _delta_fields(delta: Any) -> tuple[str, str]:
    """Return ``(content, reasoning)`` text from a stream delta.

    ``reasoning_content`` is the field DeepSeek-style endpoints use for
    chain-of-thought; the OpenAI SDK exposes it as a plain attribute when the
    server sends it.
    """
    content = getattr(delta, "content", None) or ""
    reasoning = (
        getattr(delta, "reasoning_content", None)
        or getattr(delta, "reasoning", None)
        or ""
    )
    return content, reasoning


def stream_chat_text(
    client,
    model: str,
    messages: list[dict[str, str]],
    *,
    temperature: float | None = 0.3,
    field_name: str = "streamed model response",
    deadline_seconds: float = STREAM_DEADLINE_SECONDS,
    idle_seconds: float = STREAM_IDLE_SECONDS,
    on_progress: ProgressFn | None = None,
    stop_event: Any = None,
) -> str:
    """Run one streamed chat completion and return the assembled text.

    ``on_progress(content_chars, reasoning_chars, chunks)`` is invoked about
    once a second so the UI can show a thinking model is making progress.
    ``stop_event`` (a ``threading.Event``) is checked on every chunk; setting
    it aborts the stream with :class:`GenerationCancelled`.
    """
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    if temperature is not None and not model.startswith(_TEMPERATURE_FREE_PREFIXES):
        kwargs["temperature"] = temperature

    started = time.monotonic()
    last_activity = started
    last_progress = started
    content_chars = 0
    reasoning_chars = 0
    chunks = 0
    stream = client.chat.completions.create(**kwargs)
    parts: list[str] = []
    try:
        for chunk in stream:
            now = time.monotonic()
            if stop_event is not None and stop_event.is_set():
                raise GenerationCancelled("Generation cancelled by the user.")
            if now - started > deadline_seconds:
                raise TimeoutError(
                    f"Model response exceeded {deadline_seconds / 60:.0f} min "
                    f"({reasoning_chars} reasoning chars, {content_chars} "
                    "answer chars); the endpoint may be stuck. Retry, or "
                    "shorten the input sections.",
                )
            chunks += 1
            choices = getattr(chunk, "choices", None) or []
            delta = getattr(choices[0], "delta", None) if choices else None
            content, reasoning = _delta_fields(delta)
            if content:
                parts.append(content)
                content_chars += len(content)
            if content or reasoning:
                # A thinking model streams reasoning continuously; only a
                # total silence counts as a stall.
                reasoning_chars += len(reasoning)
                last_activity = now
            elif now - last_activity > idle_seconds:
                raise TimeoutError(
                    f"No activity from the endpoint for {idle_seconds:.0f}s "
                    "(neither reasoning nor answer); it appears stalled. "
                    "Retry, or shorten the input sections.",
                )
            if on_progress is not None and now - last_progress > 1.0:
                last_progress = now
                on_progress(content_chars, reasoning_chars, chunks)
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
    logger.debug(
        "stream_chat_text done: %d answer chars / %d reasoning chars in %.1fs (model=%s)",
        content_chars, reasoning_chars, time.monotonic() - started, model,
    )
    return normalize_model_response("".join(parts), field_name)
