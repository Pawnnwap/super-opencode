"""Theme, custom CSS, and the shared left-drawer shell for every page."""

from __future__ import annotations

import time
import weakref
from pathlib import Path

from nicegui import ui

from services.jobs.job_states import ACTIVE_JOB_STATES
from services.webui.state import app_state

CUSTOM_CSS = """
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;600&family=Syne:wght@400;700;800&display=swap');
body { font-family: 'Syne', sans-serif; background-color: #0d0f14; color: #c9d1d9; }
/* Drawer nav items are buttons, not prose links: no default underline. */
a.nav-item, a.nav-item:hover { text-decoration: none; }
h1 { font-family: 'Syne', sans-serif; font-weight: 800; color: #58a6ff; letter-spacing: -1px; }
h2 { font-family: 'Syne', sans-serif; font-weight: 700; color: #79c0ff; }
h3 { font-family: 'Syne', sans-serif; font-weight: 600; color: #9ecbff; }
.log-box {
    background: #0d1117; border: 1px solid #21262d; border-radius: 8px;
    padding: 1rem 1.2rem; font-family: 'JetBrains Mono', monospace; font-size: 0.78rem;
    line-height: 1.7; max-height: 520px; overflow-y: auto; white-space: pre-wrap; word-break: break-word;
}
.log-line { white-space: pre-wrap; word-break: break-word; }
.log-ts { color: #6e7681; margin-right: 6px; }
.log-warn { color: #e3b341; }
.log-error { color: #f85149; }
.log-success { color: #3fb950; font-weight: 600; }
.log-tool { color: #39c5cf; font-weight: 600; }
.log-supervisor { color: #d2a8ff; }
.log-prompt { color: #79c0ff; }
.log-output { color: #c9d1d9; }
.mono { font-family: 'JetBrains Mono', monospace !important; }
.card { background: #161b22; border: 1px solid #21262d; border-radius: 10px; padding: 1.2rem 1.5rem; margin-bottom: 1rem; }
.pill { display: inline-block; padding: 2px 12px; border-radius: 999px; font-size: 0.75rem; font-family: 'JetBrains Mono', monospace; font-weight: 600; margin-left: 8px; }
.pill-idle { background:#21262d; color:#8b949e; }
.pill-running { background:#1f6feb22; color:#58a6ff; border: 1px solid #1f6feb55; }
.pill-success { background:#23863633; color:#3fb950; border: 1px solid #23863655; }
.pill-failure { background:#da363333; color:#f85149; border: 1px solid #da363355; }
.pill-blocked { background:#e3b34133; color:#e3b341; border: 1px solid #e3b34155; }
.log-blocked { color: #e3b341; font-weight: 600; }
/* No horizontal scrolling on any page: flex children may shrink below their
   content, long tokens (paths, hashes) break anywhere, and the boxes NiceGUI
   ships with `white-space: pre` (ui.log, ui.code, markdown code) wrap instead
   of scrolling sideways. One-line strips keep nowrap + ellipsis. */
body { overflow-wrap: anywhere; }
.nicegui-row > *, .nicegui-column > * { min-width: 0; max-width: 100%; }
.nicegui-log .q-scrollarea__content { white-space: pre-wrap !important; overflow-wrap: anywhere; }
.nicegui-markdown pre, .nicegui-markdown code, .nicegui-code pre, .nicegui-code code {
    white-space: pre-wrap !important; overflow-wrap: anywhere;
}
/* Phase breadcrumb (log_format.phase_breadcrumb): plan → [code → test → review] ×N → done.
   Reached phases strike out and fade, the active one sits in a softly breathing chip
   with a sweeping underline; the loop phases wear a bracket plus a round counter. */
.phase-crumb { font-family: 'JetBrains Mono', monospace; letter-spacing: 0.5px; white-space: nowrap; }
.phase-done { color: #6e7681; text-decoration: line-through; text-decoration-color: #484f58; }
.phase-skipped { color: #484f58; text-decoration: line-through dotted #484f58; }
.phase-todo { color: #484f58; }
.phase-arrow { margin: 0 6px; }
.phase-arrow-lit { color: #58a6ff; opacity: 0.55; }
.phase-arrow-dim { color: #3d444d; }
.phase-loop::before, .phase-loop::after { color: #3d444d; }
.phase-loop::before { content: "["; margin-right: 5px; }
.phase-loop::after { content: "]"; margin-left: 5px; }
.phase-round { color: #8b949e; font-size: 0.7rem; margin-left: 5px; vertical-align: 1px; }
.phase-current {
    display: inline-block; color: #58a6ff; font-weight: 600; line-height: 1.3;
    padding: 1px 8px 2px; border-radius: 5px;
    background: rgba(31,111,235,0.13); border: 1px solid rgba(88,166,255,0.3);
    animation: phase-enter 0.3s ease both, phase-breathe 2.6s ease-in-out 0.3s infinite;
}
/* Sweep: a narrow bright band crossing a faint track, so the motion reads
   even on a 40px chip (a band as wide as the bar only shimmered). */
.phase-current::after {
    content: ""; display: block; height: 2px; margin-top: 1px; border-radius: 1px;
    background: linear-gradient(90deg, transparent, #58a6ff 50%, transparent) -100% 0 / 45% 100% no-repeat,
                rgba(88,166,255,0.18);
    animation: phase-sheen 1.8s ease-in-out infinite;
}
/* Finished jobs: the chip goes still and takes the outcome colour. */
.phase-current[class*="phase-end-"] { animation: none; }
.phase-current[class*="phase-end-"]::after { animation: none; background: currentColor; opacity: 0.45; }
.phase-end-success { color: #3fb950; background: rgba(35,134,54,0.18); border-color: rgba(63,185,80,0.4); }
.phase-end-blocked { color: #e3b341; background: rgba(227,179,65,0.12); border-color: rgba(227,179,65,0.4); }
.phase-end-failed { color: #f85149; background: rgba(218,54,51,0.12); border-color: rgba(248,81,73,0.4); }
@keyframes phase-enter { from { opacity: 0; transform: translateY(2px); } }
@keyframes phase-breathe {
    0%, 100% { box-shadow: 0 0 2px rgba(88,166,255,0.12); }
    50% { box-shadow: 0 0 9px rgba(88,166,255,0.35); }
}
@keyframes phase-sheen { from { background-position: -100% 0; } to { background-position: 200% 0; } }
"""

