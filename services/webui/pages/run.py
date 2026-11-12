"""Live Run page: protocol readiness, launch form, board, live job screen."""

from __future__ import annotations

from pathlib import Path

from nicegui import ui

from services.webui.components.board import JobBoard
from services.webui.components.busy import busy_buttons
from services.webui.components.hownow import HowNowPanel
from services.webui.jobs import get_job_manager, sync_max_parallel_jobs
from services.webui.launch import (
    launch_job,
    render_live_screen,
    require_workspace,
    save_and_apply_settings,
)
from services.webui.layout import active_jobs
from services.webui.state import app_state


def _render_readiness(workspace: Path) -> bool:
    """Protocol readiness banner; returns True when a protocol file exists."""
    from supervisor.protocols.protocol import resolve_protocol_path

    proto_path = resolve_protocol_path(workspace)
    if proto_path.is_file():
        ui.label(f"✅ {proto_path.name} ready in {workspace.name}").classes(
            "text-sm text-[#3fb950]",
        )
        return True

    ui.label(
        "No protocol found for this workspace — create one in the Wizard.",
    ).classes("text-sm text-[#f85149]")
    ui.button("Open Protocol Wizard", on_click=lambda: ui.navigate.to("/")).props(
        "dense",
    )
    return False


def _launch_run(
    workspace: Path, plan_rounds: int, occam: bool, skills: bool,
) -> str:
    from supervisor.protocols.protocol import resolve_protocol_path

    return launch_job(
        "run",
        workspace,
        protocol_name=resolve_protocol_path(workspace).name,
        plan_mode_rounds=plan_rounds,
        enable_occam_razor=occam,
        enable_supervisor_skills=skills,
    )


def _render_launch_form(workspace: Path) -> None:
    with ui.card().classes("w-full bg-[#161b22]"):
        ui.label("Start New Task").classes("text-sm font-bold text-[#9ecbff]")
        ui.label(f"Primary workspace: {workspace}").classes("text-xs text-[#8b949e]")
        # Laid out flat on purpose: these are the settings a launch depends on,
        # and hiding them behind a disclosure made the effective options easy
        # to miss. The live summary keeps the values visible even so.
        with ui.row().classes("items-end gap-4 flex-wrap"):
            plan_rounds = ui.number(
                "Plan mode rounds",
                value=int(app_state.get("plan_mode_rounds", 1)),
                min=0,
                max=10,
                on_change=lambda _e: _update_summary(),
            )
            occam = ui.switch(
                "Occam Razor",
                value=bool(app_state.get("enable_occam_razor")),
                on_change=lambda _e: _update_summary(),
            )
            skills = ui.switch(
                "Supervisor skills",
                value=bool(app_state.get("enable_supervisor_skills")),
                on_change=lambda _e: _update_summary(),
            ).tooltip(
                "Show the supervisor a skill catalog it may load on demand "
                "(LOAD_SKILL); judge-context only.",
            )
        with ui.row().classes("items-end gap-4 flex-wrap"):
            replicas = ui.number(
                "Parallel instances",
                value=1,
                min=1,
                max=16,
                on_change=lambda _e: _update_summary(),
            ).tooltip(
                ">1 clones this workspace N times (one isolated study per "
                "copy) and launches them as one batch.",
            )
            init_mode = ui.select(
                {"copy": "复制工作区（排除产物）", "protocol": "仅 protocol.md"},
                value="copy",
                label="Replica init",
                on_change=lambda _e: _update_summary(),
            ).tooltip(
                "How each parallel instance's workspace is seeded.",
            )
        summary = ui.label("").classes("text-xs text-[#8b949e] mono")

        def _update_summary() -> None:
            count = int(replicas.value or 1)
            bits = [
                f"{count} instance{'s' if count > 1 else ''}",
                f"plan rounds {int(plan_rounds.value or 0)}",
                f"occam {'on' if occam.value else 'off'}",
                f"skills {'on' if skills.value else 'off'}",
            ]
            if count > 1:
                bits.append(f"init: {init_mode.value}")
            summary.set_text(" · ".join(bits))

        async def _launch() -> None:
            # Disabling is brief here — the enqueue is fast — but it stops a
            # rapid double click from launching the same task twice before
            # the browser leaves the page.
            async with busy_buttons(launch_button):
                count = int(replicas.value or 1)
                if count > 1:
                    from services.webui.batch_launch import launch_study_batch

                    save_and_apply_settings()
                    sync_max_parallel_jobs()
                    try:
                        _batch_id, job_ids, _cfgs = launch_study_batch(
                            app_state.as_mapping(),
                            get_job_manager(),
                            workspace,
                            count,
                            init_mode=str(init_mode.value or "copy"),
                            plan_mode_rounds=int(plan_rounds.value or 0),
                            enable_occam_razor=bool(occam.value),
                            enable_supervisor_skills=bool(skills.value),
                        )
                    # Every failure mode here is a filesystem failure
                    # (``shutil.Error`` from a file a running tool holds open,
                    # disk full, path too long), not a ValueError. Catching only
                    # ValueError let those escape into NiceGUI's async task
                    # where they vanish; busy_buttons' finally then re-enabled
                    # the button, so the operator saw a Launch that "did
                    # nothing". Surface the reason instead.
                    except Exception as exc:  # noqa: BLE001 - must never be silent
                        ui.notify(
                            f"Batch launch failed: {type(exc).__name__}: "
                            f"{str(exc)[:600]}",
                            type="negative",
                        )
                        return
                    ui.notify(f"Batch launched: {len(job_ids)} instance(s)")
                    ui.navigate.to(f"/run/{job_ids[0]}")
                else:
                    job_id = _launch_run(
                        workspace,
                        int(plan_rounds.value or 0),
                        bool(occam.value),
                        bool(skills.value),
                    )
                    ui.navigate.to(f"/run/{job_id}")

        with ui.row().classes("items-center gap-3"):
            launch_button = ui.button("Launch", on_click=_launch).props(
                "color=primary",
            )
        _update_summary()



def _has_running_job() -> bool:
    """Any live work in the store: running or queued for a slot."""
    return bool(active_jobs(get_job_manager()))


def render(job_id: str = "") -> None:
    workspace = require_workspace()
    if workspace is None:
        return

    manager = get_job_manager()
    if job_id:
        # Summary only: this check used get_job_status, which parses the job's
        # WHOLE log file just to read type/state - a multi-MB parse on the
        # event loop per detail-page navigation. LiveJobScreen re-fetches the
        # capped tail it actually renders.
        render_live_screen(
            manager,
            job_id,
            job_type="run",
            base_path="/run",
            running_message="Task is running in the background.",
            not_found_label=f"Run job {job_id} not found.",
        )
        return

    if not app_state.tests_ok() and not _has_running_job():
        ui.label(
            "🔒 Live Run is locked — pass the connectivity tests on the "
            "Protocol Wizard page first.",
        ).classes("text-sm text-[#e3b341]")
        ui.button("Open Wizard", on_click=lambda: ui.navigate.to("/")).props("dense")
        return

    ui.label("Live Run").classes("text-2xl font-bold")
    _render_readiness(workspace)
    # Launch parameters first, then the on-demand sweep, then the board.
    _render_launch_form(workspace)
    HowNowPanel()

    ui.label("Task Board").classes("text-lg font-bold mt-4")
    JobBoard(manager, "run")
