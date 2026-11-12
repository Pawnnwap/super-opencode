"""Full-page live view of one job (replaces the Streamlit JobStatusScreen)."""

from __future__ import annotations

import time

from nicegui import run as nicegui_run
from nicegui import ui

from services.jobs.job_states import ACTIVE_JOB_STATES
from services.webui.components.log_panel import LogPanel, TailStrip
from services.webui.components.progress_widgets import ProgressWidgets
from services.webui.layout import format_status_pill
from services.webui.log_format import safe_logs, workspace_name
from services.webui.state import app_state

# Consecutive unreadable status reads before the poller gives up (each tick
# is 2 s, so 5 ≈ 10 s of store unreachability). The stop is announced; a
# single failed read never stops anything (see tick).
_MAX_CONSECUTIVE_READ_FAILURES = 5


class LiveJobScreen:
    """Renders one job full-page with a 2s self-refresh timer."""

    def __init__(
        self,
        job_manager,
        *,
        job_type: str,
        job_id: str,
        base_path: str,
        running_message: str,
    ):
        self.job_manager = job_manager
        self.job_type = job_type
        self.job_id = job_id
        self.base_path = base_path
        self.running_message = running_message
        # Harvest jobs have no code/test/review pipeline to show.
        self.show_phase = job_type != "harvest"
        self.max_tokens = int(app_state.get("max_tokens") or 150000)
        self._read_failures = 0

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
        # Report slot sits ABOVE the live stream: once a job finishes, its
        # report is the primary content, not an appendix below the logs.
        self.report_slot = ui.column().classes("w-full")

        with ui.row().classes("w-full gap-4 flex-nowrap"):
            with ui.column().classes("flex-[2]"):
                self.progress = ProgressWidgets(
                    self.max_tokens, show_phase=self.show_phase,
                )
                ui.label("Latest").classes("text-sm font-bold text-[#9ecbff]")
                self.tail = TailStrip(keep=5)
                with ui.expansion("完整日志", value=False).classes("w-full"):
                    self.log_panel = LogPanel(
                        detail_fetcher=self._fetch_event_detail,
                        download_fetcher=self._fetch_full_logs,
                    )
            with ui.column().classes("flex-1 gap-2"):
                self._render_details(status)

        self.progress.update(safe_logs(status), state)
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

    async def tick(self) -> None:
        # The 2 s poll reads the job's tail (up to 2000 events) off the event
        # loop: on the loop it stalled every open detail page behind disk and
        # JSON work whenever the job's codex stream burst.
        status = await nicegui_run.io_bound(
            self.job_manager.get_job_status, self.job_id, 2000,
        )
        if status is None:
            # An unreadable status is NOT a terminal state. The store is
            # shared with the worker's writes, and one transient read error
            # (2026-11-02: "[Errno 13] Permission denied" during a concurrent
            # os.replace) used to fall through to ``or {}`` → state UNKNOWN →
            # "finished", permanently freezing this screen while the job kept
            # running. Keep the last rendered state and try again next tick.
            self._read_failures += 1
            if self._read_failures < _MAX_CONSECUTIVE_READ_FAILURES:
                return
            self.timer.active = False
            self.state_pill.set_content(format_status_pill("UNKNOWN"))
            ui.notify(
                f"Job {self.job_id} state is unreadable "
                f"({self._read_failures} consecutive failed reads) — use Refresh",
                type="warning",
            )
            return
        self._read_failures = 0
        state = status.get("state", "UNKNOWN")
        logs = safe_logs(status)
        self.progress.update(logs, state)
        self.tail.update(logs)
        self.log_panel.update(logs)
        self.state_pill.set_content(format_status_pill(state))
        if state not in ACTIVE_JOB_STATES:
            self.timer.active = False
            self._render_report(status)
            ui.notify(f"Job {self.job_id} finished: {state}")

    def _fetch_event_detail(self, ref: int) -> dict | None:
        return self.job_manager.get_event_detail(self.job_id, ref)

    def _fetch_full_logs(self) -> list[dict]:
        """Full log history for the download button (worker thread only)."""
        return self.job_manager.store.get_logs(self.job_id)

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
            name = workspace_name(s)
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
        config = status.get("config", {})
        ui.label("Details").classes("text-sm font-bold text-[#9ecbff]")
        # No State row: the header pill already shows it, live-updated.
        started = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(float(status.get("updated_at") or 0)),
        )
        ui.label(f"Started/Updated: {started}").classes("text-xs")
        engine = config.get("engine") or "opencode"
        if self.job_type == "harvest":
            ui.label(
                f"{engine.capitalize()} model: "
                f"{app_state.get('opencode_model') or '(not set)'}",
            ).classes("text-xs")
            runs = config.get("harvest_job_ids") or []
            ui.label(f"Runs selected: {len(runs)}").classes("text-xs")
            folder = config.get("harvest_dir") or config.get("workspace") or "?"
            ui.label(f"Harvest folder: {folder}").classes(
                "text-xs mono text-[#8b949e]",
            )
        else:
            ui.label(
                f"Supervisor model: {app_state.get('supervisor_model') or '(not set)'}",
            ).classes("text-xs")
            ui.label(
                f"{engine.capitalize()} model: "
                f"{app_state.get('opencode_model') or '(not set)'}",
            ).classes("text-xs")
            occam = config.get("enable_occam_razor", False)
            ui.label(f"Occam Razor: {'on' if occam else 'off'}").classes(
                "text-xs",
            )

        # No report block here on purpose: _render_report is the single
        # report surface - this column used to render the SAME report a
        # second time on every finished-job page.

    def _render_report(self, status: dict) -> None:
        report = status.get("report")
        if not report:
            return
        if self.job_type == "harvest":
            title = "Harvest Summary"
        else:
            title = "Report"
        self.report_slot.clear()
        with self.report_slot:  # noqa: SIM117 - NiceGUI element nesting
            with ui.expansion(title, value=True).classes("w-full"):
                ui.markdown(report)
                ui.button(
                    "Download",
                    on_click=lambda: ui.download(
                        report.encode("utf-8"),
                        f"{self.job_type}_report_{self.job_id}.md",
                    ),
                )
