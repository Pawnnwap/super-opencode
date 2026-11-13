from __future__ import annotations

import logging
from collections.abc import Callable, Generator

from supervisor.runners.opencode_support.command_builder import build_cmd
from supervisor.runners.opencode_support.stream import classify_line
from supervisor.runners.process_common import (
    ConfigErrorPolicy,
    EngineAttempt,
    default_child_env,
    run_prompt_common,
)

logger = logging.getLogger(__name__)

# opencode 2.x validates the run flags against its resolved configuration and
# exits 1 BEFORE any model call when a value is not accepted. Each signature
# below names the argument to drop for the retry, so a stale persisted setting
# costs one wasted process instead of every turn of the run.
_REJECTIONS: tuple[tuple[str, str], ...] = (
    ("variant unavailable", "variant"),
    ("invalid model reference", "model"),
    ("agent not found", "agent"),
)


def _rejected_arg(result) -> str:
    """Which run argument the CLI rejected, or "" when it accepted them all."""
    if result is None or result.ok or result.cancelled:
        return ""
    text = f"{result.stdout}\n{result.stderr}".lower()
    for phrase, arg in _REJECTIONS:
        if phrase in text:
            return arg
    return ""


def run_prompt(
    runner,
    prompt: str,
    *,
    find_opencode_fn: Callable[[str], str],
) -> Generator[dict]:
    """Run one opencode prompt through subprocess and update runner state.

    Thin adapter over the shared three-engine driver
    (:func:`process_common.run_prompt_common`); opencode contributes its
    command builder, stream classifier and config-error wording, and reads
    token totals straight off the stream outcome (opencode reports the real
    per-request context total — no averaging needed).

    A turn the CLI rejected on its own arguments (unknown variant, bare model
    reference, agent this build does not ship) is retried once with that
    argument dropped. The command builder already validates against the CLI's
    resolved config, but it cannot see models served by built-in providers
    (``opencode-go/…``, ``openrouter/…``) — those are absent from
    ``opencode debug config``, so the rejection only surfaces at runtime.
    Without this retry the supervisor reads an empty turn, judges the gates
    unmet, and the run stalls until an operator cancels it.
    """
    variant = getattr(runner, "opencode_variant", "") or ""
    model = getattr(runner, "opencode_model", None)
    backup = getattr(runner, "opencode_model_backup", None)
    agent = getattr(runner, "agent", "") or ""

    yield from _run_once(runner, prompt, find_opencode_fn)

    dropped = _rejected_arg(runner._last_result)
    if not dropped:
        return

    logger.warning(
        "opencode rejected the %s argument (%s); retrying once without it",
        dropped,
        (runner._last_result.stdout or runner._last_result.stderr or "").strip()[:200],
    )
    yield {
        "level": "warn",
        "msg": (
            f"opencode rejected the {dropped} argument; retrying this turn "
            "with the CLI default."
        ),
    }

    # Drop exactly the rejected argument; everything else stays as configured.
    # The backup model is cleared with the primary: it is usually the same
    # string, and run_prompt_common would otherwise retry the rejected value.
    if dropped == "variant":
        runner.opencode_variant = ""
    elif dropped == "model":
        runner.opencode_model = None
        runner.opencode_model_backup = None
    else:
        runner.agent = "build"
    try:
        yield from _run_once(runner, prompt, find_opencode_fn)
    finally:
        runner.opencode_variant = variant
        runner.opencode_model = model
        runner.opencode_model_backup = backup
        runner.agent = agent


def _run_once(
    runner,
    prompt: str,
    find_opencode_fn: Callable[[str], str],
) -> Generator[dict]:
    def _prepare(exe: str, model_for_cmd: str | None, use_shell: bool) -> EngineAttempt:
        guard_env = dict(getattr(runner, "access_guard_env", None) or {})
        env = {**default_child_env(), **guard_env} if guard_env else None
        return EngineAttempt(
            env=env,
            cmd=build_cmd(
                exe=exe,
                prompt=prompt,
                agent=runner.agent,
                opencode_model=runner.opencode_model,
                use_continue=runner._use_continue,
                session_id=runner._session_id,
                model=model_for_cmd,
                use_shell=use_shell,
                use_pure=bool(getattr(runner, "opencode_pure", False)),
                dir_=str(runner.workspace),
                variant=getattr(runner, "opencode_variant", "") or "",
            ),
        )

    return run_prompt_common(
        runner,
        prompt,
        engine="opencode",
        find_exe_fn=find_opencode_fn,
        prepare_attempt=_prepare,
        classify_line=classify_line,
        config_error=ConfigErrorPolicy(
            # ``provider.no-route`` / ``Model unavailable:`` is opencode 2.x
            # saying the configured model/provider pair does not exist. Without
            # it the run burns every retry on the same doomed model, and (before
            # the `error` event was parsed) each turn looked like agent silence.
            # The three argument rejections are listed too: they only reach the
            # judge when the retry above also failed, and then the hint is what
            # tells the operator which setting to fix.
            phrases=(
                "unable to connect",
                "is the computer able to access",
                "provider.no-route",
                "model unavailable",
                "variant unavailable",
                "invalid model reference",
                "agent not found",
            ),
            header="OPENCODE CONFIG ERROR",
            hint=(
                "opencode cannot reach the AI provider, or the model/agent/"
                "variant is not available on it.\n"
                "Fix: run 'opencode' interactively -> configure a working "
                "provider,\nor set a model that provider serves in the UI "
                "'opencode model' field."
            ),
        ),
        tokens_from_outcome=lambda outcome: outcome.tokens_total,
    )
