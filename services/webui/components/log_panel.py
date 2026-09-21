"""Incremental live log panel (ui.log — appends lines, no re-render flicker)."""

from __future__ import annotations

from nicegui import ui

from services.webui.log_format import (
    DEFAULT_HIDDEN_LEVELS,
    event_counts,
    filter_events,
    fmt_elapsed,
    fmt_ts,
    start_ts,
)
from supervisor.utils.text_utils import sanitize_event_message

_BLOCK_LABELS = {
    "opencode_prompt": "▶ PROMPT → agent",
    "opencode_output": "◀ OUTPUT ← agent",
    "supervisor_response": "🧠 SUPERVISOR",
    "supervisor_read_files": "📂 SUPERVISOR READ FILES",
}


class LogPanel:
    """Renders the job's event stream into a ui.log, pushing only new lines."""

    def __init__(self, max_lines: int = 1000):
        self.show_verbose = True
        self.newest_first = False
        self.show_noise = False
        self.search = ""
        self._pushed = 0
        self._signature = None

        with ui.row().classes("w-full items-center gap-3 flex-wrap"):
            self.search_input = ui.input(placeholder="🔍 Search logs…").classes(
                "mono w-64",
            )
            self.counts_label = ui.label("").classes("text-xs text-[#8b949e]")
            self.verbose_switch = ui.switch(
                "Verbose", value=True, on_change=self._reset,
            )
            self.newest_switch = ui.switch(
                "Newest first", value=False, on_change=self._reset,
            )
            self.noise_switch = ui.switch(
                "Show noise", value=False, on_change=self._reset,
            ).tooltip("Heartbeats, step-progress pings, and token events.")
            self.download_button = ui.button(
                icon="download", on_click=self._download,
            ).props("flat dense")
            self._download_data = ""
        self.log = ui.log(max_lines=max_lines).classes("log-box w-full h-[520px]")

    # ------------------------------------------------------------------ #

    def _reset(self) -> None:
        self._pushed = 0
        self._signature = None
        self.log.clear()

    async def _download(self) -> None:
        ui.download(self._download_data.encode("utf-8"), "log.txt")

    @staticmethod
    def _format_line(event: dict, start: float | None) -> str:
        level = event.get("level") or "info"
        msg = sanitize_event_message(event.get("msg") or "")
        ts = fmt_ts(event)
        prefix = f"[{ts} {fmt_elapsed(event.get('ts'), start)}] " if ts else ""
        if level in _BLOCK_LABELS:
            return f"{prefix}{_BLOCK_LABELS[level]}\n{msg}"
        icons = {
            "tool": "🔧 ",
            "warn": "⚠️ ",
            "error": "❌ ",
            "success": "✅ ",
        }
        return f"{prefix}{icons.get(level, '')}{msg}"

    def update(self, logs: list[dict]) -> None:
        logs = logs or []
        counts = event_counts(logs)
        self.counts_label.set_text(
            f"🔴 {counts['error']}  ·  🟡 {counts['warn']}  ·  🔧 {counts['tool']}",
        )
        self._download_data = "\n".join(
            f"[{fmt_ts(e) or '--:--:--'}] {e.get('level', 'info')}: "
            f"{sanitize_event_message(e.get('msg') or '')}"
            for e in logs
            if isinstance(e, dict)
        )

        hidden = set(DEFAULT_HIDDEN_LEVELS)
        if self.noise_switch.value:
            hidden = set()
        search = (self.search_input.value or "").strip()
        filtered = filter_events(logs, search, hidden)
        start = start_ts(logs)

        # Re-push from scratch when the stream shrank (new run); otherwise
        # append only new events.
        if len(filtered) < self._pushed:
            self.log.clear()
            self._pushed = 0
        new_events = filtered[self._pushed:]
        if self.newest_switch.value and new_events:
            new_events = list(reversed(new_events))
        for event in new_events:
            self.log.push(self._format_line(event, start))
        self._pushed = len(filtered)
