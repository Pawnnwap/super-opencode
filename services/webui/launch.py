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
from services.webui.jobs import get_job_manager, sync_max_parallel_jobs
from services.webui.state import app_state


def save_and_apply_settings() -> None:
    """Persist the current settings and push them into the runtime."""
    app_state.save()
    app_state.apply_api_config()


def launch_job(
    job_type: str,
    workspace: Path,
    *,
    protocol_name: str,
    **overrides,
) -> str:
    """Build the config, enqueue one job, notify; returns the new job id."""
    save_and_apply_settings()
    sync_max_parallel_jobs()
    config = build_supervisor_config(
        app_state.as_mapping(),
        protocol_path=workspace / protocol_name,
        workspace=workspace,
        **overrides,
    )
    job_id = get_job_manager().enqueue_job(job_type, config)
    ui.notify(f"Task launched: {job_id}")
    return job_id


def require_workspace() -> Path | None:
    """The configured workspace, or a rendered "go configure it first" notice.

    Run and Harvester each opened with the same three-widget gate; duplicating it
    meant the wording and the button could drift apart per page.
    """
    raw = app_state.get("workspace") or ""
    if raw and Path(raw).exists():
        return Path(raw)
    ui.label("Set a valid workspace path in the Protocol Wizard first.").classes(
        "text-[#f85149]",
    )
    ui.button("Open Wizard", on_click=lambda: ui.navigate.to("/")).props("dense")
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
        ui.label(not_found_label).classes("text-[#f85149]")
        ui.button(
            "Back to board", on_click=lambda: ui.navigate.to(base_path),
        ).props("dense")
        return
    LiveJobScreen(
        manager,
        job_type=job_type,
        job_id=job_id,
        base_path=base_path,
        running_message=running_message,
    ).render()


__all__ = [
    "launch_job",
    "render_live_screen",
    "require_workspace",
    "save_and_apply_settings",
]
