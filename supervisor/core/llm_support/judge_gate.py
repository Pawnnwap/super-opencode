"""Process-wide cap on concurrent judge HTTP calls.

The farm runs jobs as threads in one worker process. Eight xhigh judge calls
at once saturated the box: some completed in ~100 s, others stayed silent for
the full 600 s read timeout even though the box was still serving other
requests. This gate bounds that load without serialising the farm (the agent
engines and non-judge work keep running).

The limit is read from ``SOP_JUDGE_MAX_CONCURRENCY`` on first use (default 4),
so the worker can apply its persisted setting before the first judge call.
A per-process semaphore is exact here because the worker is a single process;
the UI's occasional advisory calls get their own process-local gate.
"""

from __future__ import annotations

import os
import threading
from typing import Self

DEFAULT_LIMIT = 4
ENV_VAR = "SOP_JUDGE_MAX_CONCURRENCY"

_init_lock = threading.Lock()
_semaphore: threading.BoundedSemaphore | None = None


def _configured_limit() -> int:
    raw = os.environ.get(ENV_VAR, "").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_LIMIT


def _gate() -> threading.BoundedSemaphore:
    global _semaphore
    with _init_lock:
        if _semaphore is None:
            _semaphore = threading.BoundedSemaphore(_configured_limit())
        return _semaphore


def reset_for_tests(limit: int | None = None) -> None:
    """Replace the gate so a test can change the env before first use."""
    global _semaphore
    with _init_lock:
        _semaphore = threading.BoundedSemaphore(limit or _configured_limit())


class judge_slot:
    """Hold one judge-call slot for the duration of an HTTP request."""

    def __enter__(self) -> Self:
        _gate().acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        _gate().release()
        return False