from __future__ import annotations

import logging
import re
import subprocess
from functools import lru_cache
from pathlib import Path

from supervisor.runners.command_common import resolve_model_arg
from supervisor.utils.text_utils import coerce_str

logger = logging.getLogger(__name__)

_DOT_MODEL_FILE = Path(__file__).resolve().parents[2] / ".opencode_model"

# `opencode --version` startup via an npm .cmd shim spawns node (~1-2s); 15s
# leaves headroom without ever looking like a hang.
_VERSION_TIMEOUT = 15.0


@lru_cache(maxsize=None)
def _opencode_major(exe: str) -> int | None:
    """Major version of the installed opencode CLI, or None when unknown.

    One ``<exe> --version`` per executable per process (cached): the answer
    decides the command shape because opencode 2.x removed ``--dir`` /
    ``--pure`` / ``--variant`` from ``run`` (unknown run flags hard-error
    with a help dump, exit 1). A probe that cannot run or parse returns
    None and the caller keeps the legacy 1.x flags.
    """
    try:
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("opencode version probe failed for %r", exe, exc_info=True)
        return None
    for stream in (proc.stdout, proc.stderr):
        match = re.search(r"v?(\d+)\.", stream or "")
        if match:
            return int(match.group(1))
    return None


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

    Version-aware command shape: opencode 2.x removed the ``run`` flags
    ``--dir``, ``--pure`` and ``--variant`` (unknown run flags hard-error with
    a help dump, exit 1 — verified live on v2.2.7). The installed major is
    probed once per executable (``<exe> --version``); on 2.x the removed
    flags are dropped and the variant folds into
    ``--model provider/model#variant`` (which 2.x parses), while 1.x — or a
    failed probe (None) — keeps the legacy flags unchanged.
    """
    exe = coerce_str(exe, "exe (_build_cmd)")
    prompt = coerce_str(prompt, "prompt (_build_cmd)")
    agent = coerce_str(agent, "agent (_build_cmd)")

    raw_model_arg = coerce_str(model, "model arg (_build_cmd)")
    raw_self_model = coerce_str(opencode_model, "opencode_model (_build_cmd)")
    resolved_model = resolve_model_arg(
        raw_model_arg, raw_self_model, _DOT_MODEL_FILE, "opencode"
    )

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

    # Shape the command for the installed CLI: opencode 2.x removed the run
    # flags --dir/--pure/--variant, so the flags below branch on the major.
    major = _opencode_major(exe)
    is_v2 = major == 2
    logger.debug("opencode major probe: %r (exe=%r)", major, exe)

    cmd: list[str] = [exe, "run"]

    # opencode 2.x removed the built-in "coder" agent; default to "build" when
    # no agent is explicitly specified to avoid "agent coder not found" errors.
    cmd += ["--agent", agent if agent else "build"]

    # --pure skips user plugins/skills so supervised runs are not hijacked by
    # global agent customization. Tested working alongside --format json on
    # opencode 1.x; opt-in via SupervisorConfig.opencode_pure. opencode 2.x
    # removed the flag, so there it is only warned about (warn + ignore
    # rather than silently rerouting to --standalone, which would change the
    # plugin/service semantics the guard was verified against).
    if use_pure:
        if is_v2:
            logger.warning(
                "opencode_pure is set but --pure was removed in opencode 2.x; "
                "ignoring (exe=%r, major=%s)",
                exe,
                major,
            )
        else:
            cmd.append("--pure")

    # Pin the working tree explicitly: without --dir, opencode's tools can
    # resolve onto a stale project context and operate outside the workspace
    # (observed as writes landing in the supervisor's own repo). opencode 2.x
    # removed --dir; the child already runs with cwd=workspace
    # (process_common.run_prompt_common) and the access-guard plugin gates
    # every tool path, so dropping it there changes nothing.
    if dir_:
        if is_v2:
            logger.debug(
                "--dir %r dropped: removed in opencode 2.x; the child's cwd "
                "is the workspace (exe=%r, major=%s)",
                dir_,
                exe,
                major,
            )
        else:
            cmd += ["--dir", dir_]

    if use_continue:
        if session_id:
            cmd += ["--session", session_id]
        else:
            cmd.append("--continue")

    if resolved_model:
        model_ref = resolved_model
        if is_v2 and variant and "#" not in model_ref:
            # opencode 2.x removed --variant; the model string carries the
            # variant instead (verified: `--model provider/model#variant`
            # parses on v2.2.7). A ref that already names a variant is kept.
            model_ref = f"{resolved_model}#{variant}"
        cmd += ["--model", model_ref]

    # Model variant (provider-specific reasoning effort, e.g. none/minimal/
    # low). Only set deliberately (connectivity probes); empty = model
    # default. Kept as a flag on opencode 1.x only — 2.x folds it into the
    # model string above.
    if variant and not is_v2:
        cmd += ["--variant", variant]

    # Stream raw JSON events (one per line) instead of the formatted TUI
    # output. The process layer parses these to keep the model's prose, drop
    # verbose tool I/O, and read real token counts.
    cmd += ["--format", "json"]

    # No message argument: opencode reads the message from stdin (see the
    # docstring for why argv prompts are unsafe on Windows — 8191 chars via
    # the npm .cmd shim's cmd.exe, 32767 via CreateProcess). The process
    # layer feeds the prompt through the stdin pipe.
    return cmd

