from __future__ import annotations

import logging

from supervisor.prompts.commands import BREVITY_COMMAND
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
    """Inline brevity rules into the first prompt of a session."""
    return f"{BREVITY_COMMAND.strip()}\n\n{prompt}"
