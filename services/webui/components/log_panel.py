"""Incremental live log panel (ui.log — appends lines, no re-render flicker).

Two widgets live here:

- :class:`TailStrip` — the compact always-visible strip showing only the
  newest K events, auto-scrolling. This is the primary view: the user asked
  for "scroll and keep the latest few lines", not a wall of history.
- :class:`LogPanel` — the full history box (behind an expansion), with the
  "Verbose" switch hiding big-block headline events and a ref viewer that
  fetches a headline event's full body from the job's sidecar.
"""

from __future__ import annotations

from collections.abc import Callable

from nicegui import ui, run as nicegui_run

from services.webui.log_format import (
    DEFAULT_HIDDEN_LEVELS,
    VERBOSE_ONLY_LEVELS,
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

_ICONS = {
    "tool": "🔧 ",
    "warn": "⚠️ ",
    "error": "❌ ",
    "success": "✅ ",
    "blocked": "[BLOCKED] ",
}


def _one_line(event: dict, start: float | None) -> str:
    """Single-line format for the tail strip: icon + first line of msg."""
    level = event.get("level") or "info"
    msg = sanitize_event_message(event.get("msg") or "")
    first = msg.splitlines()[0] if msg.splitlines() else ""
    ts = fmt_ts(event)
    prefix = f"[{ts} {fmt_elapsed(event.get('ts'), start)}] " if ts else ""
    return f"{prefix}{_ICONS.get(level, '')}{first}"


class TailStrip:
    """Fixed strip showing exactly the newest K events, one line each.

    Plain labels in a plain column — deliberately NOT ui.log, whose box
    scrolls. The strip always fits its content (at most K rows), so there is
    no scrollbar: when a new event arrives the oldest row simply falls off.
    """

    def __init__(self, keep: int = 5):
        self.keep = keep
        self._signature = None
        self.container = ui.column().classes("w-full gap-0.5")

    def update(self, logs: list[dict]) -> None:
        logs = [e for e in (logs or []) if isinstance(e, dict)]
        # Noise never reaches the strip — it exists to answer "what's
        # happening right now", not to be exhaustive.
        visible = [e for e in logs if (e.get("level") or "info") not in DEFAULT_HIDDEN_LEVELS]
        start = start_ts(logs)
        lines = [_one_line(e, start) for e in visible[-self.keep:]]
        signature = tuple(lines)
        if signature == self._signature:
            return
        self._signature = signature
        self.container.clear()
        with self.container:
            for line in lines:
                ui.label(line).classes(
                    "mono text-xs whitespace-nowrap overflow-hidden text-ellipsis",
                )


class LogPanel:
    """Renders the job's event stream into a ui.log, pushing only new lines."""

    def __init__(
        self,
        max_lines: int = 1000,
        detail_fetcher: Callable[[int], dict | None] | None = None,
        download_fetcher: Callable[[], list] | None = None,
    ):
        self.show_verbose = True
        self.newest_first = False
        self.show_noise = False
        self._pushed = 0
        self._signature = None
        self._detail_fetcher = detail_fetcher
        self._download_fetcher = download_fetcher

        with ui.row().classes("w-full items-center gap-3 flex-wrap"):
            self.search_input = ui.input(placeholder="🔍 Search logs…").classes(
                "mono w-64",
            )
            self.counts_label = ui.label("").classes("text-xs text-[#8b949e]")
            self.verbose_switch = ui.switch(
                "Verbose", value=True, on_change=self._reset,
            ).tooltip("Off hides prompt/output/verdict headline events.")
            self.newest_switch = ui.switch(
                "Newest first", value=False, on_change=self._reset,
            )
            self.noise_switch = ui.switch(
                "Show noise", value=False, on_change=self._reset,
            ).tooltip("Step-progress pings and token events.")
            if detail_fetcher is not None:
                self.ref_input = ui.input(placeholder="event #").classes("mono w-24")
                ui.button("查看全文", on_click=self._show_ref).props("flat dense")
            if download_fetcher is not None:
                ui.button(
                    icon="download", on_click=self._download,
                ).props("flat dense")
        self.log = ui.log(max_lines=max_lines).classes("log-box w-full h-[520px]")

    # ------------------------------------------------------------------ #

    def _reset(self) -> None:
        self._pushed = 0
        self._signature = None
        self.log.clear()

    async def _download(self) -> None:
        """Full log text, built on a worker thread on demand (one click)."""
        if self._download_fetcher is None:
            return
        data = await nicegui_run.io_bound(self._download_text)
        ui.download(data.encode("utf-8"), "log.txt")

    def _download_text(self) -> str:
        return "\n".join(
            f"[{fmt_ts(e) or '--:--:--'}] {e.get('level', 'info')}: "
            f"{sanitize_event_message(e.get('msg') or '')}"
            for e in (self._download_fetcher() or [])
            if isinstance(e, dict)
        )

    def _show_ref(self) -> None:
        """Fetch one headline event's full body from the job sidecar."""
        if self._detail_fetcher is None:
            return
        try:
            ref = int(str(self.ref_input.value).strip().lstrip("#"))
        except ValueError:
            ui.notify("Enter an event # from a headline line, e.g. 12", type="warning")
            return
        detail = self._detail_fetcher(ref)
        if not detail:
            ui.notify(f"No sidecar record for event #{ref}", type="negative")
            return
        body = sanitize_event_message(detail.get("body") or "")
        ts = time_str(detail.get("ts"))
        with ui.dialog() as dialog, ui.card().classes("w-[900px] h-[70vh]"):
            ui.label(
                f"Event #{ref} · {detail.get('level', '?')} · {len(body):,} chars · {ts}",
            ).classes("text-xs text-[#8b949e]")
            with ui.scroll_area().classes("w-full flex-grow"):
                ui.markdown(f"```\n{body}\n```")
            ui.button("Close", on_click=dialog.close).props("flat")
        dialog.open()

    @staticmethod
    def _format_line(event: dict, start: float | None) -> str:
        level = event.get("level") or "info"
        msg = sanitize_event_message(event.get("msg") or "")
        ts = fmt_ts(event)
        prefix = f"[{ts} {fmt_elapsed(event.get('ts'), start)}] " if ts else ""
        if level in _BLOCK_LABELS:
            return f"{prefix}{_BLOCK_LABELS[level]}\n{msg}"
        return f"{prefix}{_ICONS.get(level, '')}{msg}"

    def update(self, logs: list[dict]) -> None:
        logs = logs or []
        counts = event_counts(logs)
        self.counts_label.set_text(
            f"🔴 {counts['error']}  ·  🟡 {counts['warn']}  ·  🔧 {counts['tool']}",
        )

        hidden = set()
        if not self.verbose_switch.value:
            hidden |= set(VERBOSE_ONLY_LEVELS)
        if not self.noise_switch.value:
            hidden |= set(DEFAULT_HIDDEN_LEVELS)
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


def time_str(ts) -> str:
    if not isinstance(ts, (int, float)) or ts <= 0:
        return ""
    import time

    return time.strftime("%H:%M:%S", time.localtime(ts))
