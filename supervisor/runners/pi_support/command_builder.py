from __future__ import annotations

import logging
from pathlib import Path

from supervisor.runners.command_common import (
    parallel_runs_active,
    resolve_model_arg,
)
from supervisor.utils.text_utils import coerce_str

logger = logging.getLogger(__name__)

_DOT_MODEL_FILE = Path(__file__).resolve().parents[2] / ".pi_model"

# Directory name (inside the workspace) for pi's session files. Scoped to the
# workspace so `--continue` can only ever attach to a session of THIS run —
# the same isolation opencode gets from --session <id> and codex from
# `resume <id>` — and so sessions are archived with the workspace.
SESSIONS_DIR_NAME = ".pi-sessions"

# pi loads user extensions, MCP servers and skills into every run by default
# (they appeared as extra tools in the system prompt during live probing).
# Supervised runs stay deterministic on pi's built-in tool set (read / bash /
# edit / write): no extension can hijack the task, stall startup on external
# networks, or muddy the provider-unreachable abort. Project context files
# (AGENTS.md / CLAUDE.md) still load, matching opencode/codex behaviour.
_PI_HYGIENE_FLAGS = ["--no-extensions", "--no-mcp", "--no-skills"]

# pi's thinking ladder (`--thinking`). agent_reasoning values map onto it;
# "none" is the supervisor UI's spelling of pi's "off".
PI_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
_THINKING_ALIASES = {"none": "off"}


def sessions_dir_for(workspace) -> str:
    """Workspace-scoped pi session directory (created by the runner)."""
    return str(Path(workspace) / SESSIONS_DIR_NAME)


def thinking_flags(effort: str) -> list[str]:
    """`--thinking <level>` for the agent's reasoning control.

    Values outside pi's ladder are dropped (with a log) rather than passed
    through — an invalid level fails the whole run at CLI argument parsing,
    while omitting the flag just uses the model default.
    """
    raw = coerce_str(effort, "effort (pi thinking_flags)").strip().lower()
    if not raw:
        return []
    level = _THINKING_ALIASES.get(raw, raw)
    if level not in PI_THINKING_LEVELS:
        logger.debug("pi thinking level %r not in ladder; using model default", effort)
        return []
    return ["--thinking", level]


def build_cmd(
    *,
    exe: str,
    prompt: str,
    agent: str,
    pi_model: str | None,
    use_continue: bool,
    session_id: str | None,
    sessions_dir: str,
    model: str | None = None,
    use_shell: bool = False,
    variant: str = "",
) -> list[str]:
    """Build a ``pi -p`` CLI command list.

    Fresh run:    ``pi -p --mode json --session-dir DIR [flags]``
    Continuation: same plus ``--session <id>`` (captured from the stream's
                  ``session`` event) or ``--continue`` (workspace-scoped
                  fallback; suppressed under SOP_PARALLEL_RUNS like codex's
                  ``resume --last``).

    No message argument: pi reads the prompt from stdin (verified on pi
    1.4.0 — piped stdin with no positional message becomes the user
    message). The *prompt* argument is still validated/coerced here and its
    length is logged, but the caller owns piping it to the child.

    ``agent`` / ``use_shell`` are accepted for signature parity with the
    other engines (the process layer needs ``use_shell`` to decide
    ``shell=True`` for ``.cmd`` shims); pi has no per-agent concept.
    """
    exe = coerce_str(exe, "exe (pi build_cmd)")
    prompt = coerce_str(prompt, "prompt (pi build_cmd)")
    agent = coerce_str(agent, "agent (pi build_cmd)")

    resolved_model = resolve_model_arg(model, pi_model, _DOT_MODEL_FILE, "pi")

    logger.debug(
        "pi build_cmd - exe=%r agent=%r use_continue=%s session_id=%r "
        "model_arg=%r self_model=%r resolved_model=%r prompt_len=%d",
        exe,
        agent,
        use_continue,
        session_id,
        model,
        pi_model,
        resolved_model,
        len(prompt),
    )

    cmd: list[str] = [exe, "-p", "--mode", "json"]
    cmd += ["--session-dir", sessions_dir]

    if use_continue:
        if session_id:
            # Pin the exact session (captured from a prior turn's `session`
            # event) so concurrent pi runs never cross sessions.
            cmd += ["--session", session_id]
        elif parallel_runs_active():
            # No captured id while several runs share one workspace/session
            # dir: `--continue` could pick another replica's session, so
            # degrade to a fresh session instead of risking cross-talk.
            pass
        else:
            cmd += ["--continue"]

    if resolved_model:
        cmd += ["--model", resolved_model]

    cmd += thinking_flags(variant)
    cmd += _PI_HYGIENE_FLAGS

    # No trailing message argument: pi reads the prompt from stdin (see the
    # docstring for why argv prompts are unsafe on Windows — 8191 chars via
    # the npm .cmd shim's cmd.exe, 32767 via CreateProcess). The process
    # layer feeds the prompt through the stdin pipe.
    return cmd
