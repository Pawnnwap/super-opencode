"""Job board: metrics, filters, active queue, and history."""

from __future__ import annotations

from pathlib import Path

from nicegui import run as nicegui_run
from nicegui import ui

from services.jobs.job_states import (
    ACTIVE_JOB_STATES,
    ATTENTION_JOB_STATES,
    TERMINAL_JOB_STATES,
)
from services.webui.layout import format_status_pill
from services.webui.log_format import (
    last_event_line,
    safe_logs,
    workspace_name,
    workspace_path,
)


def may_pause_autorefresh(
    auto_refresh: bool, active: int, scan_incomplete: bool,
) -> bool:
    """Whether the 1.5 s board timer may pause itself.

    Only a COMPLETE idle scan may pause: when a listed job's state was
    momentarily unreadable (the store is shared with the worker's writes),
    that job may well be the RUNNING one being watched — pausing on an
    incomplete scan froze the board silently (2026-11-02).
    """
    return bool(auto_refresh) and not active and not scan_incomplete


def _workspace_name(job: dict) -> str:
    return workspace_name(job)


class JobBoard:
    """Self-refreshing board for one job type ("run" | "harvest")."""

    def __init__(
        self,
        job_manager,
        job_type: str,
        *,
        base_path: str | None = None,
    ):
        self.job_manager = job_manager
        self.job_type = job_type
        self.base_path = base_path or "/run"
        self._queue_signature = None

        self._build_filters()
        self.queue_col = ui.column().classes("w-full")
        self.timer = ui.timer(1.5, self.refresh)
        self.refresh_sync()

    # ------------------------------------------------------------------ #

    def _collect(self) -> tuple[list[dict], int]:
        """Return (matching jobs, count of listed-but-unreadable jobs).

        The unreadable count marks the scan incomplete: a failed state read
        is not a deleted job, and treating it as one once made the only
        active job vanish and paused the board's timer forever.
        """
        jobs: list[dict] = []
        unreadable = 0
        for job_id in self.job_manager.store.list_jobs():
            # Cards never read logs, so do not pay for parsing them: on a 1.5 s
            # timer across every historical job this was the dominant cost.
            status = self.job_manager.get_job_summary(job_id)
            if status is None:
                unreadable += 1
            elif status.get("type") == self.job_type:
                jobs.append({"id": job_id, "status": status})
        jobs.sort(key=lambda j: j["status"].get("updated_at", 0), reverse=True)
        return jobs, unreadable

    def _build_filters(self) -> None:
        # Secondary controls: collapsed until asked for. The metrics line
        # below stays visible, and the header flags when a filter is active
        # so a narrowed board is never a silent surprise.
        self.filter_exp = ui.expansion("Search & filter").classes("w-full").props(
            "dense",
        )
        self._filter_caption = "Search & filter"
        with self.filter_exp:  # noqa: SIM117 - NiceGUI element nesting
            with ui.row().classes("w-full items-end gap-3 flex-wrap"):
                self.state_filter = ui.select(
                    ["All states", "Active queue", "RUNNING", "PENDING", "SUCCESS",
                     "FAILED", "CANCELLED", "BLOCKED"],
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

    def refresh_sync(self) -> None:
        """One-off synchronous refresh (page load, after delete).

        The per-store scan is milliseconds against small state files — fine
        once, but never on the 1.5 s timer: see :meth:`refresh`.
        """
        self._render(*self._collect())

    async def refresh(self) -> None:
        """Timer/filter refresh: collect job states on a worker thread.

        The collect reads one state JSON per stored job; run synchronously on
        the event loop (this timer fires on every open board page) it stalled
        the loop behind worker-thread disk/GIL pressure whenever jobs streamed
        heavily - the browser saw the websocket die: "UI loses connection".
        """
        collected = await nicegui_run.io_bound(self._collect)
        self._render(*collected)

    def _render(self, jobs: list[dict], incomplete: int = 0) -> None:
        workspaces = sorted({_workspace_name(j) for j in jobs} - {""})
        known = {w for w in (self.workspace_filter.options or []) if w != "All workspaces"}
        if set(workspaces) != known:
            self.workspace_filter.options = ["All workspaces"] + workspaces
            self.workspace_filter.update()

        # Keys must match the STORE's state vocabulary exactly - lowercase
        # keys here once made every count stay 0 and the metrics line read
        # "0 active / 0 success" forever. The vocabulary is imported, not
        # re-typed, so a new state cannot be counted in one place and missed
        # in another.
        counts = dict.fromkeys(
            ACTIVE_JOB_STATES | TERMINAL_JOB_STATES, 0,
        )
        for job in jobs:
            state = str(job["status"].get("state", "")).upper()
            if state in counts:
                counts[state] += 1
        active = counts["RUNNING"] + counts["PENDING"]
        attention = sum(counts[s] for s in ATTENTION_JOB_STATES)
        self.metrics_label.set_text(
            f"{len(jobs)} task(s) · {active} active "
            f"({counts['RUNNING']} running / {counts['PENDING']} queued) · "
            f"{counts['SUCCESS']} success · {counts['BLOCKED']} blocked · "
            f"{attention} need attention",
        )

        filters_active = (
            self.state_filter.value != "All states"
            or self.workspace_filter.value != "All workspaces"
            or bool((self.search_input.value or "").strip())
        )
        caption = "Search & filter · active" if filters_active else "Search & filter"
        if caption != self._filter_caption:
            self._filter_caption = caption
            self.filter_exp.set_text(caption)

        filtered = self._apply_filters(jobs)
        if not filtered:
            self.queue_col.clear()
            with self.queue_col:
                ui.label("No matching tasks.").classes("text-[#8b949e]")
            self._queue_signature = None
            if may_pause_autorefresh(self.auto_refresh.value, active, incomplete):
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
                last_event_line(job, 40),
            )
            for job in filtered
        )
        if signature != self._queue_signature:
            self._queue_signature = signature
            self.queue_col.clear()
            with self.queue_col:
                self._render_queue(filtered)

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
            # Parallel-study replicas render as ONE group card per batch;
            # standalone jobs keep their individual cards. Four per row keeps
            # the whole board readable on the standard desktop width.
            by_batch: dict[str, list[dict]] = {}
            standalone: list[dict] = []
            for job in active:
                batch_id = job["status"].get("batch_id")
                if batch_id:
                    by_batch.setdefault(batch_id, []).append(job)
                else:
                    standalone.append(job)
            with ui.grid(columns=4).classes("w-full gap-2"):
                for batch_id, members in sorted(by_batch.items()):
                    self._render_batch_card(batch_id, members)
                for job in standalone:
                    self._render_card(job)
        if history:
            expanded = not active and not filters_active
            with ui.expansion(f"History ({len(history)})", value=expanded).classes(  # noqa: SIM117 - NiceGUI element nesting
                "w-full",
            ):
                with ui.grid(columns=4).classes("w-full gap-2"):
                    for job in history[:20]:
                        self._render_card(job)

    def _render_batch_card(self, batch_id: str, members: list[dict]) -> None:
        states = [m["status"].get("state", "?") for m in members]
        running = states.count("RUNNING")
        queued = states.count("PENDING")
        summary = f"{running} running / {queued} queued" if queued else f"{running} running"
        with ui.card().classes("w-full bg-[#161b22]"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label(
                    f"🗂 Study batch {batch_id} · {len(members)} replica(s) · {summary}",
                ).classes("text-sm")
                ui.button(
                    "Cancel batch",
                    on_click=lambda b=batch_id: self._cancel_batch(b),
                ).props("flat dense color=red")
            with ui.row().classes("w-full gap-2 flex-wrap"):
                for member in sorted(
                    members, key=lambda m: int(m["status"].get("replica_index") or 0),
                ):
                    state = member["status"].get("state", "?")
                    index = member["status"].get("replica_index", "?")
                    ui.button(
                        f"r{index} · {state}",
                        on_click=lambda j=member["id"]: ui.navigate.to(
                            f"{self.base_path}/{j}",
                        ),
                    ).props("outline dense").classes("text-xs")

    def _cancel_batch(self, batch_id: str) -> None:
        cancelled = self.job_manager.cancel_batch(batch_id)
        ui.notify(f"Cancelled {len(cancelled)} job(s) of batch {batch_id}")

    def _render_card(self, job: dict) -> None:
        """Three lines: identity, workspace reveal, actions."""
        job_id = job["id"]
        status = job["status"]
        state = status.get("state", "UNKNOWN")
        workspace = workspace_path(job)

        with ui.card().classes("w-full bg-[#161b22] gap-1"):
            # line 1: run id + status (stop stays with the state it changes)
            with ui.row().classes("w-full items-center justify-between flex-nowrap"):
                ui.html(
                    f"<span class='mono text-xs'>{job_id}</span>"
                    f"{format_status_pill(state)}",
                )
                if state == "RUNNING":
                    ui.button(
                        icon="stop", on_click=lambda j=job_id: self._stop(j),
                    ).props("flat dense color=red").tooltip("Stop this run")

            # line 2: workspace folder + one-click reveal in the file manager
            name = workspace_name(job) or "(no workspace)"
            with ui.row().classes("w-full items-center gap-1 flex-nowrap"):
                ui.label(f"📁 {name}").classes("text-xs text-[#8b949e] truncate")
                if workspace and Path(workspace).is_dir():
                    ui.button(
                        icon="folder_open",
                        on_click=lambda _e, j=job_id: ui.run_javascript(
                            f"fetch('/jobs/{j}/open-folder', {{method: 'POST'}})",
                        ),
                    ).props("flat dense").tooltip("Open in the file manager")

            # line 3: detail + removal
            with ui.row().classes("w-full items-center gap-2"):
                ui.button(
                    "View", icon="open_in_new",
                    on_click=lambda j=job_id: ui.navigate.to(
                        f"{self.base_path}/{j}", new_tab=True,
                    ),
                ).props("flat dense").tooltip("Open the live screen")
                if state in TERMINAL_JOB_STATES:
                    ui.button(
                        icon="delete", on_click=lambda j=job_id: self._delete(j),
                    ).props("flat dense").tooltip("Remove this record")

    def _stop(self, job_id: str) -> None:
        self.job_manager.cancel_job(job_id)
        ui.notify(f"Stop requested: {job_id}")

    def _delete(self, job_id: str) -> None:
        self.job_manager.store.delete_job(job_id)
        self.refresh_sync()

    # ------------------------------------------------------------------ #
