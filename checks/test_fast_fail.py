"""Fast-fail on provider-unreachable errors, agent and supervisor both.

A dead endpoint (5xx / connection refused / timeout) must surface in seconds:
no SDK-internal retries, no backup-model call against the same endpoint, no
backoff ladders — and the agent subprocess must be killed early instead of
hanging until the turn timeout.
"""

import time
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, InternalServerError

from supervisor.runners.command_common import provider_connection_error
from supervisor.runners.opencode_support.stream import LineEvent
from supervisor.runners.stream_driver import consume_process_stream


# --------------------------------------------------------------------------- #
# Signature matcher                                                            #
# --------------------------------------------------------------------------- #


def test_provider_connection_error_matches_endpoint_down_lines():
    assert provider_connection_error("502 Bad Gateway from api") == "bad gateway"
    assert provider_connection_error("Error: fetch failed") == "fetch failed"
    assert provider_connection_error("connect ECONNREFUSED 1.2.3.4:80")
    assert provider_connection_error("HTTP 503 Service Unavailable")
    assert provider_connection_error("request failed with status code 502")


def test_provider_connection_error_ignores_ordinary_content():
    assert provider_connection_error("the value 502 appears in column b") is None
    assert provider_connection_error("refactored connection handling") is None
    assert provider_connection_error("") is None


# --------------------------------------------------------------------------- #
# Supervisor: chat_with_retry fails fast                                       #
# --------------------------------------------------------------------------- #


def _http_error(cls, status: int):
    request = httpx.Request("POST", "http://endpoint/v1/chat/completions")
    return cls(
        f"Error code: {status}",
        response=httpx.Response(status, request=request),
        body=None,
    )


def _supervisor(create):
    return SimpleNamespace(
        _model="primary",
        _model_backup="backup",
        _client=SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        ),
        _history=[],
        _reasoning_body=None,
        _max_history_turns=40,
        _compact_intermediate_steps=False,
        _extract_and_store_opencode_output=lambda *_a, **_k: None,
    )


def test_502_raises_immediately_without_backup_attempt():
    from supervisor.core.llm_support.chat import chat_with_retry

    calls: list[int] = []

    def create(**_kwargs):
        calls.append(1)
        raise _http_error(InternalServerError, 502)

    started = time.monotonic()
    with pytest.raises(InternalServerError):
        chat_with_retry(_supervisor(create), [], "record", False)
    elapsed = time.monotonic() - started

    # exactly one call: no SDK retries, no doomed backup-model attempt
    assert len(calls) == 1
    assert elapsed < 5


def test_connection_error_gets_one_quick_retry_then_raises():
    from supervisor.core.llm_support.chat import chat_with_retry

    calls: list[int] = []

    def create(**_kwargs):
        calls.append(1)
        raise APIConnectionError(request=httpx.Request("POST", "http://x"))

    started = time.monotonic()
    with pytest.raises(APIConnectionError):
        chat_with_retry(_supervisor(create), [], "record", False)
    elapsed = time.monotonic() - started

    # one quick retry only — not the old 5/10/20/40s ladder
    assert len(calls) == 2
    assert elapsed < 6


# --------------------------------------------------------------------------- #
# Separation: slow thinking is never failed as an error                        #
# --------------------------------------------------------------------------- #


def test_judge_client_keeps_generous_read_and_fast_connect():
    """The timeout split: read bounds only slow SUCCESS (thinking); the
    connect budget and zero SDK retries own fast-fail."""
    from pathlib import Path

    from supervisor.core.llm_supervisor import LLMSupervisor
    from supervisor.protocols.protocol import parse_protocol_text

    protocol = parse_protocol_text(
        "## INPUT\nx\n## TARGET\n1. y\n## RESTRICTIONS\nnone\n",
    )
    supervisor = LLMSupervisor(
        protocol,
        Path("."),
        model="m",
        api_key="k",
        base_url="http://endpoint/v1",
    )
    timeout = supervisor._client.timeout
    assert timeout.connect == 10.0  # unreachable endpoints fail in seconds
    assert timeout.read == 600.0  # deep-think completions are not cut off
    assert supervisor._client.max_retries == 0  # no hidden SDK retries


def test_slow_success_is_not_failed_fast():
    """A model that takes its time and then answers must not hit any
    fast-fail path — slowness is not an error."""
    from supervisor.core.llm_support.chat import chat_with_retry

    calls: list[int] = []

    def create(**_kwargs):
        calls.append(1)
        time.sleep(1.0)  # "thinking"
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="DONE: no\nfeedback: ok"),
                ),
            ],
        )

    started = time.monotonic()
    chat_with_retry(_supervisor(create), [], "record", False)
    elapsed = time.monotonic() - started

    assert len(calls) == 1
    assert elapsed >= 1.0  # the slow answer was waited out, not failed


def test_stream_timeouts_stay_generous_for_thinking_models():
    """Streaming budgets: long per-chunk reads, a 30-min overall deadline,
    and idle tolerance — deep thinking must not be cut short."""
    from supervisor.utils import llm_stream

    assert llm_stream.STREAM_TIMEOUT.read >= 180
    assert llm_stream.STREAM_DEADLINE_SECONDS >= 30 * 60
    assert llm_stream.STREAM_IDLE_SECONDS >= 120


# --------------------------------------------------------------------------- #
# Agent: stream driver aborts fast when the provider is down                   #
# --------------------------------------------------------------------------- #


class _FakeProc:
    def __init__(self, lines):
        self._lines = list(lines)
        self.stdout = iter(self._lines)
        self.stderr = None
        self.returncode = None
        self.killed = False

    def kill(self):
        self.killed = True
        self.returncode = -1

    terminate = kill

    def wait(self, timeout=None):
        return self.returncode if self.returncode is not None else 0

    def poll(self):
        return self.returncode


def _raw(line: str) -> LineEvent:
    return LineEvent(kind="raw", raw=line)


def _consume(proc, classify, *, timeout=60):
    gen = consume_process_stream(
        proc,
        classify_line=classify,
        timeout=timeout,
        abort_on=provider_connection_error,
    )
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


def test_stream_aborts_fast_when_provider_down_before_any_output():
    proc = _FakeProc(["Error: unable to connect to model provider\n"])
    started = time.monotonic()
    result = _consume(proc, lambda line: _raw(line or ""))
    elapsed = time.monotonic() - started

    assert elapsed < 5
    assert result.aborted is True
    assert result.abort_reason == "unable to connect"
    assert proc.killed is True


def test_stream_does_not_abort_when_agent_already_produced_output():
    # A healthy turn whose agent merely WRITES about connection errors later
    # must never be killed by the fast-fail guard.
    events = iter(
        [
            LineEvent(kind="text", text="working on the network layer"),
            _raw("note: handle ECONNREFUSED in the retry wrapper"),
        ],
    )

    def classify(_line):
        return next(events)

    proc = _FakeProc(["line1\n", "line2\n"])
    result = _consume(proc, classify, timeout=5)
    assert result.aborted is False
