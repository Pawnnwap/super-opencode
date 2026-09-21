import json
import re

_THINKING_BLOCK_RE = re.compile(
    r"(?:<thought>|<think>).*?(?:</thought>|</think>)",
    re.DOTALL,
)


def strip_thinking_blocks(text: str) -> str:
    """Remove all ... and <thought>...</thought> blocks (non-greedy)."""
    return _THINKING_BLOCK_RE.sub("", text)


def normalize_model_response(
    value: object,
    field_name: str = "model response",
) -> str:
    """Coerce model text to string, strip hidden thinking blocks, and trim."""
    return strip_thinking_blocks(coerce_str(value, field_name)).strip()


def head_tail_excerpt(
    text: str,
    max_chars: int,
    *,
    end_ratio: float = 0.5,
    collapse_ws: bool = False,
) -> str:
    """Truncate keeping BOTH ends: agent/verdict messages put conclusions,
    outcomes, and next actions at the END, so a head-only cut loses exactly
    the valuable part. Follows the head-tail truncation convention used by
    other terminal coding agents (keep first N + last N chars with an
    omission marker between).

    ``end_ratio`` is the fraction of ``max_chars`` kept from the end (default
    half). ``collapse_ws`` squeezes whitespace first (for single-line
    excerpts). Returns the text unchanged when within budget.
    """
    text = text or ""
    if collapse_ws:
        text = " ".join(text.split())
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    end_chars = max(1, int(max_chars * end_ratio))
    head_chars = max(1, max_chars - end_chars)
    omitted = len(text) - head_chars - end_chars
    marker = f" [... {omitted} chars omitted ...] "
    return text[:head_chars].rstrip() + marker + text[len(text) - end_chars:].lstrip()


def sanitize_event_message(msg: object) -> str:
    """Convert event msg payloads to a deterministic string representation.

    Lists and dicts are serialized to compact JSON strings.  Existing
    strings are returned unchanged.  All other types are coerced via
    ``str()``.  This prevents implicit iteration or unsafe auto-evaluation
    when the msg value flows through the JSONL log pipeline and the
    the web UI UI rendering layer.

    Parameters
    ----------
    msg:
        The raw message payload from an event dict.

    Returns
    -------
    str
        A string-safe representation of the payload.

    """
    if isinstance(msg, str):
        return msg
    if isinstance(msg, (list, dict)):
        return json.dumps(msg, ensure_ascii=False, separators=(", ", ": "))
    if msg is None:
        return ""
    # Handle edge cases where msg might be an iterable that should be treated as a single entity
    # Convert to string first, then ensure it's a proper string
    result = str(msg)
    # Ensure the result is a proper string (handles cases where str() might return non-string)
    if not isinstance(result, str):
        result = repr(msg)
    return result


def coerce_str(value: object, field_name: str) -> str:
    """Coerce *value* to a stripped string, logging a warning when the raw type
    is not already ``str`` so the caller knows where bad data entered the system.

    Returns an empty string for ``None`` and falsy values.
    """
    import logging
    logger = logging.getLogger(__name__)
    if value is None:
        return ""
    if not isinstance(value, str):
        logger.warning(
            "Type coercion: field '%s' received %r (type=%s) — expected str. "
            "Converting automatically. Check the caller / UI widget that produced this value.",
            field_name,
            value,
            type(value).__name__,
        )
        value = str(value)
    return value.strip()


def quote_prompt(prompt: str) -> str:
    """Wrap prompt in double quotes for shell command execution.

    Escapes internal double quotes by doubling them (Windows convention).
    Ensures prompt is properly surrounded by quotation marks at start and end.
    """
    if prompt is None:
        return '""'
    prompt = coerce_str(prompt, "prompt (quote_prompt)")
    if not prompt:
        return '""'
    escaped = prompt.replace('"', '""')
    return f'"{escaped}"'
