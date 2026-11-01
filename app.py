"""app.py — opencode Supervisor UI (NiceGUI).

Run with: python app.py   (serves http://127.0.0.1:8501)
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import urllib.request

from nicegui import app, core, ui

from services.webui import layout, startup
from services.webui.jobs import get_job_manager

_UI_PORT = 8501

# Serving loop. uvicorn >= 0.36 forces ProactorEventLoop (IOCP) on Windows
# through its loop factory, ignoring the policy; `loop="none"` defers to the
# policy set below. A wedged IOCP completion port once silently stopped ALL
# socket serving (2026-11-01: accepts and receives never completed, the loop
# idled on, worker threads kept running, and nothing reached the log). The
# selector loop has no IOCP, and this app spawns no loop-level subprocesses,
# so nothing needs the proactor.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Engine.IO 4.14 turns a websocket disconnect into an empty request and then
# crashes on REQUEST_METHOD; feed back a complete, self-consistent GET environ
# so its version check rejects it cleanly (no session is created, and the
# ASGI callables are available if it decides to respond).
_original_translate_request = core.sio.eio._async["translate_request"]

async def _translate_request_without_disconnect(scope, receive, send):
    environ = await _original_translate_request(scope, receive, send)
    if environ:
        return environ
    return {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/",
        "QUERY_STRING": "",
        "RAW_URI": "/",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "asgi.receive": receive,
        "asgi.send": send,
        "asgi.scope": scope,
    }

core.sio.eio._async["translate_request"] = _translate_request_without_disconnect

# Watchdog: a dead UI used to leave no trace in the log, so "loop frozen"
# could not be told apart from "socket serving dead". A thread probes both
# every 10s and logs the failing half with a timestamp.
_watchdog: dict = {}
log = logging.getLogger("ui.watchdog")


@app.on_startup
async def _start_ui_watchdog() -> None:
    _watchdog["loop"] = asyncio.get_running_loop()
    threading.Thread(target=_ui_watchdog, name="ui-watchdog", daemon=True).start()
    log.info("UI watchdog started (loop + serving probes every 10s)")


def _ui_watchdog() -> None:
    cycles = 0
    while True:
        time.sleep(10)
        cycles += 1
        loop = _watchdog.get("loop")
        if loop is not None:
            done = threading.Event()
            started = time.perf_counter()
            try:
                loop.call_soon_threadsafe(done.set)
            except RuntimeError:
                log.error("event loop is closed")
                _watchdog["loop"] = None
                continue
            responsive = done.wait(timeout=5)
            delay = time.perf_counter() - started
            if not responsive:
                log.error("event loop did not process a callback within 5s - loop is frozen")
            elif delay > 1.0:
                log.warning("event loop scheduling delay %.2fs", delay)
        if cycles <= 3:
            # The listener may still be binding during the first seconds.
            continue
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{_UI_PORT}/", timeout=5):
                pass
        except Exception as exc:
            log.error(
                "HTTP serving probe failed after %.1fs: %r",
                time.perf_counter() - started, exc,
            )


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
    port=_UI_PORT,
    title="opencode Supervisor",
    reload=False,
    show=False,
    dark=True,
    storage_secret="opencode-supervisor",
    loop="none",
)
