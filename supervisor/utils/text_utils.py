import json
import re

_THINKING_BLOCK_RE = re.compile(
    r"(?:<thought>|<think>).*?(?:</thought>|</think>)",
    re.DOTALL,
)


def strip_thinking_blocks(text: str) -> str:
    """Remove all ... and <thought>...</thought> blocks (non-greedy)."""
    return _THINKING_BLOCK_RE.sub("", text)


def sanitize_event_message(msg: object) -> str:
    """Convert event msg payloads to a deterministic string representation.

    Lists and dicts are serialized to compact JSON strings.  Existing
    strings are returned unchanged.  All other types are coerced via
    ``str()``.  This prevents implicit iteration or unsafe auto-evaluation
    when the msg value flows through the JSONL log pipeline and the
    Streamlit UI rendering layer.

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
