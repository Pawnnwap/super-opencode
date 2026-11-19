"""Job board: metrics, filters, active queue, and history."""

from __future__ import annotations

import time
from pathlib import Path
from typing import ClassVar

from nicegui import run as nicegui_run
from nicegui import ui

from services.jobs.harvest import HARVEST_JOB_TYPE, collected_runs
from services.jobs.job_states import (
    ACTIVE_JOB_STATES,
    ATTENTION_JOB_STATES,
    TERMINAL_JOB_STATES,
)
from services.webui.layout import format_status_pill
from services.webui.log_format import workspace_name, workspace_path
from services.webui.ui_state import state_expansion, stored_expansion


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


def _fmt_when(status: dict) -> str:
    """Compact local timestamp of the last write, '' when unknown.

    Deliberately minute-resolution: the board redraws on state changes only,
    and a seconds field would make every streaming card look stale the moment
    it was drawn.
    """
    ts = status.get("updated_at")
    if not isinstance(ts, (int, float)) or ts <= 0:
        return ""
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def _fmt_duration(status: dict) -> str:
    """Elapsed time of a finished job, '' when the record predates created_at.

    Cards used to show only "when it last changed", so "did yesterday's run
    take 4 minutes or 4 hours" required opening the detail page.
    """
    try:
        created = float(status.get("created_at") or 0)
        updated = float(status.get("updated_at") or 0)
    except (TypeError, ValueError):
        return ""
    if created <= 0 or updated <= created:
        return ""
    seconds = int(updated - created)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, rest = divmod(minutes, 60)
    return f"{hours}h{rest:02d}m"


# History is paged. A live store holds hundreds of finished jobs (the
# retention cap prunes LOG payloads, never the summary json), and building a
# card per job is hundreds of elements in one round trip: the browser stalls,
# the websocket bursts, and the panel arrives late or not at all. One page
# renders at a time; "Show more" appends the next.
_HISTORY_PAGE = 20


