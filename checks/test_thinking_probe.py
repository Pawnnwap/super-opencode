"""The auto-generation thinking level must be measured, not assumed.

Auto-generation (protocol refine, feasibility draft, whitelist/ignore
suggestions) wants the cheapest rung that still reasons. Hardcoding it is
wrong in two ways that both showed up on the local endpoint:

* the vLLM build rejects ``minimal`` and ``high`` outright (HTTP 400);
* the compat proxy accepts an unsupported name and clamps it to the nearest
  accepted one — for ``minimal`` that is ``none``, i.e. no thinking at all.

So the probe walks the ladder and requires *observed* reasoning tokens.
"""

from __future__ import annotations

from supervisor.utils import thinking_probe


class _Usage:
    def __init__(self, reasoning_tokens):
        self.completion_tokens_details = (
            None if reasoning_tokens is None
            else type("D", (), {"reasoning_tokens": reasoning_tokens})()
        )


class _Response:
    def __init__(self, reasoning_tokens):
        self.usage = _Usage(reasoning_tokens)


class _Completions:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    def create(self, **kwargs):
        effort = (kwargs.get("extra_body") or {}).get("reasoning_effort")
        self.calls.append(effort)
        return self.behaviour(effort)


class _Client:
    def __init__(self, behaviour):
        self.chat = type("C", (), {"completions": _Completions(behaviour)})()


def _reset():
    thinking_probe.clear_cache()


def test_rejected_rungs_are_skipped():
    """A 400 on a rung means "not available here", not "no thinking"."""
    _reset()

    def behaviour(effort):
        if effort == "minimal":
            raise RuntimeError("400 unsupported reasoning_effort")
        return _Response(40)

    client = _Client(behaviour)
    assert thinking_probe.lowest_thinking_effort(client, "m", "http://x") == "low"
    assert client.chat.completions.calls == ["minimal", "low"]


def test_cap_rejection_retries_uncapped_instead_of_skipping_the_rung():
    """o1-style models refuse ``max_tokens``; that says nothing about the
    effort level, so the same rung must be retried without the cap."""
    _reset()

    def behaviour(effort):
        raise RuntimeError("Unsupported parameter: 'max_tokens' is not supported")

    client = _Client(behaviour)
    assert thinking_probe.lowest_thinking_effort(client, "m", "http://x") == ""
    # Two attempts per rung, but the whole walk is capped at 5 requests.
    assert len(client.chat.completions.calls) == thinking_probe.MAX_PROBE_ATTEMPTS


def test_probe_never_exceeds_the_attempt_cap():
    """A pathological endpoint must not turn one click into a probe storm."""
    _reset()

    def behaviour(effort):
        raise RuntimeError("Unsupported parameter: 'max_tokens' is not supported")

    client = _Client(behaviour)
    thinking_probe.lowest_thinking_effort(client, "m", "http://x")
    assert len(client.chat.completions.calls) <= thinking_probe.MAX_PROBE_ATTEMPTS


def test_accepted_but_clamped_to_off_is_not_accepted():
    """The proxy case: 200 with zero reasoning tokens means the level did
    nothing, so the next rung must be tried."""
    _reset()

    def behaviour(effort):
        return _Response(0 if effort == "minimal" else 55)

    client = _Client(behaviour)
    assert thinking_probe.lowest_thinking_effort(client, "m", "http://x") == "low"


def test_missing_usage_breakdown_falls_back_to_acceptance():
    """Endpoints that report no reasoning breakdown still get a level."""
    _reset()
    client = _Client(lambda effort: _Response(None))
    assert thinking_probe.lowest_thinking_effort(client, "m", "http://x") == "minimal"


def test_no_rung_thinks_returns_empty():
    _reset()
    client = _Client(lambda effort: _Response(0))
    assert thinking_probe.lowest_thinking_effort(client, "m", "http://x") == ""


def test_unreachable_endpoint_stops_after_one_attempt_and_is_not_cached():
    """A dead endpoint fails every rung identically; walking the ladder would
    burn ten connect timeouts before the real generation starts."""
    _reset()

    def behaviour(effort):
        raise RuntimeError("Connection error.")

    client = _Client(behaviour)
    assert thinking_probe.lowest_thinking_effort(client, "m", "http://x") == ""
    assert client.chat.completions.calls == ["minimal"]
    # Not cached: the next call re-probes instead of pinning "off" forever.
    thinking_probe.lowest_thinking_effort(client, "m", "http://x")
    assert len(client.chat.completions.calls) == 2


def test_result_is_cached_per_endpoint_and_model():
    _reset()
    client = _Client(lambda effort: _Response(30))
    thinking_probe.lowest_thinking_effort(client, "m", "http://a")
    calls_after_first = len(client.chat.completions.calls)
    thinking_probe.lowest_thinking_effort(client, "m", "http://a")
    assert len(client.chat.completions.calls) == calls_after_first
    # A different endpoint is measured separately.
    thinking_probe.lowest_thinking_effort(client, "m", "http://b")
    assert len(client.chat.completions.calls) > calls_after_first


def test_helper_exposes_the_measured_level_as_extra_body():
    from supervisor.utils.llm_stream import StreamedLLMHelper

    _reset()
    helper = StreamedLLMHelper.__new__(StreamedLLMHelper)
    helper._client = _Client(lambda effort: _Response(0 if effort == "minimal" else 12))
    helper._model = "m"
    helper._base_url = "http://x"
    helper._thinking_effort = None

    assert helper.thinking_body() == {"reasoning_effort": "low"}
    # Measured once, reused for every later call in the same helper.
    assert helper.thinking_body() == {"reasoning_effort": "low"}
