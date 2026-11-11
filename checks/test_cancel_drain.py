"""Cancel must free the worker slot even when a detached process holds the pipe.

The failure this pins down (2026-11-11): two runs were CANCELLED, but one job
thread stayed in ``consume_process_stream`` waiting for stdout EOF. A study
script the agent had started with ``Start-Process`` had inherited the pipe
write handle; killing the codex tree did not close it. The thread kept the
worker's active count at 1, so the code-freshness gate held every new job -
the harvest stayed PENDING with free slots.

Contracts under test:
- a runner's cancel event exits the stream consumer within ~0.5 s, without
  waiting for the pipe to close;
- a cancelled turn never launches a backup model retry;
- cancel-before-launch never spawns a process;
- tree-kill signals only the direct child's current descendants, never an
  unrelated live process, and never an already-exited PID.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import psutil

from supervisor.runners import base_runner, process_common, stream_driver
from supervisor.runners.base_runner import BaseRunner
from supervisor.runners.process_common import (
    ConfigErrorPolicy,
    EngineAttempt,
    run_prompt_common,
)
from supervisor.runners.process_utils import kill_process_tree
from supervisor.runners.stream_driver import StreamOutcome, consume_process_stream


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _sleeper():
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# --------------------------------------------------------------- cancel -- #


class _HangingStdout:
    """Iterator that never yields, like a pipe held open by a grandchild."""

    def __init__(self) -> None:
        self.release = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        self.release.wait(timeout=30)
        raise StopIteration


class _HangingProc:
    def __init__(self) -> None:
        self.stdout = _HangingStdout()
        self.stderr = None
        self.returncode = None

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.returncode = -1


def test_cancel_event_unblocks_a_pipe_held_open(monkeypatch):
    proc = _HangingProc()
    killed = []
    cancel = threading.Event()
    monkeypatch.setattr(
        stream_driver, "kill_process_tree", lambda target: killed.append(target),
    )
    outcome_box = {}

    def run():
        gen = consume_process_stream(
            proc,
            classify_line=lambda line: None,
            timeout=60,
            cancel_event=cancel,
        )
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            outcome_box["outcome"] = stop.value

    thread = threading.Thread(target=run, daemon=True)
    started = time.monotonic()
    thread.start()
    time.sleep(0.2)
    cancel.set()
    thread.join(timeout=3)
    elapsed = time.monotonic() - started
    try:
        assert not thread.is_alive(), "cancel waited for pipe EOF"
        assert elapsed < 2.5
        assert outcome_box["outcome"].cancelled is True
        assert killed == [proc]
    finally:
        proc.stdout.release.set()


def test_cancel_returns_with_real_grandchild_holding_pipe(tmp_path):
    """The real failure shape: a running grandchild inherits the stdout pipe."""
    pid_file = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)'])\n"
        f"open(r'{pid_file}', 'w').write(str(child.pid))\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    grandchild_pid = None
    cancel = threading.Event()
    outcome_box = {}

    def run():
        gen = consume_process_stream(
            proc,
            classify_line=lambda line: None,
            timeout=60,
            cancel_event=cancel,
        )
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            outcome_box["outcome"] = stop.value

    thread = threading.Thread(target=run, daemon=True)
    started = time.monotonic()
    thread.start()
    assert _wait_until(
        lambda: pid_file.exists() and bool(pid_file.read_text().strip()), 10,
    )
    grandchild_pid = int(pid_file.read_text().strip())
    cancel.set()
    thread.join(timeout=3)
    elapsed = time.monotonic() - started
    try:
        assert not thread.is_alive(), "cancel waited for a real inherited pipe"
        assert elapsed < 3.0
        assert outcome_box["outcome"].cancelled is True
    finally:
        kill_process_tree(proc)
        if grandchild_pid is not None and psutil.pid_exists(grandchild_pid):
            try:
                psutil.Process(grandchild_pid).kill()
            except Exception:  # noqa: BLE001, S110 - cleanup
                pass
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001, S110 - cleanup
            pass


def test_cancelled_before_launch_never_spawns(monkeypatch):
    cancel = threading.Event()
    cancel.set()
    runner = SimpleNamespace(cancel_event=cancel, _last_result=None)
    spawned = []

    def boom(*_args, **_kwargs):
        spawned.append(1)
        raise AssertionError("Popen must not run after cancel")

    monkeypatch.setattr(process_common.subprocess, "Popen", boom)
    events = list(
        run_prompt_common(
            runner,
            "prompt",
            engine="codex",
            find_exe_fn=lambda _exe: "codex",
            prepare_attempt=lambda *_a: EngineAttempt(cmd=["codex"]),
            classify_line=lambda _line: None,
            config_error=ConfigErrorPolicy(),
        ),
    )

    assert not spawned
    assert runner._last_result.cancelled is True
    assert any(e.get("msg", "").endswith("cancelled before launch.") for e in events)


def test_cancelled_stream_never_retries_with_backup(monkeypatch, tmp_path):
    cancel = threading.Event()
    runner = SimpleNamespace(
        cancel_event=cancel,
        opencode_executable="",
        opencode_model="primary",
        opencode_model_backup="backup",
        agent="",
        timeout=5,
        workspace=tmp_path,
        _last_result=None,
        _chars_exchanged=0,
    )

    class FakeProc:
        pid = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(
        process_common.subprocess, "Popen", lambda *_a, **_k: FakeProc(),
    )
    monkeypatch.setattr(process_common, "feed_stdin_prompt", lambda *_a: None)

    def fake_consume(_proc, **_kwargs):
        cancel.set()
        if False:  # pragma: no cover - makes this a generator
            yield
        return StreamOutcome(
            stdout="", stderr="", returncode=-1, timed_out=False,
            tokens_total=0, cancelled=True,
        )

    monkeypatch.setattr(process_common, "consume_process_stream", fake_consume)
    attempts = []

    def prepare(_exe, model_for_cmd, _use_shell):
        attempts.append(model_for_cmd)
        return EngineAttempt(cmd=["fake"])

    list(
        run_prompt_common(
            runner,
            "prompt",
            engine="codex",
            find_exe_fn=lambda _exe: "codex",
            prepare_attempt=prepare,
            classify_line=lambda _line: None,
            config_error=ConfigErrorPolicy(),
        ),
    )

    assert attempts == ["primary"]  # no backup retry after a STOP
    assert runner._last_result.cancelled is True


def test_stop_sets_cancel_event_and_kills_only_its_process(monkeypatch, tmp_path):
    killed = []
    monkeypatch.setattr(
        base_runner, "kill_process_tree", lambda proc: killed.append(proc),
    )

    class Runner(BaseRunner):
        _engine = "test"

    runner = Runner(workspace=tmp_path, timeout=5)
    proc = object()
    runner._process = proc
    runner.stop()

    assert runner.cancel_event.is_set()
    assert runner.is_alive is False
    assert killed == [proc]


def test_context_restart_clears_a_cleanup_stop(tmp_path):
    """Compaction stops the old process then starts a new session."""
    class Runner(BaseRunner):
        _engine = "test"

    runner = Runner(workspace=tmp_path, timeout=5)
    runner.stop()
    assert runner.cancel_event.is_set()
    runner.reset_session()
    assert not runner.cancel_event.is_set()


def test_start_and_send_after_stop_are_noops(tmp_path):
    class Runner(BaseRunner):
        _engine = "test"

    runner = Runner(workspace=tmp_path, timeout=5)
    runner.stop()

    start_events = list(runner.start("prompt"))
    send_events = list(runner.send("prompt"))

    assert runner.is_alive is False
    assert start_events and "cancelled" in start_events[0]["msg"]
    assert send_events and "cancelled" in send_events[0]["msg"]


# ------------------------------------------------------------- precision -- #


def test_kill_process_tree_never_signals_an_exited_pid():
    unrelated = _sleeper()
    try:
        assert unrelated.poll() is None
        fake = SimpleNamespace(pid=unrelated.pid, poll=lambda: 0)
        kill_process_tree(fake)
        time.sleep(0.2)
        assert unrelated.poll() is None
    finally:
        kill_process_tree(unrelated)
        try:
            unrelated.wait(timeout=10)
        except Exception:  # noqa: BLE001, S110 - cleanup
            pass


def test_kill_process_tree_touches_only_its_own_tree(tmp_path):
    unrelated = _sleeper()
    pid_file = tmp_path / "child.pid"
    code = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)'])\n"
        f"open(r'{pid_file}', 'w').write(str(child.pid))\n"
        "time.sleep(30)\n"
    )
    target = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child_pid = None
    child_created = None
    try:
        assert _wait_until(
            lambda: pid_file.exists() and bool(pid_file.read_text().strip()), 10,
        )
        child_pid = int(pid_file.read_text().strip())
        child_created = psutil.Process(child_pid).create_time()
        assert target.poll() is None

        def child_gone() -> bool:
            try:
                return (
                    abs(psutil.Process(child_pid).create_time() - child_created)
                    > 0.01
                )
            except psutil.NoSuchProcess:
                return True

        kill_process_tree(target)
        assert _wait_until(lambda: target.poll() is not None, 10)
        assert _wait_until(child_gone, 10)
        assert unrelated.poll() is None
    finally:
        kill_process_tree(target)
        if child_pid is not None and psutil.pid_exists(child_pid):
            try:
                psutil.Process(child_pid).kill()
            except Exception:  # noqa: BLE001, S110 - cleanup
                pass
        kill_process_tree(unrelated)
        for proc in (target, unrelated):
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001, S110 - cleanup
                pass
