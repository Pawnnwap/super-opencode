"""Harvester page: pick finished runs, distil them into one clean folder."""

from __future__ import annotations

import time
from pathlib import Path

from nicegui import run as nicegui_run
from nicegui import ui

from services.config.supervisor_config_builder import build_supervisor_config
from services.jobs.harvest import (
    COMPLETED_STATES,
    DEFAULT_HARVEST_PROMPT,
    HARVEST_JOB_TYPE,
    HARVESTABLE_STATES,
    make_batch_dir,
    match_query,
    search_text,
)
from services.webui.components.board import JobBoard
from services.webui.jobs import get_job_manager, sync_max_parallel_jobs
from services.webui.launch import render_live_screen, save_and_apply_settings
from services.webui.layout import format_status_pill
from services.webui.state import app_state

_FILTERS = {
    "completed": "Completed (SUCCESS + BLOCKED)",
    "terminal": "All finished (incl. failed)",
    "SUCCESS": "SUCCESS",
    "BLOCKED": "BLOCKED",
    "FAILED": "FAILED",
    "CANCELLED": "CANCELLED",
    "ABANDONED": "ABANDONED",
}


def _filter_states(key: str) -> frozenset[str]:
    if key == "completed":
        return COMPLETED_STATES
    if key == "terminal":
        return HARVESTABLE_STATES
    return frozenset({key})


def _load_runs(manager) -> list[dict]:
    """Finished run records with a lowercased search haystack (page load)."""
    runs: list[dict] = []
    for job_id in manager.store.list_jobs():
        state = manager.store.get_job_state(job_id) or {}
        if state.get("type") != "run":
            continue
        if state.get("state") not in HARVESTABLE_STATES:
            continue
        workspace = str((state.get("config") or {}).get("workspace") or "")
        runs.append({
            "id": job_id,
            "state": state,
            "search": search_text(job_id, state),
            "workspace_exists": bool(workspace) and Path(workspace).is_dir(),
        })
    runs.sort(key=lambda run: -float(run["state"].get("updated_at") or 0))
    return runs


def _run_meta(run: dict) -> str:
    state = run["state"]
    config = state.get("config") or {}
    workspace = Path(str(config.get("workspace") or "")).name or "?"
    updated = float(state.get("updated_at") or 0)
    stamp = time.strftime("%m-%d %H:%M", time.localtime(updated)) if updated else "?"
    bits = [workspace, stamp]
    if run.get("workspace_exists") is False:
        bits.append("⚠ no workspace")
    if state.get("batch_id"):
        bits.append(f"batch {state['batch_id']}")
    report_chars = len(str(state.get("report") or ""))
    if report_chars:
        bits.append(f"report {report_chars:,}c")
    return " · ".join(bits)


def _default_prompt() -> str:
    return str(app_state.get("harvest_prompt") or "").strip() or DEFAULT_HARVEST_PROMPT


def _default_location() -> str:
    saved = str(app_state.get("harvest_dir") or "").strip()
    if saved:
        return saved
    workspace = str(app_state.get("workspace") or "").strip()
    return str(Path(workspace) / "harvest") if workspace else ""


