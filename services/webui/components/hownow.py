"""Live Run page's how-now sweep: one pure button, live key steps, final report.

Click → a FRESH agent session (the configured engine of choice) is woken with
the hownow text plus the record-keeper's live-session snapshot. Its key steps
stream into a tail strip while it works, and the finished Markdown report
renders in place once it is done. The functionality is deliberately just the
button — no options, no inputs.
"""

from __future__ import annotations

import logging
import threading
import time

from nicegui import ui

from services.jobs.hownow import run_hownow_sweep
from services.webui.components.log_panel import TailStrip
from services.webui.jobs import get_job_manager
from services.webui.state import app_state

logger = logging.getLogger(__name__)


class HowNowPanel:
    """The whole hownow surface: a button, then progress, then the report."""

    # One sweep at a time, process-wide (a second click while one is running
    # would spawn a second agent session for the same question).
    _busy = False

    def __init__(self):
        self._steps: list[dict] = []
        self._report = ""
        self._error = ""
        self._done = False
        self._started_at = 0.0
        self._status: ui.label | None = None
        self._tail: TailStrip | None = None

        ui.button("How Now", on_click=self._start).props("color=primary").tooltip(
            "Wakes a fresh agent session to sweep every live run session "
            "(runs and batch sub-runs) and report which ones need you.",
        )
        self._results = ui.column().classes("w-full gap-2")
        self._timer = ui.timer(1.0, self._tick)
        self._timer.active = False

    # ------------------------------------------------------------------ #

    def _start(self) -> None:
        if HowNowPanel._busy:
            ui.notify("A how-now sweep is already running.", type="warning")
            return
        HowNowPanel._busy = True
        self._steps = []
        self._report = ""
        self._error = ""
        self._done = False
        self._started_at = time.time()
        self._results.clear()
        with self._results:
            self._status = ui.label("Waking a fresh session…").classes(
                "text-xs text-[#8b949e]",
            )
            self._tail = TailStrip(keep=6)
        self._timer.active = True
        threading.Thread(
            target=self._run, daemon=True, name="hownow-sweep",
        ).start()

    def _run(self) -> None:
        try:
            app_state.save()
            app_state.apply_api_config()
            self._report = run_hownow_sweep(
                app_state.as_mapping(),
                get_job_manager().store,
                on_event=self._on_event,
            )
        except Exception as exc:  # noqa: BLE001 — surfaced on the panel
            logger.exception("hownow sweep failed")
            self._error = str(exc)
        finally:
            self._done = True
            HowNowPanel._busy = False

    def _on_event(self, event: dict) -> None:
        # Key steps of the hownow session's progress (info + tool markers).
        self._steps.append({
            "ts": time.time(),
            "level": event.get("level") or "info",
            "msg": event.get("msg") or "",
        })

    def _tick(self) -> None:
        if self._tail is not None:
            self._tail.update(list(self._steps))
        if not self._done:
            if self._status is not None:
                elapsed = int(time.time() - self._started_at)
                self._status.set_text(
                    f"Sweeping live sessions… {elapsed}s, "
                    f"{len(self._steps)} step(s)",
                )
            return
        self._timer.active = False
        if self._status is not None:
            if self._error:
                self._status.set_text(f"Sweep failed: {self._error}")
            else:
                self._status.set_text("Sweep complete.")
        if self._error:
            ui.notify(f"How-now sweep failed: {self._error}", type="negative")
            return
        with self._results:
            ui.markdown(self._report)
