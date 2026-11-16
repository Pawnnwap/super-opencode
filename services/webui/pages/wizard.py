"""Protocol Wizard page: configuration, editors, connectivity, refine flow."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from nicegui import app as nicegui_app
from nicegui import run as nicegui_run
from nicegui import ui

from services.webui import theme
from services.webui.components.busy import CancellableCall, busy_buttons
from services.webui.state import app_state
from supervisor.utils.llm_stream import GenerationCancelled

_MAX_LISTED_FILES = 2000
# The scan walks up to _MAX_LISTED_FILES entries, but the droplist only
# offers the first _DROPLIST_FILES of them to stay responsive.
_DROPLIST_FILES = 500
_IGNORE_SUGGEST_FILES = 1000

# Holds the refine call currently in flight, if any, so the Cancel button can
# abort it.
_refine_call = CancellableCall()


def _cancel_refine(status) -> None:
    _refine_call.cancel(status, message="Cancelling refine…")


# The protocol editor holds the only long text in the app, and two controls
# (engine, model) reload the page to re-align their droplists. Without this
# slot a 1400-character draft vanished silently. The draft lives in
# server-side general storage, which survives a reload for the same operator,
# and is re-offered on the next render.
_draft_slot: dict[str, object] = {"workspace": "", "getter": None}


def _stash_protocol_draft() -> None:
    """Keep the in-page protocol text across a reload."""
    getter = _draft_slot.get("getter")
    workspace = str(_draft_slot.get("workspace") or "")
    if not getter or not workspace:
        return
    try:
        payload = getter()  # type: ignore[operator]
    except Exception:  # noqa: BLE001 — a failed stash must not block switching
        return
    if any(str(value or "").strip() for value in payload.values()):
        nicegui_app.storage.general[f"protocol_draft::{workspace}"] = payload


def _take_protocol_draft(workspace: Path) -> dict:
    """Pop a stashed draft for this workspace, if any."""
    try:
        return nicegui_app.storage.general.pop(
            f"protocol_draft::{workspace}", {},
        ) or {}
    except Exception:  # noqa: BLE001 — no client context, nothing to restore
        return {}


def _save() -> None:
    """Persist settings after an edit, coalescing keystrokes.

    Debounced on purpose: a bound field fires per character, and a full JSON
    write per character is both slow and the exact pattern that froze the
    event loop before. Anything that needs the file on disk *now* (launch,
    auto-generate) calls ``app_state.flush()`` instead.
    """
    app_state.schedule_save()


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
    # Bounded, not w-full: a full-width input pushes its row-mates onto the
    # next line, which is how every row of settings became one-per-line.
    ).classes(classes or "w-72 mono")


def _bind_switch(key: str, label: str, *, on_change: Callable[[], None] | None = None):
    def _change(e) -> None:
        app_state[key] = bool(e.value)
        _save()
        if on_change is not None:
            on_change()

    return ui.switch(label, value=bool(app_state.get(key)), on_change=_change)


def _bind_number(key: str, label: str, *, min: int, max: int, format: str = "%.0f",
                 classes: str = "w-44"):
    def _change(e) -> None:
        if e.value is not None:
            app_state[key] = int(e.value)
            _save()

    # w-44 so the LABEL fits: a cramped field ellipsizes its own title.
    return ui.number(
        label, value=int(app_state.get(key) or 0), min=min, max=max,
        format=format, on_change=_change,
    ).classes(classes)


def _bind_float(
    key: str,
    label: str,
    *,
    min: float,
    max: float,
    format: str = "%.2f",
    classes: str = "w-44",
):
    def _change(e) -> None:
        if e.value is not None:
            app_state[key] = float(e.value)
            _save()

    return ui.number(
        label, value=float(app_state.get(key) or 0), min=min, max=max,
        step=0.01, format=format, on_change=_change,
    ).classes(classes)


def _workspace() -> Path | None:
    raw = app_state.get("workspace") or ""
    path = Path(raw) if raw else None
    if path and path.exists():
        return path
    return None


def _profile_sync(workspace: Path, *, create: bool = False) -> dict:
    """The workspace profile is authoritative; seed it from settings if absent."""
    from supervisor.security.access_profile import (
        PROFILE_KEYS,
        load_profile,
        save_profile,
    )

    if create and not load_profile(workspace):
        save_profile(workspace, {key: app_state.get(key) for key in PROFILE_KEYS})
    profile = load_profile(workspace)
    for key in PROFILE_KEYS:
        if key in profile:
            app_state[key] = profile[key]
    return profile


def _profile_save(workspace: Path, *, configured: bool = False) -> None:
    """Persist every protection edit into the workspace immediately.

    ``configured`` marks the Access & Protection step as done for this
    workspace: a manual edit is as much a setup decision as a clean
    auto-generate run, and both unlock the hand-off button at the page bottom.
    """
    from supervisor.security.access_profile import PROFILE_KEYS, save_profile

    values = {key: app_state.get(key) for key in PROFILE_KEYS}
    if configured:
        values["access_configured"] = True
    save_profile(workspace, values)
    # The hand-off button at the bottom of the page is gated on the policy
    # drill, which these edits change; the progress strip mirrors it.
    _ready_section.refresh()
    _stepper.refresh()


def _section(
    title: str,
    *,
    anchor: str = "",
    open_: bool = False,
    icon: str = "",
    caption: str = "",
):
    """One collapsible panel of the wizard, visually consistent.

    Its open state survives a ``@ui.refreshable`` rebuild: the protection and
    protocol sections are rebuilt whenever the workspace changes, and the
    plain ``ui.expansion`` used to come back at the constructor default,
    folding whatever the operator had opened.

    ``anchor`` is the id the stepper links to — without it the five-step strip
    is decorative, and the only way back to a section is 1500px of scrolling.
    """
    return theme.fold(
        f"wizard.{title}", title, icon=icon, caption=caption, default=open_,
        anchor=anchor,
    )


def _list_workspace_files(workspace: Path, limit: int) -> list[str]:
    """Sorted forward-slash relative paths of readable, non-cache files.

    Feeds the protected-files droplist and the ignore-pattern suggester, so
    the candidate list excludes cache dirs (__pycache__, node_modules, build
    outputs, ...) and unreadable/binary files — the agent can neither protect
    nor meaningfully ignore-generate for byte-compiled junk.
    """
    from supervisor.utils.filesystem.path_filters import is_candidate_file

    return sorted(
        str(p.relative_to(workspace)).replace("\\", "/")
        for p in workspace.rglob("*")
        if is_candidate_file(p)
    )[:limit]


_last_refreshed_workspace: dict[str, str] = {"path": ""}


def _on_workspace_change() -> None:
    # Workspace-dependent sections below capture the workspace at render
    # time; refresh them so they never serve (or write to) the old folder.
    #
    # Only when the typed value RESOLVES to a DIFFERENT workspace. This fires
    # on every keystroke; rebuilding three panels per character (one scans the
    # whole workspace, one reloads the protocol editor from disk, discarding
    # unsaved text) reflowed the page under the operator's hands while they
    # were still typing the path.
    workspace = _workspace()
    if workspace is None:
        return
    resolved = str(workspace)
    if resolved == _last_refreshed_workspace["path"]:
        return
    _last_refreshed_workspace["path"] = resolved
    _protection_section.refresh()
    _skills_listing.refresh()
    _protocol_section.refresh()
    _ready_section.refresh()
    _stepper.refresh()


# ── Configuration section ─────────────────────────────────────────────────────


def _config_section() -> None:
    """Essentials only: the few fields every launch depends on.

    Progressive disclosure: backups, reasoning effort, jev, behavior
    switches, limits and maintenance live in Advanced settings (collapsed).
    """
    with _section(
        "Configuration", anchor="configuration", open_=True, icon="key",
        caption="Credentials, workspace and the execution engine",
    ):
        theme.group_label("Credentials")
        with ui.row().classes("sop-row"):
            _bind_input("openai_key", "API key", password=True)
            _bind_input("base_url", "API base URL (optional)")
            _bind_input("supervisor_model", "Supervisor (judge) model")
        theme.group_label("Workspace")
        _bind_input(
            "workspace",
            "Workspace path",
            classes="w-full mono",
            on_change=_on_workspace_change,
        )
        theme.group_label("Runtime")
        with ui.row().classes("sop-row"):
            ui.select(
                ["opencode", "codex", "pi"],
                value=app_state.get("engine") or "opencode",
                label="Execution engine",
                on_change=lambda e: _set_engine(e.value),
            ).classes("w-48")
            _agent_model_field()


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
        ui.label(
            "Supervisor skill bank — loaded only when the judge asks for one.",
        ).classes("sop-hint")
        skills = bank.skills()
        if not skills:
            ui.label("No skills discovered.").classes("sop-hint")
        for skill in skills:
            with ui.row().classes("items-baseline gap-2 flex-nowrap w-full"):
                ui.label(skill.name).classes("mono text-xs").style(
                    "color:var(--sop-text-2)",
                )
                ui.label(skill.description).classes("sop-hint truncate grow")
                ui.html(f'<span class="chip">{skill.source}</span>')


def _set_engine(value: str) -> None:
    _stash_protocol_draft()
    app_state["engine"] = value
    # flush, not _save: the reload below would race the 400ms debounce and
    # bring the page back with the previous engine.
    app_state.flush()
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

    opencode: exact per-model variants from the CLI's own resolved config (the
    same names `--model provider/model#variant` accepts). A model that
    declares NO variants yields an empty list — offering the generic ladder
    there is what persisted ``xhigh`` for a model that has none, and opencode
    2.x then exited 1 with "Variant unavailable" before every model call.
    codex: the standard reasoning-effort ladder (codex exposes no per-model
    enumeration; providers accept their subset). pi: its own `--thinking`
    ladder. Detection failures degrade to the plain effort ladder.
    """
    from services.config.connectivity import (
        PI_THINKING_CHOICES,
        REASONING_EFFORT_CHOICES,
        opencode_variant_choices,
    )

    if engine == "opencode":
        return list(
            opencode_variant_choices(
                app_state.get("opencode_executable") or "opencode", model or "",
            )
        )
    if engine == "pi":
        return list(PI_THINKING_CHOICES)
    return list(REASONING_EFFORT_CHOICES)


