"""Protocol Wizard page: configuration, editors, connectivity, refine flow."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from nicegui import ui, run as nicegui_run

from services.webui.components.busy import busy_buttons
from services.webui.state import app_state
from supervisor.utils.llm_stream import GenerationCancelled

_MAX_LISTED_FILES = 2000
# The scan walks up to _MAX_LISTED_FILES entries, but the droplist only
# offers the first _DROPLIST_FILES of them to stay responsive.
_DROPLIST_FILES = 500
_IGNORE_SUGGEST_FILES = 1000

# Holds the stop event of the refine call currently in flight, if any, so the
# Cancel button can abort it.
_cancel_holder: dict = {}


def _cancel_refine(status) -> None:
    event = _cancel_holder.get("event")
    if event is None:
        ui.notify("No refine is running.")
        return
    event.set()
    status.set_text("Cancelling refine…")


def _save() -> None:
    app_state.save()


def _bind_input(
    key: str,
    label: str,
    *,
    password: bool = False,
    classes: str = "",
    on_change: Callable[[], None] | None = None,
):
    value = app_state.get(key) or ""

    def _change(e) -> None:
        app_state[key] = e.value
        _save()
        if on_change is not None:
            on_change()

    return ui.input(
        label,
        value=value,
        password=password,
        on_change=_change,
    ).classes(classes or "w-full mono")


def _bind_switch(key: str, label: str):
    def _change(e) -> None:
        app_state[key] = bool(e.value)
        _save()

    return ui.switch(label, value=bool(app_state.get(key)), on_change=_change)


def _bind_number(key: str, label: str, *, min: int, max: int, format: str = "%.0f"):
    def _change(e) -> None:
        if e.value is not None:
            app_state[key] = int(e.value)
            _save()

    return ui.number(
        label, value=int(app_state.get(key) or 0), min=min, max=max,
        format=format, on_change=_change,
    )


def _workspace() -> Path | None:
    raw = app_state.get("workspace") or ""
    path = Path(raw) if raw else None
    if path and path.exists():
        return path
    return None


def _section(title: str, *, open_: bool = False):
    """One collapsible panel of the wizard, visually consistent."""
    return ui.expansion(title, value=open_).classes("w-full bg-[#161b22]")


def _list_workspace_files(workspace: Path, limit: int) -> list[str]:
    """Sorted forward-slash relative paths of files under ``workspace``."""
    return sorted(
        str(p.relative_to(workspace)).replace("\\", "/")
        for p in workspace.rglob("*")
        if p.is_file()
    )[:limit]


def _on_workspace_change() -> None:
    # Workspace-dependent sections below capture the workspace at render
    # time; refresh them so they never serve (or write to) the old folder.
    _protected_section.refresh()
    _ignore_section.refresh()
    _skills_listing.refresh()
    _protocol_file_row.refresh()


# ── Configuration section ─────────────────────────────────────────────────────


def _config_section() -> None:
    with _section("Configuration", open_=True):
        with ui.row().classes("w-full gap-4 flex-wrap"):
            _bind_input("openai_key", "API key", password=True)
            _bind_input("base_url", "API base URL (optional)")
        with ui.row().classes("w-full gap-4 flex-wrap items-end"):
            _bind_input(
                "workspace",
                "Workspace path",
                on_change=_on_workspace_change,
            )
            ui.button(
                "Clean artifacts",
                on_click=lambda: _clean_artifacts(),
            ).props("flat dense")
        with ui.row().classes("w-full gap-4 flex-wrap"):
            _bind_input("supervisor_model", "Supervisor (judge) model")
            _bind_input("supervisor_model_backup", "Supervisor backup model")
        from services.config.connectivity import REASONING_EFFORT_CHOICES

        _reasoning_select(
            "supervisor_reasoning",
            "Supervisor thinking effort",
            choices=REASONING_EFFORT_CHOICES,
        )
        with ui.row().classes("w-full gap-4 flex-wrap items-end"):
            engine_select = ui.select(
                ["opencode", "codex"],
                value=app_state.get("engine") or "opencode",
                label="Execution engine",
                on_change=lambda e: _set_engine(e.value),
            ).classes("w-48")
            _ = engine_select
        if (app_state.get("engine") or "opencode") == "codex":
            with ui.row().classes("w-full gap-4 flex-wrap"):
                _bind_input("codex_base_url", "Codex base URL (responses API)")
                _bind_input("codex_api_key", "Codex API key", password=True)
        _model_section()
        with ui.row().classes("w-full gap-6 flex-wrap"):
            _bind_switch("enable_python_scanner", "Python scanner")
            _bind_switch("enable_occam_razor", "Occam Razor")
            _bind_switch("enable_supervisor_skills", "Supervisor skills")
        with ui.row().classes("w-full gap-4 flex-wrap"):
            _bind_number("max_retries", "Max retries", min=1, max=10)
            _bind_number("context_threshold", "Context threshold %", min=10, max=95)
            _bind_number("max_tokens", "Max tokens", min=1000, max=2000000)
            _bind_number("timeout", "Turn timeout (min)", min=1, max=120)
        with ui.row().classes("w-full gap-4 flex-wrap"):
            _bind_input(
                "npm_registry", "npm registry for CLI upgrades (empty = default)",
            )
        with ui.row().classes("w-full gap-4 flex-wrap"):
            _bind_input(
                "supervisor_skills_dir",
                "Supervisor skills extra dir (optional; beyond built-in + "
                "workspace bank)",
                on_change=_skills_listing.refresh,
            )
        _skills_listing()


@ui.refreshable
def _skills_listing() -> None:
    """Read-only view of the supervisor skill bank the judge can load from."""
    from supervisor.core.skill_bank import SkillBank, builtin_bank_dir

    extra_dir = (app_state.get("supervisor_skills_dir") or "").strip() or None
    workspace = _workspace()
    bank = SkillBank(
        builtin_dir=builtin_bank_dir(),
        workspace_dir=(
            workspace / ".opencode" / "supervisor_skills" if workspace else None
        ),
        extra_dir=extra_dir,
    )
    with ui.column().classes("w-full gap-1"):
        ui.label("Supervisor skill bank (loaded only on the judge's request):").classes(
            "text-xs text-[#8b949e]",
        )
        skills = bank.skills()
        if not skills:
            ui.label("No skills discovered.").classes("text-xs text-[#8b949e]")
        for skill in skills:
            ui.label(f"• {skill.name} — {skill.description}  [{skill.source}]").classes(
                "text-xs",
            )


def _set_engine(value: str) -> None:
    app_state["engine"] = value
    _save()
    ui.run_javascript("location.reload()")


def _clean_artifacts() -> None:
    from services.runtime.workspace_cleanup import clean_workspace_artifacts

    workspace = _workspace()
    if workspace:
        clean_workspace_artifacts(workspace)
        ui.notify(f"Cleaned artifacts in {workspace.name}")


# ── Models section ────────────────────────────────────────────────────────────


def _reasoning_select(key: str, label: str, choices: list[str]) -> None:
    """Thinking-effort droplist saved to ``key`` ("" = model default)."""
    options = {"": "Default (model default)"} | {c: c for c in choices}
    current = (app_state.get(key) or "").strip()
    if current and current not in options:
        options[current] = current
    ui.select(
        options,
        value=current if current in options else "",
        label=label,
        on_change=lambda e, k=key: _set_reasoning(k, e.value),
    ).classes("w-48").tooltip(
        "Thinking/reasoning effort for real runs. Choose Default unless the "
        "model documents these values.",
    )


def _set_reasoning(key: str, value: str) -> None:
    app_state[key] = value or ""
    _save()


def _agent_variant_choices(engine: str, model: str) -> list[str]:
    """Variant names for the selected agent model, cheapest first.

    opencode: exact per-model variants from its own model metadata (the same
    names `--variant` accepts). codex: the standard reasoning-effort ladder
    (codex exposes no per-model enumeration; providers accept their subset).
    Detection failures degrade to the plain effort ladder.
    """
    from services.config.connectivity import REASONING_EFFORT_CHOICES

    if engine == "opencode":
        from services.config.connectivity import opencode_variant_choices

        found = opencode_variant_choices(
            app_state.get("opencode_executable") or "opencode", model or "",
        )
        if found:
            return list(found)
    return list(REASONING_EFFORT_CHOICES)


def _model_section() -> None:
    engine = (app_state.get("engine") or "opencode").strip().lower()
    models = (
        app_state.opencode_models if app_state.model_list_engine == engine else []
    )
    with ui.row().classes("w-full gap-4 flex-wrap items-end"):
        if models:
            current = app_state.get("opencode_model") or models[0]
            options = models + ["(custom)"]
            if current and current not in options:
                options = [current] + options
            ui.select(
                options,
                value=current,
                label=f"Agent model ({engine})",
                on_change=lambda e: _set_model(e.value),
            ).classes("w-80")
            refresh_button = ui.button(
                "Refresh models", on_click=lambda: _refresh_models(refresh_button),
            ).props("flat dense")
        else:
            _bind_input("opencode_model", f"Agent model ({engine})")
            refresh_button = ui.button(
                "Fetch models", on_click=lambda: _refresh_models(refresh_button),
            ).props("flat dense")
        _bind_input("opencode_model_backup", "Agent backup model")
        variant_label = (
            "Codex reasoning effort" if engine == "codex" else "Thinking variant"
        )
        _reasoning_select(
            "agent_reasoning", variant_label, _agent_variant_choices(
                engine, app_state.get("opencode_model") or "",
            ),
        )


def _set_model(value: str) -> None:
    if value and value != "(custom)":
        app_state["opencode_model"] = value
        # Variant names are model-specific — drop a stale selection and
        # reload so the variant droplist re-aligns with the new model.
        app_state["agent_reasoning"] = ""
        _save()
        ui.run_javascript("location.reload()")


async def _refresh_models(button=None) -> None:
    from services.config.agent_models import fetch_agent_models

    engine = (app_state.get("engine") or "opencode").strip().lower()
    ui.notify(f"Fetching {engine} model list…")
    async with busy_buttons(*([button] if button is not None else [])):
        models = await nicegui_run.io_bound(
            fetch_agent_models,
            engine,
            app_state.get("opencode_executable") or "",
            app_state.get("codex_base_url") or "",
            app_state.get("codex_api_key") or "",
        )
    if models:
        app_state.opencode_models = models
        app_state.model_list_engine = engine
        if not app_state.get("opencode_model"):
            app_state["opencode_model"] = models[0]
        _save()
        ui.notify(f"Fetched {len(models)} {engine} models — reloading page.")
        ui.run_javascript("location.reload()")
    else:
        ui.notify(
            f"No models returned for {engine} "
            "(codex needs a base URL with a /models endpoint).",
        )


# ── Protected files ───────────────────────────────────────────────────────────


@ui.refreshable
def _protected_section() -> None:
    with _section("Protected Files"):
        ui.label(
            "Files the agent may not modify or delete (selection lives for "
            "this app session).",
        ).classes("text-xs text-[#8b949e]")
        workspace = _workspace()
        if not workspace:
            ui.label("Set a valid workspace path first.").classes("text-[#e3b341]")
            return
        add_select = ui.select(
            [], multiple=True, label="Add protected files (search workspace)",
        ).classes("w-full")
        options_holder: dict[str, list[str]] = {}

        async def _load_options() -> None:
            async with busy_buttons(scan_button):
                entries = await nicegui_run.io_bound(_scan_workspace, workspace)
                options_holder["all"] = entries
                add_select.options = entries[:_DROPLIST_FILES]
                add_select.update()

        scan_button = ui.button("Scan workspace", on_click=_load_options).props(
            "flat dense",
        )

        def _add(e) -> None:
            chosen = list(e.value or [])
            if chosen:
                current = list(app_state.get("protected_files") or [])
                merged = sorted(set(current) | set(chosen))
                app_state["protected_files"] = merged
                _save()
                add_select.value = []
                _refresh_chips(merged, options_holder, chips_container)

        add_select.on_value_change(_add)

        chips_container = ui.column().classes("w-full gap-1")
        _refresh_chips(list(app_state.get("protected_files") or []), options_holder, chips_container)


def _scan_workspace(workspace: Path) -> list[str]:
    return _list_workspace_files(workspace, _MAX_LISTED_FILES)


def _refresh_chips(protected: list[str], options_holder: dict, container) -> None:
    container.clear()
    with container:
        if not protected:
            ui.label("No protected files.").classes("text-xs text-[#8b949e]")
        for path in protected:
            with ui.row().classes("items-center gap-2"):
                ui.label(f"🔒 {path}").classes("text-xs mono")
                ui.button(
                    icon="close",
                    on_click=lambda p=path: _remove_protected(p, options_holder, container),
                ).props("flat dense")


def _remove_protected(path: str, options_holder: dict, container) -> None:
    merged = [p for p in (app_state.get("protected_files") or []) if p != path]
    app_state["protected_files"] = merged
    _save()
    _refresh_chips(merged, options_holder, container)


# ── Ignore editor ─────────────────────────────────────────────────────────────


@ui.refreshable
def _ignore_section() -> None:
    from supervisor.workspace.ignore_patterns import IGNORE_FILE

    with _section(".opencodeignore Editor"):
        workspace = _workspace()
        if not workspace:
            ui.label("Set a valid workspace path first.").classes("text-[#e3b341]")
            return
        from supervisor.workspace.ignore_patterns import write_ignore_file

        ignore_path = workspace / IGNORE_FILE
        current = (
            ignore_path.read_text(encoding="utf-8") if ignore_path.exists() else ""
        )
        area = ui.textarea(".opencodeignore content", value=current).classes(
            "w-full mono",
        )
        status = ui.label("").classes("text-xs text-[#8b949e]")

        def _save_ignore() -> None:
            write_ignore_file(workspace, area.value or "")
            status.set_text("Saved .opencodeignore.")

        ui.button("Save ignore file", on_click=_save_ignore).props("dense")

        async def _suggest() -> None:
            status.set_text("Generating ignore patterns (LLM call)…")
            async with busy_buttons(suggest_button):
                app_state.save()
                app_state.apply_api_config()
                try:
                    generated = await nicegui_run.io_bound(
                        _suggest_ignore_patterns, workspace,
                    )
                except Exception as exc:
                    status.set_text(f"Failed to generate ignore patterns: {exc}")
                    return
            area.value = generated
            write_ignore_file(workspace, generated)
            status.set_text("Ignore patterns generated and saved.")

        suggest_button = ui.button(
            "Suggest and Apply Ignore Patterns", on_click=_suggest,
        ).props("dense outline")


def _suggest_ignore_patterns(workspace: Path) -> str:
    from supervisor.utils.llm_stream import make_stream_client, stream_chat_text

    entries = _list_workspace_files(workspace, _IGNORE_SUGGEST_FILES)
    client = make_stream_client(
        api_key=app_state.get("openai_key"),
        base_url=app_state.get("base_url") or None,
    )
    return stream_chat_text(
        client,
        app_state.get("supervisor_model") or "gpt-4o",
        [
            {
                "role": "system",
                "content": (
                    "Given a list of files and directories in a workspace, generate "
                    "a .opencodeignore file that ignores common build artifacts, "
                    "dependency directories, cache files, and other files that should "
                    "not be modified by an autonomous coding agent. Patterns should be "
                    "in gitignore format. Output patterns only."
                ),
            },
            {"role": "user", "content": "Workspace entries:\n" + "\n".join(entries)},
        ],
        temperature=0.3,
        field_name="generated ignore patterns response",
    )


# ── Connectivity tests ────────────────────────────────────────────────────────


def _connectivity_section() -> None:
    from services.config.connectivity import (
        test_agent_connectivity,
        test_supervisor_connectivity,
    )

    # Engine is captured at render time; safe because switching the engine
    # reloads the whole page (see _set_engine).
    with _section("Connectivity Tests"):
        engine = app_state.get("engine") or "opencode"
        status = ui.label("").classes("text-xs")
        _refresh_status(status)

        async def _run(kind: str) -> None:
            if not (app_state.get("workspace") or "").strip():
                ui.notify("Set workspace path before running tests.")
                return
            status.set_text("Testing… (agent up to ~60s)")
            async with busy_buttons(*test_buttons):
                if kind in ("agent", "all"):
                    ok, message = await nicegui_run.io_bound(
                        test_agent_connectivity,
                        app_state.get("engine") or "opencode",
                        app_state.get("opencode_executable") or "",
                        app_state.get("opencode_model"),
                        app_state.get("opencode_model_backup"),
                        45,
                        app_state.get("codex_base_url") or "",
                        app_state.get("codex_api_key") or "",
                    )
                    app_state.flags["opencode_test_passed"] = ok
                    status.set_text(f"{engine}: {'✅' if ok else '❌'} {message[:160]}")
                if kind in ("supervisor", "all"):
                    ok, message = await nicegui_run.io_bound(
                        test_supervisor_connectivity,
                        app_state.get("openai_key"),
                        app_state.get("supervisor_model") or "gpt-4o",
                        app_state.get("base_url") or None,
                    )
                    app_state.flags["supervisor_test_passed"] = ok
                    status.set_text(
                        f"supervisor: {'✅' if ok else '❌'} {message[:160]}",
                    )
                if app_state.tests_ok():
                    status.set_text("✅ Both connectivity tests passed — Run/Evolve unlocked.")

        with ui.row().classes("gap-3"):
            test_buttons = [
                ui.button("Run Tests", on_click=lambda: _run("all")).props(
                    "color=primary",
                ),
                ui.button(f"Test {engine}", on_click=lambda: _run("agent")).props(
                    "outline",
                ),
                ui.button(
                    "Test Supervisor", on_click=lambda: _run("supervisor"),
                ).props("outline"),
            ]


def _refresh_status(_label) -> None:
    if app_state.tests_ok():
        ui.notify("Both connectivity tests passed.")


# ── Protocol drafts + refine flow ─────────────────────────────────────────────


@ui.refreshable
def _protocol_file_row() -> None:
    """Existing-protocol indicator; refreshed on workspace switches."""
    workspace = _workspace()
    if workspace and (workspace / "protocol.md").exists():
        with ui.row().classes("items-center gap-2"):
            ui.label("✅ Existing protocol.md found in this workspace.").classes(
                "text-xs text-[#3fb950]",
            )
            ui.button("Preview", on_click=_preview_protocol).props("flat dense")


def _preview_protocol() -> None:
    workspace = _workspace()
    if workspace is not None:
        _preview_file(workspace / "protocol.md")


def _protocol_section() -> None:
    with _section("Protocol Draft", open_=True):
        _protocol_file_row()
        with ui.row().classes("w-full gap-4"):
            ui.input(
                "INPUT (context)",
                value=app_state.get("raw_input") or "",
                on_change=lambda e: _set_draft("raw_input", e.value, quality_label),
            ).classes("w-full mono").props("autogrow")
        ui.input(
            "TARGET (objectives)",
            value=app_state.get("raw_target") or "",
            on_change=lambda e: _set_draft("raw_target", e.value, quality_label),
        ).classes("w-full mono").props("autogrow")
        ui.input(
            "RESTRICTIONS (boundaries)",
            value=app_state.get("raw_restrictions") or "",
            on_change=lambda e: _set_draft(
                "raw_restrictions", e.value, quality_label,
            ),
        ).classes("w-full mono").props("autogrow")

        quality_label = ui.label("").classes("text-xs text-[#8b949e]")
        _update_quality(quality_label)

        status = ui.label("").classes("text-xs")
        cancel_button = ui.button(
            "Cancel", on_click=lambda: _cancel_refine(status),
        ).props("outline color=red")
        cancel_button.set_visibility(False)
        refine_button = ui.button(
            "Refine with AI",
            on_click=lambda: _refine(status, refine_button, cancel_button),
        ).props("color=primary")

        if app_state.get("protocol_md"):
            _refined_editor()


def _set_draft(key: str, value: str, quality_label) -> None:
    app_state[key] = value
    _save()
    _update_quality(quality_label)


def _update_quality(label) -> None:
    from supervisor.protocols.protocol_analyzer import ProtocolAnalyzer

    text = (
        f"## INPUT\n{app_state.get('raw_input') or ''}\n\n"
        f"## TARGET\n{app_state.get('raw_target') or ''}\n\n"
        f"## RESTRICTIONS\n{app_state.get('raw_restrictions') or ''}\n"
    )
    try:
        analysis = ProtocolAnalyzer().analyze_text(text)
        label.set_text(
            f"Quality: overall {analysis.overall_score:.0%} · "
            f"INPUT {analysis.input_score.overall:.0%} · "
            f"TARGET {analysis.target_score.overall:.0%} · "
            f"RESTRICTIONS {analysis.restrictions_score.overall:.0%}"
            + (f" · {len(analysis.issues)} issue(s)" if analysis.issues else ""),
        )
    except Exception:
        label.set_text("Complete all three sections to see quality scores.")


async def _refine(status, refine_button, cancel_button) -> None:
    if not all(
        (app_state.get(k) or "").strip()
        for k in ("raw_input", "raw_target", "raw_restrictions")
    ):
        ui.notify("Fill INPUT, TARGET and RESTRICTIONS first.")
        return
    status.set_text("Refining protocol with AI… (the model reasons at length before answering)")
    stop_event = threading.Event()
    _cancel_holder["event"] = stop_event
    cancel_button.set_visibility(True)
    async with busy_buttons(refine_button):
        from supervisor.protocols.protocol_wizard import (
            ProtocolGateError,
            ProtocolWizard,
        )

        app_state.save()
        app_state.apply_api_config()
        try:
            wizard = ProtocolWizard(
                model=app_state.get("supervisor_model") or "gpt-4o",
                api_key=app_state.get("openai_key") or None,
                base_url=app_state.get("base_url") or None,
            )
            started = time.monotonic()

            def _on_progress(content_chars: int, reasoning_chars: int, _chunks: int) -> None:
                # Called from the io_bound worker thread; NiceGUI queues the
                # update onto the client's event loop.
                phase = "reasoning" if not content_chars else "writing answer"
                status.set_text(
                    f"Refining — {phase}… {reasoning_chars:,} reasoning chars · "
                    f"{content_chars:,} answer chars · "
                    f"{time.monotonic() - started:.0f}s",
                )
                status.update()

            def _on_gate_round(
                fix_round: int, max_fix_rounds: int, issues: tuple[str, ...],
            ) -> None:
                # Same worker-thread contract as _on_progress: the audit
                # rejected the draft and the wizard is editing it again.
                status.set_text(
                    f"Gate check failed — auto-fix round {fix_round}/"
                    f"{max_fix_rounds}: " + " ".join(issues),
                )
                status.update()

            markdown, _protocol = await nicegui_run.io_bound(
                wizard.refine,
                app_state.get("raw_input") or "",
                app_state.get("raw_target") or "",
                app_state.get("raw_restrictions") or "",
                on_progress=_on_progress,
                stop_event=stop_event,
                on_round=_on_gate_round,
            )
        except GenerationCancelled:
            status.set_text("Refine cancelled.")
            return
        except ProtocolGateError as exc:
            status.set_text(f"Refine failed: {exc}")
            return
        except Exception as exc:
            status.set_text(f"Refine failed: {exc}")
            return
        finally:
            _cancel_holder["event"] = None
            cancel_button.set_visibility(False)
    app_state["protocol_md"] = markdown
    app_state.save()
    status.set_text(
        "Refined protocol ready — target audit passed; review and accept below.",
    )
    ui.run_javascript("location.reload()")


def _refined_editor() -> None:
    with _section("Refined protocol — review and accept", open_=True):
        area = ui.textarea(
            "protocol.md", value=app_state.get("protocol_md") or "",
        ).classes("w-full mono").props("autogrow")
        quality_label = ui.label("").classes("text-xs text-[#8b949e]")

        def _on_change(e) -> None:
            app_state["protocol_md"] = e.value
            app_state.save()
            _update_quality(quality_label)

        area.on_value_change(_on_change)

        async def _accept() -> None:
            from supervisor.protocols.protocol import parse_protocol_text
            from supervisor.protocols.target_audit import audit_target

            try:
                protocol = parse_protocol_text(area.value or "")
            except ValueError as exc:
                ui.notify(f"Protocol invalid: {exc}")
                return
            audit = audit_target(
                protocol.target_section, protocol.restrictions_section,
            )
            if not audit.is_actionable:
                ui.notify("TARGET not ready: " + " ".join(audit.issues))
                return
            # Resolve the workspace at accept time: it may have changed
            # since this editor was rendered.
            workspace = _workspace()
            if workspace is None:
                ui.notify("Set a valid workspace first.")
                return
            (workspace / "protocol.md").write_text(
                area.value or "", encoding="utf-8",
            )
            ui.notify(f"protocol.md saved to {workspace.name}")
            app_state["protocol_md"] = ""
            app_state.save()
            ui.navigate.to("/run")

        with ui.row():
            ui.button("Accept & Save", on_click=_accept).props("color=primary")
            ui.button(
                "Re-refine",
                on_click=lambda: (
                    app_state.__setitem__("protocol_md", ""),
                    app_state.save(),
                    ui.run_javascript("location.reload()"),
                ),
            ).props("outline")


def _preview_file(path: Path) -> None:
    with ui.dialog() as dialog, ui.card().classes("w-full"):
        ui.code(path.read_text(encoding="utf-8")[:3000], language="markdown")
        ui.button("Close", on_click=dialog.close).props("dense")
    dialog.open()


# ── Page entry ────────────────────────────────────────────────────────────────


def render() -> None:
    ui.label("Protocol Wizard").classes("text-2xl font-bold")
    _config_section()
    _protected_section()
    _ignore_section()
    _connectivity_section()
    _protocol_section()
