"""Measure the lowest thinking effort a chat endpoint actually honours.

Auto-generation calls (protocol refine, feasibility draft, whitelist and
ignore suggestions) are transcription-grade work: they want *some* reasoning,
but the cheapest rung that still thinks. That rung cannot be hardcoded:

* providers accept different names — the local vLLM build rejects ``minimal``
  and ``high`` outright with a 400;
* a name can be accepted yet silently clamped to "off" — the compat proxy
  rewrites an unsupported effort to the nearest accepted one, and for
  ``minimal`` the nearest is ``none``.

So the level is measured, not assumed: walk the ladder from the lowest
non-off rung upward and take the first one that is accepted AND actually
produces reasoning tokens. The probe runs through the same client/base_url
the generation will use, so a clamping proxy is part of what gets measured.
Results are cached per (base_url, model); a restart re-probes in a couple of
tiny calls.

The whole walk is capped at :data:`MAX_PROBE_ATTEMPTS` requests. A rung that
needs the uncapped retry costs two, so a pathological endpoint can never turn
one generation click into a long probe storm.
"""

from __future__ import annotations

import threading

# Lowest non-off rung first. "none"/"off" is deliberately excluded: the point
# is the cheapest level that still thinks.
LADDER: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh")

# Hard ceiling on probe requests per measurement, across the whole ladder.
MAX_PROBE_ATTEMPTS = 5

# A prompt that reliably elicits chain-of-thought on a thinking model and
# needs none on a non-thinking one — measured 0 reasoning tokens at "none"
# and 126-245 at every thinking rung on the local endpoint.
_PROBE_PROMPT = (
    "Three pipes fill a tank in 6, 8 and 12 hours. If all three run "
    "together, how long does filling take? Reply with the number only."
)

_cache: dict[tuple[str, str], str] = {}
_lock = threading.Lock()


def _reasoning_tokens(response) -> int | None:
    usage = getattr(response, "usage", None)
    details = getattr(usage, "completion_tokens_details", None)
    value = getattr(details, "reasoning_tokens", None)
    return value if isinstance(value, int) else None


# A reasoning model that refuses ``max_tokens`` says so explicitly; anything
# else (unknown parameter, unsupported effort) is a verdict on the rung.
_CAP_REJECTION_MARKERS = (
    "max_tokens",
    "max_completion_tokens",
    "unsupported parameter",
    "unrecognized request argument",
)


def _is_cap_rejection(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _CAP_REJECTION_MARKERS)


# A dead endpoint fails every rung identically; walking the whole ladder would
# spend the entire attempt budget before the real generation even starts.
_UNREACHABLE_MARKERS = (
    "connection error",
    "connection refused",
    "connect error",
    "timed out",
    "timeout",
    "name or service not known",
    "nodename nor servname",
    "getaddrinfo",
    "temporary failure in name resolution",
)


def _is_unreachable(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _UNREACHABLE_MARKERS)


class _Budget:
    """Shared request allowance for one measurement."""

    def __init__(self, limit: int = MAX_PROBE_ATTEMPTS):
        self.left = max(0, int(limit))

    def spend(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        return True


def _honoured(client, model: str, effort: str, budget: _Budget) -> bool | None:
    """True/False for this rung; None when probing must stop.

    None means either the endpoint is unreachable or the attempt budget is
    spent — both make further rungs pointless.
    """
    # Cap the probe's output so a thinking rung cannot run for minutes. A
    # reasoning model that rejects the cap (o1-style) is retried uncapped
    # rather than being misread as "this rung does not think"; any other
    # rejection means the rung itself is unavailable here.
    for capped in (True, False):
        if not budget.spend():
            return None
        kwargs: dict = {
            "model": model,
            "messages": [{"role": "user", "content": _PROBE_PROMPT}],
            "extra_body": {"reasoning_effort": effort},
        }
        if capped:
            kwargs["max_tokens"] = 64
        try:
            response = client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - a rejected rung is a result
            if _is_unreachable(exc):
                return None
            if capped and _is_cap_rejection(exc):
                continue
            return False
        tokens = _reasoning_tokens(response)
        # No breakdown reported: acceptance is the only signal available.
        return True if tokens is None else tokens > 0
    return False


def lowest_thinking_effort(client, model: str, base_url: str = "") -> str:
    """Cheapest effort that still thinks on this endpoint ("" = none works).

    An unreachable endpoint is not cached: the next call re-probes, so a
    transient outage does not pin the level to "off" for the whole process.
    """
    key = (str(base_url or ""), str(model or ""))
    with _lock:
        if key in _cache:
            return _cache[key]
    budget = _Budget()
    chosen = ""
    for effort in LADDER:
        honoured = _honoured(client, model, effort, budget)
        if honoured is None:
            return ""
        if honoured:
            chosen = effort
            break
    with _lock:
        _cache[key] = chosen
    return chosen


def clear_cache() -> None:
    """Drop measured levels (tests, or after the endpoint changes)."""
    with _lock:
        _cache.clear()


__all__ = [
    "LADDER",
    "MAX_PROBE_ATTEMPTS",
    "clear_cache",
    "lowest_thinking_effort",
]
