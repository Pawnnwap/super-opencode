"""Theme install and the shared left-drawer shell for every page.

The visual system lives in :mod:`services.webui.theme`; this module only
composes it into the two persistent chrome surfaces (rail + top bar) and keeps
the old ``CUSTOM_CSS`` name working for callers that still reference it.
"""

from __future__ import annotations

import time
import weakref
from html import escape
from pathlib import Path

from nicegui import ui

from services.jobs.job_states import (
    ACTIVE_JOB_STATES,
    PILL_TONE_CLASS,
    STATE_CONTRACT,
)
from services.webui import theme
from services.webui.state import app_state

# Kept as an alias: the stylesheet is a single source of truth in theme.py.
CUSTOM_CSS = theme.CSS

# Derived from the state contract instead of a hand-written table: the two
# used to disagree (CANCELLED was painted as a failure here while the board
# painted it neutral).
PILL_MAP = {
    state: (
        f'<span class="pill {PILL_TONE_CLASS[style.tone]}" role="status">'
        f"{style.label}</span>"
    )
    for state, style in STATE_CONTRACT.items()
}

_PAGES = [
    ("wizard", "Protocol Wizard", "edit_note"),
    ("run", "Live Run", "bolt"),
    ("harvest", "Harvester", "inventory_2"),
]


# Browser-side death recorder: forwards tab lifecycle + socket events to the
# server log via NiceGUI's "log" channel, so "the page went silent" leaves a
# server-side trail (socket reason, reconnect attempts, tab freeze/resume,
# uncaught JS errors). Kept dependency-free and fail-safe: any internal error
# must never break the page it instruments.
BEACON_JS = """
<script>
(function () {
  function send(level, message) {
    try {
      if (window.socket && window.clientId) {
        window.socket.emit('log', {
          client_id: window.clientId,
          level: level,
          message: '[beacon] ' + message,
        });
      }
    } catch (e) {}
  }
  window.addEventListener('error', function (e) {
    send('error', 'window.onerror: ' + (e.message || e.type));
  });
  window.addEventListener('unhandledrejection', function (e) {
    send('error', 'unhandledrejection: ' + ((e.reason && e.reason.message) || e.reason));
  });
  document.addEventListener('visibilitychange', function () {
    send('info', 'visibilitychange -> ' + document.visibilityState);
  });
  window.addEventListener('freeze', function () { send('warning', 'page freeze (tab frozen by browser)'); });
  window.addEventListener('resume', function () { send('warning', 'page resume'); });
  var tries = 0;
  var timer = setInterval(function () {
    tries++;
    if (window.socket && window.clientId) {
      clearInterval(timer);
      window.socket.on('connect', function () { send('info', 'socket connect'); });
      window.socket.on('disconnect', function (reason) {
        send('warning', 'socket disconnect: ' + reason);
      });
      window.socket.on('connect_error', function (err) {
        send('warning', 'connect_error: ' + (err && err.message));
      });
      if (window.socket.io) {
        window.socket.io.on('reconnect_attempt', function (n) {
          send('warning', 'reconnect attempt ' + n);
        });
        window.socket.io.on('reconnect_failed', function () {
          send('error', 'reconnect failed (gave up)');
        });
      }
      send('info', 'beacon armed');
    } else if (tries > 120) {
      clearInterval(timer);
    }
  }, 500);
})();
</script>
"""


def apply_theme() -> None:
    """Install the design system + the browser-side death recorder."""
    theme.apply()
    ui.add_head_html(BEACON_JS)


def format_status_pill(state: str) -> str:
    return PILL_MAP.get(state, f'<span class="pill pill-idle">{state}</span>')


def _count_states(rows: dict[str, dict], job_type: str) -> tuple[int, int]:
    """(running, queued) among one job type's records."""
    running = pending = 0
    for state_data in rows.values():
        if state_data.get("type") != job_type:
            continue
        state = state_data.get("state")
        if state == "RUNNING":
            running += 1
        elif state == "PENDING":
            pending += 1
    return running, pending


def _queue_counts(job_manager, job_type: str) -> tuple[int, int]:
    return _count_states(_store_snapshot(job_manager), job_type)