def _agent_model_field() -> None:
    """Agent model droplist (or free input) + fetch button, nothing else."""
    engine = (app_state.get("engine") or "opencode").strip().lower()
    models = (
        app_state.opencode_models if app_state.model_list_engine == engine else []
    )
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
        ).classes("w-96 mono")
        refresh_button = ui.button(
            "Refresh models", icon="sync",
            on_click=lambda: _refresh_models(refresh_button),
        ).props("flat dense").tooltip("Re-read the engine's model list.")
    else:
        _bind_input("opencode_model", f"Agent model ({engine})")
        refresh_button = ui.button(
            "Fetch models", icon="cloud_download",
            on_click=lambda: _refresh_models(refresh_button),
        ).props("outline color=none dense").tooltip("Read the engine's model list.")


def _advanced_section() -> None:
    """The rarely-touched 20% of configuration, collapsed until asked for.

    Deliberately quiet: it sits in the middle of the setup flow only because
    the order is fixed by contract, so it wears the muted surface and says so
    in its caption — everything here can be left alone.
    """
    from services.config.connectivity import REASONING_EFFORT_CHOICES

    engine = (app_state.get("engine") or "opencode").strip().lower()
    with _section(
        "Advanced settings", icon="tune",
        caption="Not needed for a normal run — models, arbiter, limits",
    ):
        theme.group_label("Models & reasoning")
        with ui.row().classes("sop-row"):
            _bind_input("opencode_model_backup", "Agent backup model")
            _bind_input("supervisor_model_backup", "Supervisor backup model")
            if engine == "codex":
                variant_label = "Codex reasoning effort"
            elif engine == "pi":
                variant_label = "Pi thinking level"
            else:
                variant_label = "Thinking variant"
            _reasoning_select(
                "agent_reasoning", variant_label,
                _agent_variant_choices(
                    engine, app_state.get("opencode_model") or "",
                ),
            )
            _reasoning_select(
                "supervisor_reasoning",
                "Supervisor thinking effort",
                REASONING_EFFORT_CHOICES,
            )
        if engine == "codex":
            with ui.row().classes("sop-row"):
                _bind_input("codex_base_url", "Codex base URL (responses API)")
                _bind_input("codex_api_key", "Codex API key", password=True)

        # Structured decision service (jev): opt-in final arbiter for the
        # textual judgment calls. Deterministic gates stay hard.
        theme.group_label("Decision arbiter (jev)")
        with ui.row().classes("sop-row"):
            _bind_switch(
                "enable_jev_decisions",
                "Jev decision arbiter",
            ).tooltip(
                "Ask the structured-decision service to arbitrate textual "
                "judgment calls: terminal / stand-down wording, MET-claim "
                "contradictions, stagnation, protocol flags, DONE proposals. "
                "Falls back to the text heuristics when the service is "
                "unavailable.",
            )
            _bind_input(
                "jev_decision_url",
                "Jev decision URL",
                classes="flex-grow mono",
            ).tooltip(
                "POST target of the noul/choice decision API, e.g. "
                "http://DECISION_HOST/systemone/api/v1/systemone.",
            )
            _bind_input(
                "jev_decision_api_key",
                "Jev API key (optional)",
                password=True,
            )
            _bind_number("jev_decision_timeout", "Jev timeout (s)", min=1, max=300)
        with ui.row().classes("sop-row"):
            _bind_float(
                "jev_decision_threshold",
                "Jev confidence threshold",
                min=0.50, max=0.99,
            )
            _bind_float(
                "jev_choice_threshold",
                "Jev choice threshold",
                min=0.50, max=0.99,
            ).tooltip(
                "Minimum probability for a multi-way jev ruling (DONE, "
                "stagnation, protocol review, unblock plan) to take effect.",
            )

        theme.group_label("Run behavior")
        with ui.row().classes("w-full gap-6 flex-wrap"):
            _bind_switch("enable_occam_razor", "Occam Razor")
            _bind_switch("enable_supervisor_skills", "Supervisor skills")

            def _log_detail_change(e) -> None:
                app_state["log_detail"] = "full" if e.value else "headline"
                _save()

            ui.switch(
                "Full job logs (debug)",
                value=(app_state.get("log_detail") or "headline") == "full",
                on_change=_log_detail_change,
            ).tooltip(
                "Off: one key line per event, full bodies kept in the job's "
                "sidecar (errors always inline). On: verbatim events.",
            )

        theme.group_label("Limits")
        with ui.row().classes("sop-row"):
            _bind_number("max_retries", "Max retries", min=1, max=10)
            _bind_number("context_threshold", "Context threshold %", min=10, max=95)
            _bind_number("max_tokens", "Max tokens", min=1000, max=2000000)
            # The cap is a ceiling, not the value: a long compute turn (MATLAB
            # backtest, big batch) needs far more than two hours, and a 120
            # ceiling silently clamped whatever the operator typed.
            _bind_number("timeout", "Turn timeout (min)", min=1, max=1440).tooltip(
                "Wall clock per turn before the agent engine is killed. "
                "Default 120 min; raise it for long compute turns "
                "(1440 = 24 h). Enforced as config.timeout * 60 seconds.",
            )
            _bind_number("max_parallel_jobs", "Max parallel jobs", min=1, max=32)
            _bind_number(
                "max_stored_jobs", "Stored jobs (log retention)",
                min=5, max=1000, classes="w-56",
            )

        theme.group_label("Maintenance")
        # The detail lives in the tooltips so the labels never ellipsize.
        with ui.row().classes("sop-row"):
            npm_registry = _bind_input("npm_registry", "npm registry for CLI upgrades",
                                       classes="flex-grow mono")
            npm_registry.tooltip("Empty = the default registry.")
            skills_dir = _bind_input(
                "supervisor_skills_dir",
                "Supervisor skills extra dir (optional)",
                classes="flex-grow mono",
                on_change=_skills_listing.refresh,
            )
            skills_dir.tooltip(
                "Beyond the built-in bank and the workspace's "
                ".opencode/supervisor_skills directory.",
            )
        ui.button(
            "Clean artifacts", on_click=lambda: _clean_artifacts(),
        ).props("flat dense").tooltip(
            "Delete build artifacts and caches in the configured workspace.",
        )
        _skills_listing()


