from __future__ import annotations

import os
import threading
from pathlib import Path

from openai import OpenAI

from supervisor.runners.codex_runner import CodexRunner, find_codex
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


def test_codex_connectivity(
    codex_executable: str,
    codex_model: str | None,
    codex_model_backup: str | None,
    timeout: int = 30,
    base_url: str = "",
    api_key: str = "",
) -> tuple[bool, str]:
    workspace = (
        Path(os.environ.get("TEMP", os.environ.get("TMPDIR", "/tmp")))
        / "codex_test_dummy"
    )
    workspace.mkdir(exist_ok=True)
    try:
        exe = find_codex(codex_executable or "")
    except FileNotFoundError as exc:
        return False, str(exc)

    runner = CodexRunner(
        workspace=workspace,
        opencode_model=codex_model,
        opencode_executable=exe,
        opencode_model_backup=codex_model_backup,
        timeout=timeout,
        codex_base_url=base_url,
        codex_api_key=api_key,
    )

    def _inner():
        runner._alive = True
        # codex needs no session-list capture lock (it uses resume/--last),
        # so the probe is a single self-contained `codex exec "hi"` call.
        for _ in runner._run_prompt("hi"):
            pass
        _output, timed_out = runner.read_output(timeout=25)
        if timed_out:
            return False, "codex timed out reading output."
        if runner._last_result and runner._last_result.ok:
            return True, "codex responded successfully."
        diag = runner.last_diagnostic() if runner._last_result else "(no result)"
        return False, f"codex returned an error.\n{diag}"

    try:
        return run_with_timeout(_inner, seconds=timeout)
    except TimeoutError:
        return False, "codex test timed out."
    except Exception as exc:
        return False, f"codex test failed: {exc}"
    finally:
        try:
            runner.stop()
        except Exception:
            pass


def test_agent_connectivity(
    engine: str,
    executable: str,
    model: str | None,
    model_backup: str | None,
    timeout: int = 30,
    base_url: str = "",
    api_key: str = "",
) -> tuple[bool, str]:
    """Dispatch the execution-agent connectivity probe by engine.

    ``base_url`` / ``api_key`` configure codex's external OpenAI-compatible
    (Responses API) endpoint; they are ignored for opencode, which manages its
    own provider config.
    """
    if (engine or "opencode").strip().lower() == "codex":
        return test_codex_connectivity(
            executable, model, model_backup, timeout, base_url, api_key
        )
    return test_opencode_connectivity(executable, model, model_backup, timeout)


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
