"""Self-Evolution page: meta-protocol build, launch form, board, live screen."""

from __future__ import annotations

from pathlib import Path

from nicegui import ui, run as nicegui_run

from services.config.supervisor_config_builder import build_supervisor_config
from services.webui.components.board import JobBoard
from services.webui.components.busy import busy_buttons
from services.webui.components.live_screen import LiveJobScreen
from services.webui.jobs import get_job_manager
from services.webui.state import app_state


def _snapshot_digest(workspace: Path) -> str:
    from supervisor.analyzers.codebase_analyzer import snapshot_codebase

    snapshot = snapshot_codebase(workspace)
    return snapshot.digest_for_prompt(max_files=15)


def _build_meta_protocol(workspace: Path, goal: str, extra: str) -> str:
    from supervisor.analyzers.codebase_analyzer import snapshot_codebase
    from supervisor.protocols.meta_protocol_builder import MetaProtocolBuilder

    builder = MetaProtocolBuilder(
        model=app_state.get("supervisor_model") or "gpt-4o",
        api_key=app_state.get("openai_key") or None,
        base_url=app_state.get("base_url") or None,
    )
    return builder.build(goal, snapshot_codebase(workspace), extra_restrictions=extra)


def _write_meta(content: str, workspace: Path) -> Path:
    from supervisor.protocols.meta_protocol_builder import write_meta_protocol

    return write_meta_protocol(content, workspace)


def _launch_evolve(
    workspace: Path, scanner: bool, occam: bool, skills: bool,
) -> None:
    app_state.save()
    app_state.apply_api_config()
    config = build_supervisor_config(
        app_state.as_mapping(),
        protocol_path=workspace / "meta_protocol.md",
        workspace=workspace,
        enable_python_scanner=scanner,
        enable_occam_razor=occam,
        enable_supervisor_skills=skills,
    )
    job_id = get_job_manager().enqueue_job("evolve", config)
    ui.notify(f"Evolution launched: {job_id}")
    ui.navigate.to(f"/evolve/{job_id}")


def render(job_id: str = "") -> None:
    workspace_raw = app_state.get("workspace") or ""
    if not workspace_raw or not Path(workspace_raw).exists():
        ui.label("Set a valid workspace path in the Protocol Wizard first.").classes(
            "text-[#f85149]",
        )
        ui.button("Open Wizard", on_click=lambda: ui.navigate.to("/")).props("dense")
        return
    workspace = Path(workspace_raw)

    manager = get_job_manager()
    if job_id:
        status = manager.get_job_status(job_id)
        if not status or status.get("type") != "evolve":
            ui.label(f"Evolution job {job_id} not found.").classes("text-[#f85149]")
            ui.button(
                "Back to board", on_click=lambda: ui.navigate.to("/evolve"),
            ).props("dense")
            return
        LiveJobScreen(
            manager,
            job_type="evolve",
            job_id=job_id,
            base_path="/evolve",
            is_evolution=True,
            running_message="Evolution is running in the background.",
        ).render()
        return

    ui.label("Self-Evolution").classes("text-2xl font-bold")

    with ui.card().classes("w-full bg-[#161b22]"):
        ui.label("Evolution Goal").classes("text-sm font-bold text-[#9ecbff]")
        goal_area = ui.textarea(
            "What should the system improve?",
            value=app_state.get("evo_goal") or "",
        ).classes("w-full mono")
        extra_area = ui.textarea(
            "Extra restrictions (optional)",
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
            app_state["protocol_md"] = content
            status_label.set_text("meta_protocol.md written to workspace.")
            meta_exp.classes(remove="hidden")
            with meta_exp:
                ui.markdown(content[:3000])

        build_button = ui.button(
            "Build meta-protocol", on_click=_generate,
        ).props("color=primary")

    with ui.card().classes("w-full bg-[#161b22]"):
        ui.label("Start Evolution").classes("text-sm font-bold text-[#9ecbff]")
        with ui.row().classes("items-end gap-4 flex-wrap"):
            scanner = ui.switch(
                "Python scanner", value=bool(app_state.get("enable_python_scanner")),
            )
            occam = ui.switch(
                "Occam Razor", value=bool(app_state.get("enable_occam_razor")),
            )
            skills = ui.switch(
                "Supervisor skills",
                value=bool(app_state.get("enable_supervisor_skills")),
            )
        async def _launch() -> None:
            async with busy_buttons(launch_button):
                _launch_evolve(
                    workspace,
                    bool(scanner.value),
                    bool(occam.value),
                    bool(skills.value),
                )

        launch_button = ui.button("Launch", on_click=_launch).props("color=primary")

    ui.label("Evolution Board").classes("text-lg font-bold mt-4")
    JobBoard(manager, "evolve", is_evolution=True)