def _set_model(value: str) -> None:
    if value and value != "(custom)":
        _stash_protocol_draft()
        app_state["opencode_model"] = value
        # Variant names are model-specific — drop a stale selection and
        # reload so the variant droplist re-aligns with the new model.
        app_state["agent_reasoning"] = ""
        app_state.flush()
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
        _stash_protocol_draft()
        app_state.opencode_models = models
        app_state.model_list_engine = engine
        if not app_state.get("opencode_model"):
            app_state["opencode_model"] = models[0]
        app_state.flush()
        ui.notify(f"Fetched {len(models)} {engine} models — reloading page.")
        ui.run_javascript("location.reload()")
    else:
        ui.notify(
            f"No models returned for {engine} "
            "(codex needs a base URL with a /models endpoint; "
            "pi needs a working `pi --list-models`).",
        )


# ── Access & protection ──────────────────────────────────────────────────────


@ui.refreshable
def _protection_section() -> None:
    """One panel for everything that bounds what the agent may touch."""
    with _section(
        "Access & Protection", anchor="access", icon="shield",
        caption="Read-only paths, protected files, ignore list",
    ):
        theme.hint(
            "What the agent may touch: read-only reference directories, files it "
            "must never modify, and the ignore list. Saved to "
            "access_protection.json in the workspace; the config files protect "
            "themselves automatically.",
        )
        workspace = _workspace()
        if not workspace:
            theme.notice("warn", "Set a valid workspace path in Configuration first.")
            return
        # create=False: only reading this page must not write into the
        # operator's workspace. The profile file is created by the first real
        # edit (_profile_save), which is the moment the decision is made.
        _profile_sync(workspace)

        status = ui.label("").classes("sop-meta")
        # Same gate as the Live Run page's connectivity lock: the generator
        # reads the SAVED protocol, so with no protocol on disk the button is
        # disabled and a standing notice says what to do first. A click that
        # silently generates from an empty document is worse than a lock.
        ready, reason = _protocol_ready(workspace)
        gate_label = theme.notice("warn", reason)
        gate_label.set_visibility(not ready)

        async def _auto_generate() -> None:
            from supervisor.security.access_profile import (
                apply_generation,
                mark_configured,
            )

            if not ready_now():
                return
            app_state.flush()
            app_state.apply_api_config()
            notes: list[str] = []
            paths: list[str] = []
            status.set_text("Starting…")
            status.update()
            async with busy_buttons(auto_button):
                step = {"n": 1}
                started = time.monotonic()

                def _elapsed() -> str:
                    return f"{time.monotonic() - started:.0f}s"

                def _live(phase: str, content: int, reasoning: int) -> None:
                    # Called from the io_bound worker thread; NiceGUI queues
                    # the update onto this client's loop.
                    detail = (
                        f"{phase} — {reasoning:,} reasoning chars · "
                        f"{content:,} answer chars · {_elapsed()}"
                    )
                    status.set_text(detail)
                    status.update()

                def _on_phase(note: str) -> None:
                    _live(f"{step['n']}/2 {note}", 0, 0)

                def _on_stream(content: int, reasoning: int) -> None:
                    phase = "waiting for the model" if not (content or reasoning) else (
                        "reasoning" if not content else "writing answer"
                    )
                    _live(f"{step['n']}/2 {phase}", content, reasoning)

                step["n"] = 1
                _live(
                    "1/2 suggesting read-only directories (LLM) — waiting for the "
                    "model",
                    0, 0,
                )
                try:
                    result = await nicegui_run.io_bound(
                        _suggest_whitelist_paths,
                        workspace,
                        _protocol_text(workspace),
                        _list_str("access_whitelist"),
                        _on_phase,
                        _on_stream,
                    )
                    paths = [str(item.path) for item in result.accepted]
                    if result.rejected:
                        notes.append(
                            f"{len(result.rejected)} suggestion(s) rejected by validation",
                        )
                except Exception as exc:  # noqa: BLE001 — surfaced in the status
                    notes.append(
                        f"whitelist suggestion failed: "
                        f"{type(exc).__name__}: {str(exc)[:200]}",
                    )
                    _live(f"1/2 FAILED after {_elapsed()}", 0, 0)
                step["n"] = 2
                _live(
                    "2/2 generating ignore patterns (LLM) — waiting for the model",
                    0, 0,
                )
                patterns = ""
                try:
                    patterns = await nicegui_run.io_bound(
                        _suggest_ignore_patterns,
                        workspace,
                        lambda c, r, _k: _on_stream(c, r),
                        None,
                    )
                except Exception as exc:  # noqa: BLE001 — surfaced in the status
                    notes.append(
                        f"ignore suggestion failed: "
                        f"{type(exc).__name__}: {str(exc)[:200]}",
                    )
                    _live(f"2/2 FAILED after {_elapsed()}", 0, 0)
                # apply_generation + _profile_sync WRITE profile/ignore files;
                # an OSError there escaped into NiceGUI's async task, which only
                # logs to stderr, so the button looked dead while nothing was
                # applied and no reason was shown.
                try:
                    summary = apply_generation(workspace, paths, patterns)
                    _profile_sync(workspace)
                    # The step has run: the hand-off button at the page bottom
                    # may unlock. Warnings stay visible in the status line, and
                    # the operator decides when to leave.
                    mark_configured(workspace)
                except Exception as exc:  # noqa: BLE001 — must never be silent
                    status.set_text(
                        f"Auto-generate failed while saving: "
                        f"{type(exc).__name__}: {str(exc)[:200]}",
                    )
                    return
            _access_section.refresh()
            _protected_section.refresh()
            _ignore_section.refresh()
            _ready_section.refresh()
            _stepper.refresh()
            status.set_text(
                f"Auto-generated: +{len(summary['whitelist_added'])} read-only "
                f"dir(s); ignore {'updated' if summary['ignore_written'] else 'unchanged'}; "
                "config files protected."
                + (f" ({'; '.join(notes)})" if notes else ""),
            )
            # No automatic jump: the operator reviews the result and leaves via
            # the "Ready for Live Run!" button at the bottom of the page.

        def ready_now() -> bool:
            # Re-checked at click time: the protocol may have been saved (or
            # removed) since this panel rendered, and the gate must follow the
            # file, not a stale render-time capture.
            ok, _ = _protocol_ready(workspace)
            gate_label.set_visibility(not ok)
            auto_button.set_enabled(ok)
            return ok

        with ui.row().classes("items-center gap-3 flex-wrap"):
            auto_button = ui.button(
                "Auto-generate access & protection (LLM)", on_click=_auto_generate,
            ).props("dense color=primary").tooltip(
                "One LLM pass: suggests read-only reference directories from the "
                "saved protocol, generates .opencodeignore patterns, validates "
                "every entry, and saves both into the workspace.",
            )
            if not ready:
                auto_button.set_enabled(False)

        _access_section()
        _protected_section()
        _ignore_section()


