"""Full-page live view of one job (replaces the Streamlit JobStatusScreen)."""

from __future__ import annotations

import time
from pathlib import Path

from nicegui import ui

from services.webui.components.log_panel import LogPanel, TailStrip
from services.webui.components.progress_widgets import ProgressWidgets
from services.webui.layout import format_status_pill
from services.webui.log_format import safe_logs
from services.webui.state import app_state

ACTIVE_JOB_STATES = {"PENDING", "RUNNING"}


class LiveJobScreen:
    """Renders one job full-page with a 2s self-refresh timer."""

    def __init__(
        self,
        job_manager,
        *,
        job_type: str,
        job_id: str,
        base_path: str,
        is_evolution: bool,
        running_message: str,
    ):
        self.job_manager = job_manager
        self.job_type = job_type
        self.job_id = job_id
        self.base_path = base_path
        self.is_evolution = is_evolution
        self.running_message = running_message
        self.max_tokens = int(app_state.get("max_tokens") or 150000)
        self._last_state = ""

    # ------------------------------------------------------------------ #

    def render(self) -> None:
        # Capped tail. Events are headline-sized since the write-time
        # recorder landed, so 2000 covers a full run's stream; the true full
        # history stays on disk (max_logs=None) for the download button.
        status = self.job_manager.get_job_status(self.job_id, max_logs=2000)
        if not status:
            ui.label(f"Job {self.job_id} not found.").classes("text-[#f85149]")
            ui.button("Back to board", on_click=lambda: ui.navigate.to(self.base_path))
            return

        state = status["state"]
        self._render_header(status, state)
        self._render_switcher(status, state)

        with ui.row().classes("w-full gap-4 flex-nowrap"):
            with ui.column().classes("flex-[2]"):
                self.progress = ProgressWidgets(self.max_tokens)
                ui.label("Latest").classes("text-sm font-bold text-[#9ecbff]")
                self.tail = TailStrip(keep=5)
                with ui.expansion("完整日志", value=False).classes("w-full"):
                    self.log_panel = LogPanel(
                        detail_fetcher=self._fetch_event_detail,
                    )
            with ui.column().classes("flex-1 gap-2"):
                self._render_details(status)

        self.progress.update(safe_logs(status), state, self.is_evolution)
        self.tail.update(safe_logs(status))
        self.log_panel.update(safe_logs(status))

        if state in ACTIVE_JOB_STATES:
            ui.label(self.running_message + " (auto-refreshing)").classes(
                "text-xs text-[#8b949e]",
            )
            self.timer = ui.timer(2.0, self.tick)
        else:
            self._render_report(status)

    # ------------------------------------------------------------------ #

    def tick(self) -> None:
        status = self.job_manager.get_job_status(self.job_id, max_logs=2000) or {}
        state = status.get("state", "UNKNOWN")
        logs = safe_logs(status)
        self.progress.update(logs, state, self.is_evolution)
        self.tail.update(logs)
        self.log_panel.update(logs)
        self.state_pill.set_content(format_status_pill(state))
        if state not in ACTIVE_JOB_STATES:
            self.timer.active = False
            self._render_report(status)
            ui.notify(f"Job {self.job_id} finished: {state}")

    def _fetch_event_detail(self, ref: int) -> dict | None:
        return self.job_manager.get_event_detail(self.job_id, ref)

    # ------------------------------------------------------------------ #

    def _render_header(self, status: dict, state: str) -> None:
        with ui.row().classes("w-full items-center gap-3"):
            ui.label(f"Job: {self.job_id}").classes("text-lg font-bold")
            self.state_pill = ui.html(format_status_pill(state))
            ui.space()
            if state in ACTIVE_JOB_STATES:
                ui.button(
                    "Stop",
                    on_click=lambda: self.job_manager.cancel_job(self.job_id),
                ).props("color=red")
            else:
                ui.button(
                    "Back to board", on_click=lambda: ui.navigate.to(self.base_path),
                ).props("flat")
            ui.button(
                "Refresh", on_click=lambda: ui.run_javascript("location.reload()"),
            ).props("flat")

    def _render_switcher(self, status: dict, state: str) -> None:
        # Summaries only (no logs) and read ONCE per job: this widget used to
        # call get_job_status in the scan, in fmt() and in the sort key, i.e.
        # full log-file parses O(N log N) times per refresh.
        siblings: dict[str, dict] = {}
        for job_id in self.job_manager.store.list_jobs():
            sibling = self.job_manager.get_job_summary(job_id) or {}
            if sibling.get("type") != self.job_type:
                continue
            if sibling.get("state") in ACTIVE_JOB_STATES or job_id == self.job_id:
                siblings[job_id] = sibling
        if len(siblings) <= 1:
            return

        def fmt(job_id: str) -> str:
            s = siblings.get(job_id) or {}
            workspace = s.get("config", {}).get("workspace", "")
            name = Path(workspace).name if workspace else ""
            # Batch replicas swap the random job suffix for r1/r2/… so the
            # switcher reads as "the same study, instance 2 of 3".
            replica = (
                f" r{s.get('replica_index', '?')}/{s.get('replica_count', '?')}"
                if s.get("batch_id") else ""
            )
            return f"{job_id}{replica} [{s.get('state', '?')}{(' - ' + name) if name else ''}]"

        ordered = sorted(
            siblings,
            key=lambda j: (
                j != self.job_id,
                -float(siblings[j].get("updated_at") or 0),
            ),
        )
        ui.select(
            ordered,
            value=self.job_id if self.job_id in ordered else ordered[0],
            label="Active tasks",
            on_change=lambda e: ui.navigate.to(f"{self.base_path}/{e.value}"),
        ).classes("w-96 mono")

    def _render_details(self, status: dict) -> None:
        state = status.get("state", "UNKNOWN")
        ui.label("Details").classes("text-sm font-bold text-[#9ecbff]")
        ui.label(f"State: {state}").classes("text-xs")
        started = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(float(status.get("updated_at") or 0)),
        )
        ui.label(f"Started/Updated: {started}").classes("text-xs")
        ui.label(
            f"Supervisor model: {app_state.get('supervisor_model') or '(not set)'}",
        ).classes("text-xs")
        engine = status.get("config", {}).get("engine") or "opencode"
        ui.label(f"{engine.capitalize()} model: {app_state.get('opencode_model') or '(not set)'}").classes(
            "text-xs",
        )
        if not self.is_evolution:
            occam = status.get("config", {}).get("enable_occam_razor", False)
            ui.label(f"Occam Razor: {'on' if occam else 'off'}").classes("text-xs")

        report = status.get("report")
        if report:
            title = "Evolution Report" if self.is_evolution else "Report"
            with ui.expansion(title).classes("w-full"):
                ui.markdown(report)
                ui.button(
                    "Download",
                    on_click=lambda: ui.download(
                        report.encode("utf-8"),
                        f"{self.job_type}_report_{self.job_id}.md",
                    ),
                )

    def _render_report(self, status: dict) -> None:
        report = status.get("report")
        if not report:
            return
        title = "Evolution Report" if self.is_evolution else "Report"
        with ui.expansion(title, value=True).classes("w-full"):
            ui.markdown(report)
            ui.button(
                "Download",
                on_click=lambda: ui.download(
                    report.encode("utf-8"),
                    f"{'evolution' if self.is_evolution else 'run'}_report_{self.job_id}.md",
                ),
            )
