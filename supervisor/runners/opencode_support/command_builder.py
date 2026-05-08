from __future__ import annotations

import logging
from pathlib import Path

from supervisor.prompts.commands import BREVITY_COMMAND
from supervisor.utils.text_utils import coerce_str, quote_prompt

logger = logging.getLogger(__name__)

_DOT_MODEL_FILE = Path(__file__).resolve().parents[2] / ".opencode_model"


def validate_message(message: str, context: str = "message") -> str | None:
    """Return cleaned message or None when empty after coercion."""
    message = coerce_str(message, context)
    if not message:
        logger.warning(
            "Empty message provided to opencode (%s). Returning None to trigger graceful handling.",
            context,
        )
        return None
    return message


def fresh_session_prompt(prompt: str) -> str:
    """Inline brevity rules when session attach cannot be trusted."""
    return f"{BREVITY_COMMAND.strip()}\n\n{prompt}"


def build_cmd(
    *,
    exe: str,
    prompt: str,
    agent: str,
    opencode_model: str | None,
    use_continue: bool,
    session_id: str | None,
    model: str | None = None,
    use_shell: bool = False,
) -> list[str]:
    """Build opencode CLI command list."""
    exe = coerce_str(exe, "exe (_build_cmd)")
    prompt = coerce_str(prompt, "prompt (_build_cmd)")
    agent = coerce_str(agent, "agent (_build_cmd)")

    raw_model_arg = coerce_str(model, "model arg (_build_cmd)")
    raw_self_model = coerce_str(opencode_model, "opencode_model (_build_cmd)")
    resolved_model = raw_model_arg or raw_self_model

    if not resolved_model and _DOT_MODEL_FILE.exists():
        resolved_model = _DOT_MODEL_FILE.read_text(encoding="utf-8").strip()
        logger.debug("Model resolved from .opencode_model file: %r", resolved_model)

    logger.debug(
        "_build_cmd - exe=%r agent=%r use_continue=%s model_arg=%r "
        "self_model=%r resolved_model=%r prompt_len=%d",
        exe,
        agent,
        use_continue,
        raw_model_arg,
        raw_self_model,
        resolved_model,
        len(prompt),
    )

    cmd: list[str] = [exe, "run"]

    # opencode 2.x removed the built-in "coder" agent; default to "build" when
    # no agent is explicitly specified to avoid "agent coder not found" errors.
    cmd += ["--agent", agent if agent else "build"]

    if use_continue:
        if session_id:
            cmd += ["--session", session_id]
        else:
            cmd.append("--continue")

    if resolved_model:
        cmd += ["--model", resolved_model]

    cmd.append("--")
    cmd.append(quote_prompt(prompt) if use_shell else prompt)

    return cmd

