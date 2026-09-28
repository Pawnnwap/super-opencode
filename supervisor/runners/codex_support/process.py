from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Generator

from supervisor.analyzers.loop_detector import LoopDetector
from supervisor.runners.command_common import (
    feed_stdin_prompt,
    provider_connection_error,
    safe_command_summary,
)
from supervisor.runners.codex_support.command_builder import (
    CODEX_API_KEY_ENV,
    build_cmd,
)
from supervisor.runners.codex_support.stream import classify_line
from supervisor.runners.opencode_support.result import RunResult
from supervisor.runners.stream_driver import consume_process_stream
from supervisor.utils.text_utils import coerce_str

logger = logging.getLogger(__name__)

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

    Structurally mirrors ``opencode_support.process.run_prompt`` (streaming,
    timeout handling, primary->backup model fallback, char accounting) but
    drives the ``codex exec --json`` CLI and captures the codex session id from
    the structured ``thread.started`` event (regex on raw output as fallback).
    """
    prompt = coerce_str(prompt, "prompt (codex _run_prompt)")

    exe = find_codex_fn(runner.opencode_executable)
    using_backup = False

    while True:
        model_for_cmd = (
            runner.opencode_model_backup if using_backup else runner.opencode_model
        )

        logger.debug(
            "codex _run_prompt pre-build - using_backup=%s model_for_cmd=%r (type=%s) "
            "prompt_len=%d agent=%r",
            using_backup,
            model_for_cmd,
            type(model_for_cmd).__name__,
            len(prompt),
            runner.agent,
        )

        use_shell = sys.platform == "win32" and exe.lower().endswith(
            (".cmd", ".bat", ".ps1"),
        )
        base_url = getattr(runner, "codex_base_url", "") or ""
        api_key = getattr(runner, "codex_api_key", "") or ""
        context_window = getattr(runner, "codex_context_window", 0) or 0
        reasoning_effort = getattr(runner, "codex_reasoning_effort", "") or ""

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
            context_window=context_window,
            reasoning_effort=reasoning_effort,
            extra_config_flags=getattr(runner, "codex_extra_config_flags", None),
        )

        msg = f"Running codex command: {safe_command_summary(cmd)}"
        logger.info(msg)
        yield {"level": "info", "msg": msg}

        # Per-subprocess env: feed the external API key via env var (codex
        # reads it through the provider's env_key) instead of the command line.
        # Scoped to this Popen call, so concurrent runners never collide.
        # codex_extra_env (e.g. an isolated CODEX_HOME for smoke tests)
        # layers on top the same way.
        child_env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb"}
        if base_url and api_key:
            child_env[CODEX_API_KEY_ENV] = api_key
        child_env.update(getattr(runner, "codex_extra_env", None) or {})

        try:
            runner._process = subprocess.Popen(
                cmd,
                # stdin carries the prompt (build_cmd emits `-` as the
                # prompt argument): Windows command-line limits (8191 via
                # cmd.exe shims, 32767 via CreateProcess) make argv prompts
                # fail silently with "The command line is too long".
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(runner.workspace),
                env=child_env,
                shell=use_shell,
            )
            threading.Thread(
                target=feed_stdin_prompt,
                args=(runner._process, prompt),
                daemon=True,
                name="codex-stdin-writer",
            ).start()

            outcome = yield from consume_process_stream(
                runner._process,
                classify_line=classify_line,
                timeout=runner.timeout,
                loop_detector=LoopDetector(),
                abort_on=provider_connection_error,
            )

            if outcome.tokens_total > 0:
                runner._last_tokens_total = outcome.tokens_total

            if outcome.aborted:
                # Provider down: the backup model shares the endpoint, so a
                # fallback turn is doomed — fail this turn immediately.
                runner._last_result = RunResult(
                    stdout=outcome.stdout,
                    stderr=(
                        "[PROVIDER UNREACHABLE] codex could not reach the "
                        f"model endpoint ({outcome.abort_reason}); turn "
                        "aborted fast."
                    ),
                    returncode=-1,
                    prose=outcome.prose,
                )
                logger.error(
                    "codex provider unreachable (%s) — turn aborted fast",
                    outcome.abort_reason,
                )
                runner._chars_exchanged += len(prompt)
                return

            # Prefer the structured thread_id; fall back to a regex over output.
            captured_id = (
                outcome.session_id
                or _extract_session_id(outcome.stdout)
                or _extract_session_id(outcome.stderr)
            )
            if captured_id and captured_id != runner._session_id:
                runner._session_id = captured_id
                logger.info("Captured codex session id: %s", captured_id)

            if outcome.looped:
                runner._last_result = RunResult(
                    stdout=outcome.stdout,
                    stderr=outcome.stderr,
                    returncode=-1,
                    looped=True,
                    loop_reason=outcome.loop_reason,
                    prose=outcome.prose,
                )
                logger.warning("codex loop detected: %s", outcome.loop_reason)
                runner._chars_exchanged += len(prompt) + len(runner._last_result.output)
                return

            if outcome.timed_out:
                runner._last_result = RunResult(
                    stdout=outcome.stdout,
                    stderr=outcome.stderr,
                    returncode=-1,
                    timed_out=True,
                    prose=outcome.prose,
                )
                logger.warning("codex timed out after %ds", runner.timeout)

                if not using_backup and runner.opencode_model_backup:
                    logger.warning(
                        "Primary model %r timed out, falling back to backup %r",
                        runner.opencode_model,
                        runner.opencode_model_backup,
                    )
                    using_backup = True
                    continue

                runner._chars_exchanged += len(prompt) + len(runner._last_result.output)
                return

            stdout = outcome.stdout or ""
            stderr = outcome.stderr or ""
            returncode = outcome.returncode

            combined_lower = (stdout + stderr).lower()
            if (
                "unable to connect" in combined_lower
                or "is the computer able to access" in combined_lower
                or "no model configured" in combined_lower
            ):
                stderr = (
                    "[CODEX CONFIG ERROR] codex cannot reach the AI provider or has no model.\n"
                    "Fix: run 'codex' once to log in / configure a provider, or set the\n"
                    "model in the UI 'model' field (and your provider key/base URL).\n\n"
                    "Raw error:\n" + (stdout + stderr).strip()
                )
                stdout = ""

            runner._last_result = RunResult(
                stdout=stdout,
                stderr=stderr,
                returncode=returncode,
                prose=outcome.prose,
            )
            logger.info(
                "codex exit=%d stdout=%d chars stderr=%d chars tokens=%d",
                returncode,
                len(stdout),
                len(stderr),
                outcome.tokens_total,
            )
            if stderr.strip():
                logger.info("stderr snippet: %s", stderr[:400])

            if (
                not runner._last_result.ok
                and not using_backup
                and runner.opencode_model_backup
            ):
                logger.warning(
                    "Primary model %r failed (exit=%d), falling back to backup %r",
                    runner.opencode_model,
                    returncode,
                    runner.opencode_model_backup,
                )
                using_backup = True
                continue

        except Exception as exc:
            time.sleep(3)
            logger.error(
                "codex launch error - exc=%s using_backup=%s model_for_cmd=%r (type=%s) "
                "prompt_len=%d agent=%r",
                exc,
                using_backup,
                model_for_cmd,
                type(model_for_cmd).__name__,
                len(prompt),
                runner.agent,
            )
            if not using_backup and runner.opencode_model_backup:
                logger.warning(
                    "Falling back to backup model %r after launch error on primary %r",
                    runner.opencode_model_backup,
                    runner.opencode_model,
                )
                using_backup = True
                continue
            runner._last_result = RunResult(exception=str(exc), returncode=-1)

        runner._chars_exchanged += len(prompt) + len(runner._last_result.output)
        return
