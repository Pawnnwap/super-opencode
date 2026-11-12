"""How-now panel: process-wide sweep state, final-only display, cached result.

The sweep used to live on the page instance: closing the tab killed its
timer, the finished report was never rendered anywhere, and a reopened page
showed nothing while the busy flag blocked a new sweep. These tests pin the
singleton contract: state survives any page lifecycle, the busy guard is
process-wide, and the result is cached to disk with timestamps.
"""
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.webui.components import hownow as hn


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Fresh singleton state + tmp cache + no real settings/store access."""
    monkeypatch.setattr(hn, "_CACHE_PATH", tmp_path / "hownow_cache.json")
    monkeypatch.setattr(hn, "app_state", SimpleNamespace(
        save=lambda: None, apply_api_config=lambda: None, as_mapping=dict,
    ))
    monkeypatch.setattr(
        hn, "get_job_manager", lambda: SimpleNamespace(store=None),
    )
    with hn._state.lock:
        hn._state.busy = False
        hn._state.steps = []
        hn._state.report = ""
        hn._state.error = ""
        hn._state.started_at = 0.0
        hn._state.finished_at = 0.0
    yield
    # Let any in-flight sweep thread settle so it cannot leak into the next
    # test (the thread is a daemon owned by the module, not by a page).
    deadline = time.time() + 6
    while hn._state.busy and time.time() < deadline:
        time.sleep(0.02)


def _wait_done(timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while hn._state.busy and time.time() < deadline:
        time.sleep(0.02)
    assert not hn._state.busy, "sweep thread did not finish"


def test_running_sweep_is_reattachable_and_not_page_bound(isolated, monkeypatch):
    gate = threading.Event()
    started = threading.Event()

    def fake_sweep(settings, store, on_event=None):
        started.set()
        on_event({"level": "info", "msg": "step one"})
        gate.wait(5)
        on_event({"level": "info", "msg": "step two"})
        return "| id | verdict |\n|---|---|\n| x | RUNNING |"

    monkeypatch.setattr(hn, "run_hownow_sweep", fake_sweep)
    assert hn.start_sweep() is True
    assert started.wait(5)
    # A second click from ANY page must not spawn a second sweep.
    assert hn.start_sweep() is False
    # "Page closed and reopened": the state is module-level, and steps keep
    # arriving with timestamps regardless of which views exist.
    with hn._state.lock:
        assert hn._state.busy
        assert [s["msg"] for s in hn._state.steps] == ["step one"]
        assert hn._state.steps[0]["ts"] > 0
    gate.set()
    _wait_done()
    assert "RUNNING" in hn._state.report
    assert hn._state.finished_at >= hn._state.started_at > 0
    with hn._state.lock:
        assert [s["msg"] for s in hn._state.steps] == ["step one", "step two"]


def test_result_is_cached_with_timestamps_and_reloaded(isolated, monkeypatch):
    monkeypatch.setattr(hn, "run_hownow_sweep", lambda *a, **k: "final report")
    assert hn.start_sweep()
    _wait_done()
    data = json.loads(hn._CACHE_PATH.read_text(encoding="utf-8"))
    assert data["report"] == "final report"
    assert data["finished_at"] > 0
    assert data["started_at"] > 0
    # Simulate a UI restart: wipe in-memory state, reload from disk.
    with hn._state.lock:
        hn._state.report = ""
        hn._state.finished_at = 0.0
    hn._load_cache()
    assert hn._state.report == "final report"
    assert hn._state.finished_at == data["finished_at"]


def test_failed_sweep_records_error_and_clears_busy(isolated, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(hn, "run_hownow_sweep", boom)
    assert hn.start_sweep()
    _wait_done()
    assert hn._state.error == "engine exploded"
    assert hn._state.report == ""
    assert hn._state.busy is False
    # A new sweep can start after a failure, and clears the old error.
    monkeypatch.setattr(hn, "run_hownow_sweep", lambda *a, **k: "ok")
    assert hn.start_sweep()
    _wait_done()
    assert hn._state.report == "ok"
    assert hn._state.error == ""


def test_panel_shows_only_final_report_when_done():
    src = Path(hn.__file__).read_text(encoding="utf-8")
    # The finished branch renders the report WITHOUT a tail strip; the
    # TailStrip constructor appears only in the running branch.
    done_branch = src.split("# Finished (or cached")[1]
    assert "TailStrip(" not in done_branch
    assert "ui.markdown(report)" in done_branch
    assert "_fmt_time(finished_at)" in done_branch
    assert src.count("TailStrip(keep=") == 1
    # Page load re-attaches immediately instead of waiting for the timer.
    panel_src = src.split("class HowNowPanel")[1]
    init_body = panel_src.split("def __init__")[1].split("def _start")[0]
    assert "self._tick()" in init_body