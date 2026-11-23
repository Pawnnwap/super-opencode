"""Failure classification drives retry policy and operator triage."""

from __future__ import annotations

import httpx
from openai import (
    APIConnectionError,
    APITimeoutError,
    BadRequestError,
    InternalServerError,
    OpenAIError,
    RateLimitError,
)

from supervisor.core.failure_class import (
    classify_failure,
    classify_failure_text,
    failure_summary,
    is_retryable,
    summarize_failure_text,
)


def _request() -> httpx.Request:
    return httpx.Request("POST", "http://endpoint/v1/chat/completions")


def _status_error(cls, status: int, *, text: str = "", body=None):
    response = httpx.Response(
        status,
        request=_request(),
        headers={"x-request-id": f"req-{status}"},
        text=text,
    )
    if text and body is None:
        body = {"error": {"message": text}}
    message = f"Error code: {status}" + (f" - {text}" if text else "")
    return cls(message, response=response, body=body)


def test_provider_failures_map_to_stable_classes():
    assert classify_failure(APITimeoutError(request=_request())) == "provider_timeout"
    assert classify_failure(APIConnectionError(request=_request())) == "provider_connection"
    assert classify_failure(_status_error(RateLimitError, 429)) == "rate_limit"
    assert classify_failure(_status_error(InternalServerError, 502)) == "provider_5xx"
    assert classify_failure(_status_error(InternalServerError, 503)) == "provider_5xx"
    assert classify_failure(_status_error(InternalServerError, 511)) == "token_limit"
    assert classify_failure(
        _status_error(InternalServerError, 500, text="too many tokens in request"),
    ) == "token_limit"
    assert classify_failure(
        _status_error(BadRequestError, 400, text="max tokens exceeded"),
    ) == "token_limit"
    assert classify_failure(
        _status_error(BadRequestError, 400, text="invalid field"),
    ) == "provider_bad_request"
    assert classify_failure(OpenAIError("LLM response returned no choices")) == "empty_response"


def test_local_failures_are_not_misread_as_provider_outages():
    assert classify_failure(
        RuntimeError("access guard could not be installed for codex: [WinError 32]"),
    ) == "access_guard"
    assert classify_failure(
        RuntimeError("access policy drill failed: gate artifacts unchanged"),
    ) == "access_guard"
    assert classify_failure(FileNotFoundError("codex not found")) == "engine_start"


def test_retryable_set_is_explicit():
    assert is_retryable("provider_5xx")
    assert is_retryable("provider_timeout")
    assert is_retryable("provider_connection")
    assert not is_retryable("access_guard")
    assert not is_retryable("token_limit")
    assert not is_retryable("provider_auth")


def test_failure_summary_includes_status_and_request_id():
    exc = _status_error(InternalServerError, 502)
    exc.failure_context = {
        "message": "Error code: 502",
        "status_code": 502,
        "request_id": "req-502",
    }
    summary = failure_summary(exc)
    assert "HTTP 502" in summary
    assert "req-502" in summary


def test_text_classifier_handles_log_only_failures():
    assert classify_failure_text(
        "Traceback...\nopenai.APITimeoutError: Request timed out.",
    ) == "provider_timeout"
    assert classify_failure_text(
        "Traceback...\nopenai.InternalServerError: Error code: 502",
    ) == "provider_5xx"
    assert classify_failure_text(
        "RuntimeError: access policy drill failed: hooks.json: missing",
    ) == "access_guard"
    assert classify_failure_text("something unexpected") == "unknown"
    text = "Traceback (most recent call last):\n  ...\nRuntimeError: boom"
    assert summarize_failure_text(text) == "RuntimeError: boom"