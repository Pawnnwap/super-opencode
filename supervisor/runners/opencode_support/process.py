from __future__ import annotations

from collections.abc import Callable, Generator

from supervisor.runners.opencode_support.command_builder import build_cmd
from supervisor.runners.opencode_support.stream import classify_line
from supervisor.runners.process_common import (
    ConfigErrorPolicy,
    EngineAttempt,
    default_child_env,
    run_prompt_common,
)


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
    """

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
            phrases=("unable to connect", "is the computer able to access"),
            header="OPENCODE CONFIG ERROR",
            hint=(
                "opencode cannot reach the AI provider.\n"
                "Fix: run 'opencode' interactively -> configure a working provider,\n"
                "or set the model in UI 'opencode model' field."
            ),
        ),
        tokens_from_outcome=lambda outcome: outcome.tokens_total,
    )