@ui.refreshable
def _protected_section() -> None:
    with ui.column().classes("w-full gap-2"):
        theme.group_label("Protected files")
        theme.hint(
            "Files the agent may not modify or delete. Saved in "
            "access_protection.json; its own config files are locked.",
        )
        workspace = _workspace()
        if not workspace:
            ui.label("Set a valid workspace path first.").classes("sop-hint")
            return
        add_select = ui.select(
            [], multiple=True, label="Add protected files (workspace scan)",
        ).classes("w-full")
        options_holder: dict[str, list[str]] = {}

        async def _load_options() -> None:
            entries = await nicegui_run.io_bound(_scan_workspace, workspace)
            options_holder["all"] = entries
            add_select.options = entries[:_DROPLIST_FILES]
            add_select.update()

        # Cheap local scan: no button, populate as soon as the panel renders.
        ui.timer(0.05, _load_options, once=True)

        def _add(e) -> None:
            chosen = list(e.value or [])
            if chosen:
                current = list(app_state.get("protected_files") or [])
                merged = sorted(set(current) | set(chosen))
                app_state["protected_files"] = merged
                _save()
                _profile_save(workspace, configured=True)
                add_select.value = []
                _refresh_chips(merged, options_holder, chips_container)

        add_select.on_value_change(_add)

        chips_container = ui.column().classes("w-full gap-1")
        _refresh_chips(list(app_state.get("protected_files") or []), options_holder, chips_container)


def _scan_workspace(workspace: Path) -> list[str]:
    return _list_workspace_files(workspace, _MAX_LISTED_FILES)


def _refresh_chips(protected: list[str], options_holder: dict, container) -> None:
    from supervisor.security.access_profile import CONFIG_FILES

    container.clear()
    with container:
        if not protected:
            ui.label("No protected files.").classes("sop-hint")
        for path in protected:
            auto = path.lower() in {name.lower() for name in CONFIG_FILES}
            with ui.row().classes(
                "items-center gap-2 flex-nowrap w-full sop-listrow",
            ):
                ui.icon("lock").style("color:var(--sop-warn);font-size:15px")
                ui.label(path).classes("mono text-xs").style(
                    "color:var(--sop-text-2)",
                )
                if auto:
                    ui.html('<span class="chip">config · auto</span>')
                else:
                    ui.space()
                    ui.button(
                        icon="close",
                        on_click=lambda p=path: _remove_protected(p, options_holder, container),
                    ).props("flat dense").tooltip("Stop protecting this file")


def _remove_protected(path: str, options_holder: dict, container) -> None:
    merged = [p for p in (app_state.get("protected_files") or []) if p != path]
    app_state["protected_files"] = merged
    _save()
    workspace = _workspace()
    if workspace:
        _profile_save(workspace, configured=True)
    _refresh_chips(merged, options_holder, container)


# ── Access whitelist ──────────────────────────────────────────────────────────


