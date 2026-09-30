"""Job board: metrics, filters, active queue, history, snapshot pane."""

from __future__ import annotations

import time
from pathlib import Path

from nicegui import ui

from services.webui.components.progress_widgets import ProgressWidgets
from services.webui.layout import format_status_pill
from services.webui.log_format import fmt_ts, markdown_preview, safe_logs
from services.webui.state import app_state

ACTIVE_JOB_STATES = {"PENDING", "RUNNING"}


def _workspace_name(job: dict) -> str:
    workspace = job.get("status", {}).get("config", {}).get("workspace", "")
    return Path(workspace).name if workspace else ""


def _last_event_line(job: dict, limit: int = 120) -> str:
    for event in reversed(safe_logs(job.get("status", {}))):
        if not isinstance(event, dict) or event.get("level") == "heartbeat":
            continue
        msg = (event.get("msg") or "").strip()
        if msg:
            return msg.splitlines()[0][:limit]
    return ""


class JobBoard:
    """Self-refreshing board for one job type ("run" | "evolve")."""

    def __init__(self, job_manager, job_type: str, *, is_evolution: bool):
        self.job_manager = job_manager
        self.job_type = job_type
        self.is_evolution = is_evolution
        self.base_path = "/evolve" if is_evolution else "/run"
        self.max_tokens = int(app_state.get("max_tokens") or 150000)
        self._queue_signature = None
        self._snapshot_signature = None

        self._build_filters()
        self.queue_col = ui.column().classes("w-full")
        self._queue_signature = None
        self._snapshot_signature = None
        self.timer = ui.timer(1.5, self.refresh)
        self.refresh()

    # ------------------------------------------------------------------ #

    def _collect(self) -> list[dict]:
        jobs: list[dict] = []
        for job_id in self.job_manager.store.list_jobs():
            status = self.job_manager.get_job_status(job_id)
            if status and status.get("type") == self.job_type:
                jobs.append({"id": job_id, "status": status})
        jobs.sort(key=lambda j: j["status"].get("updated_at", 0), reverse=True)
        return jobs

    def _build_filters(self) -> None:
        with ui.row().classes("w-full items-end gap-3 flex-wrap"):
            self.state_filter = ui.select(
                ["All states", "Active queue", "RUNNING", "PENDING", "SUCCESS",
                 "FAILED", "CANCELLED"],
                value="All states",
                label="State",
                on_change=self.refresh,
            ).classes("w-40")
            self.workspace_filter = ui.select(
                ["All workspaces"], value="All workspaces", label="Workspace",
                on_change=self.refresh,
            ).classes("w-48")
            self.search_input = ui.input("Search", on_change=self.refresh).classes(
                "mono w-64",
            )
            self.auto_refresh = ui.switch("Auto-refresh", value=True)
        self.metrics_label = ui.label("").classes("text-xs text-[#8b949e]")

    def _apply_filters(self, jobs: list[dict]) -> list[dict]:
        state = self.state_filter.value
        workspace = self.workspace_filter.value
        query = (self.search_input.value or "").strip().lower()
        out = []
        for job in jobs:
            job_state = job["status"].get("state", "UNKNOWN")
            name = _workspace_name(job)
            if state == "Active queue" and job_state not in ACTIVE_JOB_STATES:
                continue
            if state not in {"All states", "Active queue"} and job_state != state:
                continue
            if workspace != "All workspaces" and name != workspace:
                continue
            if query and query not in f"{job['id']} {name} {job_state}".lower():
                continue
            out.append(job)
        return out

    # ------------------------------------------------------------------ #

    def refresh(self) -> None:
        jobs = self._collect()
        workspaces = sorted({_workspace_name(j) for j in jobs} - {""})
        known = {w for w in (self.workspace_filter.options or []) if w != "All workspaces"}
        if set(workspaces) != known:
            self.workspace_filter.options = ["All workspaces"] + workspaces
            self.workspace_filter.update()

        counts = {
            "running": 0, "pending": 0, "success": 0, "failed": 0, "cancelled": 0,
        }
        for job in jobs:
            state = job["status"].get("state", "UNKNOWN")
            if state in counts:
                counts[state] += 1
        active = counts["running"] + counts["pending"]
        self.metrics_label.set_text(
            f"{len(jobs)} task(s) · {active} active "
            f"({counts['running']} running / {counts['pending']} queued) · "
            f"{counts['success']} success · {counts['failed'] + counts['cancelled']}"
            " need attention",
        )

        filtered = self._apply_filters(jobs)
        if not filtered:
            self.queue_col.clear()
            with self.queue_col:
                ui.label("No matching tasks.").classes("text-[#8b949e]")
            self._queue_signature = None
            self._snapshot_signature = None
            if self.auto_refresh.value and not active:
                self.timer.active = False
            return

        if not self.auto_refresh.value:
            if self.timer.active:
                self.timer.active = False
        elif not self.timer.active:
            self.timer.active = True

        signature = tuple(
            (
                job["id"],
                job["status"].get("state", ""),
                len(safe_logs(job["status"])),
                _last_event_line(job, 40),
            )
            for job in filtered
        )
        selected_id = filtered[0]["id"]
        selected = filtered[0]
        snapshot_signature = (
            selected_id,
            selected["status"].get("state", ""),
            len(safe_logs(selected["status"])),
        )
        self._snapshot_selected = (selected, snapshot_signature)
        if signature != self._queue_signature or snapshot_signature != self._snapshot_signature:
            self._queue_signature = signature
            self._snapshot_signature = snapshot_signature
            self.queue_col.clear()
            with self.queue_col:
                with ui.row().classes("w-full gap-4 flex-nowrap"):
                    with ui.column().classes("flex-[1.7] gap-2"):
                        self._render_queue(filtered)
                    with ui.column().classes("flex-1 gap-2"):
                        self._render_snapshot(selected)

    # ------------------------------------------------------------------ #

    def _render_queue(self, jobs: list[dict]) -> None:
        active = [j for j in jobs if j["status"].get("state") in ACTIVE_JOB_STATES]
        history = [j for j in jobs if j["status"].get("state") not in ACTIVE_JOB_STATES]
        filters_active = (
            self.state_filter.value != "All states"
            or self.workspace_filter.value != "All workspaces"
            or bool((self.search_input.value or "").strip())
        )
        if active:
            ui.label(f"Active Queue ({len(active)})").classes(
                "text-sm font-bold text-[#9ecbff]",
            )
            for job in active:
                self._render_card(job)
        if history:
            expanded = not active and not filters_active
            with ui.expansion(f"History ({len(history)})", value=expanded).classes(
                "w-full",
            ):
                for job in history[:20]:
                    self._render_card(job)

    def _render_card(self, job: dict) -> None:
        job_id = job["id"]
        status = job["status"]
        state = status.get("state", "UNKNOWN")
        with ui.card().classes("w-full bg-[#161b22]"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.html(
                    f"<span class='mono text-xs'>{job_id}</span>"
                    f"{format_status_pill(state)}",
                )
                with ui.row():
                    if state == "RUNNING":
                        ui.button(
                            icon="stop", on_click=lambda j=job_id: self._stop(j),
                        ).props("flat dense color=red")
                    elif state in ("SUCCESS", "FAILED", "CANCELLED"):
                        ui.button(
                            icon="delete",
                            on_click=lambda j=job_id: self._delete(j),
                        ).props("flat dense")
                    ui.button(
                        icon="open_in_new",
                        on_click=lambda j=job_id: ui.navigate.to(
                            f"{self.base_path}/{j}", new_tab=True,
                        ),
                    ).props("flat dense")
            meta = []
            workspace = status.get("config", {}).get("workspace", "")
            if workspace:
                meta.append(f"📁 {Path(workspace).name}")
            updated = float(status.get("updated_at") or 0)
            if updated and state == "RUNNING":
                mins, secs = divmod(max(0, int(time.time() - updated)), 60)
                meta.append(f"⏱ {mins}m {secs:02d}s")
            errors = sum(
                1 for e in safe_logs(status)
                if isinstance(e, dict) and e.get("level") == "error"
            )
            if errors:
                meta.append(f"❌ {errors}")
            if meta:
                ui.label(" · ".join(meta)).classes("text-xs text-[#8b949e]")
            last = _last_event_line(job)
            if last:
                ui.label(f"› {last}").classes("text-xs text-[#8b949e]")

    def _stop(self, job_id: str) -> None:
        self.job_manager.cancel_job(job_id)
        ui.notify(f"Stop requested: {job_id}")

    def _delete(self, job_id: str) -> None:
        self.job_manager.store.delete_job(job_id)
        self.refresh()

    # ------------------------------------------------------------------ #

    def _render_snapshot(self, selected: dict) -> None:
        selected_id = selected["id"]
        ui.label("Task Snapshot").classes("text-sm font-bold text-[#9ecbff]")
        ui.html(
            f"<span class='mono text-xs'>{selected_id}</span>"
            f"{format_status_pill(selected['status'].get('state', 'UNKNOWN'))}",
        )
        workspace = selected["status"].get("config", {}).get("workspace", "")
        if workspace:
            ui.label(f"Workspace: {Path(workspace).name}").classes(
                "text-xs text-[#8b949e]",
            )
        updated = float(selected["status"].get("updated_at") or 0)
        if updated:
            ui.label(
                "Updated: "
                + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(updated)),
            ).classes("text-xs text-[#8b949e]")
        last = _last_event_line(selected, 180)
        if last:
            ui.label(f"Last: {last}").classes("text-xs text-[#8b949e]")
        logs = safe_logs(selected["status"])
        ProgressWidgets(self.max_tokens).update(
            logs,
            selected["status"].get("state", "UNKNOWN"),
            is_evolution=self.is_evolution,
        )
        with ui.expansion("Recent log").classes("w-full"):
            mini = ui.log(max_lines=60).classes("log-box w-full h-56")
            for event in logs[-40:]:
                level = event.get("level") or "info"
                if level in ("heartbeat", "step_progress", "tokens"):
                    continue
                ts = fmt_ts(event)
                text = sanitize_short(event.get("msg") or "")
                mini.push(f"[{ts}] {text}" if ts else text)
        report = selected["status"].get("report")
        if report:
            with ui.expansion("Report preview").classes("w-full"):
                ui.markdown(markdown_preview(report, 2000))


def sanitize_short(msg: str) -> str:
    from supervisor.utils.text_utils import sanitize_event_message

    return sanitize_event_message(msg)
