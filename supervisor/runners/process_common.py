"""Engine-agnostic subprocess driver shared by opencode, codex, and pi.

Every execution engine drives its CLI the same way: resolve the executable,
build one command per attempt (primary model, then backup), pipe the prompt
over stdin, consume the JSON event stream with the engine's own
``classify_line``, and apply identical failure handling — fast abort on a
provider-down signature, loop-kill, timeout, config-error wrapping and
primary→backup model fallback.

This module owns that whole loop; each engine's ``<engine>_support/process.py``
only supplies its seams via :class:`EngineAttempt` (command + child env) and
optional hooks (token accounting, session capture). Engine process modules
therefore shrink to ~40 lines while keeping their public
``run_prompt(runner, prompt, find_*_fn=...)`` signatures (and tests) intact.

Owns a subprocess import at module level so tests can keep monkeypatching
``subprocess.Popen`` globally (the engine modules share the same module
object).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass

from supervisor.analyzers.loop_detector import LoopDetector
from supervisor.runners.command_common import (
    feed_stdin_prompt,
    provider_connection_error,
    safe_command_summary,
)
from supervisor.runners.opencode_support.result import RunResult
from supervisor.runners.opencode_support.stream import LineEvent
from supervisor.runners.process_utils import remember_process_identity
from supervisor.runners.stream_driver import StreamOutcome, consume_process_stream
from supervisor.utils.text_utils import coerce_str

logger = logging.getLogger(__name__)


def _cancel_event(runner):
    """The runner's cancel Event, or None for test doubles without one."""
    event = getattr(runner, "cancel_event", None)
    return event if callable(getattr(event, "is_set", None)) else None


def _cancel_requested(runner) -> bool:
    event = _cancel_event(runner)
    return bool(event is not None and event.is_set())


def _cancelled_result() -> RunResult:
    return RunResult(stderr="[CANCELLED]", returncode=-1, cancelled=True)


def default_child_env() -> dict[str, str]:
    """Child environment disabling colors/TUI for every engine."""
    return {**os.environ, "NO_COLOR": "1", "TERM": "dumb"}


def windows_shell_for(exe: str) -> bool:
    """``shell=True`` is required to spawn npm ``.cmd``/``.bat`` shims on Windows."""
    return sys.platform == "win32" and exe.lower().endswith((".cmd", ".bat", ".ps1"))


@dataclass
class EngineAttempt:
    """One engine command plus its child environment.

    ``env=None`` means the default sanitized environment
    (:func:`default_child_env`); engines that must inject secrets (codex's
    external-provider API key) build a copy instead.
    """

    cmd: list[str]
    env: dict[str, str] | None = None


@dataclass(frozen=True)
class ConfigErrorPolicy:
    """How to recognise and label an engine's "cannot reach/configured" failure.

    When the combined output contains any *phrase*, stdout is emptied and
    stderr is replaced by *header* + *hint* + the raw output, so the judge
    sees an actionable fix instead of a raw provider stack trace.
    """

    phrases: tuple[str, ...] = ()
    header: str = "CONFIG ERROR"
    hint: str = "The agent CLI cannot reach the AI provider."