@ui.refreshable
def _access_section() -> None:
    from supervisor.security.access_policy import validate_whitelist_entry

    with ui.column().classes("w-full gap-2"):
        theme.group_label("Path access whitelist (hard rule)")
        theme.hint(
            "The project cwd is read/write. Whitelisted directories are READ-ONLY. "
            "Everything else is denied for every backend (opencode, codex, pi), "
            "regardless of the agent's own rules. Shell commands are parsed; "
            "dynamic constructs (substitutions, unknown variables, encoded "
            "commands) are denied outright.",
        )
        workspace = _workspace()
        if not workspace:
            ui.label("Set a valid workspace path first.").classes("sop-hint")
            return

        def _persist() -> None:
            _profile_save(workspace, configured=True)
            _drill_verdict.refresh()

        with ui.row().classes("w-full gap-6 flex-wrap"):
            _bind_switch(
                "access_policy_enabled", "Enforce the hard path policy",
                on_change=_persist,
            ).props("dense")
            _bind_switch(
                "access_policy_strict",
                "Strict: also deny interpreter execution (python/node/perl scripts)",
                on_change=_persist,
            ).props("dense")

        status = ui.label("").classes("sop-meta")

        with ui.row().classes("w-full items-end gap-2 flex-nowrap"):
            path_input = ui.input(
                "Add a read-only reference directory (absolute path)",
            ).classes("grow mono")
            add_button = ui.button("Add").props("outline color=none dense")
            add_button.on_click(lambda: _add_path())
            path_input.on("keydown.enter", lambda _e: _add_path())

        def _add_path(value: str | None = None) -> None:
            candidate = (value if value is not None else path_input.value or "").strip()
            if not candidate:
                status.set_text("Enter an absolute directory path.")
                return
            resolved, error = validate_whitelist_entry(candidate, workspace)
            if resolved is None:
                status.set_text(f"Rejected: {error}")
                return
            current = _list_str("access_whitelist")
            key = str(resolved).lower()
            if key not in {item.lower() for item in current}:
                current.append(str(resolved))
                app_state["access_whitelist"] = current
                _save()
                _profile_save(workspace, configured=True)
            path_input.value = ""
            status.set_text(f"Added read-only: {resolved}")
            chips.refresh()
            _drill_verdict.refresh()

        @ui.refreshable
        def chips() -> None:
            current = _list_str("access_whitelist")
            if not current:
                ui.label("No whitelisted directories.").classes("sop-hint")
                return
            for entry in current:
                with ui.row().classes(
                    "items-center gap-2 flex-nowrap w-full sop-listrow",
                ):
                    ui.icon("visibility").style(
                        "color:var(--sop-accent-2);font-size:15px",
                    )
                    ui.label(entry).classes("mono text-xs grow truncate").style(
                        "color:var(--sop-text-2)",
                    )
                    ui.html('<span class="chip chip--accent">read-only</span>')
                    ui.button(
                        icon="close",
                        on_click=lambda e=entry: _remove_whitelist(e),
                    ).props("flat dense").tooltip("Remove from the whitelist")

        chips()

        @ui.refreshable
        def _drill_verdict() -> None:
            # Deterministic and cheap: never a button, and re-run on every
            # whitelist/policy change so the verdict is always current.
            from supervisor.security.access_policy import AccessPolicy
            from supervisor.security.preflight import run_policy_drill

            policy = AccessPolicy(
                workspace=workspace,
                whitelist=tuple(Path(item) for item in _list_str("access_whitelist")),
                enabled=bool(app_state.get("access_policy_enabled", True)),
                strict_dynamic=bool(app_state.get("access_policy_strict", False)),
            )
            report = run_policy_drill(workspace, policy)
            theme.notice(
                "ok" if report.ok else "bad",
                ("Policy drill passed — " if report.ok else "Policy drill FAILED — ")
                + report.summary(),
            )
            if not report.ok:
                ui.label(report.fail_text()).classes(
                    "text-xs mono whitespace-pre-wrap",
                ).style("color:var(--sop-bad)")

        _drill_verdict()


def _list_str(key: str) -> list[str]:
    value = app_state.get(key) or []
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    return [str(item) for item in value]


def _remove_whitelist(entry: str) -> None:
    app_state["access_whitelist"] = [
        item for item in _list_str("access_whitelist") if item != entry
    ]
    _save()
    workspace = _workspace()
    if workspace:
        _profile_save(workspace, configured=True)
    _access_section.refresh()


def _protocol_text(workspace: Path) -> str:
    from supervisor.protocols.protocol import read_protocol_text, resolve_protocol_path

    return read_protocol_text(resolve_protocol_path(workspace))[:6000]


def _protocol_ready(workspace: Path) -> tuple[bool, str]:
    """Whether the SAVED protocol exists, and why not when it does not.

    The access auto-generator reads the protocol FROM DISK: an unsaved draft
    in the editor above is invisible to it, so generating first would propose
    read-only directories from nothing. Same rule as the Live Run page's
    connectivity lock: no silent no-op, and the button says why.
    """
    from supervisor.protocols.protocol import resolve_protocol_path

    path = resolve_protocol_path(workspace)
    if not path.is_file():
        return False, (
            "⚠ No saved protocol yet — write INPUT / TARGET / RESTRICTIONS in "
            "the Protocol section above, then click “Accept & Save”. The "
            "auto-generator reads the protocol from disk, so an unsaved draft "
            "is not visible to it."
        )
    if not _protocol_text(workspace).strip():
        return False, (
            f"⚠ {path.name} is empty — fill in the Protocol section above and "
            "save it before auto-generating access & protection."
        )
    return True, ""


def _suggest_whitelist_paths(
    workspace: Path,
    protocol_text: str,
    existing: list[str],
    on_phase=None,
    on_stream=None,
):
    from supervisor.security.whitelist_advisor import suggest_with_supervisor_llm

    return suggest_with_supervisor_llm(
        workspace,
        protocol_text,
        api_key=str(app_state.get("openai_key") or ""),
        base_url=str(app_state.get("base_url") or ""),
        model=str(app_state.get("supervisor_model") or "swift-1.5"),
        existing=existing,
        on_progress=on_stream,
        on_phase=on_phase,
    )


# ── Ignore editor ─────────────────────────────────────────────────────────────


@ui.refreshable
def _ignore_section() -> None:
    from supervisor.workspace.ignore_patterns import IGNORE_FILE, write_ignore_file

    with ui.column().classes("w-full gap-2"):
        theme.group_label(".opencodeignore")
        theme.hint(
            "Glob patterns the agent must leave alone. Auto-saved on every edit.",
        )
        workspace = _workspace()
        if not workspace:
            ui.label("Set a valid workspace path first.").classes("sop-hint")
            return

        ignore_path = workspace / IGNORE_FILE
        current = (
            ignore_path.read_text(encoding="utf-8") if ignore_path.exists() else ""
        )
        area = ui.textarea(".opencodeignore content", value=current).classes(
            "w-full mono",
        )
        status = ui.label("").classes("sop-hint")

        def _autosave(e) -> None:
            write_ignore_file(workspace, e.value or "")
            status.set_text("auto-saved")

        area.on_value_change(_autosave)


def _suggest_ignore_patterns(
    workspace: Path,
    on_progress=None,
    stop_event=None,
) -> str:
    from supervisor.utils.llm_stream import make_stream_client, stream_chat_text
    from supervisor.utils.thinking_probe import lowest_thinking_effort

    if on_progress is not None:
        on_progress(0, 0, 0)
    entries = _list_workspace_files(workspace, _IGNORE_SUGGEST_FILES)
    base_url = app_state.get("base_url") or ""
    model = app_state.get("supervisor_model") or "swift-1.5"
    client = make_stream_client(
        api_key=app_state.get("openai_key"),
        base_url=base_url or None,
    )
    # Cheapest thinking level this endpoint actually honours (measured, not
    # assumed: providers differ in accepted names and a proxy may clamp).
    effort = lowest_thinking_effort(client, model, base_url)
    return stream_chat_text(
        client,
        model,
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
        on_progress=on_progress,
        stop_event=stop_event,
        extra_body={"reasoning_effort": effort} if effort else None,
    )


# ── Connectivity tests ────────────────────────────────────────────────────────


