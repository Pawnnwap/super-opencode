from __future__ import annotations

from collections.abc import Callable, Generator

from supervisor.runners.pi_support.command_builder import build_cmd
from supervisor.runners.pi_support.stream import classify_line
from supervisor.runners.process_common import (
    ConfigErrorPolicy,
    EngineAttempt,
    run_prompt_common,
)


def run_prompt(
    runner,
    prompt: str,
    *,
    find_pi_fn: Callable[[str], str],
) -> Generator[dict]:
    """Run one pi prompt through subprocess and update runner state.

    Thin adapter over the shared three-engine driver
    (:func:`process_common.run_prompt_common`). pi-specific seams: the
    workspace-scoped session dir passed to the command builder, session-id
    capture from the stream's ``session`` event, and straight per-request
    token totals (pi reports the final request's real context size — no
    averaging).
    """

    def _prepare(exe: str, model_for_cmd: str | None, use_shell: bool) -> EngineAttempt:
        return EngineAttempt(
            cmd=build_cmd(
                exe=exe,
                prompt=prompt,
                agent=runner.agent,
                pi_model=runner.opencode_model,
                use_continue=runner._use_continue,
                session_id=runner._session_id,
                sessions_dir=runner.pi_sessions_dir,
                model=model_for_cmd,
                use_shell=use_shell,
                variant=getattr(runner, "opencode_variant", "") or "",
            ),
        )

    return run_prompt_common(
        runner,
        prompt,
        engine="pi",
        find_exe_fn=find_pi_fn,
        prepare_attempt=_prepare,
        classify_line=classify_line,
        config_error=ConfigErrorPolicy(
            # pi exits 1 with `Error: ...` on stderr for configuration
            # problems; the model-not-found spelling is the common one for a
            # mis-typed agent model.
            phrases=("error: model", "error: no provider"),
            header="PI CONFIG ERROR",
            hint=(
                "pi cannot run with the configured model/provider.\n"
                "Fix: run 'pi --list-models' to see available models and 'pi'\n"
                "interactively to configure providers, or set the model in the\n"
                "UI 'Agent model (pi)' field (e.g. provider/model)."
            ),
        ),
        tokens_from_outcome=lambda outcome: outcome.tokens_total,
        session_from_outcome=lambda outcome: outcome.session_id or None,
    )
