"""app.py — opencode Supervisor UI (NiceGUI).

Run with: python app.py   (serves http://127.0.0.1:8501)
"""

from __future__ import annotations

from nicegui import app, ui

from services.webui import layout, startup
from services.webui.jobs import get_job_manager


@ui.page("/")
def wizard_page() -> None:
    layout.apply_theme()
    job_manager = get_job_manager()
    layout.render_shell("wizard", job_manager)
    layout.render_startup_banner()
    from services.webui.pages import wizard

    wizard.render()


@ui.page("/run")
@ui.page("/run/{job_id}")
def run_page(job_id: str = "") -> None:
    layout.apply_theme()
    job_manager = get_job_manager()
    layout.render_shell("run", job_manager)
    layout.render_startup_banner()
    from services.webui.pages import run

    run.render(job_id=job_id)


@ui.page("/evolve")
@ui.page("/evolve/{job_id}")
def evolve_page(job_id: str = "") -> None:
    layout.apply_theme()
    job_manager = get_job_manager()
    layout.render_shell("evolve", job_manager)
    layout.render_startup_banner()
    from services.webui.pages import evolve

    evolve.render(job_id=job_id)


app.on_startup(startup.launch_startup_tasks)

ui.run(
    host="127.0.0.1",
    port=8501,
    title="opencode Supervisor",
    reload=False,
    show=False,
    dark=True,
    storage_secret="opencode-supervisor",
)
