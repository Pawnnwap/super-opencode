from __future__ import annotations

import re
from collections.abc import Callable, Generator

from supervisor.runners.codex_support.command_builder import (
    CODEX_API_KEY_ENV,
    build_cmd,
)
from supervisor.runners.codex_support.stream import (
    classify_line,
    context_estimate_from_turn_usage,
)
from supervisor.runners.process_common import (
    ConfigErrorPolicy,
    EngineAttempt,
    default_child_env,
    run_prompt_common,
)

# Fallback extraction of a codex session id from human-readable output, used
# only when the structured `thread.started` event did not carry one. Codex
# prints either a `session_id: <uuid>` style line and/or a rollout file path
# of the form `rollout-<timestamp>-<uuid>.jsonl`. Either form gives us the id.
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_SESSION_PATTERNS = [
    re.compile(rf"session[_ ]?id[\"']?\s*[:=]\s*[\"']?({_UUID})", re.IGNORECASE),
    re.compile(rf"rollout-[0-9T:\-]+-({_UUID})", re.IGNORECASE),
]


def _extract_session_id(text: str) -> str | None:
    for pattern in _SESSION_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def run_prompt(
    runner,
    prompt: str,
    *,
    find_codex_fn: Callable[[str], str],
) -> Generator[dict]:
    """Run one codex prompt through subprocess and update runner state.

    Thin adapter over the shared three-engine driver
    (:func:`process_common.run_prompt_common`). Codex-specific seams: the
    external-provider `-c` overrides + API-key env var, session-id capture
    from the structured ``thread.started`` event (regex on raw output as
    fallback), and turn-aggregated usage averaged into a per-request context
    estimate (see ``context_estimate_from_turn_usage``).
    """

    def _prepare(exe: str, model_for_cmd: str | None, use_shell: bool) -> EngineAttempt:
        base_url = getattr(runner, "codex_base_url", "") or ""
        api_key = getattr(runner, "codex_api_key", "") or ""
        cmd = build_cmd(
            exe=exe,
            prompt=prompt,
            agent=runner.agent,
            codex_model=runner.opencode_model,
            use_continue=runner._use_continue,
            session_id=runner._session_id,
            model=model_for_cmd,
            use_shell=use_shell,
            base_url=base_url,
            api_key=api_key,
            context_window=getattr(runner, "codex_context_window", 0) or 0,
            reasoning_effort=getattr(runner, "codex_reasoning_effort", "") or "",
            extra_config_flags=getattr(runner, "codex_extra_config_flags", None),
        )
        # Per-subprocess env: feed the external API key via env var (codex
        # reads it through the provider's env_key) instead of the command
        # line. Scoped to this attempt's env copy, so concurrent runners
        # never collide. codex_extra_env (e.g. an isolated CODEX_HOME for
        # smoke tests) layers on top the same way.
        env = default_child_env()
        if base_url and api_key:
            env[CODEX_API_KEY_ENV] = api_key
        env.update(getattr(runner, "codex_extra_env", None) or {})
        return EngineAttempt(cmd=cmd, env=env)

    def _session_id(outcome) -> str | None:
        # Prefer the structured thread_id; fall back to a regex over output.
        return (
            outcome.session_id
            or _extract_session_id(outcome.stdout)
            or _extract_session_id(outcome.stderr)
        )

    return run_prompt_common(
        runner,
        prompt,
        engine="codex",
        find_exe_fn=find_codex_fn,
        prepare_attempt=_prepare,
        classify_line=classify_line,
        config_error=ConfigErrorPolicy(
            phrases=(
                "unable to connect",
                "is the computer able to access",
                "no model configured",
            ),
            header="CODEX CONFIG ERROR",
            hint=(
                "codex cannot reach the AI provider or has no model.\n"
                "Fix: run 'codex' once to log in / configure a provider, or set the\n"
                "model in the UI 'model' field (and your provider key/base URL)."
            ),
        ),
        tokens_from_outcome=lambda outcome: context_estimate_from_turn_usage(
            outcome.tokens_total,
            outcome.tool_count,
        ),
        session_from_outcome=_session_id,
    )
