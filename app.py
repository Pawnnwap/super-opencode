"""app.py — opencode Supervisor UI (NiceGUI).

Run with: python app.py   (serves http://127.0.0.1:8501)
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from nicegui import app, core, ui

from services.config.settings import sanitize_proxy_env
from services.runtime import death_capture
from services.webui import layout, startup
from services.webui.jobs import get_job_manager

# The UI also builds openai clients (wizard, protocol builder, connectivity
# probes); a bracketed IPv6 NO_PROXY entry makes httpx2 raise on construction.
sanitize_proxy_env()

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
# could not be told apart from "socket serving dead". The thread now probes
# loop responsiveness every 10s and keeps the ui.lock death clock; serving
# health and revival belong to the task-owned soprun_watchdog. The old
# in-process HTTP probe was redundant with it and never repaired anything -
# and every probe rendered a full page, churning one "client deleted" line
# into ui.lifecycle per probe (that churn WAS the noise floor of this log).
_watchdog: dict = {}
log = logging.getLogger("ui.watchdog")
lifecycle_log = logging.getLogger("ui.lifecycle")

# Liveness clock for the whole UI process: the watchdog thread touches it
# every 10s, so after a silent death its mtime is the exact time of death -
# the same trick as .job_store/worker.lock for the worker daemon. It is the
# only fine-grained clock for a kill that takes the cmd wrappers too (no
# exitcode sentinel). Not a *.json file: the job store globs those as jobs.
_UI_LOCK = Path(".job_store") / "ui.lock"


@app.on_startup
def _install_forensics() -> None:
    """Arm crash capture + client lifecycle logging before any traffic.

    Every UI death so far left zero evidence (see death_capture module doc),
    so the forensics must be armed at startup, not on demand.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    death_capture.install("app.fatal.log")
    log.info("death capture armed (app.fatal.log), pid %d", os.getpid())

    @app.on_connect
    def _log_connect(client) -> None:
        lifecycle_log.info(
            "client connected id=%s ip=%s tab=%s", client.id, client.ip, client.tab_id,
        )

    @app.on_disconnect
    def _log_disconnect(client) -> None:
        # Called on every socket loss AND on reconnects: a string of these
        # without deletes = transport flapping; a delete right after =
        # the reconnect window expired (the old silent page death).
        lifecycle_log.info("client socket lost id=%s", client.id)

    @app.on_delete
    def _log_delete(client) -> None:
        lifecycle_log.info(
            "client deleted id=%s (page gone: closed tab or reconnect window expired)",
            client.id,
        )


@app.on_startup
async def _start_ui_watchdog() -> None:
    _watchdog["loop"] = asyncio.get_running_loop()
    threading.Thread(target=_ui_watchdog, name="ui-watchdog", daemon=True).start()
    # print, not logging: basicConfig only runs when the first job creates a
    # SupervisorLoop, so a lifespan-time INFO record would be dropped.
    print(
        "UI watchdog started (loop probe + ui.lock death clock every 10s; "
        "serving health owned by soprun_watchdog)",
        file=sys.stderr,
        flush=True,
    )


def _touch_ui_lock() -> None:
    try:
        _UI_LOCK.parent.mkdir(parents=True, exist_ok=True)
        if not _UI_LOCK.exists():
            _UI_LOCK.write_text(f'{{"pid": {os.getpid()}}}', encoding="utf-8")
        else:
            os.utime(_UI_LOCK)
    except OSError:
        pass  # forensics must never take the watchdog down


def _ui_watchdog() -> None:
    frozen_cycles = 0
    while True:
        time.sleep(10)
        _touch_ui_lock()
        loop = _watchdog.get("loop")
        if loop is None:
            continue
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
            frozen_cycles += 1
            log.error("event loop did not process a callback within 5s - loop is frozen")
            if frozen_cycles == 3:
                # 30 s without a callback: capture the blocking stacks
                # before the task-owned watchdog restarts us; stderr alone
                # never said which call wedged the loop.
                death_capture.dump_stacks(
                    "event loop frozen for ~30s - dumping all thread stacks",
                )
        else:
            frozen_cycles = 0
            if delay > 1.0:
                log.warning("event loop scheduling delay %.2fs", delay)


# Side-effect-free liveness target for the task-owned watchdog. Probing "/"
# builds a full page and registers a UI client that expires 60s later; this
# plain route answers without ever creating one.
@app.get('/healthz')
def _healthz() -> dict:
    return {'ok': True}


@app.post('/jobs/{job_id}/open-folder')
def _open_job_folder(job_id: str):
    """Reveal a job's workspace in the OS file manager (local board shortcut).

    The path comes from the job record only - never from the request - and the
    server binds 127.0.0.1, so this cannot open arbitrary locations remotely.
    """
    from fastapi.responses import JSONResponse

    record = get_job_manager().get_job_summary(job_id) or {}
    workspace = str((record.get("config") or {}).get("workspace") or "")
    path = Path(workspace)
    if not workspace or not path.is_dir():
        return JSONResponse(
            {"ok": False, "error": "workspace folder not found"}, status_code=404,
        )
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return {"ok": True}


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


@ui.page("/harvest")
@ui.page("/harvest/{job_id}")
async def harvest_page(job_id: str = "") -> None:
    layout.apply_theme()
    job_manager = get_job_manager()
    layout.render_shell("harvest", job_manager)
    layout.render_startup_banner()
    from services.webui.pages import harvest

    await harvest.render(job_id=job_id)


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
    # Reconnect grace. This single knob derives the engine.io keepalive
    # (ping_interval = 0.8x, ping_timeout = 0.4x of it) and how long the
    # server keeps a disconnected client before deleting it. NiceGUI's 3.0 s
    # default meant a 2 s pong delay or a 3 s socket blip - routine while
    # long jobs burst log renders into the browser - permanently deleted the
    # page client mid-task ("UI silently disconnects"). 60 s rides out such
    # blips transparently (outbox rewind covers the gap).
    reconnect_timeout=60.0,
)
