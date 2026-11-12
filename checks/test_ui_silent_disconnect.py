"""UI pollers must never read a transient store failure as "job finished".

Incident (2026-11-02): the UI process logged
``Cannot read job state for run_...: [Errno 13] Permission denied`` for a
live job. One failed read made ``LiveJobScreen.tick`` see an empty status
(``or {}`` -> state "UNKNOWN" -> not in ACTIVE_JOB_STATES) and permanently
disable its 2 s refresh - the screen froze ("UI silently disconnected")
while the job kept running in the worker daemon. The board has the twin
trap: a scan where the only active job is momentarily unreadable reads as
"idle" and pauses its 1.5 s timer forever. Contract under test:

- an unreadable (None) status keeps the live poller running, bounded by a
  consecutive-failure limit that stops with an honest notice, never silently;
- only a state that was actually READ with a non-active value stops polling;
- the board only pauses auto-refresh on a COMPLETE idle scan;
- the app grants the websocket a real reconnect grace period instead of
  NiceGUI's 3.0 s default (2 s ping cliff -> client deleted mid-blip).
"""

from __future__ import annotations

import asyncio
import importlib
import sys

import pytest

RUNNING = {"state": "RUNNING", "logs": []}
SUCCESS = {"state": "SUCCESS", "logs": []}


class StubWidget:
    def __init__(self):
        self.updates = 0

    def update(self, *args, **kwargs):
        self.updates += 1

    def set_content(self, *args, **kwargs):
        self.updates += 1


class StubTimer:
    def __init__(self):
        self.active = True


class StubManager:
    """Feeds canned get_job_status results; None means "read failed"."""

    def __init__(self, results):
        self.results = list(results)

    def get_job_status(self, job_id, max_logs=None):
        return self.results.pop(0) if self.results else None


def _make_screen(manager):
    import services.webui.components.live_screen as ls

    screen = ls.LiveJobScreen(
        manager,
        job_type="run",
        job_id="run_x",
        base_path="/run",
        running_message="running",
    )
    screen.progress = StubWidget()
    screen.tail = StubWidget()
    screen.log_panel = StubWidget()
    screen.state_pill = StubWidget()
    screen.timer = StubTimer()
    screen._render_report = lambda status: None
    return screen


@pytest.fixture()
def tick_env(monkeypatch):
    """Run LiveJobScreen.tick for real, stubbing only the UI surface."""
    import nicegui.run as nicegui_run

    import services.webui.components.live_screen as ls

    async def immediate_io_bound(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(nicegui_run, "io_bound", immediate_io_bound)
    notifications = []
    monkeypatch.setattr(ls.ui, "notify", lambda *a, **k: notifications.append(a))
    return notifications


def _tick(screen):
    asyncio.run(screen.tick())


def test_one_failed_read_keeps_the_live_poller_running(tick_env):
    screen = _make_screen(StubManager([None, RUNNING, RUNNING]))
    _tick(screen)
    assert screen.timer.active, (
        "a transient read failure stopped the 2 s refresh - the silent freeze"
    )
    _tick(screen)
    assert screen.timer.active
    assert not tick_env  # never claims a finish it did not read


def test_read_terminal_state_stops_the_live_poller(tick_env):
    screen = _make_screen(StubManager([SUCCESS]))
    _tick(screen)
    assert not screen.timer.active
    assert tick_env, "a finished job should notify once"


def test_live_poller_failure_limit_is_bounded_and_honest(tick_env):
    screen = _make_screen(StubManager([None] * 8))
    for _ in range(4):
        _tick(screen)
        assert screen.timer.active, "must tolerate several consecutive read failures"
    for _ in range(4):
        _tick(screen)
        if not screen.timer.active:
            break
    assert not screen.timer.active, "unbounded polling of an unreachable job"
    assert tick_env, "the stop must announce itself, never be silent"


def test_board_pauses_autorefresh_only_on_a_complete_idle_scan():
    from services.webui.components.board import may_pause_autorefresh

    assert may_pause_autorefresh(auto_refresh=True, active=0, scan_incomplete=False)
    assert not may_pause_autorefresh(True, 0, True), (
        "an incomplete scan may hide the very job being watched"
    )
    assert not may_pause_autorefresh(True, 1, False)
    assert not may_pause_autorefresh(False, 0, False)


def test_board_collect_reports_unreadable_jobs():
    from services.webui.components.board import JobBoard

    class FakeStore:
        def list_jobs(self):
            return ["run_ok", "run_ghost"]

    class FakeManager:
        store = FakeStore()

        def get_job_summary(self, job_id):
            return {"type": "run", "state": "RUNNING"} if job_id == "run_ok" else None

    board = JobBoard.__new__(JobBoard)
    board.job_manager = FakeManager()
    board.job_type = "run"
    jobs, incomplete = board._collect()
    assert [j["id"] for j in jobs] == ["run_ok"]
    assert incomplete == 1, "a listed-but-unreadable job must mark the scan incomplete"


def test_ui_run_grants_a_real_reconnect_grace_period(monkeypatch):
    """reconnect_timeout drives engine.io ping_interval/ping_timeout and the
    delete-after-disconnect window (nicegui.py _startup). The 3.0 s default
    turns any 2 s stall into a permanently deleted client."""
    captured = {}
    import nicegui.ui as ui_mod

    monkeypatch.setattr(ui_mod, "run", lambda **kwargs: captured.update(kwargs))
    sys.modules.pop("app", None)
    try:
        importlib.import_module("app")
    finally:
        sys.modules.pop("app", None)
    assert captured, "app.py must call ui.run (module did not import as expected)"
    assert captured.get("reconnect_timeout", 3.0) >= 30.0
