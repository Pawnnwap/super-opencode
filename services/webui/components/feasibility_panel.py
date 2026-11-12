"""Feasibility gate panel - draft, review, and auto-save acceptance gates.

Gates sit with protocol generation in the wizard: every edit, add, remove,
or generated draft writes ``feasibility_facts.json`` immediately (no Save
button), exactly like the protocol draft auto-saves. The Live Run page is
reserved for run parameters.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

from nicegui import run as nicegui_run
from nicegui import ui

from services.webui.components.busy import CancellableCall, busy_buttons
from services.webui.state import app_state
from supervisor.utils.llm_stream import GenerationCancelled

# One in-flight draft; the Cancel button aborts it.
_draft_call = CancellableCall()

_STATUS_LABELS = {
    "open": "open — untested",
    "passing": "passing — passed before",
    "blocked": "blocked — never passed",
}


def _read_facts(workspace: Path) -> dict:
    from supervisor.protocols.feasibility_gate import default_facts_path

    path = default_facts_path(workspace)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return {}


def draft(
    workspace: Path, protocol_text: str, on_progress=None, stop_event=None,
) -> dict:
    """LLM draft of feasibility facts (runs in the io thread pool)."""
    from supervisor.protocols.feasibility_wizard import (
        FeasibilityWizard,
        collect_evidence,
    )

    wizard = FeasibilityWizard(
        model=app_state.get("supervisor_model") or "swift-1.5",
        api_key=app_state.get("openai_key"),
        base_url=app_state.get("base_url") or None,
    )
    return wizard.draft(
        protocol_text,
        collect_evidence(workspace),
        on_progress=on_progress,
        stop_event=stop_event,
    )


def draft_and_save(workspace: Path, protocol_text: str) -> int:
    """Draft gates and write the facts file; returns the gate count.

    ``pending_blocked`` proposals from run-end harvests are preserved: they
    are operator decisions, not transcription output.
    """
    from supervisor.protocols.feasibility_gate import (
        default_facts_path,
        gates_to_facts,
        write_feasibility_facts,
    )
    from supervisor.protocols.feasibility_wizard import facts_to_gate_rows

    rows = facts_to_gate_rows(draft(workspace, protocol_text))
    facts = gates_to_facts(rows)
    if facts:
        pending = [
            item for item in (_read_facts(workspace).get("pending_blocked") or [])
            if isinstance(item, dict)
        ]
        if pending:
            facts["pending_blocked"] = pending
    write_feasibility_facts(facts, default_facts_path(workspace))
    return len(rows)


def render(workspace: Path, protocol_text: str | Callable[[], str] = "") -> None:
    """Gate editor bound to *workspace*; every change auto-saves.

    ``protocol_text`` may be a callable so the draft button always derives
    from the live protocol editor, not a stale render-time capture.
    """
    from supervisor.protocols.feasibility_gate import (
        check_feasibility,
        default_facts_path,
        gates_to_facts,
        parse_gates,
        write_feasibility_facts,
    )
    from supervisor.protocols.feasibility_wizard import (
        count_target_gates,
        facts_to_gate_rows,
        legacy_facts_to_gate_rows,
        workspace_sources,
    )
    from supervisor.protocols.gate_rubric import validate_refs

    def _current_text() -> str:
        if callable(protocol_text):
            return str(protocol_text() or "").strip()
        return str(protocol_text or "").strip()

    current = _read_facts(workspace)
    legacy_file = bool(current) and not parse_gates(current)
    rows: list[dict] = (
        legacy_facts_to_gate_rows(current)
        if legacy_file
        else facts_to_gate_rows(current)
    )
    pending: list[dict] = [
        item for item in (current.get("pending_blocked") or [])
        if isinstance(item, dict) and item.get("gate")
    ]

    sources = workspace_sources(workspace)
    draft_text = _current_text()
    target_gates = count_target_gates(draft_text) if draft_text else sources["target_gates"]

    from services.webui.ui_state import state_expansion

    gate_exp = state_expansion(
        "feasibility.gates",
        "Feasibility gates",
        # Proposals need attention: open on the first render that has them.
        # Afterwards the operator's toggle wins (the panel is inside a
        # @ui.refreshable section, so a rebuild must not fold it).
        default=bool(pending),
    ).classes("w-full bg-[#161b22]")
    with gate_exp:
        ui.label(
            "One row per acceptance criterion: what DONE means, gate by gate. "
            "Every change is saved to feasibility_facts.json automatically; "
            "any blocked gate marks the goal unreachable before the run starts.",
        ).classes("text-xs text-[#8b949e]")

        if draft_text:
            gate_note = (
                f"protocol draft — {target_gates} numbered TARGET item(s)"
                if target_gates
                else "protocol draft (no numbered TARGET items detected)"
            )
        elif sources["protocol_found"]:
            gate_note = (
                f"protocol.md — {target_gates} numbered TARGET item(s)"
                if target_gates
                else "protocol.md (no numbered TARGET items detected)"
            )
        else:
            gate_note = "no protocol.md in this workspace yet"
        artifact_note = (
            ", ".join(sources["artifacts"])
            if sources["artifacts"]
            else "no prior-attempt artifacts"
        )
        ui.label(
            f"In {workspace.name}: {gate_note}; prior-attempt evidence: {artifact_note}.",
        ).classes("text-xs mono text-[#8b949e]")

        drift = validate_refs(
            parse_gates(gates_to_facts(rows) or {}) or (),
            target_gates,
        )
        if drift:
            ui.label(
                "⚠ Protocol changed since these gates were saved: "
                + "; ".join(drift)
                + " — regenerate or fix the refs.",
            ).classes("text-xs text-[#e3b341]")

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
                            _persist()

                        def _dismiss(_e, e_entry=entry) -> None:
                            pending.remove(e_entry)
                            _rebuild_pending()
                            _persist()

                        ui.button("Mark blocked", on_click=_accept).props(
                            "dense outline color=warning",
                        )
                        ui.button("Dismiss", on_click=_dismiss).props(
                            "flat dense",
                        )

        saved_label = ui.label("").classes("text-xs text-[#8b949e]")

        def _persist() -> None:
            facts = gates_to_facts(rows, notes.value)
            if facts and pending:
                facts["pending_blocked"] = pending
            write_feasibility_facts(facts, default_facts_path(workspace))
            stamp = time.strftime("%H:%M:%S")
            saved_label.set_text(
                "auto-saved (no gates — file cleared)"
                if facts is None
                else f"auto-saved · {len(facts['gates'])} gate(s) · {stamp}",
            )

        def _refresh_verdict() -> None:
            assessment = check_feasibility(gates_to_facts(rows, notes.value))
            icon = "✅" if assessment.reachable else "🚫"
            color = "#3fb950" if assessment.reachable else "#f85149"
            verdict_label.set_text(f"{icon} {assessment.reason}")
            verdict_label.style(f"color: {color}")
            if assessment.required_gates is None:
                gate_exp.set_text("Feasibility gates — no gates recorded")
            else:
                blocked_n = len(assessment.blocked_gates)
                gate_exp.set_text(
                    f"Feasibility gates · {icon} {assessment.required_gates} gate(s)"
                    + (f" · {blocked_n} blocked" if blocked_n else ""),
                )

        def _edit(row: dict, key: str):
            def _handle(e) -> None:
                row[key] = e.value
                _refresh_verdict()
                _persist()

            return _handle

        def _rebuild_rows() -> None:
            rows_container.clear()
            with rows_container:
                if not rows:
                    ui.label(
                        "No gates yet — add one manually or draft them "
                        "from the protocol.",
                    ).classes("text-xs text-[#8b949e]")
                for index, row in enumerate(rows):
                    with ui.row().classes("w-full gap-2 items-center flex-nowrap"):
                        ui.input(
                            "gate name",
                            value=row.get("name", ""),
                            on_change=_edit(row, "name"),
                        ).classes("w-44 mono shrink-0").props("dense")
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
                            ui.chip(
                                f"⚙ {check.get('kind')}",
                            ).props("dense square outline").tooltip(
                                "Machine-checked gate (deterministic; the "
                                "judge is never asked): "
                                + json.dumps(check, ensure_ascii=False),
                            )

                        def _remove(_e, r_index=index) -> None:
                            rows.pop(r_index)
                            _rebuild_rows()
                            _refresh_verdict()
                            _persist()

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
            _persist()

        ui.button("Add gate", on_click=_add_row).props("dense outline")

        verdict_label = ui.label("").classes("text-xs mono")
        notes = ui.input(
            "Overall notes (optional)", value=str(current.get("notes", "")),
        ).classes("w-full")

        def _notes_changed(e) -> None:
            _refresh_verdict()
            _persist()

        notes.on_value_change(_notes_changed)
        _refresh_verdict()

        gen_status = ui.label("").classes("text-xs text-[#8b949e]")

        def _cancel_draft() -> None:
            _draft_call.cancel(gen_status, message="Cancelling draft…")

        async def _generate() -> None:
            text = _current_text()
            if not text:
                from supervisor.protocols.protocol import (
                    read_protocol_text,
                    resolve_protocol_path,
                )

                text = read_protocol_text(resolve_protocol_path(workspace))
            if not text.strip():
                gen_status.set_text(
                    "No protocol yet — refine one above; nothing to derive gates from.",
                )
                return
            app_state.save()
            app_state.apply_api_config()
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

            gen_status.set_text("Drafting gates from the protocol (LLM)…")
            stop_event = _draft_call.begin()
            cancel_button.set_visibility(True)
            try:
                async with busy_buttons(gen_button):
                    try:
                        facts = await nicegui_run.io_bound(
                            draft,
                            workspace,
                            text,
                            on_progress=_on_progress,
                            stop_event=stop_event,
                        )
                    except GenerationCancelled:
                        gen_status.set_text("Draft cancelled.")
                        return
                    except Exception as exc:  # noqa: BLE001 — surface, keep rows
                        gen_status.set_text(f"Draft failed: {str(exc)[:200]}")
                        return
                    drafted = facts_to_gate_rows(facts)
                    if not drafted:
                        gen_status.set_text(
                            "Draft contained no gates — nothing filled.",
                        )
                        return
                    rows[:] = drafted
                    _rebuild_rows()
                    _refresh_verdict()
                    _persist()
                    blocked = sum(1 for row in rows if row["status"] == "blocked")
                    gen_status.set_text(
                        f"Drafted {len(rows)} gate(s) ({blocked} blocked) — "
                        "auto-saved; edit freely.",
                    )
            # ``_persist`` writes the facts file; an OSError there (read-only
            # dir, locked file) used to escape into NiceGUI's async task, which
            # only logs to stderr — the operator saw a button that did nothing
            # after the rows were already redrawn.
            except Exception as exc:  # noqa: BLE001 — must never be silent
                gen_status.set_text(f"Draft failed: {type(exc).__name__}: {str(exc)[:200]}")
            finally:
                _draft_call.end()
                cancel_button.set_visibility(False)

        with ui.row().classes("gap-3"):
            gen_button = ui.button(
                "Draft gates from protocol (LLM)", on_click=_generate,
            ).props("dense outline").tooltip(
                "The supervisor model transcribes one gate per TARGET item, "
                "with statuses only where prior-attempt artifacts evidence "
                "them; it never invents results.",
            )
            cancel_button = ui.button(
                "Cancel", on_click=_cancel_draft,
            ).props("dense outline color=red")
            cancel_button.set_visibility(False)
