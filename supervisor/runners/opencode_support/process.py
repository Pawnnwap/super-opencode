from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable, Generator

from supervisor.analyzers.loop_detector import LoopDetector
from supervisor.runners.opencode_support.command_builder import build_cmd
from supervisor.runners.opencode_support.result import RunResult
from supervisor.runners.opencode_support.stream import classify_line
from supervisor.runners.stream_driver import consume_process_stream
from supervisor.utils.text_utils import coerce_str

logger = logging.getLogger(__name__)


def run_prompt(
    runner,
    prompt: str,
    *,
    find_opencode_fn: Callable[[str], str],
) -> Generator[dict]:
    """Run one opencode prompt through subprocess and update runner state."""
    prompt = coerce_str(prompt, "prompt (_run_prompt)")

    exe = find_opencode_fn(runner.opencode_executable)
    using_backup = False

    while True:
        model_for_cmd = (
            runner.opencode_model_backup if using_backup else runner.opencode_model
        )

        logger.debug(
            "_run_prompt pre-build - using_backup=%s model_for_cmd=%r (type=%s) "
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
        cmd = build_cmd(
            exe=exe,
            prompt=prompt,
            agent=runner.agent,
            opencode_model=runner.opencode_model,
            use_continue=runner._use_continue,
            session_id=runner._session_id,
            model=model_for_cmd,
            use_shell=use_shell,
        )

        msg = f"Running opencode command: {' '.join(cmd)}"
        logger.info(msg)
        yield {"level": "info", "msg": msg}

        try:
            runner._process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(runner.workspace),
                env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
                shell=use_shell,
            )

            outcome = yield from consume_process_stream(
                runner._process,
                classify_line=classify_line,
                timeout=runner.timeout,
                loop_detector=LoopDetector(),
            )

            if outcome.tokens_total > 0:
                runner._last_tokens_total = outcome.tokens_total

            if outcome.looped:
                runner._last_result = RunResult(
                    stdout=outcome.stdout,
                    stderr=outcome.stderr,
                    returncode=-1,
                    looped=True,
                    loop_reason=outcome.loop_reason,
                )
                logger.warning("opencode loop detected: %s", outcome.loop_reason)
                runner._chars_exchanged += len(prompt) + len(runner._last_result.output)
                return

            if outcome.timed_out:
                runner._last_result = RunResult(
                    stdout=outcome.stdout,
                    stderr=outcome.stderr,
                    returncode=-1,
                    timed_out=True,
                )
                logger.warning("opencode timed out after %ds", runner.timeout)

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
            ):
                stderr = (
                    "[OPENCODE CONFIG ERROR] opencode cannot reach the AI provider.\n"
                    "Fix: run 'opencode' interactively -> configure a working provider,\n"
                    "or set the model in UI 'opencode model' field.\n\n"
                    "Raw error:\n" + (stdout + stderr).strip()
                )
                stdout = ""

            runner._last_result = RunResult(
                stdout=stdout,
                stderr=stderr,
                returncode=returncode,
            )
            logger.info(
                "opencode exit=%d stdout=%d chars stderr=%d chars tokens=%d",
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
                "opencode launch error - exc=%s using_backup=%s model_for_cmd=%r (type=%s) "
                "prompt_snippet=%r agent=%r",
                exc,
                using_backup,
                model_for_cmd,
                type(model_for_cmd).__name__,
                prompt[:120],
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