# One shared, briefly-cached view of the whole store.
#
# render_shell needs three reads of "what is active" on EVERY page render, and
# the store's summary files are never deleted (retention prunes payloads only),
# so a live store is hundreds of files. Walking it synchronously on the event
# loop blocked the loop for seconds under the worker's disk pressure: on
# 2026-11-13 the watchdog dumped a frozen loop inside
# render_shell -> active_jobs -> json.load, the websocket dropped, the client
# was deleted after the reconnect window, and the page reloaded with every
# panel back at its default. A 2 s stale "is a job running" is harmless - it
# only gates a nav lock - while a blocked loop is not.
_SNAPSHOT_TTL = 2.0
_snapshots: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
# Per-store parse cache: job_id -> (mtime_ns, size, record). Finished
# summaries never change again, so a full walk after the first tick is a
# stat loop instead of a parse of every file.
_state_cache: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _store_snapshot(job_manager) -> dict[str, dict]:
    """Job id -> state record, shared for ``_SNAPSHOT_TTL`` seconds.

    Cached per store object (weakly: test doubles are recreated per test and
    must never see a previous test's rows). Unreadable records are skipped,
    matching the record-by-record tolerance of the old loops.

    The walk itself is cached by (mtime, size) per file: the store's summaries
    are never deleted (retention prunes log payloads only), so a live store
    holds hundreds of immutable files and re-parsing them every tick was the
    blocking work behind the 2026-11-13 frozen event loop.
    """
    store = getattr(job_manager, "store", None)
    if store is None:
        return {}
    now = time.monotonic()
    try:
        cached = _snapshots.get(store)
    except TypeError:  # not weak-referenceable: walk uncached
        cached = None
    if cached is not None and now - cached[0] < _SNAPSHOT_TTL:
        return cached[1]
    rows: dict[str, dict] = {}
    try:
        job_ids = store.list_jobs()
    except OSError:
        job_ids = []
    if job_ids:
        try:
            parse_cache = _state_cache.setdefault(store, {})
        except TypeError:
            parse_cache = None
        for job_id in job_ids:
            if parse_cache is not None and hasattr(store, "get_job_state_cached"):
                state = store.get_job_state_cached(job_id, parse_cache)
            else:
                try:
                    state = store.get_job_state(job_id)
                except OSError:
                    state = None
            if state:
                rows[job_id] = state
        if parse_cache is not None:
            # Drop entries for deleted jobs so the cache cannot grow forever.
            for dead in set(parse_cache) - set(job_ids):
                parse_cache.pop(dead, None)
    try:
        _snapshots[store] = (now, rows)
    except TypeError:
        pass
    return rows


def active_jobs(job_manager, job_type: str = "") -> list[dict]:
    """Store records of queued/running jobs (optionally one job type).

    One helper for the three call sites that each re-walked the store with
    their own copy of the active-state tuple: the nav-shell lock, the run
    page's lock and the board's queue. Reads the shared snapshot.
    """
    return [
        state
        for state in _store_snapshot(job_manager).values()
        if state.get("state") in ACTIVE_JOB_STATES
        and (not job_type or state.get("type") == job_type)
    ]


def _has_active_jobs(job_manager) -> bool:
    """Any run job queued or running right now (store read)."""
    return any(
        state.get("type") == "run"
        for state in active_jobs(job_manager)
    )