def _connectivity_section() -> None:
    from services.config.connectivity import (
        test_agent_connectivity,
        test_supervisor_connectivity,
    )

    # Engine is captured at render time; safe because switching the engine
    # reloads the whole page (see _set_engine).
    # Not collapsible: the first-run flow is "configure the LLM, then prove
    # it", so the buttons sit on the page right below the config.
    def _head() -> None:
        ui.label("Connectivity Tests").classes("sop-panel__title")
        ui.label(
            "Two probes: the agent engine and the judge model. Both must pass "
            "before Live Run unlocks.",
        ).classes("sop-panel__sub")

    with theme.panel(icon="cable", head=_head, anchor="connectivity"):
        engine = app_state.get("engine") or "opencode"
        with ui.row().classes("items-center gap-3 flex-wrap"):
            status = ui.label("").classes("sop-meta grow whitespace-pre-line")
            _refresh_status(status)

        async def _run(kind: str) -> None:
            if not (app_state.get("workspace") or "").strip():
                ui.notify("Set workspace path before running tests.")
                return
            status.set_text("Testing… (agent up to ~60s)")
            async with busy_buttons(*test_buttons):
                # The probes swallow their own transport failures, but the temp
                # dir mkdir (connectivity._ephemeral_workspace) and the OpenAI
                # client constructor run OUTSIDE those guards and can raise
                # OSError / ValueError. Uncaught they vanished into NiceGUI's
                # async task while the buttons re-enabled: a dead-looking Test.
                results: dict[str, tuple[bool, str]] = {}
                try:
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
                        results[str(engine)] = (ok, message)
                    if kind in ("supervisor", "all"):
                        ok, message = await nicegui_run.io_bound(
                            test_supervisor_connectivity,
                            app_state.get("openai_key"),
                            app_state.get("supervisor_model") or "swift-1.5",
                            app_state.get("base_url") or None,
                        )
                        app_state.flags["supervisor_test_passed"] = ok
                        results["supervisor"] = (ok, message)
                except Exception as exc:  # noqa: BLE001 — must never be silent
                    status.set_text(
                        f"Connectivity test failed: {type(exc).__name__}: "
                        f"{str(exc)[:200]}",
                    )
                    return
                # One line per probe: a single label meant the second result
                # overwrote the first, so a failed agent probe could look like
                # a pass because the supervisor one succeeded.
                status.set_text(
                    "\n".join(
                        f"{'✅' if ok else '❌'} {name}: {message[:140]}"
                        for name, (ok, message) in results.items()
                    ),
                )
                # Persist the measurement so a restart does not re-lock Launch
                # for the same, unchanged configuration.
                from services.webui import probes

                probes.record(app_state.tests_ok(), app_state.as_mapping())
                if app_state.tests_ok():
                    status.set_text(
                        status.text
                        + "\n✅ Both connectivity tests passed — Live Run unlocked.",
                    )
                # The hand-off button at the page bottom is gated on these
                # results, so it must re-evaluate as soon as they change.
                _ready_section.refresh()
                _stepper.refresh()

        with ui.row().classes("gap-3 flex-wrap items-center"):
            test_buttons = [
                ui.button("Run Tests", on_click=lambda: _run("all")).props(
                    "color=primary",
                ),
                ui.button(f"Test {engine}", on_click=lambda: _run("agent")).props(
                    "outline color=none",
                ),
                ui.button(
                    "Test Supervisor", on_click=lambda: _run("supervisor"),
                ).props("outline color=none"),
            ]
            if app_state.tests_ok():
                ui.html('<span class="chip chip--ok">both probes passed</span>')
            else:
                ui.html('<span class="chip chip--warn">probes not passed yet</span>')


def _refresh_status(_label) -> None:
    if app_state.tests_ok():
        ui.notify("Both connectivity tests passed.")


# ── Protocol editor + refine flow ─────────────────────────────────────────────


def _compose_protocol(sections: dict[str, str]) -> str:
    return (
        f"## INPUT\n\n{sections.get('input', '').strip()}\n\n"
        f"## TARGET\n\n{sections.get('target', '').strip()}\n\n"
        f"## RESTRICTIONS\n\n{sections.get('restrictions', '').strip()}\n"
    )


def _quality_score(value: float) -> tuple[str, str]:
    """(tone, colour) for a 0..1 completeness score."""
    if value >= 0.8:
        return "ok", "var(--sop-ok)"
    if value >= 0.5:
        return "warn", "var(--sop-warn)"
    return "bad", "var(--sop-bad)"


def _update_quality(labels: dict, sections: dict[str, str]) -> None:
    """Refresh the four score chips under the protocol editor.

    Chips instead of one long sentence: completeness is a set of independent
    numbers, and a sentence forced the operator to parse it every keystroke.
    """
    from supervisor.protocols.protocol_analyzer import ProtocolAnalyzer

    try:
        analysis = ProtocolAnalyzer().analyze_text(_compose_protocol(sections))
    except Exception:  # noqa: BLE001 - UI action boundary, keep the page alive
        for label in labels.values():
            label.set_text("—")
        return
    scores = {
        "overall": analysis.overall_score,
        "input": analysis.input_score.overall,
        "target": analysis.target_score.overall,
        "restrictions": analysis.restrictions_score.overall,
    }
    for key, value in scores.items():
        tone, color = _quality_score(float(value))
        label = labels.get(key)
        if label is None:
            continue
        label.set_text(f"{value:.0%}")
        label.style(f"color:{color}")
    issue_label = labels.get("issues")
    if issue_label is not None:
        count = len(analysis.issues)
        issue_label.set_text(
            f"{count} issue(s)" if count else "no issues",
        )
        issue_label.style(
            "color:" + ("var(--sop-warn)" if count else "var(--sop-text-3)"),
        )


