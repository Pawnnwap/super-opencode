"""Live Run page: protocol readiness, launch form, board, live job screen."""

from __future__ import annotations

from pathlib import Path

from nicegui import ui

from services.webui import theme
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
from services.webui.preflight import hard_blockers
from services.webui.state import app_state


def _has_protocol(workspace: Path) -> bool:
    """True when a protocol file actually resolves in this workspace."""
    from supervisor.protocols.protocol import resolve_protocol_path

    return resolve_protocol_path(workspace).is_file()


def _render_readiness(workspace: Path, *, ready: bool | None = None) -> bool:
    """Protocol readiness strip; returns True when a protocol file exists.

    ``ready`` is passed in when the caller already resolved it, so the page
    and the launch gate can never disagree about the same filesystem fact.
    """
    from supervisor.protocols.protocol import resolve_protocol_path

    if ready is None:
        ready = _has_protocol(workspace)
    proto_path = resolve_protocol_path(workspace)
    if ready:
        theme.notice(
            "ok",
            f"{proto_path.name} ready in {workspace.name} — this launch reads it.",
        )
        return True

    def _go() -> None:
        ui.button(
            "Open Protocol Wizard", on_click=lambda: ui.navigate.to("/"),
        ).props("outline color=none dense")

    theme.notice(
        "bad",
        "No protocol in this workspace — Live Run would have nothing to execute.",
        action=_go,
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


def _render_launch_form(
    workspace: Path,
    *,
    locked: bool = False,
    missing: list[str] | None = None,
) -> None:
    """Launch controls: parameters left, the single action right."""
    missing = list(missing or [])
    with theme.panel(
        "Start a task",
        icon="play_arrow",
        subtitle=f"Protocol: {workspace.name} · one job per launch",
    ):
        if locked:
            # The lock covers LAUNCHING only. The rest of the page (board,
            # history, live screens) stays usable: a locked user still needs
            # to reopen yesterday's run and read what happened.
            #
            # No CTA here on purpose: the readiness strip above already owns
            # the one "Open Protocol Wizard" button. Two identical CTAs for two
            # different causes was the thing that made this screen unreadable.
            theme.notice(
                "warn",
                "Launch is locked: "
                + " and ".join(missing or ["requirements are not met"])
                + ". Browsing and opening finished tasks below is unaffected.",
            )
        # Laid out flat on purpose: these are the settings a launch depends on,
        # and hiding them behind a disclosure made the effective options easy
        # to miss. The live summary keeps the values visible even so.
        with ui.row().classes("sop-row"):
            plan_rounds = ui.number(
                "Plan mode rounds",
                value=int(app_state.get("plan_mode_rounds", 1)),
                min=0,
                max=10,
                on_change=lambda _e: _update_summary(),
            ).classes("w-44")
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
        with ui.row().classes("sop-row"):
            replicas = ui.number(
                "Parallel instances",
                value=1,
                min=1,
                max=16,
                on_change=lambda _e: _update_summary(),
            ).classes("w-44").tooltip(
                ">1 clones this workspace N times (one isolated study per "
                "copy) and launches them as one batch.",
            )
            init_mode = ui.select(
                {"copy": "Copy workspace (artifacts excluded)",
                 "protocol": "protocol.md only"},
                value="copy",
                label="Replica init",
                on_change=lambda _e: _update_summary(),
            ).tooltip(
                "How each parallel instance's workspace is seeded.",
            ).classes("w-72")

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
                try:
                    if count > 1:
                        from services.webui.batch_launch import launch_study_batch

                        save_and_apply_settings()
                        sync_max_parallel_jobs()
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
                        ui.notify(f"Batch launched: {len(job_ids)} instance(s)")
                        target = f"/run/{job_ids[0]}"
                    else:
                        job_id = _launch_run(
                            workspace,
                            int(plan_rounds.value or 0),
                            bool(occam.value),
                            bool(skills.value),
                        )
                        target = f"/run/{job_id}"
                # BOTH branches need this, not just the batch one. Every way a
                # launch fails is a filesystem/settings failure (``shutil.Error``
                # on a file a running tool holds open, ``OSError`` writing the
                # job store, ``KeyError`` on a missing setting), never a
                # ValueError. Uncaught, they vanish into NiceGUI's async task
                # (its default handler only writes stderr, which the browser
                # never sees) while busy_buttons' finally re-enables the button -
                # the operator sees a Launch click that "does nothing".
                except Exception as exc:  # noqa: BLE001 - must never be silent
                    ui.notify(
                        f"Launch failed: {type(exc).__name__}: {str(exc)[:600]}",
                        type="negative",
                    )
                    return
                ui.navigate.to(target)

        with ui.row().classes("w-full items-end gap-4 flex-wrap"):
            summary = ui.label("").classes("sop-meta mono grow")
            with ui.column().classes("items-end gap-1"):
                launch_button = ui.button("Launch", on_click=_launch).props(
                    "color=primary",
                ).classes("px-6")
                if locked:
                    launch_button.set_enabled(False)
                    ui.label(
                        "disabled: "
                        + " and ".join(missing or ["requirements are not met"]),
                    ).classes("sop-hint")
        _update_summary()


def _has_running_job() -> bool:
    """Any live work in the store: running or queued for a slot."""
    return bool(active_jobs(get_job_manager()))


def render(job_id: str = "") -> None:
    manager = get_job_manager()
    if job_id:
        # Viewing an existing job must never depend on the CURRENT workspace
        # resolving: history is history. (The live screen reads the folder
        # from the job record, not from the settings.)
        render_live_screen(
            manager,
            job_id,
            job_type="run",
            base_path="/run",
            running_message="Task is running in the background.",
            not_found_label=f"Run job {job_id} not found.",
        )
        return

    workspace = require_workspace()
    if workspace is None:
        # No workspace means no launch — but the board is still the record of
        # what already ran, so it stays on the page instead of a dead end.
        with ui.row().classes("sop-section-head"):
            ui.icon("view_kanban")
            ui.label("Task Board").classes("sop-h2")
        JobBoard(manager, "run")
        return

    # The lock gates LAUNCHING, never VIEWING: the board and every live screen
    # stay reachable so a half-configured machine can still read its own
    # history.
    #
    # One source of truth (preflight) instead of a hand-rolled flag: the old
    # `locked = not tests_ok()` ignored a missing protocol entirely, so the
    # button stayed bright and the job failed on the worker side.
    has_running = _has_running_job()
    proto_ok = _has_protocol(workspace)
    blocked = hard_blockers(workspace, forgive_probes=has_running)
    locked = bool(blocked)
    missing = [blocker.label for blocker in blocked]

    theme.page_header(
        "Live Run",
        "Launch against the saved protocol, watch the queue, open any task for "
        "its live stream.",
        kicker="Execute",
    )
    _render_readiness(workspace, ready=proto_ok)
    # Launch parameters first, then the on-demand sweep, then the board.
    _render_launch_form(workspace, locked=locked, missing=missing)
    HowNowPanel()

    with ui.row().classes("sop-section-head"):
        ui.icon("view_kanban")
        ui.label("Task Board").classes("sop-h2")
        ui.space()
        ui.label("live · 1.5 s refresh").classes("sop-hint")
    JobBoard(manager, "run")
