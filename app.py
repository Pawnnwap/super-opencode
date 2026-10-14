"""app.py — opencode Supervisor UI (NiceGUI).

Run with: python app.py   (serves http://127.0.0.1:8501)
"""

from __future__ import annotations

from nicegui import app, core, ui

from services.webui import layout, startup
from services.webui.jobs import get_job_manager

# Engine.IO 4.14 turns a websocket disconnect into an empty request and then
# crashes on REQUEST_METHOD; return a benign stub so navigation stays quiet.
_original_translate_request = core.sio.eio._async["translate_request"]

async def _translate_request_without_disconnect(scope, receive, send):
    environ = await _original_translate_request(scope, receive, send)
    if not environ:
        return {"REQUEST_METHOD": "GET"}
    return environ

core.sio.eio._async["translate_request"] = _translate_request_without_disconnect


@ui.page("/")
@ui.page("/wizard")
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