@ui.refreshable
def _protocol_section() -> None:
    """The workspace protocol as one field per section (loaded, never residual)."""
    from supervisor.protocols.protocol import load_protocol, resolve_protocol_path

    with _section(
        "Protocol", anchor="protocol", open_=True, icon="description",
        caption="What the task is: context, objectives, boundaries",
    ):
        workspace = _workspace()
        if not workspace:
            theme.notice(
                "warn",
                "Set a valid workspace path in Configuration — the protocol is "
                "read from and written to that folder.",
            )
            return

        path = resolve_protocol_path(workspace)
        loaded = None
        if path.is_file():
            try:
                loaded = load_protocol(path)
            except (OSError, ValueError) as exc:
                theme.notice("bad", f"Could not load {path.name}: {exc}")
            else:
                theme.notice(
                    "ok", f"Loaded {path.name} from {workspace.name}.",
                )
        else:
            theme.notice(
                "info",
                "No protocol yet — fill the three sections, refine with AI, "
                "then Accept & Save. Access & Protection unlocks on save.",
            )

        areas = {}
        for key, label in (
            ("input", "INPUT (context)"),
            ("target", "TARGET (objectives)"),
            ("restrictions", "RESTRICTIONS (boundaries)"),
        ):
            areas[key] = ui.textarea(
                label,
                value=getattr(loaded, f"{key}_section", "") if loaded else "",
            ).classes("w-full mono").props("autogrow").style("min-height: 4.5rem").style("min-height: 4.5rem")

        def _sections() -> dict[str, str]:
            return {key: str(area.value or "") for key, area in areas.items()}

        # Register the live getter so any reload-triggering control (engine,
        # model, fetch) can stash this text first; restore it here on the way
        # back in. Without both halves the draft disappeared with no warning.
        _draft_slot["workspace"] = str(workspace)
        _draft_slot["getter"] = _sections
        draft = _take_protocol_draft(workspace)
        if draft:
            for key, area in areas.items():
                if draft.get(key):
                    area.set_value(draft[key])
            theme.notice(
                "warn",
                "Restored an unsaved protocol draft from before the last page "
                "reload — Accept & Save to keep it.",
            )

        # Quality: four independent numbers, read at a glance.
        with ui.row().classes("sop-chips items-center"):
            chips: dict[str, object] = {}
            for key, label in (
                ("overall", "overall"), ("input", "INPUT"), ("target", "TARGET"),
                ("restrictions", "RESTRICTIONS"),
            ):
                ui.label(label).classes("sop-hint")
                chips[key] = ui.label("—").classes("mono text-xs font-bold")
            chips["issues"] = ui.label("").classes("sop-hint")
        _update_quality(chips, _sections())
        for area in areas.values():
            area.on_value_change(
                lambda _e: _update_quality(chips, _sections()),
            )

        status = ui.label("").classes("sop-meta")
        with ui.row().classes("items-center gap-2 flex-wrap"):
            refine_button = ui.button(
                "Refine with AI",
                on_click=lambda: _refine(
                    workspace, _sections, areas, status, refine_button, cancel_button,
                ),
            ).props("outline color=none")
            cancel_button = ui.button(
                "Cancel", on_click=lambda: _cancel_refine(status),
            ).props("outline color=red")
            cancel_button.set_visibility(False)
            ui.button(
                "Accept & Save",
                on_click=lambda: _accept_protocol(workspace, _sections),
            ).props("color=primary")

        _feasibility_section(_sections)


def _feasibility_section(sections_getter) -> None:
    """Acceptance gates live with the protocol; every edit auto-saves."""
    workspace = _workspace()
    if not workspace:
        return
    from services.webui.components.feasibility_panel import render as render_gates

    render_gates(workspace, lambda: _compose_protocol(sections_getter()))


def _accept_protocol(workspace: Path, sections_getter) -> None:
    from supervisor.protocols.protocol import parse_protocol_text, save_protocol
    from supervisor.protocols.target_audit import audit_target

    try:
        protocol = parse_protocol_text(_compose_protocol(sections_getter()))
    except ValueError as exc:
        ui.notify(f"Protocol invalid: {exc}")
        return
    audit = audit_target(protocol.target_section, protocol.restrictions_section)
    if not audit.is_actionable:
        ui.notify("TARGET not ready: " + " ".join(audit.issues))
        return
    path = save_protocol(workspace, protocol)
    # The draft is now on disk; a stale stash would resurrect it on the next
    # render and make the page disagree with the file.
    try:
        nicegui_app.storage.general.pop(f"protocol_draft::{workspace}", None)
    except Exception:  # noqa: BLE001
        pass
    # Refresh the workspace profile so the new config file is protected too.
    _profile_save(workspace)
    # Stay on the page: Access & Protection sits directly below and only
    # unlocks once the protocol is on disk, so jumping to Live Run here sent
    # the operator away mid-setup (and made them find their way back). The
    # nav drawer still reaches Live Run whenever they are actually done.
    _protection_section.refresh()
    _stepper.refresh()
    ui.notify(
        f"{path.name} saved to {workspace.name} — Access & Protection below "
        "is now unlocked.",
    )