class HarvesterPanel:
    """Selection list + prompt/location form for one harvest launch."""

    def __init__(self, manager, runs: list[dict]):
        self.manager = manager
        self.runs = runs
        self.selected: set[str] = set()
        self._build()

    # -------------------------------------------------------------- build --

    def _build(self) -> None:
        with ui.card().classes("w-full bg-[#161b22]"):
            ui.label("Select finished runs").classes("text-sm font-bold text-[#9ecbff]")
            ui.label(
                "Type a string to auto-select matching runs (id, workspace, "
                "batch or final report), or tick runs by hand.",
            ).classes("text-xs text-[#8b949e]")
            with ui.row().classes("w-full items-end gap-3 flex-wrap"):
                self.match_input = ui.input(
                    "Match", value="", on_change=self._on_query,
                ).classes("mono w-80").props("clearable debounce=250")
                self.state_filter = ui.select(
                    _FILTERS, value="completed", label="State",
                    on_change=self._on_filter,
                ).classes("w-64")
                ui.button("Select shown", on_click=self._select_shown).props(
                    "dense outline",
                )
                ui.button("Clear", on_click=self._clear).props("dense flat")
            self.count_label = ui.label("").classes("text-xs text-[#8b949e]")
            if self.runs:
                with ui.scroll_area().classes("w-full").style("height: 21rem"):
                    self.list_col = ui.column().classes("w-full gap-0")
            else:
                ui.label(
                    "No finished runs in the store yet — run something first.",
                ).classes("text-sm text-[#8b949e]")
                self.list_col = None

        with ui.card().classes("w-full bg-[#161b22]"):
            ui.label("Harvest output").classes("text-sm font-bold text-[#9ecbff]")
            engine = app_state.get("engine") or "opencode"
            model = app_state.get("opencode_model") or "(not set)"
            ui.label(f"Agent: {engine} · {model}").classes(
                "text-xs text-[#8b949e]",
            )
            self.prompt_input = ui.textarea(
                "Harvest prompt (applied to each run)", value=_default_prompt(),
            ).classes("w-full mono").props("autogrow").style("min-height: 5rem")
            self.location_input = ui.input(
                "Harvest location", value=_default_location(),
            ).classes("w-full mono")
            ui.label(
                "A fresh harvest_<timestamp>/ folder is created inside this "
                "location. Each run gets its own subfolder with report.md, "
                "run.json and harvest.md; the root gets INDEX.md and a batch "
                "HARVEST.md.",
            ).classes("text-xs text-[#8b949e]")
            with ui.row().classes("items-center gap-3"):
                self.launch_button = ui.button(
                    "Harvest selected", on_click=self._launch,
                ).props("color=primary")
                self.launch_status = ui.label("").classes("text-xs text-[#8b949e]")
        self._rebuild()

    # ------------------------------------------------------------ filters --

    def _visible(self) -> list[dict]:
        states = _filter_states(str(self.state_filter.value or "completed"))
        query = str(self.match_input.value or "")
        return [
            run
            for run in self.runs
            if run["state"].get("state") in states
            and match_query(query, run["search"])
        ]

    def _on_query(self, _event) -> None:
        query = str(self.match_input.value or "").strip()
        if query:
            self.selected = {run["id"] for run in self._visible()}
        self._rebuild()

    def _on_filter(self, _event) -> None:
        if str(self.match_input.value or "").strip():
            self.selected = {run["id"] for run in self._visible()}
        self._rebuild()

    def _select_shown(self) -> None:
        self.selected.update(run["id"] for run in self._visible())
        self._rebuild()

    def _clear(self) -> None:
        self.selected.clear()
        self._rebuild()

    def _toggle(self, job_id: str, event) -> None:
        if event.value:
            self.selected.add(job_id)
        else:
            self.selected.discard(job_id)
        self._update_counts()

    # ------------------------------------------------------------- render --

    def _update_counts(self) -> None:
        shown = self._visible()
        self.count_label.set_text(
            f"{len(shown)} shown · {len(self.selected)} selected · "
            f"{len(self.runs)} finished run(s)",
        )
        self.launch_button.set_text(
            f"Harvest selected ({len(self.selected)})"
            if self.selected
            else "Harvest selected",
        )

    def _rebuild(self) -> None:
        self._update_counts()
        if self.list_col is None:
            return
        self.list_col.clear()
        with self.list_col:
            for run in self._visible():
                job_id = run["id"]
                state = str(run["state"].get("state") or "UNKNOWN")
                with ui.row().classes(
                    "w-full items-center gap-2 flex-nowrap py-1 "
                    "border-b border-[#21262d]",
                ):
                    ui.checkbox(
                        value=job_id in self.selected,
                        on_change=lambda e, jid=job_id: self._toggle(jid, e),
                    ).props("dense").classes("shrink-0")
                    ui.label(job_id).classes("mono text-xs w-32 shrink-0")
                    ui.html(format_status_pill(state)).classes("shrink-0")
                    ui.label(_run_meta(run)).classes(
                        "text-xs text-[#8b949e] truncate grow",
                    )
            if not self._visible():
                ui.label("No matching runs.").classes("text-xs text-[#8b949e]")

    # ------------------------------------------------------------- launch --

    def _launch(self) -> None:
        job_ids = [run["id"] for run in self.runs if run["id"] in self.selected]
        if not job_ids:
            ui.notify("Select at least one finished run.", type="warning")
            return
        location = str(self.location_input.value or "").strip()
        if not location:
            ui.notify("Set a harvest location.", type="warning")
            return
        prompt = (
            str(self.prompt_input.value or "").strip() or DEFAULT_HARVEST_PROMPT
        )
        try:
            batch = make_batch_dir(location)
        except OSError as exc:
            ui.notify(f"Cannot create harvest folder: {exc}", type="negative")
            return

        app_state["harvest_prompt"] = prompt
        app_state["harvest_dir"] = location
        save_and_apply_settings()
        sync_max_parallel_jobs()
        config = build_supervisor_config(
            app_state.as_mapping(),
            protocol_path=batch / "protocol.md",
            workspace=batch,
            harvest_job_ids=tuple(job_ids),
            harvest_prompt=prompt,
            harvest_dir=str(batch),
        )
        try:
            job_id = self.manager.enqueue_job(HARVEST_JOB_TYPE, config)
        except ValueError as exc:
            ui.notify(str(exc), type="negative")
            try:
                batch.rmdir()
            except OSError:
                pass
            return
        ui.notify(f"Harvest launched: {job_id} → {batch}")
        ui.navigate.to(f"/harvest/{job_id}")


async def render(job_id: str = "") -> None:
    manager = get_job_manager()
    if job_id:
        render_live_screen(
            manager,
            job_id,
            job_type=HARVEST_JOB_TYPE,
            base_path="/harvest",
            is_evolution=False,
            running_message="Harvest is running in the background.",
            not_found_label=f"Harvest job {job_id} not found.",
        )
        return

    ui.label("Harvester").classes("text-2xl font-bold")
    ui.label(
        "Collect finished runs into one clean folder: one subfolder per run, "
        "a root index and a batch summary.",
    ).classes("text-sm text-[#8b949e]")
    runs = await nicegui_run.io_bound(_load_runs, manager)
    HarvesterPanel(manager, runs)

    ui.label("Harvest Board").classes("text-lg font-bold mt-4")
    JobBoard(
        manager, HARVEST_JOB_TYPE, is_evolution=False, base_path="/harvest",
    )
