"""Shared redaction for credentials crossing diagnostic or persistence boundaries."""

from __future__ import annotations

import logging
import re
import traceback
from typing import Any

REDACTED = "[REDACTED]"

_SECRET_FIELDS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "codex_api_key",
        "credential",
        "credentials",
        "encryption_key",
        "jev_api_key",
        "jev_decision_api_key",
        "openai_api_key",
        "openai_key",
        "passwd",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "storage_secret",
        "token",
        "access_token",
        "admin_token",
        "auth_token",
        "codex_key",
        "signing_key",
    }
)
_SECRET_SUFFIXES = (
    "_api_key",
    "_auth_token",
    "_access_token",
    "_refresh_token",
    "_admin_token",
    "_client_secret",
    "_codex_key",
    "_credentials",
    "_encryption_key",
    "_password",
    "_private_key",
    "_secret",
    "_signing_key",
)

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
_URL_CREDENTIALS_RE = re.compile(
    r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@",
)
_AUTH_VALUE_RE = re.compile(
    r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+",
)
_QUOTED_SECRET_RE = re.compile(
    r"(?i)([\"'](?:api[_-]?key|codex[_-]?key|token|access[_-]?token|"
    r"refresh[_-]?token|admin[_-]?token|secret|client[_-]?secret|password|"
    r"passwd|authorization|signing[_-]?key|encryption[_-]?key|private[_-]?key)"
    r"[\"']\s*:\s*[\"'])"
    r"(.*?)([\"'])",
)
_ENV_SECRET_RE = re.compile(
    r"(?i)(\b[A-Z][A-Z0-9_]*(?:API[_-]?KEY|CODEX_KEY|ACCESS[_-]?TOKEN|"
    r"REFRESH[_-]?TOKEN|AUTH[_-]?TOKEN|ADMIN_TOKEN|TOKEN|SECRET|PASSWORD|"
    r"PASSWD|SIGNING_KEY|ENCRYPTION_KEY|PRIVATE_KEY)[A-Z0-9_]*\s*=\s*)"
    r"([\"']?)([^\s\"',;}]+)",
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:api[_-]?key|codex[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"auth[_-]?token|admin[_-]?token|token|secret|client[_-]?secret|password|"
    r"passwd|authorization|signing[_-]?key|encryption[_-]?key|private[_-]?key)"
    r"\b\s*[:=]\s*)"
    r"([\"']?)([^\s\"',;}]+)",
)
_SECRET_FLAG_RE = re.compile(
    r"(?i)((?:^|\s)--?(?:api[_-]?key|codex[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"auth[_-]?token|admin[_-]?token|token|secret|client[_-]?secret|password|"
    r"passwd|signing[_-]?key|encryption[_-]?key|private[_-]?key)\s+)"
    r"([\"']?)([^\s\"',;}]+)",
)
_KNOWN_TOKEN_RE = re.compile(
    r"(?i)\b(?:sk-(?:proj-|svcacct-)?|ghp_|github_pat_|xox[baprs]-|AKIA|ASIA)"
    r"[A-Za-z0-9_-]{8,}\b",
)
_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
)


def redact_text(text: str) -> str:
    """Remove credential values while retaining enough context to debug."""
    value = str(text)
    value = _PRIVATE_KEY_RE.sub(f"{REDACTED} PRIVATE KEY", value)
    value = _URL_CREDENTIALS_RE.sub(rf"\1{REDACTED}@", value)
    value = _AUTH_VALUE_RE.sub(rf"\1 {REDACTED}", value)
    value = _QUOTED_SECRET_RE.sub(rf"\g<1>{REDACTED}\g<3>", value)
    value = _ENV_SECRET_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}",
        value,
    )
    value = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}",
        value,
    )
    value = _SECRET_FLAG_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}",
        value,
    )
    value = _KNOWN_TOKEN_RE.sub(REDACTED, value)
    return _JWT_RE.sub(REDACTED, value)


def _is_secret_field(name: object) -> bool:
    key = re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")
    if key.endswith("_enc"):
        return False
    return key in _SECRET_FIELDS or key.endswith(_SECRET_SUFFIXES)


def redact_data(value: Any) -> Any:
    """Return a recursively redacted copy of JSON-like diagnostic data."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            output_key = redact_text(key) if isinstance(key, str) else key
            if _is_secret_field(key) and item not in (None, "", False, 0):
                cleaned[output_key] = REDACTED
            else:
                cleaned[output_key] = redact_data(item)
        return cleaned
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_data(item) for item in value)
    return value


def redact_log_record(record: logging.LogRecord) -> logging.LogRecord:
    """Redact a record's message, exception text, and stack before formatting."""
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001 - logging must never hide the original record
        message = str(record.msg)
    record.msg = redact_text(message)
    record.args = ()
    if record.exc_info:
        record.exc_text = redact_text(
            "".join(traceback.format_exception(*record.exc_info)),
        )
    if record.stack_info:
        record.stack_info = redact_text(record.stack_info)
    return record


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        redact_log_record(record)
        return True


def install_log_redaction() -> None:
    """Attach redaction to configured root handlers, once per handler."""
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(item, RedactingFilter) for item in handler.filters):
            handler.addFilter(RedactingFilter())
