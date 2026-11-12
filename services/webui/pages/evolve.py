"""Self-Evolution page: meta-protocol build, launch form, board, live screen."""

from __future__ import annotations

from pathlib import Path

from nicegui import ui, run as nicegui_run

from services.webui.components.board import JobBoard
from services.webui.components.busy import busy_buttons
from services.webui.jobs import get_job_manager
from services.webui.launch import (
    launch_job,
    render_live_screen,
    require_workspace,
)
from services.webui.log_format import markdown_preview
from services.webui.state import app_state


def _build_meta_protocol(workspace: Path, goal: str, extra: str) -> str:
    from supervisor.analyzers.codebase_analyzer import snapshot_codebase
    from supervisor.protocols.meta_protocol_builder import MetaProtocolBuilder

    builder = MetaProtocolBuilder(
        model=app_state.get("supervisor_model") or "swift-1.5",
        api_key=app_state.get("openai_key") or None,
        base_url=app_state.get("base_url") or None,
    )
    return builder.build(goal, snapshot_codebase(workspace), extra_restrictions=extra)


def _write_meta(content: str, workspace: Path) -> Path:
    from supervisor.protocols.meta_protocol_builder import write_meta_protocol

    return write_meta_protocol(content, workspace)


def _launch_evolve(
    workspace: Path, occam: bool, skills: bool,
) -> None:
    job_id = launch_job(
        "evolve",
        workspace,
        protocol_name="meta_protocol.md",
        enable_occam_razor=occam,
        enable_supervisor_skills=skills,
    )
    ui.navigate.to(f"/evolve/{job_id}")


def render(job_id: str = "") -> None:
    workspace = require_workspace()
    if workspace is None:
        return

    manager = get_job_manager()
    if job_id:
        # Summary only - get_job_status here parsed the whole log file on the
        # event loop per navigation (see pages/run.py).
        render_live_screen(
            manager,
            job_id,
            job_type="evolve",
            base_path="/evolve",
            is_evolution=True,
            running_message="Evolution is running in the background.",
            not_found_label=f"Evolution job {job_id} not found.",
        )
        return

    ui.label("Self-Evolution").classes("text-2xl font-bold")

    with ui.card().classes("w-full bg-[#161b22]"):
        ui.label("Evolution Goal").classes("text-sm font-bold text-[#9ecbff]")
        goal_area = ui.textarea(
            "What should the system improve?",
            value=app_state.get("evo_goal") or "",
        ).classes("w-full mono")
        # Optional extra input: collapsed when empty, open when it carries
        # constraints so they are never silently in effect.
        with ui.expansion(
            "Extra restrictions (optional)",
            value=bool((app_state.get("evo_extra_restrictions") or "").strip()),
        ).classes("w-full").props("dense"):
            extra_area = ui.textarea(
                "Extra restrictions",
                value=app_state.get("evo_extra_restrictions") or "",
            ).classes("w-full mono")

        status_label = ui.label("").classes("text-xs text-[#8b949e]")
        meta_exp = ui.expansion("Generated meta_protocol.md").classes("w-full hidden")

        async def _generate() -> None:
            goal = (goal_area.value or "").strip()
            if not goal:
                ui.notify("Enter an evolution goal first.")
                return
            status_label.set_text("Building meta-protocol (LLM call)…")
            async with busy_buttons(build_button):
                app_state["evo_goal"] = goal_area.value or ""
                app_state["evo_extra_restrictions"] = extra_area.value or ""
                app_state.save()
                app_state.apply_api_config()
                try:
                    content = await nicegui_run.io_bound(
                        _build_meta_protocol, workspace, goal, extra_area.value or "",
                    )
                except Exception as exc:
                    status_label.set_text(f"Meta-protocol build failed: {exc}")
                    return
            _write_meta(content, workspace)
            status_label.set_text("meta_protocol.md written to workspace.")
            meta_exp.classes(remove="hidden")
            with meta_exp:
                ui.markdown(markdown_preview(content, 3000))

        build_button = ui.button(
            "Build meta-protocol", on_click=_generate,
        ).props("color=primary")

    with ui.card().classes("w-full bg-[#161b22]"):
        ui.label("Start Evolution").classes("text-sm font-bold text-[#9ecbff]")
        # Same pattern as the Run page: defaults collapsed, effective
        # settings kept visible in a live summary.
        with ui.expansion("Launch options").classes("w-full").props("dense"):
            with ui.row().classes("items-end gap-4 flex-wrap"):
                occam = ui.switch(
                    "Occam Razor",
                    value=bool(app_state.get("enable_occam_razor")),
                    on_change=lambda _e: _update_summary(),
                )
                skills = ui.switch(
                    "Supervisor skills",
                    value=bool(app_state.get("enable_supervisor_skills")),
                    on_change=lambda _e: _update_summary(),
                )
        summary = ui.label("").classes("text-xs text-[#8b949e] mono")

        def _update_summary() -> None:
            summary.set_text(
                f"occam {'on' if occam.value else 'off'} · "
                f"skills {'on' if skills.value else 'off'}",
            )

        async def _launch() -> None:
            async with busy_buttons(launch_button):
                _launch_evolve(
                    workspace,
                    bool(occam.value),
                    bool(skills.value),
                )

        with ui.row().classes("items-center gap-3"):
            launch_button = ui.button("Launch", on_click=_launch).props(
                "color=primary",
            )
        _update_summary()

    ui.label("Evolution Board").classes("text-lg font-bold mt-4")
    JobBoard(manager, "evolve", is_evolution=True)
