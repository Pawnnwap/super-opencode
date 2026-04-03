import re

_THINKING_BLOCK_RE = re.compile(
    r"(?:<thought>|<think>).*?(?:</thought>|</think>)", re.DOTALL
)


def strip_thinking_blocks(text: str) -> str:
    """Remove all ... and <thought>...</thought> blocks (non-greedy)."""
    return _THINKING_BLOCK_RE.sub("", text)
