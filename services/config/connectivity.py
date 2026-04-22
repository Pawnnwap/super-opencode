from __future__ import annotations

import os
import threading
from pathlib import Path

from openai import OpenAI

from supervisor.runners.opencode_runner import (
    _SESSION_CAPTURE_LOCK,
    OpencodeRunner,
    find_opencode,
)
from supervisor.utils.text_utils import normalize_model_response


def run_with_timeout(fn, seconds: int = 30):
    result, error = [], []

    def worker():
        try:
            result.append(fn())
        except Exception as exc:
            error.append(exc)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=seconds)
    if thread.is_alive():
        raise TimeoutError(f"Timed out after {seconds}s")
    if error:
        raise error[0]
    return result[0]


def run_test_with_timeout(
    test_fn,
    test_name: str,
    timeout_seconds: int = 30,
) -> tuple[bool, str]:
    try:
        return run_with_timeout(test_fn, seconds=timeout_seconds)
    except TimeoutError:
        return False, f"{test_name} timed out."
    except Exception as exc:
        return False, f"{test_name} failed: {exc}"


def test_opencode_connectivity(
    opencode_executable: str,
    opencode_model: str | None,
    opencode_model_backup: str | None,
    timeout: int = 30,
) -> tuple[bool, str]:
    workspace = (
        Path(os.environ.get("TEMP", os.environ.get("TMPDIR", "/tmp")))
        / "opencode_test_dummy"
    )
    workspace.mkdir(exist_ok=True)
    try:
        exe = find_opencode(opencode_executable or "")
    except FileNotFoundError as exc:
        return False, str(exc)

    runner = OpencodeRunner(
        workspace=workspace,
        opencode_model=opencode_model,
        opencode_executable=exe,
        opencode_model_backup=opencode_model_backup,
        timeout=timeout,
    )

    def _inner():
        runner._alive = True
        # Hold the session-capture lock while the probe runs. The probe creates
        # a throwaway opencode session in a temp workspace, and concurrently a
        # real task's start() uses a before/after session-list diff to isolate
        # *its* session ID. Letting the probe fire inside that diff window
        # would make the diff ambiguous and force the task into a --continue
        # fallback. Holding the lock serialises the two paths.
        with _SESSION_CAPTURE_LOCK:
            for _ in runner._run_prompt("hi"):
                pass
        _output, timed_out = runner.read_output(timeout=25)
        if timed_out:
            return False, "opencode timed out reading output."
        if runner._last_result and runner._last_result.ok:
            return True, "opencode responded successfully."
        diag = runner.last_diagnostic() if runner._last_result else "(no result)"
        return False, f"opencode returned an error.\n{diag}"

    try:
        return run_with_timeout(_inner, seconds=timeout)
    except TimeoutError:
        return False, "opencode test timed out."
    except Exception as exc:
        return False, f"opencode test failed: {exc}"
    finally:
        try:
            runner.stop()
        except Exception:
            pass


def test_supervisor_connectivity(
    api_key: str,
    model: str,
    base_url: str | None = None,
    timeout: float = 25.0,
) -> tuple[bool, str]:
    if not api_key:
        return False, "API key is not set."

    client = OpenAI(api_key=api_key, base_url=base_url or None, timeout=timeout)

    def _inner():
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "hi"}],
        )
        text = normalize_model_response(
            resp.choices[0].message.content,
            "supervisor connectivity test response",
        )
        if text.strip():
            return True, f"Supervisor responded: {text.strip()[:120]}"
        return False, "Supervisor returned an empty response."

    return run_test_with_timeout(_inner, "Supervisor test")
