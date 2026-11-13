"""Live Run page's how-now sweep: one button, process-wide state, final report.

The sweep runs in a process-wide daemon thread with ALL state in a module
singleton — never on the page instance — so switching or closing the page
loses nothing: any later page load re-attaches to a running sweep, and the
finished report is cached (in memory and on disk) with its timestamp.

Display contract:
- while running: elapsed/step status plus the newest key steps (tail strip);
- once finished: ONLY the final report (or error), stamped with its time.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from nicegui import ui

from services.jobs.hownow import run_hownow_sweep
from services.webui.components.log_panel import TailStrip
from services.webui.jobs import get_job_manager
from services.webui.state import app_state

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
# Plain root-level cache, like codex_capability_cache.json. NOT inside
# .job_store: the store globs *.json there as job records.
_CACHE_PATH = _PROJECT_ROOT / "hownow_cache.json"


class _SweepState:
    """Process-wide sweep state; every page is only a view onto it."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.busy = False
        self.steps: list[dict] = []
        self.report = ""
        self.error = ""
        self.started_at = 0.0
        self.finished_at = 0.0


_state = _SweepState()


def _load_cache() -> None:
    try:
        data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    with _state.lock:
        _state.report = str(data.get("report") or "")
        _state.error = str(data.get("error") or "")
        _state.started_at = float(data.get("started_at") or 0)
        _state.finished_at = float(data.get("finished_at") or 0)


def _save_cache() -> None:
    with _state.lock:
        data = {
            "report": _state.report,
            "error": _state.error,
            "started_at": _state.started_at,
            "finished_at": _state.finished_at,
        }
    try:
        _CACHE_PATH.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("cannot write hownow cache: %s", exc)


_load_cache()


def _on_event(event: dict) -> None:
    with _state.lock:
        _state.steps.append({
            "ts": time.time(),
            "level": event.get("level") or "info",
            "msg": event.get("msg") or "",
        })


def _run_sweep() -> None:
    # No UI element is ever touched from this thread: pages come and go
    # while the sweep runs, and none of them own its outcome.
    report = ""
    error = ""
    try:
        app_state.save()
        app_state.apply_api_config()
        report = run_hownow_sweep(
            app_state.as_mapping(),
            get_job_manager().store,
            on_event=_on_event,
        )
    except Exception as exc:
        logger.exception("hownow sweep failed")
        error = str(exc)
    finally:
        with _state.lock:
            _state.report = report
            _state.error = error
            _state.finished_at = time.time()
        # Persist BEFORE publishing "done": a restart (or a page tick) right
        # after busy clears must already find the result on disk.
        _save_cache()
        with _state.lock:
            _state.busy = False


def start_sweep() -> bool:
    """Start the one process-wide sweep; False when one is already running."""
    with _state.lock:
        if _state.busy:
            return False
        _state.busy = True
        _state.steps = []
        _state.report = ""
        _state.error = ""
        _state.started_at = time.time()
        _state.finished_at = 0.0
    threading.Thread(target=_run_sweep, daemon=True, name="hownow-sweep").start()
    return True


def _fmt_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else ""


class HowNowPanel:
    """A view onto the process-wide sweep: button, progress, final report.

    Every instance polls the same singleton, so any number of pages/tabs
    re-attach to a running sweep and all show the cached result afterwards.
    """

    def __init__(self):
        self._mode = ""
        self._signature = None
        self._status: ui.label | None = None
        self._tail: TailStrip | None = None

        ui.button("How Now", on_click=self._start).props("color=primary").tooltip(
            "Wakes a fresh agent session to sweep every live run session "
            "(runs and batch sub-runs) and report which ones need you. "
            "The sweep keeps running if you leave this page.",
        )
        self._results = ui.column().classes("w-full gap-2")
        self._timer = ui.timer(1.0, self._tick)
        # Immediate tick: re-attach to a running sweep or show the cached
        # result right at page load, without waiting a second.
        self._tick()

    def _start(self) -> None:
        if not start_sweep():
            ui.notify("A how-now sweep is already running.", type="warning")
            return
        # Re-arm the poller: _tick() stops it once a sweep settles.
        self._timer.active = True
        self._tick()

    def _tick(self) -> None:
        with _state.lock:
            busy = _state.busy
            steps = list(_state.steps) if busy else []
            report = _state.report
            error = _state.error
            started_at = _state.started_at
            finished_at = _state.finished_at

        if busy:
            if self._mode != "running":
                self._mode = "running"
                self._signature = None
                self._results.clear()
                with self._results:
                    self._status = ui.label("").classes("text-xs text-[#8b949e]")
                    self._tail = TailStrip(keep=6)
            elapsed = int(time.time() - started_at)
            self._status.set_text(
                f"Sweeping live sessions… {elapsed}s, {len(steps)} step(s)",
            )
            self._tail.update(steps)
            return

        # Finished (or cached from an earlier run): ONLY the final message,
        # stamped with its time — the progress tail is gone.
        signature = (finished_at, report, error)
        if self._mode == "done" and signature == self._signature:
            # Nothing left to watch: stop waking the event loop every second.
            # A new sweep re-arms the timer in _start().
            self._timer.active = False
            return
        self._mode = "done"
        self._signature = signature
        self._status = None
        self._tail = None
        self._results.clear()
        if not report and not error:
            self._timer.active = False
            return
        with self._results:
            took = (
                int(finished_at - started_at)
                if started_at and finished_at else 0
            )
            ui.label(
                f"How-now sweep · {_fmt_time(finished_at)}"
                + (f" · {took}s" if took else ""),
            ).classes("text-xs text-[#8b949e] mono")
            if error:
                ui.label(f"Sweep failed: {error}").classes(
                    "text-sm text-[#f85149]",
                )
            else:
                ui.markdown(report)