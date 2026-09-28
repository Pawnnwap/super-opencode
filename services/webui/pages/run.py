"""Live Run page: protocol readiness, launch form, board, live job screen."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from nicegui import ui, run as nicegui_run

from services.config.supervisor_config_builder import build_supervisor_config
from services.webui.components.board import JobBoard
from services.webui.components.busy import busy_buttons
from services.webui.components.live_screen import LiveJobScreen
from services.webui.jobs import get_job_manager
from services.webui.state import app_state
from supervisor.utils.llm_stream import GenerationCancelled

# Stop event of the feasibility draft currently in flight, if any, so the
# Cancel button can abort it.
_cancel_holder: dict = {}


def _cancel_draft(status_label) -> None:
    event = _cancel_holder.get("event")
    if event is None:
        ui.notify("No draft is running.")
        return
    event.set()
    status_label.set_text("Cancelling draft…")


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



def _save_feasibility(
    workspace: Path, rows: list[dict], notes: str, pending: list[dict],
) -> None:
    from supervisor.protocols.feasibility_gate import (
        gates_to_facts,
        write_feasibility_facts,
    )

    facts = gates_to_facts(rows, notes)
    if facts and pending:
        facts["pending_blocked"] = pending
    write_feasibility_facts(facts, workspace / "logs" / "feasibility_facts.json")
    if facts is None:
        ui.notify("Cleared feasibility facts (no gates)")
    else:
        ui.notify(f"Saved {len(facts['gates'])} gate(s)")


_STATUS_LABELS = {
    "open": "open — untested",
    "passing": "passing — passed before",
    "blocked": "blocked — never passed",
}


def _draft_facts(workspace: Path, on_progress=None, stop_event=None) -> dict:
    """LLM draft of feasibility facts (runs in the io thread pool)."""
    from supervisor.protocols.feasibility_wizard import (
        FeasibilityWizard,
        collect_evidence,
    )

    protocol = workspace / "protocol.md"
    protocol_text = (
        protocol.read_text(encoding="utf-8", errors="replace")
        if protocol.exists()
        else ""
    )
    wizard = FeasibilityWizard(
        model=app_state.get("supervisor_model") or "gpt-4o",
        api_key=app_state.get("openai_key"),
        base_url=app_state.get("base_url") or None,
    )
    return wizard.draft(
        protocol_text,
        collect_evidence(workspace),
        on_progress=on_progress,
        stop_event=stop_event,
    )


def _render_feasibility_form(workspace: Path) -> None:
    import json as _json

    from supervisor.protocols.feasibility_gate import (
        check_feasibility,
        gates_to_facts,
        parse_gates,
    )
    from supervisor.protocols.feasibility_wizard import (
        facts_to_gate_rows,
        legacy_facts_to_gate_rows,
        workspace_sources,
    )

    path = workspace / "logs" / "feasibility_facts.json"
    current: dict = {}
    if path.exists():
        try:
            current = _json.loads(path.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError):
            current = {}
    legacy_file = bool(current) and not parse_gates(current)
    rows: list[dict] = (
        legacy_facts_to_gate_rows(current)
        if legacy_file
        else facts_to_gate_rows(current)
    )
    pending: list[dict] = [
        p for p in (current.get("pending_blocked") or [])
        if isinstance(p, dict) and p.get("gate")
    ]

    sources = workspace_sources(workspace)
    with ui.card().classes("w-full bg-[#161b22]"):
        ui.label("Feasibility gate (optional)").classes(
            "text-sm font-bold text-[#9ecbff]",
        )
        ui.label(
            "Define what DONE means, gate by gate: one row per acceptance "
            "criterion — its name, its exact pass condition, and what "
            "earlier attempts showed. Any gate marked blocked flags the "
            "goal unreachable before the run starts.",
        ).classes("text-xs text-[#8b949e]")
        if sources["protocol_found"]:
            gate_hint = sources["target_gates"]
            gate_note = (
                f"protocol.md found — {gate_hint} numbered TARGET item(s)"
                if gate_hint
                else "protocol.md found (no numbered TARGET items detected)"
            )
        else:
            gate_note = "no protocol.md in this workspace yet"
        artifact_note = (
            ", ".join(sources["artifacts"])
            if sources["artifacts"]
            else "no prior-attempt artifacts found"
        )
        ui.label(
            f"In {workspace.name}: {gate_note}; prior-attempt evidence: {artifact_note}.",
        ).classes("text-xs mono text-[#8b949e]")

        # Drift guard: saved refs must still match the current TARGET.
        from supervisor.protocols.gate_rubric import validate_refs

        drift = validate_refs(
            parse_gates(gates_to_facts(rows) or {}) or (),
            sources["target_gates"],
        )
        if drift:
            ui.label(
                "⚠ Protocol changed since these gates were saved: "
                + "; ".join(drift)
                + " — re-generate or fix the refs.",
            ).classes("text-xs text-[#e3b341]")

        # Operator confirmation for blocked proposals from run-end harvests.
        pending_container = ui.column().classes("w-full gap-1")

        def _rebuild_pending() -> None:
            pending_container.clear()
            with pending_container:
                for entry in list(pending):
                    name = str(entry.get("gate", ""))
                    streak = entry.get("streak", "?")
                    with ui.row().classes("w-full items-center gap-2 flex-nowrap"):
                        ui.label(
                            f"⚠ {name} failed {streak} consecutive run(s) — "
                            "mark it blocked?",
                        ).classes("text-xs text-[#e3b341] grow")

                        def _accept(_e, e_entry=entry, e_name=name) -> None:
                            for row in rows:
                                if row.get("name") == e_name:
                                    row["status"] = "blocked"
                                    row["evidence"] = str(
                                        e_entry.get("evidence", ""),
                                    ) or row.get("evidence", "")
                            pending.remove(e_entry)
                            _rebuild_pending()
                            _rebuild_rows()
                            _refresh_verdict()

                        def _dismiss(_e, e_entry=entry) -> None:
                            pending.remove(e_entry)
                            _rebuild_pending()

                        ui.button("Mark blocked", on_click=_accept).props(
                            "dense outline color=warning",
                        )
                        ui.button("Dismiss", on_click=_dismiss).props(
                            "flat dense",
                        )

        with ui.expansion("What do these fields mean?").classes("w-full").props(
            "dense",
        ):
            for line in (
                "A gate is one numbered acceptance criterion in this workspace's "
                'protocol.md TARGET section (e.g. "3. Pass gate corr").',
                "Pass condition — the exact test that defines acceptance for "
                "that gate, shown to the agent and judge so \"pass\" means the "
                "same thing to everyone.",
                "Status: open = not tested yet; passing = satisfied in at "
                "least one earlier attempt; blocked = failed in every "
                "attempt so far.",
                "Any blocked gate makes the done condition unreachable; the "
                "supervisor says so up front instead of letting the agent "
                "rediscover it over many attempts.",
                "Saved to logs/feasibility_facts.json in this workspace; "
                "removing every row clears it. Sources for statuses are "
                "recorded automatically when prior-attempt artifacts exist.",
            ):
                ui.label(f"• {line}").classes("text-xs text-[#8b949e]")

        if legacy_file:
            ui.label(
                "Loaded an older count-based facts file as rows — review and "
                "Save to upgrade it to the gate-list format.",
            ).classes("text-xs text-[#e3b341]")

        def _refresh_verdict() -> None:
            assessment = check_feasibility(gates_to_facts(rows, notes.value))
            icon = "✅" if assessment.reachable else "🚫"
            color = "#3fb950" if assessment.reachable else "#f85149"
            verdict_label.set_text(f"{icon} {assessment.reason}")
            verdict_label.style(f"color: {color}")

        def _edit(row: dict, key: str):
            def _handle(e) -> None:
                row[key] = e.value
                _refresh_verdict()

            return _handle

        def _rebuild_rows() -> None:
            rows_container.clear()
            with rows_container:
                if not rows:
                    ui.label(
                        "No gates yet — add one manually or Generate them "
                        "from the protocol.",
                    ).classes("text-xs text-[#8b949e]")
                for index, row in enumerate(rows):
                    with ui.row().classes("w-full gap-2 items-center flex-nowrap"):
                        ui.input(
                            "gate name",
                            value=row.get("name", ""),
                            on_change=_edit(row, "name"),
                        ).classes("w-44 mono shrink-0").props("dense")
                        # autogrow textarea: long conditions wrap and stay
                        # readable instead of scrolling off one cramped line
                        ui.textarea(
                            "pass condition",
                            value=row.get("definition", ""),
                            on_change=_edit(row, "definition"),
                        ).classes("grow mono").props(
                            "dense autogrow",
                        ).style("min-height: 2.3rem")
                        ui.select(
                            _STATUS_LABELS,
                            value=row.get("status", "open"),
                            on_change=_edit(row, "status"),
                        ).classes("w-56 shrink-0").props("dense")
                        check = row.get("check")
                        if isinstance(check, dict) and check.get("kind"):
                            import json as _json_chip

                            ui.chip(
                                f"⚙ {check.get('kind')}",
                            ).props("dense square outline").tooltip(
                                "Machine-checked gate (deterministic; the "
                                "judge is never asked): "
                                + _json_chip.dumps(check, ensure_ascii=False),
                            )

                        def _remove(_e, r_index=index) -> None:
                            rows.pop(r_index)
                            _rebuild_rows()
                            _refresh_verdict()

                        ui.button(icon="delete", on_click=_remove).props(
                            "flat dense",
                        ).tooltip("Remove this gate")

        rows_container = ui.column().classes("w-full gap-2")
        _rebuild_rows()
        _rebuild_pending()

        def _add_row() -> None:
            rows.append(
                {"name": "", "definition": "", "status": "open", "evidence": ""},
            )
            _rebuild_rows()
            _refresh_verdict()

        ui.button("Add gate", on_click=_add_row).props("dense outline")

        verdict_label = ui.label("").classes("text-xs mono")
        notes = ui.input(
            "Overall notes (optional)", value=str(current.get("notes", "")),
        ).classes("w-full")
        notes.on_value_change(lambda _e: _refresh_verdict())
        _refresh_verdict()

        gen_status = ui.label("").classes("text-xs text-[#8b949e]")

        def _collect() -> None:
            _save_feasibility(workspace, rows, notes.value, pending)

        async def _generate() -> None:
            if not (workspace / "protocol.md").exists():
                gen_status.set_text(
                    "No protocol.md in this workspace — nothing to derive gates from.",
                )
                return
            app_state.save()
            app_state.apply_api_config()
            stop_event = threading.Event()
            _cancel_holder["event"] = stop_event
            started = time.monotonic()

            def _on_progress(content_chars: int, reasoning_chars: int, _chunks: int) -> None:
                elapsed = time.monotonic() - started
                if reasoning_chars and not content_chars:
                    gen_status.set_text(
                        f"Thinking… {reasoning_chars:,} reasoning chars "
                        f"({elapsed:.0f}s) — Cancel if this drags",
                    )
                elif content_chars:
                    gen_status.set_text(
                        f"Writing… {content_chars:,} chars ({elapsed:.0f}s)",
                    )

            gen_status.set_text("Generating gate draft (LLM)…")
            cancel_button.set_visibility(True)
            try:
                async with busy_buttons(gen_button):
                    try:
                        facts = await nicegui_run.io_bound(
                            _draft_facts,
                            workspace,
                            on_progress=_on_progress,
                            stop_event=stop_event,
                        )
                    except GenerationCancelled:
                        gen_status.set_text("Draft cancelled.")
                        return
                    except Exception as exc:  # noqa: BLE001 — surface, keep rows
                        gen_status.set_text(f"Draft failed: {str(exc)[:200]}")
                        return
            finally:
                _cancel_holder["event"] = None
                cancel_button.set_visibility(False)
            drafted = facts_to_gate_rows(facts)
            if not drafted:
                gen_status.set_text("Draft contained no gates — nothing filled.")
                return
            rows[:] = drafted
            _rebuild_rows()
            _refresh_verdict()
            blocked = sum(1 for r in rows if r["status"] == "blocked")
            gen_status.set_text(
                f"Drafted {len(rows)} gate(s) ({blocked} blocked) — review, "
                "edit, then Save.",
            )

        with ui.row().classes("gap-3"):
            gen_button = ui.button(
                "Generate gates from protocol (LLM)", on_click=_generate,
            ).props("dense outline").tooltip(
                "The supervisor model transcribes one gate per TARGET item, "
                "with statuses only where prior-attempt artifacts evidence "
                "them; it never invents results.",
            )
            cancel_button = ui.button(
                "Cancel",
                on_click=lambda: _cancel_draft(gen_status),
            ).props("dense outline color=red")
            cancel_button.set_visibility(False)
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
