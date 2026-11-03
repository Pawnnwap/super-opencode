"""Silent-death forensics must stay wired in.

Every UI/worker death (2026-11-01..11-03) left zero evidence - empty stderr,
no event log, no dump - so "crash" vs "hard kill" vs "serving death" could
not be told apart and each fix went in blind. These tests pin the capture
layer end to end:

- ``death_capture`` records process identity and unhandled exceptions;
- the launchers append an exit-code sentinel ONLY on clean exits
  (hard kills leave the file absent = the kill fingerprint);
- the UI watchdog touches ``.job_store/ui.lock`` (death clock) and logs
  client connect/disconnect/delete;
- the browser beacon forwards socket/tab events into the server log;
- the self-heal watcher is actually ARMED (it was defined but never
  called, so the 2026-11-03 death stayed dead).
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

from services.runtime import death_capture

_REPO = Path(__file__).resolve().parents[1]


def _read(*parts: str) -> str:
    return _REPO.joinpath(*parts).read_text(encoding="utf-8")


def test_death_capture_records_identity_and_exceptions(tmp_path):
    log = tmp_path / "proc.fatal.log"
    death_capture.install(str(log))

    lines = log.read_text(encoding="utf-8").strip().splitlines()
    identity = json.loads(lines[-1])
    assert identity["chain"][0]["pid"] == os.getpid()
    assert identity["chain"][0]["name"]  # our own process identified

    # Unhandled-exception hooks write a traceback into the same file.
    try:
        raise RuntimeError("boom-for-death-capture")
    except RuntimeError:
        info = sys.exc_info()
    threading.excepthook(SimpleNamespace(
        exc_type=info[0], exc_value=info[1], exc_traceback=info[2],
        thread=threading.current_thread(),
    ))
    text = log.read_text(encoding="utf-8")
    assert "boom-for-death-capture" in text
    assert "unhandled exception in thread" in text


def test_launcher_sentinels_use_exit_time_values():
    src = _read("start_suprun.ps1")
    # Delayed expansion: %VAR% expands at parse (launch) time and would
    # record the wrong errorlevel/date for the sentinel.
    assert "!ERRORLEVEL!" in src and "%ERRORLEVEL%" not in src
    assert src.count("exitcode=!ERRORLEVEL!") == 3  # proxy, app, worker
    for name in ("supervisor_app.exitcode", "job_worker.exitcode",
                 "codex_compat_proxy.exitcode"):
        assert name in src


def test_self_heal_watcher_is_armed():
    src = _read("start_suprun.ps1")
    # The function existed since e111c14 but was never invoked - the
    # 2026-11-03 UI death then stayed dead until manual restart.
    assert "function Start-UiWatcher" in src
    assert "Start-UiWatcher" in src.replace("function Start-UiWatcher", "")


def test_ui_arms_death_capture_and_lifecycle_logs():
    src = _read("app.py")
    assert 'death_capture.install("app.fatal.log")' in src
    assert "app.on_connect" in src
    assert "app.on_disconnect" in src
    assert "app.on_delete" in src


def test_ui_watchdog_ticks_the_liveness_clock():
    src = _read("app.py")
    assert 'Path(".job_store") / "ui.lock"' in src
    assert "_touch_ui_lock()" in src
    watchdog = src.split("def _ui_watchdog")[1]
    assert "_touch_ui_lock()" in watchdog.split("def ")[0]


def test_worker_arms_death_capture():
    src = _read("services", "jobs", "worker_service.py")
    assert 'death_capture.install("job_worker.fatal.log")' in src


def test_browser_beacon_reports_socket_and_tab_events():
    src = _read("services", "webui", "layout.py")
    assert "[beacon]" in src
    for needle in ("socket disconnect", "connect_error", "reconnect_attempt",
                   "freeze", "visibilitychange", "window.onerror"):
        assert needle in src
    assert "BEACON_JS" in src.split("def apply_theme")[1]
