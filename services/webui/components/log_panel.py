"""Live log widgets: a compact tail strip and a full newest-first panel.

Two widgets live here:

- :class:`TailStrip` — the compact always-visible strip showing only the
  newest K events, auto-scrolling. This is the primary view: the user asked
  for "scroll and keep the latest few lines", not a wall of history.
- :class:`LogPanel` — the full history box (behind an expansion), with
  "Agent speech" / "Tool use" toggles choosing which agent-content
  categories render, and a ref viewer that fetches a headline event's
  full body from the job's sidecar. Noise levels are never shown.
"""

from __future__ import annotations

from collections.abc import Callable

from nicegui import run as nicegui_run
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
    "agent_thought": "💭 AGENT THOUGHT",
    "supervisor_response": "🧠 SUPERVISOR",
    "supervisor_read_files": "📂 SUPERVISOR READ FILES",
}

_ICONS = {
    "agent_thought": "💭 ",
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
                # w-full: the column aligns children flex-start, so without
                # it the label sizes to its text and the ellipsis never cuts.
                ui.label(line).classes(
                    "mono text-xs w-full whitespace-nowrap overflow-hidden text-ellipsis",
                )


class LogPanel:
    """The job's event stream, newest first, rebuilt only when it changes."""

    def __init__(
        self,
        max_lines: int = 1000,
        detail_fetcher: Callable[[int], dict | None] | None = None,
        download_fetcher: Callable[[], list] | None = None,
    ):
        self.max_lines = max_lines
        self._signature = None
        self._logs: list[dict] = []
        self._detail_fetcher = detail_fetcher
        self._download_fetcher = download_fetcher

        with ui.row().classes("w-full items-center gap-3 flex-wrap"):
            self.search_input = ui.input(
                placeholder="🔍 Search logs…", on_change=lambda _e: self._refresh(),
            ).classes("mono w-64").props("debounce=300")
            self.counts_label = ui.label("").classes("text-xs text-[#8b949e]")
            self.speech_switch = ui.switch(
                "Agent speech", value=True, on_change=lambda _e: self._refresh(),
            ).tooltip(
                "The agent's plain-text prose and reasoning summaries "
                "(💭 thoughts). Tool I/O stays out; tool markers have their "
                "own toggle.",
            )
            self.tool_switch = ui.switch(
                "Tool use", value=True, on_change=lambda _e: self._refresh(),
            ).tooltip("Shell / edit / mcp tool markers from the agent.")
            if detail_fetcher is not None:
                self.ref_input = ui.input(placeholder="event #").classes("mono w-24")
                ui.button("查看全文", on_click=self._show_ref).props("flat dense")
            if download_fetcher is not None:
                ui.button(
                    icon="download", on_click=self._download,
                ).props("flat dense")
        self.log = ui.log(max_lines=max_lines).classes(
            "log-box log-panel-newest w-full h-[520px]",
        )

    # ------------------------------------------------------------------ #

    def _refresh(self) -> None:
        # Re-render from the cached stream so search and the category
        # toggles work even on a finished job (no live tick to piggyback on).
        self.update(self._logs)

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
        ts = fmt_ts(detail)
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
        self._logs = logs
        counts = event_counts(logs)
        self.counts_label.set_text(
            f"🔴 {counts['error']}  ·  🟡 {counts['warn']}  ·  🔧 {counts['tool']}",
        )

        # Noise (heartbeat / step-progress / token pings) is never shown.
        # The two toggles choose the agent-content categories.
        hidden = set(DEFAULT_HIDDEN_LEVELS)
        if not self.tool_switch.value:
            hidden.add("tool")
        if not self.speech_switch.value:
            hidden.add("opencode_output")
            hidden.add("agent_thought")
        search = (self.search_input.value or "").strip()
        filtered = filter_events(logs, search, hidden)

        # Newest first is hardcoded. ui.log appends and follows the bottom,
        # so a newest-first view is rendered by re-pushing the newest
        # max_lines in reverse and pinning the scroll to the top. Rebuild
        # only when the visible content actually changes (new events, search,
        # or a category toggle) so an idle stream costs nothing.
        signature = (
            len(filtered),
            filtered[-1].get("ts") if filtered else 0,
            search,
            bool(self.tool_switch.value),
            bool(self.speech_switch.value),
        )
        if signature == self._signature:
            return
        self._signature = signature
        start = start_ts(logs)
        visible = filtered[-self.max_lines:] if self.max_lines else filtered
        self.log.clear()
        for event in reversed(visible):
            self.log.push(self._format_line(event, start))
        # ui.log auto-follows the bottom on render; a large log renders
        # slower than a single delayed scroll, so pin the top repeatedly for
        # a short window until rendering settles. Landing at the top also
        # flips ui.log's bottom-follow off for subsequent updates.
        ui.run_javascript(
            '(() => {'
            ' const sel = ".log-panel-newest .q-scrollarea__container";'
            ' let n = 0;'
            ' const t = setInterval(() => {'
            '   const el = document.querySelector(sel);'
            '   if (el) el.scrollTop = 0;'
            '   if (++n > 15) clearInterval(t);'
            ' }, 40);'
            '})()',
        )
