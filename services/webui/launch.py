"""Shared launch plumbing for the Run and Harvester pages.

Both pages do the same five things before enqueueing a job: persist the
settings, apply the API environment, push the concurrency caps, build the
config from the page's overrides, and enqueue. Only the job type, the protocol
filename and the option set differ, so the sequence lives here once instead of
being re-typed per page.
"""

from __future__ import annotations

from pathlib import Path

from nicegui import ui

from services.config.supervisor_config_builder import build_supervisor_config
from services.webui import i18n
from services.webui.jobs import get_job_manager, sync_max_parallel_jobs
from services.webui.state import app_state


class LaunchBlocked(RuntimeError):
    """A launch precondition failed. The message is shown to the operator."""


def save_and_apply_settings() -> None:
    """Persist the current settings and push them into the runtime.

    ``flush`` rather than ``save``: edits are debounced, so a launch issued
    right after typing a value would otherwise enqueue a job built from the
    previous value.
    """
    app_state.flush()
    app_state.apply_api_config()


def launch_job(
    job_type: str,
    workspace: Path,
    *,
    protocol_name: str,
    **overrides,
) -> str:
    """Build the config, enqueue one job, notify; returns the new job id.

    The preflight check is the last line of defence: the Run page disables its
    button from the same list, but that is a render-time snapshot and a file
    can be deleted between render and click. Raising here turns a silent
    FAILED job into a message the operator can act on.
    """
    from services.webui.preflight import hard_blockers

    # Probes are forgiven here on purpose: this is the last gate before work
    # is queued, and the only thing that guarantees a failure is a missing
    # protocol or workspace. Refusing on a stale probe would block an operator
    # who already has a job running and just wants to start another.
    blocked = hard_blockers(workspace, forgive_probes=True)
    if blocked:
        raise LaunchBlocked(
            i18n.tr(
                "{label} — fix it in the Protocol Wizard before launching.",
                label=blocked[0].label,
            ),
        )
    save_and_apply_settings()
    sync_max_parallel_jobs()
    config = build_supervisor_config(
        app_state.as_mapping(),
        protocol_path=workspace / protocol_name,
        workspace=workspace,
        **overrides,
    )
    job_id = get_job_manager().enqueue_job(job_type, config)
    ui.notify(i18n.tr("Task launched: {id}", id=job_id))
    return job_id


def require_workspace() -> Path | None:
    """The configured workspace, or a rendered "go configure it first" notice.

    Run and Harvester each opened with the same gate; duplicating it meant the
    wording and the button could drift apart per page. The notice is an empty
    state, not a red line: a dead end with no next step is the worst kind of
    error message.
    """
    raw = app_state.get("workspace") or ""
    if raw and Path(raw).exists():
        return Path(raw)
    from services.webui import theme

    theme.page_header(
        "Live Run", "This page needs a workspace before it can do anything.",
        kicker="Execute",
    )
    theme.empty_state(
        "folder_off",
        "No usable workspace",
        (
            "The saved path does not exist — deleted, moved, or on a drive that "
            "is not connected." if raw else "No workspace path is set yet."
        ) + (
            " Point Configuration at an existing folder: the protocol and every "
            "run live there."
        ),
        detail=raw or i18n.t("(empty)"),
        action_label="Open Protocol Wizard",
        action=lambda: ui.navigate.to("/"),
    )
    return None


def render_live_screen(
    manager,
    job_id: str,
    *,
    job_type: str,
    base_path: str,
    running_message: str,
    not_found_label: str,
) -> None:
    """Render one job's live screen, or the "not found" notice.

    Both detail pages repeated this lookup/type check verbatim; the only
    differences are the arguments.
    """
    from services.webui.components.live_screen import LiveJobScreen

    status = manager.get_job_summary(job_id)
    if not status or status.get("type") != job_type:
        from services.webui import theme

        theme.empty_state(
            "search_off",
            not_found_label,
            "The record may have been removed by retention, or the id in the "
            "address bar belongs to another job type.",
            action_label="Back to board",
            action=lambda: ui.navigate.to(base_path),
        )
        return
    LiveJobScreen(
        manager,
        job_type=job_type,
        job_id=job_id,
        base_path=base_path,
        running_message=running_message,
    ).render()


__all__ = [
    "LaunchBlocked",
    "launch_job",
    "render_live_screen",
    "require_workspace",
    "save_and_apply_settings",
]
