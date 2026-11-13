"""Checks for the portable stack guardian's decision plan."""

from __future__ import annotations

import os
import sys

from services.runtime import guardian
from services.runtime.guardian import GuardianState, Snapshot, plan

LOCK_MAX = 30.0
STALL = 300.0
BACKOFF = 1800.0


def snap(**overrides) -> Snapshot:
    base = {
        "app_port_up": True,
        "app_http_ok": True,
        "app_process_alive": True,
        "app_lock_age": 2.0,
        "proxy_ok": True,
        "worker_pid": 1234,
        "worker_alive": True,
        "worker_token": 1.0,
        "worker_age": 1000.0,
        "worker_lock_age": 1.0,
        "pending": 0,
        "running": 0,
        "max_parallel": 4,
    }
    base.update(overrides)
    return Snapshot(**base)


def decide(snapshot, state=None, now=1000.0, queue_due=False):
    return plan(
        snapshot,
        state or GuardianState(),
        now,
        queue_due=queue_due,
        lock_max_age=LOCK_MAX,
        stall_seconds=STALL,
        backoff_seconds=BACKOFF,
    )


def kinds(actions):
    return [action.kind for action in actions]


def test_healthy_stack_has_no_actions():
    assert kinds(decide(snap())) == []


def test_app_down_starts_stack():
    actions = decide(
        snap(
            app_port_up=False,
            app_http_ok=False,
            app_process_alive=False,
            app_lock_age=None,
        ),
    )
    assert kinds(actions) == ["start_stack"]


def test_app_booting_is_left_alone():
    actions = decide(
        snap(
            app_port_up=False,
            app_http_ok=False,
            app_process_alive=True,
            app_lock_age=3.0,
        ),
    )
    assert kinds(actions) == ["note"]


def test_wedged_app_restarts():
    actions = decide(
        snap(
            app_port_up=False,
            app_http_ok=False,
            app_process_alive=True,
            app_lock_age=120.0,
        ),
    )
    assert kinds(actions) == ["restart_app"]


def test_app_http_failure_needs_two_strikes():
    state = GuardianState()
    assert kinds(decide(snap(app_http_ok=False), state)) == []
    assert kinds(decide(snap(app_http_ok=False), state, now=1020.0)) == ["restart_app"]


def test_healthy_probe_resets_strikes():
    state = GuardianState(app_http_failures=1)
    decide(snap(), state)
    assert state.app_http_failures == 0


def test_proxy_down_restarts_proxy():
    assert kinds(decide(snap(proxy_ok=False))) == ["restart_proxy"]


def test_worker_dead_starts_worker():
    actions = decide(snap(worker_pid=None, worker_alive=False))
    assert kinds(actions) == ["start_worker"]


def test_stale_worker_with_running_jobs_is_never_killed():
    actions = decide(snap(worker_lock_age=120.0, running=1))
    assert kinds(actions) == ["note"]
    assert "not touching" in actions[0].detail


def test_young_stale_worker_is_left_to_finish_starting():
    assert kinds(decide(snap(worker_lock_age=120.0, worker_age=60.0))) == ["note"]


def test_wedged_idle_worker_is_restarted_with_identity():
    actions = decide(snap(worker_lock_age=120.0, worker_age=900.0))
    assert kinds(actions) == ["restart_worker"]
    assert actions[0].pid == 1234
    assert actions[0].token == 1.0


def test_queue_stall_idle_repairs_after_stall_window():
    state = GuardianState()
    decide(snap(pending=3, running=0), state, now=1000.0, queue_due=True)
    assert kinds(decide(snap(pending=3, running=0), state, now=1000.0 + STALL - 1, queue_due=True)) == []
    actions = decide(snap(pending=3, running=0), state, now=1000.0 + STALL + 1, queue_due=True)
    assert kinds(actions) == ["restart_worker"]


def test_queue_stall_with_running_jobs_never_kills():
    state = GuardianState()
    decide(snap(pending=3, running=1), state, now=1000.0, queue_due=True)
    actions = decide(snap(pending=3, running=1), state, now=1000.0 + STALL + 1, queue_due=True)
    assert kinds(actions) == ["note"]


def test_queue_change_resets_the_stall_clock():
    state = GuardianState()
    decide(snap(pending=3, running=0), state, now=1000.0, queue_due=True)
    decide(snap(pending=2, running=0), state, now=1200.0, queue_due=True)
    actions = decide(snap(pending=2, running=0), state, now=1200.0 + STALL + 1, queue_due=True)
    assert kinds(actions) == ["restart_worker"]


def test_queue_backoff_suppresses_repeat_repairs():
    state = GuardianState()
    decide(snap(pending=3, running=0), state, now=1000.0, queue_due=True)
    first = decide(snap(pending=3, running=0), state, now=1000.0 + STALL + 1, queue_due=True)
    assert kinds(first) == ["restart_worker"]
    second = decide(snap(pending=3, running=0), state, now=1000.0 + STALL + 60, queue_due=True)
    assert kinds(second) == ["note"]


def test_queue_stall_ignores_a_dead_worker():
    state = GuardianState()
    dead = snap(
        pending=3, running=0, worker_pid=None, worker_alive=False, worker_lock_age=900.0,
    )
    assert kinds(decide(dead, state, now=1000.0, queue_due=True)) == ["start_worker"]
    assert kinds(decide(dead, state, now=1000.0 + STALL + 1, queue_due=True)) == ["start_worker"]


def test_pid_probes():
    assert guardian.pid_alive(os.getpid())
    assert not guardian.pid_alive(2**31 - 1)


def test_job_counts_reads_recent_records_only(tmp_path, monkeypatch):
    monkeypatch.setattr(guardian, "JOB_STORE", tmp_path)
    (tmp_path / "a.json").write_text('{"state": "PENDING"}', encoding="utf-8")
    (tmp_path / "b.json").write_text('{"state": "RUNNING"}', encoding="utf-8")
    (tmp_path / "worker_settings.json").write_text(
        '{"max_parallel_jobs": 2}', encoding="utf-8",
    )
    assert guardian.job_counts() == (1, 1, 2)


def test_wait_for_worker_exit_clears_the_still_fresh_lock(tmp_path, monkeypatch):
    """A stopped worker leaves a fresh lock; the launcher would skip the start.

    ``Test-JobWorkerAlive`` judges liveness from ``worker.lock`` mtime (15 s),
    not from the process. Waiting for the process to disappear and then
    dropping the lock is what lets the launcher start a fresh daemon at once
    instead of leaving the queue unclaimed for ~40 s.
    """
    monkeypatch.setattr(guardian, "JOB_STORE", tmp_path)
    lock = tmp_path / "worker.lock"
    lock.write_text('{"pid": 1234}', encoding="utf-8")

    monkeypatch.setattr(guardian, "find_worker_pids", list)
    assert guardian.wait_for_worker_exit(timeout=2.0) is True
    assert not lock.exists(), "the stale lock was left behind"


def test_wait_for_worker_exit_reports_a_worker_that_never_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(guardian, "JOB_STORE", tmp_path)
    lock = tmp_path / "worker.lock"
    lock.write_text('{"pid": 1234}', encoding="utf-8")

    monkeypatch.setattr(guardian, "find_worker_pids", lambda: [1234])
    assert guardian.wait_for_worker_exit(timeout=0.2) is False
    assert lock.exists(), "the lock of a live worker must not be removed"


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
