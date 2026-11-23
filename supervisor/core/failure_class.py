"""One stable label per failure, for retry policy and operator triage.

A run record used to carry only ``FAILED`` plus a traceback. The farm could
not tell a provider outage (retryable) from an access-guard install race
(config/environment) or a token-limit overflow without a human reading
``job_worker.err.log``. This module is the single classifier both the worker
and the judge diagnostics use.
"""

from __future__ import annotations

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)

# Classes the farm may re-enqueue without a human changing anything. Timeouts
# are retryable only with a checkpoint (the agent work must not be re-run).
RETRYABLE_FAILURES = frozenset({
    "provider_5xx",
    "provider_timeout",
    "provider_connection",
    "rate_limit",
})

_TOKEN_LIMIT_MARKERS = (
    "max tokens",
    "context length",
    "maximum context",
    "too many tokens",
    "input length",
    "prompt is too long",
)


def _text(exc: BaseException) -> str:
    body = getattr(exc, "body", None)
    if body:
        return f"{exc} {body}".lower()
    return str(exc).lower()


def _is_token_limit(exc: BaseException) -> bool:
    text = _text(exc)
    return any(marker in text for marker in _TOKEN_LIMIT_MARKERS)


def classify_failure(exc: BaseException) -> str:
    """Map an exception to the run-record failure class."""
    # APITimeoutError subclasses APIConnectionError: check it first.
    if isinstance(exc, APITimeoutError):
        return "provider_timeout"
    if isinstance(exc, APIConnectionError):
        return "provider_connection"
    if isinstance(exc, RateLimitError):
        return "rate_limit"
    if isinstance(exc, BadRequestError):
        return "token_limit" if _is_token_limit(exc) else "provider_bad_request"
    if isinstance(exc, InternalServerError):
        status = getattr(exc, "status_code", None)
        if status == 511 or _is_token_limit(exc):
            return "token_limit"
        return "provider_5xx"
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", None)
        if status in (401, 403):
            return "provider_auth"
        if status == 404:
            return "model_not_found"
        return "provider_status" if status is None else f"provider_{status}"

    text = str(exc).lower()
    if "returned no choices" in text:
        return "empty_response"
    if "access guard" in text or "access policy drill" in text:
        return "access_guard"
    if "codex not found" in text or "codex timed out" in text:
        return "engine_start"
    if isinstance(exc, FileNotFoundError):
        return "engine_start"
    return "unknown"


def is_retryable(failure_class: str) -> bool:
    return failure_class in RETRYABLE_FAILURES


def classify_failure_text(text: str) -> str:
    """Classify a traceback/error-event body when no exception object is left."""
    lower = (text or "").lower()
    if "apitimeouterror" in lower or "request timed out" in lower:
        return "provider_timeout"
    if "apiconnectionerror" in lower or "connection error" in lower:
        return "provider_connection"
    if "ratelimiterror" in lower:
        return "rate_limit"
    if "internalservererror" in lower or "error code: 5" in lower:
        return "provider_5xx"
    if "returned no choices" in lower:
        return "empty_response"
    if "access guard" in lower or "access policy drill" in lower:
        return "access_guard"
    if "codex not found" in lower or "codex timed out" in lower:
        return "engine_start"
    if "badrequesterror" in lower:
        return "provider_bad_request"
    return "unknown"


def summarize_failure_text(text: str) -> str:
    """The last non-empty traceback line is the exception line worth showing."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1][:300] if lines else ""


def failure_summary(exc: BaseException) -> str:
    """A one-line operator summary, including API status/request id if known."""
    ctx = getattr(exc, "failure_context", None) or {}
    detail = ctx.get("message") or str(exc) or type(exc).__name__
    detail = " ".join(str(detail).split())
    status = ctx.get("status_code")
    if status is not None:
        detail = f"HTTP {status}: {detail}"
    request_id = ctx.get("request_id")
    if request_id:
        detail = f"{detail} (request_id={request_id})"
    return detail[:300]