def run_prompt_common(
    runner,
    prompt: str,
    *,
    engine: str,
    find_exe_fn: Callable[[str], str],
    prepare_attempt: Callable[[str, str | None, bool], EngineAttempt],
    classify_line: Callable[[str | None], LineEvent | None],
    config_error: ConfigErrorPolicy,
    tokens_from_outcome: Callable[[StreamOutcome], int] | None = None,
    session_from_outcome: Callable[[StreamOutcome], str | None] | None = None,
) -> Generator[dict]:
    """Run one agent prompt through subprocess and update runner state.

    ``prepare_attempt(exe, model_for_cmd, use_shell)`` builds the command and
    environment for one attempt; ``model_for_cmd`` is the backup model when
    retrying, else the primary. ``tokens_from_outcome`` converts the stream
    outcome into the session-level context-token estimate stored on the runner
    (pi/opencode report it directly; codex averages its turn-aggregated
    usage). ``session_from_outcome`` returns the resumable session id when the
    engine reports one in-stream (codex thread id, pi session id); engines
    that capture sessions out-of-band (opencode) pass ``None``.
    """
    prompt = coerce_str(prompt, f"prompt ({engine} _run_prompt)")

    if _cancel_requested(runner):
        runner._last_result = _cancelled_result()
        yield {"level": "info", "msg": f"{engine} turn cancelled before launch."}
        return

    exe = find_exe_fn(runner.opencode_executable)
    using_backup = False

    while True:
        # Cancel can arrive between a failed attempt and its backup retry:
        # never launch another model call after the operator pressed STOP.
        if _cancel_requested(runner):
            runner._last_result = _cancelled_result()
            yield {"level": "info", "msg": f"{engine} turn cancelled."}
            return

        model_for_cmd = (
            runner.opencode_model_backup if using_backup else runner.opencode_model
        )

        logger.debug(
            "%s _run_prompt pre-build - using_backup=%s model_for_cmd=%r (type=%s) "
            "prompt_len=%d agent=%r",
            engine,
            using_backup,
            model_for_cmd,
            type(model_for_cmd).__name__,
            len(prompt),
            runner.agent,
        )

        use_shell = windows_shell_for(exe)
        attempt = prepare_attempt(exe, model_for_cmd, use_shell)
        cmd = attempt.cmd
        child_env = attempt.env if attempt.env is not None else default_child_env()

        # Event stream only — the same line lands in the job log; a stdlib
        # mirror here wrote every turn twice to shared stderr.
        yield {
            "level": "info",
            "msg": f"Running {engine} command: {safe_command_summary(cmd)}",
        }

        try:
            runner._process = subprocess.Popen(
                cmd,
                # stdin carries the prompt (engines never place it in argv):
                # Windows command-line limits (8191 via cmd.exe shims, 32767
                # via CreateProcess) make argv prompts fail silently with
                # "The command line is too long".
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
            # Record the OS identity at spawn so a later tree-kill can never
            # signal a reused PID (see process_utils.remember_process_identity).
            remember_process_identity(runner._process)
            threading.Thread(
                target=feed_stdin_prompt,
                args=(runner._process, prompt),
                daemon=True,
                name=f"{engine}-stdin-writer",
            ).start()

            outcome = yield from consume_process_stream(
                runner._process,
                classify_line=classify_line,
                timeout=runner.timeout,
                loop_detector=LoopDetector(),
                abort_on=provider_connection_error,
                cancel_event=_cancel_event(runner),
            )

            if outcome.cancelled:
                # Never treat a STOP as a failed turn: no backup fallback, no
                # retry, no session capture. The job worker writes CANCELLED.
                runner._last_result = RunResult(
                    stdout=outcome.stdout,
                    stderr=outcome.stderr or "[CANCELLED]",
                    returncode=-1,
                    cancelled=True,
                    prose=outcome.prose,
                )
                logger.info("%s turn cancelled by operator", engine)
                return

            if tokens_from_outcome is not None:
                estimated = tokens_from_outcome(outcome)
                if estimated > 0:
                    runner._last_tokens_total = estimated

            if outcome.aborted:
                # Provider down: the backup model shares the endpoint, so a
                # fallback turn is doomed — fail this turn immediately.
                runner._last_result = RunResult(
                    stdout=outcome.stdout,
                    stderr=(
                        "[PROVIDER UNREACHABLE] agent engine could not reach "
                        f"the model endpoint ({outcome.abort_reason}); turn "
                        "aborted fast."
                    ),
                    returncode=-1,
                    prose=outcome.prose,
                )
                logger.error(
                    "%s provider unreachable (%s) — turn aborted fast",
                    engine,
                    outcome.abort_reason,
                )
                runner._chars_exchanged += len(prompt)
                return

            if session_from_outcome is not None:
                captured_id = session_from_outcome(outcome)
                if captured_id and captured_id != runner._session_id:
                    runner._session_id = captured_id
                    logger.info("Captured %s session id: %s", engine, captured_id)
                    # Record-keeper feed: the job log is where the hownow
                    # sweep reads each live session's id back (opencode's
                    # out-of-band capture already yields the same message).
                    yield {
                        "level": "info",
                        "msg": f"Session ID captured: {captured_id}",
                    }

            if outcome.looped:
                runner._last_result = RunResult(
                    stdout=outcome.stdout,
                    stderr=outcome.stderr,
                    returncode=-1,
                    looped=True,
                    loop_reason=outcome.loop_reason,
                    prose=outcome.prose,
                )
                logger.warning("%s loop detected: %s", engine, outcome.loop_reason)
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
                logger.warning("%s timed out after %ds", engine, runner.timeout)

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
            if any(phrase in combined_lower for phrase in config_error.phrases):
                stderr = (
                    f"[{config_error.header}] {config_error.hint}\n\n"
                    "Raw error:\n" + (stdout + stderr).strip()
                )
                stdout = ""

            # Last-resort evidence floor: an engine that exits non-zero with
            # nothing on stdout AND stderr hands the judge a bare `[exit N]`.
            # That reads as "the agent refused to produce evidence", so gates
            # get closed as UNMET on a harness fault. Label it for what it is.
            if returncode not in (0, None) and not stdout.strip() and not stderr.strip():
                stdout = (
                    f"[{engine} produced no output at all and exited {returncode}] "
                    "This is an engine/harness failure, not agent silence: no turn "
                    "evidence exists, so judge any gate on this turn as UNVERIFIED "
                    "rather than unmet, and report the engine as broken."
                )
                logger.error(
                    "%s exited %d with empty stdout and stderr — the judge would "
                    "have seen a bare '[exit %d]'",
                    engine,
                    returncode,
                    returncode,
                )

            runner._last_result = RunResult(
                stdout=stdout,
                stderr=stderr,
                returncode=returncode,
                prose=outcome.prose,
            )
            # Per-turn accounting is already visible in the event stream
            # (output/diagnostic events); stderr keeps only anomalies.
            logger.debug(
                "%s exit=%d stdout=%d chars stderr=%d chars tokens=%d",
                engine,
                returncode,
                len(stdout),
                len(stderr),
                outcome.tokens_total,
            )
            if stderr.strip():
                logger.debug("stderr snippet: %s", stderr[:400])

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

        except Exception as exc:  # noqa: BLE001 - whole attempt boundary
            if _cancel_requested(runner):
                runner._last_result = _cancelled_result()
                logger.info("%s turn cancelled during launch", engine)
                return
            time.sleep(3)
            logger.error(
                "%s launch error - exc=%s using_backup=%s model_for_cmd=%r (type=%s) "
                "prompt_len=%d agent=%r",
                engine,
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
