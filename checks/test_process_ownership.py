"""The process-ownership hard rule: a kill may only reach this run's own tree.

Background: cleanup used to be name/keyword based (``taskkill /IM``,
``Stop-Process -Name``, ``pkill -f``), which can reach any process on the
machine — including another run's MATLAB or a user's own session. The rule
now has three legs, each covered here:

1. the ledger (``(pid, create_time)`` written at spawn) proves ownership;
2. the job object bounds the tree structurally (members only);
3. the shell gate denies every kill shape that cannot be proven owned.

MATLAB is the priority case: ``bin\\matlab.exe`` forks ``MATLAB.exe``, so the
pair can only be cleaned up as a group — and only when this run started it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from supervisor.runtime.process_control import (
    KillRefused,
    scope_for_run,
    spawn_tracked,
    terminate_pid,
    terminate_run,
    terminate_tree,
    verify_owned,
)
from supervisor.runtime.process_registry import ProcessRegistry, ProcRecord
from supervisor.runtime.processes import (
    MatlabProcess,
    PythonProcess,
    RustProcess,
    find_matlab,
)
from supervisor.security.access_policy import build_policy
from supervisor.security.guard_cli import _ownership_violations
from supervisor.security.shell_scan import analyze, kill_pids

WINDOWS = os.name == "nt"


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path, monkeypatch):
    """Every test gets its own ledger directory (never the real job store)."""
    monkeypatch.setenv("SOP_JOB_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.delenv("SOP_RUN_ID", raising=False)
    yield


def _sleeper(seconds: int = 30) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_gone(proc: subprocess.Popen, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.05)
    return False


# --------------------------------------------------------------------------- #
# Ledger + ownership proofs
# --------------------------------------------------------------------------- #


def test_foreign_pid_kill_is_refused(tmp_path):
    """A process this run never started is never signalled."""
    foreign = _sleeper()
    try:
        with pytest.raises(KillRefused):
            terminate_pid("run_a", foreign.pid, reason="test")
        time.sleep(0.2)
        assert foreign.poll() is None
        refusals = ProcessRegistry("run_a").refusal_path
        assert refusals.exists() and "not owned" in refusals.read_text(encoding="utf-8")
    finally:
        foreign.kill()
        foreign.wait(timeout=10)


def test_registered_pid_kill_succeeds(tmp_path):
    spawned = spawn_tracked(
        run_id="run_b",
        role="python",
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        assert verify_owned("run_b", spawned.pid)
        assert terminate_pid("run_b", spawned.pid, reason="test")
        assert _wait_gone(spawned.popen)
    finally:
        if spawned.popen.poll() is None:
            spawned.popen.kill()
            spawned.popen.wait(timeout=10)


def test_recycled_pid_is_never_signalled(tmp_path):
    """A ledger record whose create time no longer matches is dead evidence.

    The realistic shape: the run's child exited, its pid was reused by an
    unrelated process, and the ledger still holds the old record. The reused
    process is not in the run's job, so nothing proves ownership of it.
    """
    foreign = _sleeper()
    try:
        registry = ProcessRegistry("run_c")
        registry.register(
            ProcRecord(
                run_id="run_c",
                pid=foreign.pid,
                create_time=time.time() - 500.0,
                role="python",
                state="live",
            ),
        )
        assert not verify_owned("run_c", foreign.pid)
        with pytest.raises(KillRefused):
            terminate_pid("run_c", foreign.pid, reason="recycled")
        assert foreign.poll() is None
    finally:
        foreign.kill()
        foreign.wait(timeout=10)


def test_job_member_is_adopted_and_killable(tmp_path):
    """A descendant the engine spawned is provable through the job scope."""
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(30)\n"
    )
    spawned = spawn_tracked(
        run_id="run_d",
        role="engine",
        argv=[sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        line = spawned.popen.stdout.readline().strip()
        grandchild = int(line)
        assert verify_owned("run_d", grandchild)
        assert terminate_pid("run_d", grandchild, reason="adopted member")
        assert not verify_owned("run_other", grandchild)
    finally:
        terminate_run("run_d", reason="cleanup")
        if spawned.popen.poll() is None:
            spawned.popen.kill()
            spawned.popen.wait(timeout=10)


def test_job_terminate_leaves_sibling_alive(tmp_path):
    """Two scopes: terminating one must not touch the other's tree."""
    if not WINDOWS:
        pytest.skip("job objects are Windows-only")
    first = spawn_tracked(
        run_id="run_e1",
        role="python",
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
    )
    second = spawn_tracked(
        run_id="run_e2",
        role="python",
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        assert terminate_tree("run_e1", first.pid, reason="scope kill")
        assert _wait_gone(first.popen)
        time.sleep(0.3)
        assert second.popen.poll() is None
    finally:
        terminate_run("run_e1", reason="cleanup")
        terminate_run("run_e2", reason="cleanup")
        for proc in (first.popen, second.popen):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


def test_sibling_of_the_same_run_survives_a_per_child_kill(tmp_path):
    """Terminating one child must not take the run's other processes down.

    Every child of a run shares one job object, so terminating the job would
    kill the engine and every sibling whenever a single helper timed out (a
    judge ``run_tests`` pytest call). ``terminate_tree`` is the per-child
    operation and must stay inside the root's own subtree; the job is only
    the run-level backstop (``terminate_run``).
    """
    if not WINDOWS:
        pytest.skip("job objects are Windows-only")
    engine = spawn_tracked(
        run_id="run_sib",
        role="engine",
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
    )
    helper = spawn_tracked(
        run_id="run_sib",
        role="python",
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        assert terminate_tree("run_sib", helper.pid, reason="helper timeout")
        assert _wait_gone(helper.popen)
        time.sleep(0.3)
        assert engine.popen.poll() is None, (
            "a per-child kill took the run's engine down with it"
        )
    finally:
        terminate_run("run_sib", reason="cleanup")
        for proc in (engine.popen, helper.popen):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


def test_reconcile_labels_stale_without_killing(tmp_path):
    """Crash recovery classifies; it never kills a leftover."""
    spawned = spawn_tracked(
        run_id="run_f",
        role="python",
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        registry = ProcessRegistry("run_f")
        registry.register(
            ProcRecord(
                run_id="run_f",
                pid=spawned.pid + 100000,
                create_time=1.0,
                role="ghost",
                state="live",
            ),
        )
        live, stale = registry.reconcile()
        assert [item.pid for item in live] == [spawned.pid]
        assert [item.role for item in stale] == ["ghost"]
        assert spawned.popen.poll() is None
    finally:
        terminate_run("run_f", reason="cleanup")
        if spawned.popen.poll() is None:
            spawned.popen.kill()
            spawned.popen.wait(timeout=10)


# --------------------------------------------------------------------------- #
# Language wrappers
# --------------------------------------------------------------------------- #


def test_python_child_is_tracked_and_timed_out(tmp_path):
    child = PythonProcess(
        run_id="run_g",
        code="import time; time.sleep(30)",
        cwd=tmp_path,
        timeout=1.5,
    )
    result = child.run()
    assert result.timed_out
    records = ProcessRegistry("run_g").records()
    assert records and records[0].role == "python"
    assert records[0].state == "exited"


def test_cargo_child_is_tracked(tmp_path):
    try:
        child = RustProcess(run_id="run_h", subcommand="--version", cwd=tmp_path, timeout=120)
        result = child.run()
    except FileNotFoundError:
        pytest.skip("cargo is not installed")
    assert result.returncode == 0
    records = ProcessRegistry("run_h").records()
    assert records and records[0].role == "rust"


def test_matlab_child_is_grouped(tmp_path):
    """MATLAB runs through the owned wrapper and lands in the ledger."""
    try:
        exe = find_matlab()
    except FileNotFoundError:
        pytest.skip("MATLAB is not installed")
    child = MatlabProcess(
        run_id="run_i",
        script="disp('ownership-check')",
        cwd=tmp_path,
        matlab_exe=exe,
        timeout=300,
    )
    result = child.run()
    assert result.returncode == 0, result.summary()
    records = ProcessRegistry("run_i").records()
    assert records and records[0].role == "matlab"
    assert records[0].job_id, "the MATLAB launcher must be inside the run's job"


# --------------------------------------------------------------------------- #
# Shell gate: static shapes
# --------------------------------------------------------------------------- #


def _policy(tmp_path):
    return build_policy(tmp_path, [])


@pytest.mark.parametrize(
    "command",
    [
        "taskkill /IM matlab.exe /F",
        "taskkill /F /T /PID 1234",
        "Stop-Process -Name MATLAB -Force",
        "pkill -f matlab",
        "killall python",
        "taskkill /PID $p",
        "taskkill /PID *",
    ],
)
def test_shell_gate_denies_unprovable_kills(tmp_path, command):
    assert analyze(_policy(tmp_path), command, cwd=tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "taskkill /PID 4321",
        "taskkill /F /PID 4321",
        "Stop-Process -Id 4321 -Force",
        "kill -9 4321",
    ],
)
def test_shell_gate_allows_literal_pid_kills(tmp_path, command):
    assert analyze(_policy(tmp_path), command, cwd=tmp_path) == []
    assert kill_pids(command) == [4321]


def test_direct_matlab_launch_is_denied(tmp_path):
    policy = _policy(tmp_path)
    assert analyze(policy, "matlab -batch run_factor", cwd=tmp_path)
    assert analyze(policy, "matlab.exe -batch run_factor", cwd=tmp_path)
    assert (
        analyze(
            policy,
            "python -m supervisor.tools.run_child matlab --script run_factor",
            cwd=tmp_path,
        )
        == []
    )


# --------------------------------------------------------------------------- #
# Shell gate: dynamic ownership proof
# --------------------------------------------------------------------------- #


def test_gate_allows_only_owned_pids(tmp_path, monkeypatch):
    spawned = spawn_tracked(
        run_id="run_j",
        role="python",
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
    )
    foreign = _sleeper()
    try:
        monkeypatch.setenv("SOP_RUN_ID", "run_j")
        assert _ownership_violations(f"taskkill /PID {spawned.pid}") == []
        assert _ownership_violations(f"taskkill /PID {foreign.pid}")
        assert _ownership_violations("taskkill /IM python.exe")
        assert _ownership_violations(f"taskkill /T /PID {spawned.pid}")
    finally:
        terminate_run("run_j", reason="cleanup")
        foreign.kill()
        foreign.wait(timeout=10)


def test_gate_fails_closed_without_a_run(tmp_path, monkeypatch):
    monkeypatch.delenv("SOP_RUN_ID", raising=False)
    violations = _ownership_violations("taskkill /PID 4321")
    assert violations and "no ownership ledger" in violations[0].reason


@pytest.mark.parametrize(
    "command",
    [
        "cmd /c taskkill /PID 999999",
        'powershell -Command "Stop-Process -Id 999999"',
        'bash -c "kill 999999"',
        "env FOO=1 taskkill /PID 999999",
    ],
)
def test_wrapped_kill_is_still_ownership_checked(tmp_path, monkeypatch, command):
    """One shell layer must not hide a kill from the ownership gate.

    The static analyzer unwraps ``cmd /c``, ``powershell -Command`` and
    ``bash -c`` and allows the inner literal-PID kill. The dynamic gate used
    to scan only top-level segments, so the wrapped form produced no pid and
    no "kill present" signal — it was allowed outright, and any pid on the
    machine could be terminated.
    """
    monkeypatch.setenv("SOP_RUN_ID", "run_wrapped")
    assert kill_pids(command) == [999999]
    violations = _ownership_violations(command)
    assert violations, f"wrapped kill was not ownership-checked: {command}"
    assert "not owned by run run_wrapped" in violations[0].reason


def test_wrapped_kill_of_an_owned_pid_is_allowed(tmp_path, monkeypatch):
    """Unwrapping must not turn a legitimate owned kill into a refusal."""
    spawned = spawn_tracked(
        run_id="run_wrapped_ok",
        role="python",
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        monkeypatch.setenv("SOP_RUN_ID", "run_wrapped_ok")
        assert _ownership_violations(f"cmd /c taskkill /PID {spawned.pid}") == []
    finally:
        terminate_run("run_wrapped_ok", reason="cleanup")
        if spawned.popen.poll() is None:
            spawned.popen.kill()
            spawned.popen.wait(timeout=10)


def test_scope_for_run_is_reused(tmp_path):
    first = scope_for_run("run_k")
    second = scope_for_run("run_k")
    assert first is second


def test_posix_scope_is_usable_and_tracks_each_child(tmp_path, monkeypatch):
    """The POSIX scope must be a real containment, not an inert placeholder.

    ``JobScope.create`` used to return a handle-less scope whose ``active``
    was False, so ``spawn_tracked`` dropped it and ``assign`` never ran: the
    documented session/process-group containment did not exist at all.
    """
    from supervisor.runtime.job_object import JobScope

    monkeypatch.setattr("supervisor.runtime.job_object._WINDOWS", False)
    scope = JobScope.create()
    assert scope.active, "a POSIX scope must be usable right after creation"
    assert scope.assign(4321) is True
    assert scope.assign(8765) is True
    assert scope._pgids == {4321, 8765}
    scope.close()
    assert not scope.active
    assert scope.assign(1111) is False


def test_posix_spawn_starts_a_new_session(tmp_path, monkeypatch):
    """spawn_tracked must pass start_new_session so the pgid is real."""
    from supervisor.runtime import process_control

    captured: dict = {}
    real_popen = subprocess.Popen

    def spy(argv, **kwargs):
        captured.update(kwargs)
        return real_popen(argv, **kwargs)

    monkeypatch.setattr("supervisor.runtime.process_control._WINDOWS", False)
    monkeypatch.setattr(process_control.subprocess, "Popen", spy)
    spawned = spawn_tracked(
        run_id="run_posix",
        role="python",
        argv=[sys.executable, "-c", "import time; time.sleep(5)"],
    )
    try:
        assert captured.get("start_new_session") is True
    finally:
        terminate_run("run_posix", reason="cleanup")
        if spawned.popen.poll() is None:
            spawned.popen.kill()
            spawned.popen.wait(timeout=10)
