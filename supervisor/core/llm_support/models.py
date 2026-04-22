from __future__ import annotations

import re
from dataclasses import dataclass, field

from supervisor.utils.filesystem.path_filters import (
    DEFAULT_IGNORE_DIRS,
    DEFAULT_IGNORE_PREFIXES,
)


def _get_model_token_limit(model: str) -> int:
    """Get maximum input token limit for given model."""
    return 128_000


_TOKEN_LIMIT_ERROR_MARKERS = (
    "range of input length",
    "input length",
    "input token count",
    "maximum number of tokens",
    "maximum context length",
    "context length exceeded",
    "context_length_exceeded",
    "max tokens",
    "exceeds the maximum",
    "too many tokens",
    "token count exceeds",
    "prompt is too long",
    "request too large",
    "reduce the length",
)


def _is_token_limit_error(exc: Exception) -> bool:
    """Return True when error indicates prompt exceeded token limit."""
    message = str(exc).lower()
    if any(marker in message for marker in _TOKEN_LIMIT_ERROR_MARKERS):
        return True
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error") if isinstance(body.get("error"), dict) else body
        code = str(err.get("code", "")) if isinstance(err, dict) else ""
        status = str(err.get("status", "")) if isinstance(err, dict) else ""
        msg = str(err.get("message", "")).lower() if isinstance(err, dict) else ""
        if status == "INVALID_ARGUMENT" and any(
            marker in msg for marker in _TOKEN_LIMIT_ERROR_MARKERS
        ):
            return True
        if code in {"context_length_exceeded", "string_above_max_length"}:
            return True
    return False


def _check_completion_phrases(reply: str, phrases: list[str]) -> bool:
    """Check if completion phrase appears positively in reply."""
    reply_lower = reply.lower()
    negation_prefixes = [
        r"not\s+",
        r"never\s+",
        r"failed\s+to\s+",
        r"unable\s+to\s+",
        r"did\s+not\s+",
        r"does\s+not\s+",
        r"don't\s+",
        r"doesn't\s+",
        r"won't\s+",
        r"cannot\s+",
        r"can't\s+",
    ]

    for phrase in phrases:
        pattern = r"\b" + re.escape(phrase) + r"\b"
        for match in re.finditer(pattern, reply_lower):
            start = match.start()
            prefix_context = reply_lower[max(0, start - 50):start]
            if not any(re.search(neg, prefix_context) for neg in negation_prefixes):
                return True
    return False


_OPENCODE_GENERATED_MD: set[str] = {
    "summary.md",
    "failure_report.md",
    "evolution_report.md",
}

_SKIP_DIRS = DEFAULT_IGNORE_DIRS | {".opencode", "opencode_supervisor.egg-info"}
_SKIP_DIR_PREFIXES = DEFAULT_IGNORE_PREFIXES

_DONE_PHRASES = [
    "all targets met",
    "all targets are met",
    "targets achieved",
    "task complete",
    "task is complete",
    "objectives met",
    "protocol satisfied",
]


@dataclass
class SupervisorVerdict:
    raw: str
    all_targets_met: bool
    feedback: str


@dataclass
class StepContext:
    current_step: int = 0
    total_steps_estimate: int = 5
    phase: str = "unknown"
    completed_phases: list[str] = field(default_factory=list)
