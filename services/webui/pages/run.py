"""Live Run page: protocol readiness, launch form, board, live job screen."""

from __future__ import annotations

from pathlib import Path

from nicegui import ui

from services.config.supervisor_config_builder import build_supervisor_config
from services.webui.components.board import JobBoard
from services.webui.components.busy import busy_buttons
from services.webui.components.live_screen import LiveJobScreen
from services.webui.jobs import get_job_manager
from services.webui.state import app_state


def _save_draft(workspace: Path, text: str) -> None:
    (workspace / "protocol.md").write_text(text, encoding="utf-8")
    ui.notify(f"Saved protocol.md to {workspace.name}")
    ui.navigate.to("/run")


def _render_readiness(workspace: Path) -> bool:
    """Protocol readiness banner; returns True when protocol.md exists."""
    proto_path = workspace / "protocol.md"
    saved_text = proto_path.read_text(encoding="utf-8") if proto_path.exists() else ""
    draft = (app_state.get("protocol_md") or "").strip()

    if proto_path.exists():
        ui.label(f"✅ protocol.md ready in {workspace.name}").classes(
            "text-sm text-[#3fb950]",
        )
        if draft and draft != saved_text.strip():
            ui.label(
                "⚠️ Current Protocol Wizard draft differs from saved protocol.md.",
            ).classes("text-xs text-[#e3b341]")
            ui.button(
                "Save current draft",
                on_click=lambda: _save_draft(workspace, app_state.get("protocol_md")),
            ).props("dense")
        return True

    if draft:
        ui.label("No saved protocol.md yet — the wizard draft can be reused.").classes(
            "text-xs text-[#e3b341]",
        )
        ui.button(
            "Save current draft as protocol.md",
            on_click=lambda: _save_draft(workspace, draft),
        ).props("dense")
        return False

    ui.label(
        "No protocol.md found for this workspace — create one in the Wizard.",
    ).classes("text-sm text-[#f85149]")
    ui.button("Open Protocol Wizard", on_click=lambda: ui.navigate.to("/")).props(
        "dense",
    )
    return False


def _launch_run(
    workspace: Path, plan_rounds: int, scanner: bool, occam: bool, skills: bool,
) -> None:
    app_state.save()
    app_state.apply_api_config()
    config = build_supervisor_config(
        app_state.as_mapping(),
        protocol_path=workspace / "protocol.md",
        workspace=workspace,
        plan_mode_rounds=plan_rounds,
        enable_python_scanner=scanner,
        enable_occam_razor=occam,
        enable_supervisor_skills=skills,
    )
    job_id = get_job_manager().enqueue_job("run", config)
    ui.notify(f"Task launched: {job_id}")
    ui.navigate.to(f"/run/{job_id}")


def _render_launch_form(workspace: Path) -> None:
    with ui.card().classes("w-full bg-[#161b22]"):
        ui.label("Start New Task").classes("text-sm font-bold text-[#9ecbff]")
        ui.label(f"Primary workspace: {workspace}").classes("text-xs text-[#8b949e]")
        with ui.row().classes("items-end gap-4 flex-wrap"):
            plan_rounds = ui.number(
                "Plan mode rounds",
                value=int(app_state.get("plan_mode_rounds", 1)),
                min=0,
                max=10,
            )
            scanner = ui.switch(
                "Python scanner", value=bool(app_state.get("enable_python_scanner")),
            )
            occam = ui.switch(
                "Occam Razor", value=bool(app_state.get("enable_occam_razor")),
            )
            skills = ui.switch(
                "Supervisor skills",
                value=bool(app_state.get("enable_supervisor_skills")),
            ).tooltip(
                "Show the supervisor a skill catalog it may load on demand "
                "(LOAD_SKILL); judge-context only.",
            )
        async def _launch() -> None:
            # Disabling is brief here — the enqueue is fast — but it stops a
            # rapid double click from launching the same task twice before
            # the browser leaves the page.
            async with busy_buttons(launch_button):
                _launch_run(
                    workspace,
                    int(plan_rounds.value or 0),
                    bool(scanner.value),
                    bool(occam.value),
                    bool(skills.value),
                )

        launch_button = ui.button("Launch", on_click=_launch).props("color=primary")