async def _refine(
    workspace: Path | None, sections_getter, areas, status, refine_button, cancel_button,
) -> None:
    sections = sections_getter()
    if not all(sections[key].strip() for key in ("input", "target", "restrictions")):
        ui.notify("Fill INPUT, TARGET and RESTRICTIONS first.")
        return
    status.set_text(
        "Refining protocol with AI… (the model reasons at length before answering)",
    )
    stop_event = _refine_call.begin()
    cancel_button.set_visibility(True)
    async with busy_buttons(refine_button):
        from supervisor.protocols.protocol_wizard import (
            ProtocolGateError,
            ProtocolWizard,
        )

        app_state.flush()
        app_state.apply_api_config()
        try:
            wizard = ProtocolWizard(
                model=app_state.get("supervisor_model") or "swift-1.5",
                api_key=app_state.get("openai_key") or None,
                base_url=app_state.get("base_url") or None,
            )
            started = time.monotonic()

            def _on_progress(
                content_chars: int, reasoning_chars: int, _chunks: int,
            ) -> None:
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

            markdown, protocol = await nicegui_run.io_bound(
                wizard.refine,
                sections["input"],
                sections["target"],
                sections["restrictions"],
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
        except Exception as exc:  # noqa: BLE001 - UI action boundary - surface the failure, keep the page alive
            status.set_text(f"Refine failed: {exc}")
            return
        finally:
            _refine_call.end()
            cancel_button.set_visibility(False)

    # Refined sections replace the editor content in place; nothing touches
    # the workspace until Accept & Save.
    areas["input"].value = protocol.input_section
    areas["target"].value = protocol.target_section
    areas["restrictions"].value = protocol.restrictions_section
    status.set_text("Refined — drafting feasibility gates…")
    status.update()
    try:
        from services.webui.components.feasibility_panel import draft_and_save

        if workspace is None:
            status.set_text("Refined — set a workspace to save gates.")
        else:
            count = await nicegui_run.io_bound(draft_and_save, workspace, markdown)
            status.set_text(
                f"Refined — {count} feasibility gate(s) auto-saved. "
                "Review, then Accept & Save."
                if count
                else "Refined — no gates could be drafted from the TARGET.",
            )
    except Exception as exc:  # noqa: BLE001 — the protocol itself is still valid
        status.set_text(
            "Refined — gate draft failed: "
            f"{str(exc)[:160]} (draft them in the gates panel below).",
        )


# ── Hand-off to Live Run ──────────────────────────────────────────────────────


def _setup_blockers(workspace: Path | None) -> list[str]:
    """What still stands between the operator and Live Run, in setup order."""
    from supervisor.security.access_policy import AccessPolicy
    from supervisor.security.access_profile import load_profile
    from supervisor.security.preflight import run_policy_drill

    if workspace is None:
        return ["Set a valid workspace path in Configuration."]
    blockers: list[str] = []
    if not app_state.tests_ok():
        blockers.append("Connectivity Tests: run them above — both must pass.")
    protocol_ok, _reason = _protocol_ready(workspace)
    if not protocol_ok:
        blockers.append("Protocol: fill the sections and click “Accept & Save”.")
    profile = load_profile(workspace)
    if not profile.get("access_configured"):
        blockers.append(
            "Access & Protection: auto-generate it, or edit it by hand.",
        )
    policy = AccessPolicy(
        workspace=workspace,
        whitelist=tuple(
            Path(item) for item in (profile.get("access_whitelist") or [])
        ),
        enabled=bool(profile.get("access_policy_enabled", True)),
        strict_dynamic=bool(profile.get("access_policy_strict", False)),
    )
    report = run_policy_drill(workspace, policy)
    if not report.ok:
        blockers.append(
            f"Access & Protection: policy drill failed — {report.fail_text()}",
        )
    return blockers


def _setup_steps(workspace: Path | None) -> list[tuple[str, bool]]:
    """The five setup steps in the order the page presents them.

    One source for both the progress strip and the hand-off checklist, so the
    two can never disagree about what "done" means.
    """
    from supervisor.security.access_policy import AccessPolicy
    from supervisor.security.access_profile import load_profile
    from supervisor.security.preflight import run_policy_drill

    configured = bool((app_state.get("openai_key") or "").strip()) and workspace is not None
    protocol_ok = False
    if workspace is not None:
        protocol_ok, _reason = _protocol_ready(workspace)
    access_ok = False
    if workspace is not None:
        profile = load_profile(workspace)
        drill_ok = run_policy_drill(
            workspace,
            AccessPolicy(
                workspace=workspace,
                whitelist=tuple(
                    Path(item) for item in (profile.get("access_whitelist") or [])
                ),
                enabled=bool(profile.get("access_policy_enabled", True)),
                strict_dynamic=bool(profile.get("access_policy_strict", False)),
            ),
        ).ok
        access_ok = bool(profile.get("access_configured")) and drill_ok
    tested = app_state.tests_ok()
    return [
        # (label, done, anchor) — the anchor is what the stepper links to.
        ("Configure", configured, "configuration"),
        ("Test", tested, "connectivity"),
        ("Protocol", protocol_ok, "protocol"),
        ("Protect", access_ok, "access"),
        ("Launch", configured and tested and protocol_ok and access_ok, "handoff"),
    ]


@ui.refreshable
def _stepper() -> None:
    """Where the operator is in the five-step path — the page's table of contents.

    The page is long by necessity (credentials, probes, protocol, protections,
    hand-off). This strip answers "what is left" without scrolling, which is
    the whole point of a disclosure hierarchy.
    """
    steps = _setup_steps(_workspace())
    done_count = sum(1 for _label, done, _anchor in steps if done)
    current = next(
        (index for index, step in enumerate(steps) if not step[1]), len(steps) - 1,
    )
    with ui.row().classes("sop-steps"):
        for index, (label, done, anchor) in enumerate(steps):
            state = "done" if done else ("current" if index == current else "todo")
            # A real link, not a styled div: clickable, focusable, and it works
            # with the keyboard and the browser's own history.
            link = ui.element("a").classes(f"sop-step sop-step--{state}").props(
                f'href="#{anchor}" aria-label="{label}"',
            )
            if index == current:
                link.props('aria-current="step"')
            with link:
                if done:
                    # A check mark, not a green number: colour alone left done
                    # and current indistinguishable for colour-blind users.
                    with ui.element("div").classes("sop-step__num"):
                        ui.icon("check").style("font-size:14px")
                else:
                    ui.label(str(index + 1)).classes("sop-step__num")
                ui.label(label).classes("sop-step__label")
    # The bottom hand-off is a page away from the top; naming the next single
    # action here is what turns a five-step checklist into a path.
    blockers = _setup_blockers(_workspace())
    with ui.row().classes("sop-next"):
        if blockers:
            ui.label("Next").classes("sop-kicker")
            ui.icon("arrow_right_alt").style(
                "color:var(--sop-accent-2);font-size:16px",
            )
            ui.label(blockers[0]).classes("sop-meta")
        else:
            ui.icon("check_circle").style("color:var(--sop-ok);font-size:15px")
            ui.label("Setup complete — hand off to Live Run at the bottom.").classes(
                "sop-meta",
            )
        ui.space()
        # Moved out of the strip: a progress count sharing the tab row read as
        # a sixth step.
        ui.label(f"{done_count}/{len(steps)} steps done").classes("sop-hint mono")


@ui.refreshable
def _ready_section() -> None:
    """The one deliberate exit to Live Run, unlocked by a complete setup.

    Nothing navigates on its own: finishing a step (auto-generate, protocol
    save, tests) leaves the operator on this page to review the result, and
    this button is the only hand-off.
    """
    blockers = _setup_blockers(_workspace())
    with theme.panel(
        "Hand-off", icon="rocket_launch", tone="" if blockers else "quiet",
        anchor="handoff",
        subtitle=(
            f"{len(blockers)} step(s) left before this page can be left"
            if blockers else "Every setup step and probe is complete"
        ),
    ):
        with ui.row().classes("w-full items-center gap-4 flex-wrap"):
            with ui.column().classes("grow gap-1 min-w-0"):
                if blockers:
                    ui.label("Locked until every setup step is complete:").classes(
                        "sop-hint",
                    )
                    for item in blockers:
                        with ui.row().classes("items-start gap-2 flex-nowrap"):
                            ui.icon("radio_button_unchecked").style(
                                "color:var(--sop-warn);font-size:14px;margin-top:2px",
                            )
                            ui.label(item).classes("sop-meta")
                else:
                    theme.notice(
                        "ok",
                        "Setup complete — the run page is unlocked and reads "
                        "this workspace and protocol.",
                    )
            with ui.column().classes("items-end gap-1"):
                if blockers:
                    # An outline, not a dimmed primary: "not yet" must not
                    # look like "nearly".
                    button = ui.button(
                        "Ready for Live Run!",
                        on_click=lambda: ui.navigate.to("/run"),
                    ).props("outline color=none size=lg")
                    button.set_enabled(False)
                    ui.label("unlocks automatically").classes("sop-hint")
                else:
                    button = ui.button(
                        "Ready for Live Run!",
                        on_click=lambda: ui.navigate.to("/run"),
                    ).props("color=primary size=lg")


# ── Page entry ────────────────────────────────────────────────────────────────


def render() -> None:
    theme.page_header(
        "Protocol Wizard",
        "Write the task down, prove the models answer, bound what the agent may "
        "touch — then hand off. Steps 1–4 are done once and reused by every run.",
        kicker="Setup",
    )
    # The page rendered with this workspace: any later edit triggers a
    # rebuild only when it actually resolves to a different one.
    workspace = _workspace()
    _last_refreshed_workspace["path"] = str(workspace) if workspace else ""
    _stepper()
    _config_section()
    # Directly after the LLM configuration: the tests prove that config.
    _connectivity_section()
    # The rarely-touched knobs (backups, reasoning, jev, limits) stay third:
    # they are set once and then left alone.
    _advanced_section()
    # Protocol BEFORE Access & Protection: the auto-generator derives the
    # read-only whitelist from the workspace protocol (and the ignore list
    # from the files it describes), so it cannot produce anything useful
    # until the protocol has been written and saved above it.
    _protocol_section()
    _protection_section()
    # Last: the hand-off to Live Run, greyed out until every step above is done.
    _ready_section()