PILL_MAP = {
    "RUNNING": '<span class="pill pill-running">RUNNING</span>',
    "PENDING": '<span class="pill pill-running">PENDING</span>',
    "SUCCESS": '<span class="pill pill-success">SUCCESS</span>',
    "FAILED": '<span class="pill pill-failure">FAILED</span>',
    "CANCELLED": '<span class="pill pill-failure">CANCELLED</span>',
    # Target unreachable under recorded constraints: amber, not red.
    "BLOCKED": '<span class="pill pill-blocked">BLOCKED</span>',
    # Killed by a worker restart, not by the run itself: amber, like BLOCKED.
    "ABANDONED": '<span class="pill pill-blocked">ABANDONED</span>',
}

_PAGES = [
    ("wizard", "Protocol Wizard", "✍️"),
    ("run", "Live Run", "▶"),
    ("harvest", "Harvester", "🧺"),
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
    ui.add_head_html(f"<style>{CUSTOM_CSS}</style>")
    ui.add_head_html(BEACON_JS)
    ui.colors(primary="#1f6feb", secondary="#21262d", accent="#58a6ff")
    ui.dark_mode(True)


def format_status_pill(state: str) -> str:
    return PILL_MAP.get(state, f'<span class="pill pill-idle">{state}</span>')


def _queue_stats_line(job_manager, job_type: str) -> str:
    running = pending = 0
    for state_data in _store_snapshot(job_manager).values():
        if state_data.get("type") != job_type:
            continue
        state = state_data.get("state")
        if state == "RUNNING":
            running += 1
        elif state == "PENDING":
            pending += 1
    return f"{running} running · {pending} queued"


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
    """Left drawer (nav + queue stats + lock badges) and a slim header."""
    tests_ok = app_state.tests_ok()
    with ui.left_drawer(value=True, fixed=True, top_corner=False).classes(
        "bg-[#0a0c10] border-r border-[#21262d]",
    ).props("bordered"):
        ui.label("opencode Supervisor").classes("text-xl font-bold text-[#58a6ff]")
        ui.separator()
        for key, label, icon in _PAGES:
            # The lock gates new work only: with a live/queued job the pages
            # must stay open - that is where the run is watched and stopped.
            locked = (
                key == "run"
                and not tests_ok
                and not _has_active_jobs(job_manager)
            )
            # Empty link text: the icon + label row inside is the whole
            # item. Passing the label here too printed every name twice.
            nav = ui.link(target=f"/{key}").classes(
                "nav-item block w-full text-left px-3 py-2 rounded"
                + (
                    " bg-[#1f6feb22] text-[#58a6ff] font-bold"
                    if key == active else ""
                )
                + (" opacity-50" if locked else " hover:bg-[#161b22]"),
            )
            with nav, ui.row().classes("items-center gap-2"):
                ui.icon(icon)
                ui.label(label + (" 🔒" if locked else ""))
        ui.separator()
        with ui.column().classes("gap-1 text-xs text-[#8b949e] w-full"):
            ui.label("Queues").classes("font-bold text-[#9ecbff]")
            for queue_label, job_type in (
                ("Run", "run"), ("Harvest", "harvest"),
            ):
                with ui.row().classes("w-full justify-between items-center"):
                    ui.label(queue_label)
                    ui.label(_queue_stats_line(job_manager, job_type)).classes(
                        "mono",
                    )
        ui.space()
        ui.label("nicegui · opencode").classes("text-[#484f58] text-xs mt-auto")

    # Global context, not a second page title: every page already carries
    # its own heading, so repeating the page name here just printed it twice.
    workspace_raw = str(app_state.get("workspace") or "").strip()
    where = Path(workspace_raw).name if workspace_raw else "(no workspace)"
    engine = app_state.get("engine") or "opencode"
    model = app_state.get("opencode_model") or "(no model)"
    with ui.header().classes("bg-[#0a0c10] border-b border-[#21262d] items-center"):
        ui.label(f"📁 {where} · {engine} · {model}").classes(
            "text-xs text-[#8b949e] mono",
        )
        ui.space()
        if tests_ok:
            ui.label("✅ ready to launch").classes("text-xs text-[#3fb950]")
        else:
            ui.label("🔒 tests pending").classes("text-xs text-[#e3b341]")


def render_startup_banner() -> None:
    """Live banner of the background startup tasks (upgrades, MCP, cleanup).

    Progressive disclosure: once every task is done the banner disappears
    (finished startup is not news); a failed task keeps it open so the bad
    news is never buried in a collapsed panel.
    """
    from services.webui import startup

    items = startup.statuses()
    if not items:
        ui.label("⏳ Preparing (CLI upgrades, MCP config)…").classes(
            "text-xs text-[#8b949e]",
        )
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
    icons = {"running": "⏳", "done": "✅", "failed": "❌"}
    from services.webui.ui_state import state_expansion

    with state_expansion(
        "layout.startup_tasks",
        f"Startup tasks ({done}/{len(items)} done)",
        # Bad news must never be buried: force it open the first time a task
        # fails, but never re-open (or re-close) a panel the operator set.
        default=failed,
    ).classes("w-full").props("dense"):
        for item in items:
            icon = icons.get(item["state"], "•")
            detail = f" — {item['detail']}" if item["detail"] else ""
            ui.label(f"{icon} {item['task']}{detail}").classes("text-xs")