def _save_feasibility(workspace: Path, values: dict) -> None:
    from supervisor.protocols.feasibility_gate import (
        feasibility_facts_from_kv,
        write_feasibility_facts,
    )

    facts = feasibility_facts_from_kv(**values)
    dropped = write_feasibility_facts(
        facts, workspace / "logs" / "feasibility_facts.json",
    )
    if dropped is None:
        ui.notify("Cleared feasibility facts (empty form)")
    else:
        ui.notify(f"Saved {dropped} via auto-generated schema")


def _render_feasibility_form(workspace: Path) -> None:
    import json as _json

    path = workspace / "logs" / "feasibility_facts.json"
    current: dict = {}
    if path.exists():
        try:
            current = _json.loads(path.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError):
            current = {}
    ceiling = current.get("known_ceiling") or {}

    def _int_value(value):
        from supervisor.protocols.feasibility_gate import _as_int

        parsed = _as_int(value)
        return parsed if parsed is not None else None

    with ui.card().classes("w-full bg-[#161b22]"):
        ui.label("Feasibility facts (optional)").classes("text-sm font-bold text-[#9ecbff]")
        ui.label(
            "Key-value form; saves logs/feasibility_facts.json (empty form clears it). "
            "A known_ceiling below required_gates flags an unreachable "
            "done-condition before the run starts.",
        ).classes("text-xs text-[#8b949e]")
        required = ui.number(
            "required_gates",
            value=_int_value(current.get("required_gates")),
            min=0,
        )
        ceil_count = ui.number(
            "known_ceiling count",
            value=_int_value(ceiling.get("count")),
            min=0,
        )
        ceil_of = ui.number(
            "known_ceiling of",
            value=_int_value(ceiling.get("of")),
            min=0,
        )
        blocked = ui.input(
            "blocked gates (comma-separated)",
            value=", ".join(str(g) for g in ceiling.get("blocked_gates", [])),
        )
        evidence = ui.input("evidence", value=str(ceiling.get("evidence", "")))
        notes = ui.input("notes", value=str(ceiling.get("notes", "")))
        thresholds = ui.textarea(
            "gate_thresholds (key: value per line)",
            value="\n".join(
                "{}: {}".format(k, v)
                for k, v in (current.get("gate_thresholds") or {}).items()
            ),
        )

        def _collect() -> None:
            _save_feasibility(
                workspace,
                {
                    "required_gates": required.value,
                    "ceiling_count": ceil_count.value,
                    "ceiling_of": ceil_of.value,
                    "blocked_gates": blocked.value,
                    "evidence": evidence.value,
                    "notes": notes.value,
                    "thresholds": thresholds.value,
                },
            )

        ui.button("Save feasibility facts", on_click=_collect).props("dense")


def _has_running_job() -> bool:
    manager = get_job_manager()
    return any(
        (manager.store.get_job_state(j) or {}).get("state") == "RUNNING"
        for j in manager.store.list_jobs()
    )


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
        if not status or status.get("type") != "run":
            ui.label(f"Run job {job_id} not found.").classes("text-[#f85149]")
            ui.button(
                "Back to board", on_click=lambda: ui.navigate.to("/run"),
            ).props("dense")
            return
        LiveJobScreen(
            manager,
            job_type="run",
            job_id=job_id,
            base_path="/run",
            is_evolution=False,
            running_message="Task is running in the background.",
        ).render()
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
    _render_launch_form(workspace)
    _render_feasibility_form(workspace)

    ui.label("Task Board").classes("text-lg font-bold mt-4")
    JobBoard(manager, "run", is_evolution=False)