class JobBoard:
    """Self-refreshing board for one job type ("run" | "harvest")."""

    # Class-level fallback: the board tests build JobBoard with __new__, so
    # every read needs a map even when __init__ never ran. The harvest board
    # never scans one; the empty answers "not harvested".
    _collected: ClassVar[dict[str, str]] = {}

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
        self._active_signature = None
        self._history_signature = None
        self._history_title = ""
        self._history_auto_opened = False
        # How many history cards the operator has asked to see. One page is
        # built at a time; "Show more" grows this by another page.
        self._history_limit = _HISTORY_PAGE
        self._history_total = 0
        # job_id -> (mtime_ns, size, record) for the store walk.
        self._state_cache: dict[str, tuple] = {}
        # Run-board only: run id -> its per-run harvest folder ("already
        # collected"), rebuilt by every scan.
        self._collected = {}

        self._build_filters()
        self._empty_signature = None
        self.queue_col = ui.column().classes("w-full")
        with self.queue_col:
            self._empty_label = ui.column().classes("w-full")
            self.active_col = ui.column().classes("w-full")
            # Built ONCE and never re-created: only its contents and
            # visibility change. Re-creating the expansion per refresh (the
            # old queue_col.clear() path) sent it back at the constructor
            # default, so a History the operator had just opened collapsed
            # again on the next 1.5 s tick.
            self._history_key = f"board.{self.job_type}.history"
            self.history_exp = state_expansion(
                self._history_key, "History", icon="history",
            ).classes("sop-fold")
            with self.history_exp:
                self.history_grid = ui.element("div").classes(
                    "sop-cards sop-cards--jobs",
                )
                # Footer: the one element that says how much of History is on
                # screen and offers the next page. It lives outside the grid
                # so appending cards never disturbs it.
                with ui.row().classes(
                    "w-full items-center gap-3 mt-1 flex-wrap",
                ) as self.history_footer:
                    self.history_count_label = ui.label("").classes("sop-hint")
                    self.history_more_button = ui.button(
                        "Show more", on_click=self._show_more_history,
                    ).props("flat dense")
                self.history_footer.set_visibility(False)
            self.history_exp.set_visibility(False)
            self._empty_label.set_visibility(False)
        self.timer = ui.timer(1.5, self.refresh)
        self.refresh_sync()

    # ------------------------------------------------------------------ #

    def _collect(self) -> tuple[list[dict], int]:
        """Return (matching jobs, count of listed-but-unreadable jobs).

        The unreadable count marks the scan incomplete: a failed state read
        is not a deleted job, and treating it as one once made the only
        active job vanish and paused the board's timer forever.

        Summaries are read through a per-file (mtime, size) cache: a finished
        job's summary never changes and the store keeps hundreds of them, so
        re-parsing the whole set every 1.5 s was pure disk work under the
        worker's writes (2026-11-13: the walk blocked the event loop).
        """
        jobs: list[dict] = []
        unreadable = 0
        harvest_dirs: list[str] = []
        store = self.job_manager.store
        try:
            job_ids = store.list_jobs()
        except OSError:
            return jobs, 1
        # Older/duck-typed stores and bare test doubles may not offer the
        # cached reader; fall back to the plain summary read then.
        cache = getattr(self, "_state_cache", None)
        cached_reader = (
            store.get_job_state_cached if hasattr(store, "get_job_state_cached")
            else None
        )
        for job_id in job_ids:
            # Cards never read logs, so do not pay for parsing them: on a 1.5 s
            # timer across every historical job this was the dominant cost.
            if cached_reader is not None and cache is not None:
                status = cached_reader(job_id, cache)
            else:
                status = self.job_manager.get_job_summary(job_id)
            if status is None:
                unreadable += 1
                continue
            record_type = status.get("type")
            # Harvest batch folders feed the history mark ("was this run
            # collected?"); one scandir per batch, never a second store walk.
            # No state filter: a harvest that ended FAILED still wrote a
            # folder (and report.md) for every run it reached, and that folder
            # is exactly what the mark points at.
            if record_type == HARVEST_JOB_TYPE:
                config = status.get("config") or {}
                harvest_dirs.append(
                    str(config.get("harvest_dir") or config.get("workspace") or ""),
                )
            if record_type == self.job_type:
                jobs.append({"id": job_id, "status": status})
        # Deleted jobs must not keep a parse-cache entry alive forever.
        if cache is not None and cached_reader is not None:
            for dead in set(cache) - set(job_ids):
                cache.pop(dead, None)
        # Only a RUN's history card can be marked: harvest records are never
        # harvested themselves, so that board skips the directory scans.
        if self.job_type == "run":
            self._collected = collected_runs(harvest_dirs)
        jobs.sort(key=lambda j: j["status"].get("updated_at", 0), reverse=True)
        return jobs, unreadable

    def _build_filters(self) -> None:
        # Secondary controls: collapsed until asked for. The metrics strip
        # below stays visible, and the header flags when a filter is active
        # so a narrowed board is never a silent surprise.
        self.filter_exp = state_expansion(
            f"board.{self.job_type}.filters", "Search & filter",
            icon="filter_list",
        ).classes("sop-fold sop-fold--dense")
        self._filter_caption = "Search & filter"
        with self.filter_exp:  # noqa: SIM117 - NiceGUI element nesting
            with ui.row().classes("w-full items-end gap-3 flex-wrap"):
                self.state_filter = ui.select(
                    ["All states", "Active queue", "RUNNING", "PENDING", "SUCCESS",
                     "FAILED", "CANCELLED", "BLOCKED", "ABANDONED"],
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
                ).props("debounce=350 clearable")
                # Esc clears without moving the mouse; the debounce means one
                # scan per pause, not one per keystroke.
                self.search_input.on(
                    "keydown.esc", lambda: self.search_input.set_value(""),
                )
                self.auto_refresh = ui.switch("Auto-refresh", value=True)
        # One html element re-written per tick: five chip spans are cheaper to
        # update than five widgets, and the strip never moves.
        self.metrics_label = ui.html("").classes("sop-metrics")

    def _render_empty(self, *, any_jobs: bool) -> None:
        """One honest empty state, rebuilt only when its wording changes.

        An empty board used to print the filter bar, a five-chip metrics strip
        all reading 0 and the words "No matching tasks" — a lot of chrome
        saying nothing. A store with no jobs of this type gets a plain
        explanation instead, and the filter bar hides until there is
        something to filter.
        """
        from services.webui import theme

        signature = (any_jobs, self.job_type)
        if signature != self._empty_signature:
            self._empty_signature = signature
            self._empty_label.clear()
            with self._empty_label:
                if any_jobs:
                    theme.empty_state(
                        "filter_alt_off",
                        "No tasks match these filters",
                        "Widen the state filter, clear the workspace filter or "
                        "the search box — History keeps every task the store "
                        "still holds.",
                    )
                else:
                    theme.empty_state(
                        "inbox",
                        "No tasks yet",
                        "Launches appear here the moment they are queued: an "
                        "active card while the job runs, then a History card "
                        "with its report.",
                    )
        self._empty_label.set_visibility(True)
        self.metrics_label.set_content("")
        self.filter_exp.set_visibility(bool(any_jobs))

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

        def metric(value: int, label: str, tone: str = "") -> str:
            # Tone is constant per position: it used to be applied only when
            # the value was non-zero, so "23 success" was green while
            # "0 blocked" was white and the strip could not be read by
            # position or by colour. A zero now dims the number instead.
            cls = "sop-metric" + (f" sop-metric--{tone}" if tone else "")
            if not value:
                cls += " sop-metric--zero"
            return f'<span class="{cls}"><b>{value}</b>{label}</span>'

        chips = [metric(len(jobs), "task(s)")]
        if active:
            chips.append(
                metric(
                    active,
                    f"active · {counts['RUNNING']} run / {counts['PENDING']} queued",
                    "live",
                ),
            )
        chips.append(metric(counts["SUCCESS"], "success", "ok"))
        # Blocked is rare and only meaningful when non-zero: keeping a
        # permanent "0 blocked" next to "need attention" read as a
        # contradiction.
        if counts["BLOCKED"]:
            chips.append(metric(counts["BLOCKED"], "blocked", "warn"))
        chips.append(metric(attention, "need attention", "bad"))
        self.metrics_label.set_content("".join(chips))

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
            self._active_signature = None
            self._history_signature = None
            self.active_col.clear()
            self.history_grid.clear()
            self.history_exp.set_visibility(False)
            self._render_empty(any_jobs=bool(jobs))
            if may_pause_autorefresh(self.auto_refresh.value, active, incomplete):
                self.timer.active = False
            return
        self._empty_label.set_visibility(False)
        self.filter_exp.set_visibility(True)

        if not self.auto_refresh.value:
            if self.timer.active:
                self.timer.active = False
        elif not self.timer.active:
            self.timer.active = True

        active_jobs = [
            j for j in filtered if j["status"].get("state") in ACTIVE_JOB_STATES
        ]
        history_jobs = [
            j for j in filtered if j["status"].get("state") not in ACTIVE_JOB_STATES
        ]
        self._render_active(active_jobs)
        self._render_history(history_jobs, auto_open=not active_jobs and not filters_active)

    # ------------------------------------------------------------------ #

    def _card_signature(self, job: dict) -> tuple:
        """What a card actually renders: identity, state, workspace, batch.

        Neither card type shows log content, so log churn must not be part of
        the signature - a streaming run would otherwise force a full rebuild
        of the board (History included) on every tick.
        """
        status = job["status"]
        return (
            job["id"],
            status.get("state", ""),
            _workspace_name(job),
            status.get("batch_id") or "",
            status.get("replica_index"),
            # A harvest finishing changes a history card, so the mark is part
            # of what the card renders.
            self._collected.get(job["id"], ""),
        )

    def _render_active(self, active: list[dict]) -> None:
        signature = tuple(self._card_signature(j) for j in active)
        if signature == self._active_signature:
            return
        self._active_signature = signature
        self.active_col.clear()
        if not active:
            return
        with self.active_col:
            with ui.row().classes("sop-section-head"):
                ui.icon("bolt")
                ui.label("Active Queue").classes("sop-h2")
                ui.html(
                    f'<span class="sop-section-head__count">{len(active)}</span>',
                )
                ui.space()
                ui.label("stops take effect between turns").classes("sop-hint")
            # Parallel-study replicas render as ONE group card per batch;
            # standalone jobs keep their individual cards. Auto-fill keeps the
            # whole board readable from 1100 px to 2500 px wide.
            by_batch: dict[str, list[dict]] = {}
            standalone: list[dict] = []
            for job in active:
                batch_id = job["status"].get("batch_id")
                if batch_id:
                    by_batch.setdefault(batch_id, []).append(job)
                else:
                    standalone.append(job)
            with ui.element("div").classes("sop-cards sop-cards--jobs"):
                for batch_id, members in sorted(by_batch.items()):
                    self._render_batch_card(batch_id, members)
                for job in standalone:
                    self._render_card(job)

    def _render_history(self, history: list[dict], *, auto_open: bool) -> None:
        """Fill the persistent History panel; never touch its open state.

        This expansion is created once in __init__ and only its contents and
        visibility change here. Its open state is owned by
        :mod:`services.webui.ui_state`: the operator's toggle wins forever,
        so a streaming job's 1.5 s refresh can no longer fold the panel.
        The idle default ("open when nothing is running") applies only until
        the operator toggles it once.

        Paged: only ``self._history_limit`` cards exist at a time. A store can
        hold hundreds of finished jobs, and building them all in one message
        was the "open History and the tab stalls" failure - the browser chokes
        on the burst and the websocket drops.
        """
        if not history:
            self._history_signature = None
            self._history_total = 0
            self.history_grid.clear()
            self.history_footer.set_visibility(False)
            self.history_exp.set_visibility(False)
            return
        if not self._history_auto_opened:
            self._history_auto_opened = True
            if auto_open and stored_expansion(self._history_key) is None:
                self.history_exp.value = True
        self._history_total = len(history)
        title = f"History ({len(history)})"
        if title != self._history_title:
            self._history_title = title
            self.history_exp.set_text(title)
        self.history_exp.set_visibility(True)

        shown = history[: self._history_limit]
        signature = tuple(self._card_signature(j) for j in shown)
        rendered = self._history_signature
        if signature == rendered:
            self._update_history_footer(shown=len(shown))
            return
        # Append when the new page merely EXTENDS what is on screen: rebuilding
        # the whole grid on "Show more" threw the operator back up the panel.
        appended = (
            rendered is not None
            and len(signature) > len(rendered)
            and signature[: len(rendered)] == rendered
        )
        if appended:
            new_cards = shown[len(rendered):]
            with self.history_grid:
                for job in new_cards:
                    self._render_card(job)
        else:
            self.history_grid.clear()
            with self.history_grid:
                for job in shown:
                    self._render_card(job)
        self._history_signature = signature
        self._update_history_footer(shown=len(shown))

    def _update_history_footer(self, *, shown: int) -> None:
        total = self._history_total
        more = shown < total
        self.history_count_label.set_text(
            f"Showing {shown} of {total} finished task(s)"
            + ("" if more else " — all shown"),
        )
        self.history_more_button.set_text(
            f"Show {min(_HISTORY_PAGE, total - shown)} more",
        )
        self.history_more_button.set_visibility(more)
        self.history_footer.set_visibility(True)

    def _show_more_history(self) -> None:
        """Grow History by one page, then render the new cards.

        The signature is deliberately NOT cleared: ``_render_history`` sees
        that the wider page merely extends what is already rendered and
        appends only the new cards, keeping the operator's scroll position.
        """
        self._history_limit += _HISTORY_PAGE
        self.refresh_sync()

    def _render_batch_card(self, batch_id: str, members: list[dict]) -> None:
        states = [m["status"].get("state", "?") for m in members]
        running = states.count("RUNNING")
        queued = states.count("PENDING")
        summary = f"{running} running / {queued} queued" if queued else f"{running} running"
        with ui.card().classes("sop-jobcard sop-jobcard--running gap-2"):
            # Identity line and control line stay separate: one nowrap row
            # crushed the chip, summary and button into vertical text at the
            # grid's 300-345 px column width.
            with ui.row().classes("w-full items-center gap-2 flex-nowrap"):
                ui.icon("folder_copy").style("color:var(--sop-accent-2);font-size:16px")
                ui.label(f"Study batch {batch_id}").classes(
                    "sop-jobcard__id truncate grow min-w-0",
                ).tooltip(batch_id)
                ui.html(
                    f'<span class="chip chip--accent">{len(members)} replica(s)</span>',
                )
            with ui.row().classes("w-full items-center gap-2 flex-nowrap"):
                ui.label(summary).classes("sop-jobcard__meta sop-jobcard__summary")
                ui.space()
                ui.button(
                    "Cancel batch",
                    on_click=lambda b=batch_id: self._cancel_batch(b),
                ).props("flat dense color=red")
            with ui.row().classes("w-full gap-1 flex-wrap"):
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
                    ).props("outline color=none dense").classes("text-xs")

    def _cancel_batch(self, batch_id: str) -> None:
        cancelled = self.job_manager.cancel_batch(batch_id)
        ui.notify(f"Cancelled {len(cancelled)} job(s) of batch {batch_id}")

    def _render_card(self, job: dict) -> None:
        """Three lines: identity, workspace reveal, actions."""
        job_id = job["id"]
        status = job["status"]
        state = status.get("state", "UNKNOWN")
        workspace = workspace_path(job)

        with ui.card().classes(
            f"sop-jobcard sop-jobcard--{state.lower()} gap-1",
        ):
            # line 1: run id + status (stop stays with the state it changes)
            with ui.row().classes("w-full items-center gap-2 flex-nowrap"):
                ui.label(job_id).classes("sop-jobcard__id truncate grow")
                ui.html(format_status_pill(state))
                harvest_folder = self._collected.get(job_id)
                if harvest_folder:
                    # Collected already: a quiet icon keeps the identity row's
                    # width intact; the tooltip names the folder it went to.
                    ui.icon("inventory_2").classes("sop-jobcard__harvest").style(
                        "flex:none",
                    ).tooltip(f"Harvested into {harvest_folder}")
                if state == "RUNNING":
                    ui.button(
                        icon="stop", on_click=lambda j=job_id: self._stop(j),
                    ).props('flat dense color=red aria-label="Stop this run"').tooltip(
                        "Stop this run",
                    )

            # line 2: workspace folder + one-click reveal in the file manager
            name = workspace_name(job) or "(no workspace)"
            with ui.row().classes("w-full items-center gap-1 flex-nowrap"):
                ui.icon("folder").style(
                    "color:var(--sop-text-3);font-size:14px;flex:none",
                )
                ui.label(name).classes("sop-jobcard__meta truncate")
                if workspace and Path(workspace).is_dir():
                    ui.button(
                        icon="folder_open",
                        on_click=lambda _e, j=job_id: ui.run_javascript(
                            f"fetch('/jobs/{j}/open-folder', {{method: 'POST'}})",
                        ),
                    ).props(
                        'flat dense aria-label="Open this workspace in the file manager"',
                    ).tooltip("Open in the file manager")
                ui.space()
                stamp = _fmt_when(status)
                if stamp:
                    duration = _fmt_duration(status)
                    ui.label(
                        stamp + (f" · {duration}" if duration else ""),
                    ).classes("sop-jobcard__meta mono")

            # line 3: detail + removal
            with ui.row().classes(
                "w-full items-center gap-2 sop-jobcard__actions",
            ):
                ui.button(
                    "View", icon="open_in_new",
                    on_click=lambda j=job_id: ui.navigate.to(
                        f"{self.base_path}/{j}", new_tab=True,
                    ),
                ).props("flat dense").tooltip("Open the live screen")
                if state in TERMINAL_JOB_STATES:
                    ui.space()
                    # Destructive and irreversible (the summary and the report
                    # go with it), so it carries the danger colour - Stop and
                    # Cancel batch already do, and they are the recoverable
                    # actions. Without this it rendered the same accent as
                    # "View".
                    # Neutral at rest, danger on hover: a red dot on all 20
                    # cards diluted the one colour that has to mean "this run
                    # failed". The colour still arrives before the click.
                    ui.button(
                        icon="delete", on_click=lambda j=job_id: self._delete(j),
                    ).props(
                        'flat dense color=none aria-label="Delete this job record"',
                    ).classes("sop-danger").tooltip("Remove this record")

    def _stop(self, job_id: str) -> None:
        self.job_manager.cancel_job(job_id)
        ui.notify(f"Stop requested: {job_id}")

    def _delete(self, job_id: str) -> None:
        self.job_manager.store.delete_job(job_id)
        self.refresh_sync()

    # ------------------------------------------------------------------ #
