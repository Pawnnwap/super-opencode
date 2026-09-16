from __future__ import annotations

import logging
from collections.abc import Sequence

from supervisor.utils.text_utils import coerce_str

logger = logging.getLogger(__name__)


def validate_message(message: str, context: str, engine: str) -> str | None:
    """Return cleaned message or None when empty after coercion.

    *engine* ("opencode"/"codex") only labels the warning so logs make clear
    which backend received the empty message.
    """
    message = coerce_str(message, context)
    if not message:
        logger.warning(
            "Empty message provided to %s (%s). Returning None to trigger graceful handling.",
            engine,
            context,
        )
        return None
    return message


def fresh_session_prompt(prompt: str) -> str:
    """Return the task prompt unchanged for the first turn of a session.

    Inlining brevity/style rules here was removed empirically: several free
    models (nemotron ultra, laguna) fixate on a trailing rules block and reply
    "mode active — awaiting command" / "please provide the protocol" instead
    of executing the task. The bare task prompt works everywhere; style
    guidance already lives in the supervisor-driven protocol when needed.
    """
    return prompt


def safe_command_summary(command: Sequence[str]) -> str:
    """Describe an agent invocation without logging prompt, URL, or overrides."""
    visible = [str(part) for part in command[:2] if str(part)]
    return " ".join(visible) + " [arguments redacted]"
