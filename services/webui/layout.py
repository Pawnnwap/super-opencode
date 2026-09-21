"""Theme, custom CSS, and the shared left-drawer shell for every page."""

from __future__ import annotations

from nicegui import ui

from services.webui.state import app_state

CUSTOM_CSS = """
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;600&family=Syne:wght@400;700;800&display=swap');
body { font-family: 'Syne', sans-serif; background-color: #0d0f14; color: #c9d1d9; }
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
"""

PILL_MAP = {
    "RUNNING": '<span class="pill pill-running">RUNNING</span>',
    "PENDING": '<span class="pill pill-running">PENDING</span>',
    "SUCCESS": '<span class="pill pill-success">SUCCESS</span>',
    "FAILED": '<span class="pill pill-failure">FAILED</span>',
    "CANCELLED": '<span class="pill pill-failure">CANCELLED</span>',
}

_PAGES = [
    ("wizard", "Protocol Wizard", "✍️"),
    ("run", "Live Run", "▶"),
    ("evolve", "Self-Evolution", "🧬"),
]


def apply_theme() -> None:
    ui.add_head_html(f"<style>{CUSTOM_CSS}</style>")
    ui.colors(primary="#1f6feb", secondary="#21262d", accent="#58a6ff")
    ui.dark_mode(True)


def format_status_pill(state: str) -> str:
    return PILL_MAP.get(state, f'<span class="pill pill-idle">{state}</span>')


def _queue_stats_line(job_manager, job_type: str) -> str:
    running = pending = 0
    for job_id in job_manager.store.list_jobs():
        state_data = job_manager.store.get_job_state(job_id) or {}
        if state_data.get("type") != job_type:
            continue
        state = state_data.get("state")
        if state == "RUNNING":
            running += 1
        elif state == "PENDING":
            pending += 1
    return f"{running} running · {pending} queued"


def render_shell(active: str, job_manager) -> None:
    """Left drawer (nav + queue stats + lock badges) and a slim header."""
    tests_ok = app_state.tests_ok()
    with ui.left_drawer(value=True, fixed=True, top_corner=False).classes(
        "bg-[#0a0c10] border-r border-[#21262d]",
    ).props("bordered"):
        ui.label("opencode Supervisor").classes("text-xl font-bold text-[#58a6ff]")
        ui.separator()
        for key, label, icon in _PAGES:
            locked = key in {"run", "evolve"} and not tests_ok
            nav = ui.link(label, f"/{key}").classes(
                "block w-full text-left px-3 py-2 rounded"
                + (" opacity-50" if locked else " hover:bg-[#161b22]"),
            )
            with nav:
                with ui.row().classes("items-center gap-2"):
                    ui.icon(icon)
                    ui.label(label + (" 🔒" if locked else ""))
        ui.separator()
        with ui.column().classes("gap-1 text-xs text-[#8b949e] w-full"):
            ui.label("Run queue").classes("font-bold text-[#9ecbff]")
            ui.label(_queue_stats_line(job_manager, "run"))
            ui.label("Evolution queue").classes("font-bold text-[#9ecbff] mt-2")
            ui.label(_queue_stats_line(job_manager, "evolve"))
        ui.space()
        ui.label("nicegui · opencode").classes("text-[#484f58] text-xs mt-auto")

    with ui.header().classes("bg-[#0a0c10] border-b border-[#21262d] items-center"):
        ui.label("Supervisor Console").classes("text-sm text-[#8b949e] mono")


def render_startup_banner() -> None:
    """Live banner of the background startup tasks (upgrades, MCP, cleanup)."""
    from services.webui import startup

    items = startup.statuses()
    if not items:
        ui.label("⏳ Preparing (CLI upgrades, MCP config)…").classes(
            "text-xs text-[#8b949e]",
        )
        return
    icons = {"running": "⏳", "done": "✅", "failed": "❌"}
    with ui.expansion("Startup tasks").classes("w-full").props("dense"):
        for item in items:
            icon = icons.get(item["state"], "•")
            detail = f" — {item['detail']}" if item["detail"] else ""
            ui.label(f"{icon} {item['task']}{detail}").classes("text-xs")
