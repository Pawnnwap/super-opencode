from __future__ import annotations

import logging
from pathlib import Path

from supervisor.runners.command_common import (
    validate_message as _validate_message_common,
)
from supervisor.utils.text_utils import coerce_str

logger = logging.getLogger(__name__)

_DOT_MODEL_FILE = Path(__file__).resolve().parents[2] / ".opencode_model"


def validate_message(message: str, context: str = "message") -> str | None:
    return _validate_message_common(message, context, engine="opencode")


def resolve_model(model: str | None, opencode_model: str | None) -> str:
    """Resolve the model string for one opencode command."""
    raw_model_arg = coerce_str(model, "model arg (_build_cmd)")
    raw_self_model = coerce_str(opencode_model, "opencode_model (_build_cmd)")
    resolved_model = raw_model_arg or raw_self_model
    if not resolved_model and _DOT_MODEL_FILE.exists():
        resolved_model = _DOT_MODEL_FILE.read_text(encoding="utf-8").strip()
        logger.debug("Model resolved from .opencode_model file: %r", resolved_model)
    return resolved_model


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
    use_pure: bool = False,
    dir_: str | None = None,
    variant: str = "",
) -> list[str]:
    """Build opencode CLI command list.

    The prompt is NEVER placed on the command line: with no message argument,
    ``opencode run`` reads the message from stdin (verified on opencode 1.x —
    an empty stdin errors with "You must provide a message or a command").
    The *prompt* argument is still validated/coerced here and its length is
    logged, but the caller owns piping it to the child.

    ``use_shell`` is accepted for signature compatibility (the process layer
    still needs it to decide ``shell=True`` for ``.cmd`` shims) but no longer
    changes how the prompt is passed.
    """
    exe = coerce_str(exe, "exe (_build_cmd)")
    prompt = coerce_str(prompt, "prompt (_build_cmd)")
    agent = coerce_str(agent, "agent (_build_cmd)")

    raw_model_arg = coerce_str(model, "model arg (_build_cmd)")
    raw_self_model = coerce_str(opencode_model, "opencode_model (_build_cmd)")
    resolved_model = resolve_model(raw_model_arg, raw_self_model)

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

    # --pure skips user plugins/skills so supervised runs are not hijacked by
    # global agent customization. Tested working alongside --format json on
    # opencode 1.x; opt-in via SupervisorConfig.opencode_pure.
    if use_pure:
        cmd.append("--pure")

    # Pin the working tree explicitly: without --dir, opencode's tools can
    # resolve onto a stale project context and operate outside the workspace
    # (observed as writes landing in the supervisor's own repo).
    if dir_:
        cmd += ["--dir", dir_]

    if use_continue:
        if session_id:
            cmd += ["--session", session_id]
        else:
            cmd.append("--continue")

    if resolved_model:
        cmd += ["--model", resolved_model]

    # Model variant (provider-specific reasoning effort, e.g. none/minimal/
    # low). Only set deliberately (connectivity probes); empty = model default.
    if variant:
        cmd += ["--variant", variant]

    # Stream raw JSON events (one per line) instead of the formatted TUI
    # output. The process layer parses these to keep the model's prose, drop
    # verbose tool I/O, and read real token counts. --pure is intentionally
    # NOT used (it hangs alongside --format json).
    cmd += ["--format", "json"]

    # No message argument: opencode reads the message from stdin (see the
    # docstring for why argv prompts are unsafe on Windows — 8191 chars via
    # the npm .cmd shim's cmd.exe, 32767 via CreateProcess). The process
    # layer feeds the prompt through the stdin pipe.
    return cmd

