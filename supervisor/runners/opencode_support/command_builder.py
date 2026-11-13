from __future__ import annotations

import logging
import re
import subprocess
from functools import cache
from pathlib import Path

from supervisor.runners.command_common import resolve_model_arg
from supervisor.runners.opencode_support import catalog
from supervisor.utils.text_utils import coerce_str

logger = logging.getLogger(__name__)

_DOT_MODEL_FILE = Path(__file__).resolve().parents[2] / ".opencode_model"

# `opencode --version` startup via an npm .cmd shim spawns node (~1-2s); 15s
# leaves headroom without ever looking like a hang.
_VERSION_TIMEOUT = 15.0


@cache
def _opencode_major(exe: str) -> int | None:
    """Major version of the installed opencode CLI, or None when unknown.

    One ``<exe> --version`` per executable per process (cached): the answer
    decides the command shape because opencode 2.x removed ``--dir`` /
    ``--pure`` / ``--variant`` from ``run`` (unknown run flags hard-error
    with a help dump, exit 1). A probe that cannot run or parse returns
    None and the caller keeps the legacy 1.x flags.

    The executable is resolved through the locator first: ``CreateProcess``
    does not apply PATHEXT, so a bare ``opencode`` would spawn the
    extension-less shell script (or nothing) instead of ``opencode.cmd``.
    """
    try:
        proc = subprocess.run(
            [catalog.resolve_exe(exe), "--version"],
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


def _validated_agent(exe: str, agent: str, cwd: str) -> str:
    """Agent id this build ships; falls back to ``build`` when unknown.

    opencode 2.x exits 1 with ``Agent not found: "<name>"`` before any model
    call, so a stale persisted agent (1.x's ``coder``) would kill every turn.
    An unanswerable probe keeps the configured value untouched.
    """
    declared = catalog.declared_agents(exe, cwd)
    if declared is None:
        return agent
    if agent in declared:
        return agent
    fallback = "build" if "build" in declared else min(declared)
    logger.warning(
        "opencode agent %r is not shipped by this build (%s); using %r",
        agent,
        ", ".join(sorted(declared)),
        fallback,
    )
    return fallback


def _validated_model(exe: str, model: str, cwd: str) -> str:
    """``provider/model`` the CLI accepts, or "" to let its default apply.

    opencode 2.x rejects a bare model name outright ("Invalid model
    reference"), so a persisted ``swift-1.5`` must be qualified or dropped.
    """
    resolved, note = catalog.resolve_model(exe, model, cwd)
    if note:
        logger.warning("opencode model: %s", note)
    return resolved or ""


def _validated_variant(exe: str, model: str, variant: str, cwd: str) -> str:
    """Variant the model declares, or "" when it declares none.

    opencode 2.x exits 1 with ``Variant unavailable for <model>: <variant>``
    before the model is called. A model that is configured but declares no
    variants therefore must not receive one; an unanswerable probe keeps the
    configured value (the runtime retry in ``opencode_support.process`` is the
    safety net).
    """
    if not variant:
        return ""
    declared = catalog.declared_variants(exe, model, cwd)
    if declared is None:
        return variant
    if variant in declared:
        return variant
    logger.warning(
        "opencode variant %r is not declared by %s (declared: %s); dropping it",
        variant,
        model or "(CLI default model)",
        ", ".join(declared) or "none",
    )
    return ""


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

    On 2.x every value is additionally checked against the CLI's own resolved
    configuration (``opencode debug config`` / ``debug agents``): an agent the
    build does not ship, a bare model name, or a variant the model does not
    declare each make ``run`` exit 1 before the model is called, which the
    supervisor can only read as an empty turn. Values that cannot be honoured
    are replaced (agent -> ``build``) or dropped (model -> CLI default,
    variant -> model default) with a warning, so a stale persisted setting can
    no longer kill every turn of a run.
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
    # `>= 2` (not `== 2`) so a future 3.x keeps the 2.x shape instead of
    # silently reverting to flags that no longer exist.
    major = _opencode_major(exe)
    is_v2 = major is not None and major >= 2
    logger.debug("opencode major probe: %r (exe=%r)", major, exe)

    cwd = dir_ or ""
    cmd: list[str] = [exe, "run"]

    # opencode 2.x removed the built-in "coder" agent; default to "build" when
    # no agent is explicitly specified to avoid "agent coder not found" errors.
    chosen_agent = agent if agent else "build"
    if is_v2:
        chosen_agent = _validated_agent(exe, chosen_agent, cwd)
    cmd += ["--agent", chosen_agent]

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
        if is_v2:
            # A persisted ref may already carry `#variant`; validate the base
            # model and whichever variant the ref names, then rebuild the ref.
            base, _, embedded = model_ref.partition("#")
            base = _validated_model(exe, base, cwd)
            if base:
                variant = _validated_variant(exe, base, embedded or variant, cwd)
                model_ref = f"{base}#{variant}" if variant else base
            else:
                model_ref = ""
        if model_ref:
            cmd += ["--model", model_ref]

    # Model variant (provider-specific reasoning effort, e.g. none/minimal/
    # low). Only set deliberately (connectivity probes); empty = model
    # default. Kept as a flag on opencode 1.x only — 2.x folds it into the
    # model string above.
    if variant and not is_v2:
        cmd += ["--variant", variant]

    # Auto-approve every permission the configuration does not explicitly
    # deny. This is the 2.x replacement for the `autoapprove: true` config key
    # the supervisor writes for 1.x — v2 silently drops that key (verified:
    # `debug config` no longer shows it), and a non-interactive run then
    # auto-REJECTS every `ask` rule ("permission requested: read (.env);
    # auto-rejecting"), so a user's global `ask` entry disables a tool for the
    # whole run. Explicit `deny` rules still deny, so the supervisor's access
    # guard (external_directory deny + plugin) is unaffected.
    if is_v2:
        cmd.append("--auto")

    # Stream raw JSON events (one per line) instead of the formatted TUI
    # output. The process layer parses these to keep the model's prose, drop
    # verbose tool I/O, and read real token counts.
    cmd += ["--format", "json"]

    # No message argument: opencode reads the message from stdin (see the
    # docstring for why argv prompts are unsafe on Windows — 8191 chars via
    # the npm .cmd shim's cmd.exe, 32767 via CreateProcess). The process
    # layer feeds the prompt through the stdin pipe.
    return cmd
