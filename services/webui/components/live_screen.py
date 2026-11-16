"""Full-page live view of one job (replaces the Streamlit JobStatusScreen)."""

from __future__ import annotations

import time
from pathlib import Path

from nicegui import run as nicegui_run
from nicegui import ui

from services.jobs.job_states import ACTIVE_JOB_STATES
from services.webui import theme
from services.webui.components.log_panel import LogPanel, TailStrip
from services.webui.components.progress_widgets import ProgressWidgets
from services.webui.layout import format_status_pill
from services.webui.log_format import safe_logs, workspace_name
from services.webui.state import app_state

# Consecutive unreadable status reads before the poller gives up (each tick
# is 2 s, so 5 ≈ 10 s of store unreachability). The stop is announced; a
# single failed read never stops anything (see tick).
_MAX_CONSECUTIVE_READ_FAILURES = 5

# States where the run stopped short of its target: the page owes the operator
# an explanation, not just a status pill.
_FAILURE_STATES = frozenset({"FAILED", "ABANDONED", "BLOCKED"})


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
        self._report_signature = None
        self._failure_signature = None
        self.failure_slot = None

    # ------------------------------------------------------------------ #

    def render(self) -> None:
        # Capped tail. Events are headline-sized since the write-time
        # recorder landed, so 2000 covers a full run's stream; the true full
        # history stays on disk (max_logs=None) for the download button.
        status = self.job_manager.get_job_status(self.job_id, max_logs=2000)
        if not status:
            theme.empty_state(
                "search_off",
                f"Job {self.job_id} not found.",
                "It may have been removed by retention. The board lists every "
                "task still in the store.",
                action_label="Back to board",
                action=lambda: ui.navigate.to(self.base_path),
            )
            return

        state = status["state"]
        self._render_header(status, state)
        self._render_switcher(status, state)
        # Report slot sits ABOVE the live stream: once a job finishes, its
        # report is the primary content, not an appendix below the logs.
        # The failure slot comes first: when a run dies, "why" outranks
        # everything else on the page.
        self.failure_slot = ui.column().classes("w-full")
        self.report_slot = ui.column().classes("w-full")

        with ui.row().classes("w-full gap-4 flex-nowrap items-start"):
            with ui.column().classes("grow gap-3").style("flex:2 1 0;min-width:0"):
                with theme.panel("Progress", icon="timeline"):
                    self.progress = ProgressWidgets(
                        self.max_tokens, show_phase=self.show_phase,
                    )
                with theme.panel(
                    "Latest", icon="bolt",
                    subtitle="newest events, one line each",
                ):
                    self.tail = TailStrip(keep=5)
                from services.webui.ui_state import state_expansion

                with state_expansion(
                    f"job.{self.job_type}.fulllog", "Full log", icon="receipt_long",
                ).classes("sop-fold"):
                    self.log_panel = LogPanel(
                        detail_fetcher=self._fetch_event_detail,
                        download_fetcher=self._fetch_full_logs,
                    )
            with ui.column().classes("gap-3").style(
                "flex:1 1 280px;min-width:260px;max-width:360px",
            ):
                self._render_details(status)

        self.progress.update(safe_logs(status), state, status.get("heartbeat_at"))
        self.tail.update(safe_logs(status))
        self.log_panel.update(safe_logs(status))

        if state in ACTIVE_JOB_STATES:
            ui.label(self.running_message + " (auto-refreshing)").classes(
                "sop-hint",
            )
            self.timer = ui.timer(2.0, self.tick)
        else:
            self._render_failure(status)
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
        self.progress.update(logs, state, status.get("heartbeat_at"))
        self.tail.update(logs)
        self.log_panel.update(logs)
        self.state_pill.set_content(format_status_pill(state))
        if state not in ACTIVE_JOB_STATES:
            self.timer.active = False
            self._render_failure(status)
            self._render_report(status)
            ui.notify(f"Job {self.job_id} finished: {state}")

    def _fetch_event_detail(self, ref: int) -> dict | None:
        return self.job_manager.get_event_detail(self.job_id, ref)

    def _fetch_full_logs(self) -> list[dict]:
        """Full log history for the download button (worker thread only)."""
        return self.job_manager.store.get_logs(self.job_id)

    # ------------------------------------------------------------------ #

    def _render_header(self, status: dict, state: str) -> None:
        workspace = str((status.get("config") or {}).get("workspace") or "")
        with ui.row().classes("w-full items-center gap-3 flex-wrap"):
            with ui.column().classes("gap-1 min-w-0"):
                ui.label(f"{self.job_type.capitalize()} job").classes("sop-kicker")
                with ui.row().classes("items-center gap-3 flex-nowrap"):
                    ui.label(self.job_id).classes("sop-h1 mono").style(
                        "font-size:22px;letter-spacing:-0.02em",
                    )
                    self.state_pill = ui.html(format_status_pill(state))
            ui.space()
            with ui.row().classes("items-center gap-2 flex-nowrap"):
                if state in ACTIVE_JOB_STATES:
                    ui.button(
                        "Stop",
                        on_click=lambda: self.job_manager.cancel_job(self.job_id),
                    ).props("color=red")
                else:
                    ui.button(
                        "Back to board", on_click=lambda: ui.navigate.to(self.base_path),
                    ).props("outline color=none")
                ui.button(
                    icon="refresh",
                    on_click=lambda: ui.run_javascript("location.reload()"),
                ).props("flat").tooltip("Reload this page")
        if workspace:
            with ui.row().classes("w-full items-center gap-2 flex-nowrap"):
                ui.icon("folder").style(
                    "color:var(--sop-text-3);font-size:15px",
                )
                ui.label(workspace).classes("mono text-xs truncate").style(
                    "color:var(--sop-text-3)",
                )
                if Path(workspace).is_dir():
                    # Same reveal path as the board's card: the server resolves
                    # the folder from the job record, never from the request.
                    ui.button(
                        icon="folder_open",
                        on_click=lambda _e, j=self.job_id: ui.run_javascript(
                            f"fetch('/jobs/{j}/open-folder', {{method: 'POST'}})",
                        ),
                    ).props("flat dense").tooltip("Open in the file manager")

    def _render_switcher(self, status: dict, state: str) -> None:
        # Summaries only (no logs) and read ONCE per job: this widget used to
        # call get_job_status in the scan, in fmt() and in the sort key, i.e.
        # full log-file parses O(N log N) times per refresh. It now reads the
        # shell's shared snapshot: the store holds hundreds of summary files
        # and walking them here blocked the event loop on every detail page.
        from services.webui.layout import _store_snapshot

        siblings: dict[str, dict] = {}
        for job_id, sibling in _store_snapshot(self.job_manager).items():
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
        with ui.row().classes("w-full items-center gap-2 flex-nowrap"):
            ui.icon("switches").style("color:var(--sop-text-3);font-size:15px")
            ui.select(
                ordered,
                value=self.job_id if self.job_id in ordered else ordered[0],
                label="Other active tasks",
                on_change=lambda e: ui.navigate.to(f"{self.base_path}/{e.value}"),
            ).classes("w-96 mono")

    def _render_details(self, status: dict) -> None:
        config = status.get("config", {})
        engine = config.get("engine") or "opencode"
        started = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(float(status.get("updated_at") or 0)),
        )
        rows: list[tuple[str, str]] = [("Started", started)]
        if self.job_type == "harvest":
            rows += [
                ("Engine", engine),
                ("Model", f"{app_state.get('opencode_model') or '(not set)'}"),
                ("Runs", f"{len(config.get('harvest_job_ids') or [])} selected"),
                (
                    "Folder",
                    config.get("harvest_dir") or config.get("workspace") or "?",
                ),
            ]
        else:
            rows += [
                ("Engine", engine),
                ("Agent model", app_state.get("opencode_model") or "(not set)"),
                (
                    "Judge model",
                    app_state.get("supervisor_model") or "(not set)",
                ),
                (
                    "Occam Razor",
                    "on" if config.get("enable_occam_razor", False) else "off",
                ),
            ]

        with theme.panel("Details", icon="info"):
            for key, value in rows:
                with ui.row().classes(
                    "w-full items-baseline gap-2 flex-nowrap",
                ):
                    ui.label(key).classes("sop-hint").style(
                        "width:96px;flex:none",
                    )
                    # Wrap, do not ellipsize: a model id is the fact the
                    # reader came for, and "nemotron-3.5-l…" answers nothing.
                    ui.label(value).classes("mono text-xs").style(
                        "color:var(--sop-text-2);overflow-wrap:anywhere",
                    )

        # No report block here on purpose: _render_report is the single
        # report surface - this column used to render the SAME report a
        # second time on every finished-job page.

    def _render_failure(self, status: dict) -> None:
        """Why the run stopped, in full, above the log.

        A failed job used to render only the (empty) report section, while
        "Latest" truncated each event to its first line — so the operator saw
        "❌ Unhandled exception:" with the actual cause nowhere on screen and
        had to hunt through 500 uncoloured log lines.
        """
        import json as _json

        from supervisor.utils.text_utils import sanitize_event_message

        state = status.get("state", "")
        if state not in _FAILURE_STATES:
            return

        logs = safe_logs(status)
        culprit = next(
            (
                event
                for event in reversed(logs)
                if (event.get("level") or "") in {"error", "blocked"}
            ),
            None,
        )
        body = sanitize_event_message(culprit.get("msg") or "") if culprit else ""

        signature = (state, len(body))
        if self._failure_signature == signature:
            return
        self._failure_signature = signature

        slot = self.failure_slot
        if slot is None:
            return
        slot.clear()
        workspace = str((status.get("config") or {}).get("workspace") or "")
        with slot:
            with theme.panel(
                f"Failure · {state}", icon="error", tone="warn", anchor="failure",
            ):
                if body:
                    ui.label(body[:4000]).classes(
                        "mono text-xs whitespace-pre-wrap",
                    ).style(
                        "color:var(--sop-text-2);max-height:320px;overflow:auto;"
                        "background:var(--sop-surface-2);"
                        "border:1px solid var(--sop-line);border-radius:8px;"
                        "padding:10px 12px",
                    )
                else:
                    ui.label(
                        "No error event was recorded — open the full log below.",
                    ).classes("sop-hint")
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    if body:
                        ui.button(
                            "Copy error", icon="content_copy",
                            on_click=lambda b=body: ui.run_javascript(
                                f"navigator.clipboard.writeText({_json.dumps(b)})",
                            ),
                        ).props("outline color=none dense")
                    if workspace:
                        ui.button(
                            "Open folder", icon="folder_open",
                            on_click=lambda: ui.run_javascript(
                                f"fetch('/jobs/{self.job_id}/open-folder', "
                                "{method: 'POST'})",
                            ),
                        ).props("outline color=none dense")
                    ui.button(
                        "Launch again", icon="replay",
                        on_click=lambda: ui.navigate.to("/run"),
                    ).props("outline color=none dense")

    def _render_report(self, status: dict) -> None:
        report = status.get("report")
        if not report:
            return
        if self.job_type == "harvest":
            title = "Harvest Summary"
        else:
            title = "Report"
        # Idempotent: tick() calls this whenever the state is terminal, and a
        # blind clear+rebuild folded the report the operator had collapsed
        # (and reset its scroll) every 2 s while the poller wound down.
        if self._report_signature == (title, len(report)):
            return
        self._report_signature = (title, len(report))
        from services.webui.ui_state import state_expansion

        self.report_slot.clear()
        with self.report_slot:  # noqa: SIM117 - NiceGUI element nesting
            with state_expansion(
                f"job.{self.job_id}.report", title, default=True, icon="summarize",
            ).classes("sop-fold"):
                with ui.row().classes("w-full items-center gap-2"):
                    ui.label(f"{len(report):,} chars").classes("sop-hint")
                    ui.space()
                    ui.button(
                        "Download markdown",
                        icon="download",
                        on_click=lambda: ui.download(
                            report.encode("utf-8"),
                            f"{self.job_type}_report_{self.job_id}.md",
                        ),
                    ).props("outline color=none dense")
                ui.markdown(report)