def render_shell(active: str, job_manager) -> None:
    """Left rail (brand, navigation, live counts) and the context top bar.

    Hierarchy in the chrome, in one sentence: the rail answers *where am I and
    what is running*, the top bar answers *against which workspace and models*.
    Neither repeats the page title — every page opens with its own heading, and
    a page name printed twice is a page name that means nothing.
    """
    tests_ok = app_state.tests_ok()
    run_counts = _queue_counts(job_manager, "run")
    harvest_counts = _queue_counts(job_manager, "harvest")

    with ui.left_drawer(value=True, fixed=True, top_corner=False).classes(
        "sop-drawer",
    ).props("bordered width=236"):
        with ui.row().classes("sop-brand w-full flex-nowrap"):
            with ui.element("div").classes("sop-brand__mark"):
                ui.icon("terminal")
            with ui.column().classes("gap-0 min-w-0"):
                ui.label("opencode Supervisor").classes("sop-brand__name")
                ui.label("run control").classes("sop-brand__sub")

        with ui.element("nav").props('aria-label="Workspace"').classes("sop-nav"):
            ui.label("Workspace").classes("sop-nav__caption")
            for key, label, icon in _PAGES:
                # The lock gates new work only: with a live/queued job the pages
                # must stay open - that is where the run is watched and stopped.
                locked = (
                    key == "run"
                    and not tests_ok
                    and not _has_active_jobs(job_manager)
                )
                counts = {"run": run_counts, "harvest": harvest_counts}.get(key)
                running, pending = counts if counts else (0, 0)
                # A locked launch does not lock the PAGE: the run page still
                # serves the board and every live screen, so the item stays
                # fully legible and only carries a lock marker.
                classes = "nav-item" + (" nav-item--active" if key == active else "")
                # Empty link text: the icon + label row inside is the whole
                # item. Passing the label here too printed every name twice.
                nav = ui.link(target=f"/{key}").classes(classes)
                if key == active:
                    nav.props('aria-current="page"')
                if locked:
                    nav.tooltip(
                        "Launch is locked until the connectivity probes pass — "
                        "the board and history stay readable.",
                    )
                with nav, ui.element("div").classes(
                    "flex items-center gap-2 w-full min-w-0",
                ):
                    ui.icon(icon)
                    ui.label(label).classes("truncate")
                    if locked:
                        ui.icon("lock").classes("text-sm").style(
                            "color:#4d5769;margin-left:auto",
                        )
                    elif running or pending:
                        badges = []
                        if running:
                            badges.append(
                                f'<span class="nav-item__badge nav-item__badge--live" '
                                f'title="{running} running">{running}</span>',
                            )
                        if pending:
                            badges.append(
                                f'<span class="nav-item__badge" '
                                f'title="{pending} queued">{pending}</span>',
                            )
                        ui.html(
                            f'<span class="nav-item__badges">{"".join(badges)}</span>',
                        )

        ui.space()
        with ui.column().classes("sop-drawer__foot gap-1"):
            ui.label(f"nicegui · {app_state.get('engine') or 'opencode'}")

    # Global context, not a second page title: every page already carries
    # its own heading, so repeating the page name here just printed it twice.
    workspace_raw = str(app_state.get("workspace") or "").strip()
    where = Path(workspace_raw).name if workspace_raw else "(no workspace)"
    engine = app_state.get("engine") or "opencode"
    model = app_state.get("opencode_model") or "(no model)"
    judge = app_state.get("supervisor_model") or "(no judge)"
    with ui.header().classes("sop-header items-center flex-nowrap"):
        with ui.row().classes("sop-header__where flex-nowrap min-w-0"):
            ui.icon("folder_open")
            ui.label(where).classes("truncate").tooltip(workspace_raw or "No workspace set")
            # A configured-but-missing folder is the single most common reason
            # every other page refuses to work; say it here, once, in context.
            if not workspace_raw or not Path(workspace_raw).exists():
                ui.html('<span class="chip chip--bad">missing</span>')
        ui.element("div").classes("sop-header__sep")
        with ui.row().classes("sop-chips flex-nowrap"):
            # Settings text is operator-controlled and lands in raw HTML here,
            # so it is escaped rather than trusted.
            for text in (engine, model, f"judge · {judge}"):
                ui.html(
                    f'<span class="sop-header__chip">'
                    f"<span>{escape(str(text))}</span></span>",
                )
        ui.space()
        if tests_ok:
            ui.html('<span class="chip chip--ok">✓ ready to launch</span>')
        else:
            ui.html('<span class="chip chip--warn">tests pending</span>')

    # Keyboard bindings live with the chrome: they are global, and installing
    # them per page was how the four timers ended up with four different
    # stop conditions.
    from services.webui import shortcuts

    shortcuts.install()


def render_startup_banner() -> None:
    """Live banner of the background startup tasks (upgrades, MCP, cleanup).

    Progressive disclosure: once every task is done the banner disappears
    (finished startup is not news); a failed task keeps it open so the bad
    news is never buried in a collapsed panel.
    """
    from services.webui import startup

    items = startup.statuses()
    if not items:
        theme.notice("info", "Preparing — CLI upgrades and MCP config…")
        return
    # The pipeline appends a "running" and later a "done"/"failed" record per
    # task; keep only the latest record so each task shows once and the
    # all-done check can actually fire.
    latest: dict[str, dict] = {}
    for item in items:
        latest[item["task"]] = item
    items = list(latest.values())
    done = sum(1 for item in items if item["state"] == "done")
    if done == len(items):
        return
    failed = any(item["state"] == "failed" for item in items)
    icons = {"running": "progress_activity", "done": "check", "failed": "close"}
    from services.webui.ui_state import state_expansion

    with state_expansion(
        "layout.startup_tasks",
        f"Startup tasks — {done}/{len(items)} done",
        # Bad news must never be buried: force it open the first time a task
        # fails, but never re-open (or re-close) a panel the operator set.
        default=failed,
        icon="settings",
    ).classes("sop-fold sop-fold--dense").props("dense"):
        for item in items:
            icon = icons.get(item["state"], "circle")
            detail = item["detail"] or ""
            color = {
                "done": "var(--sop-ok)", "failed": "var(--sop-bad)",
            }.get(item["state"], "var(--sop-text-3)")
            with ui.row().classes("items-center gap-2 flex-nowrap w-full"):
                ui.icon(icon).style(f"color:{color};font-size:15px")
                ui.label(item["task"]).classes("sop-meta").style("min-width:150px")
                ui.label(detail).classes("sop-hint truncate grow")
