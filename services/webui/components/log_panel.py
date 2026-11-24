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
from typing import ClassVar

from nicegui import run as nicegui_run
from nicegui import ui

from services.webui import i18n
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
    # Text markers, not emoji: they align in a monospace column, they survive
    # a colour-blind reader, and they are actually read out by a screen reader
    # (the emoji set rendered as "red circle" / "wrench").
    "agent_thought": "[THINK] ",
    "tool": "[TOOL] ",
    "warn": "[WRN] ",
    "error": "[ERR] ",
    "success": "[OK] ",
    "blocked": "[BLOCKED] ",
}


def _one_line(event: dict, start: float | None) -> str:
    """Format one tail-strip row: timestamp + marker + message.

    Errors keep up to three lines. Truncating to the first line is what turned
    a real traceback into the useless header "Unhandled exception:".
    """
    level = event.get("level") or "info"
    msg = sanitize_event_message(event.get("msg") or "")
    lines = msg.splitlines()
    if level in {"error", "blocked"}:
        first = " ".join(line.strip() for line in lines[:3] if line.strip())
    else:
        first = lines[0] if lines else ""
    ts = fmt_ts(event)
    prefix = f"[{ts} {fmt_elapsed(event.get('ts'), start)}] " if ts else ""
    return f"{prefix}{_ICONS.get(level, '')}{first}"


_LOG_SCROLL_SELECTOR = ".log-panel-newest .q-scrollarea__container"

# Reader-position tracker, installed once per page. The panel is newest-first,
# so "follow" means "pinned to the top". Without this, every arriving event
# re-pushed the whole list and yanked a reader back to the top (the panel
# used to do exactly that, and then also hammered scrollTop for 0.6 s).
_TRACKER_JS = f"""
(() => {{
  const sel = {_LOG_SCROLL_SELECTOR!r};
  const s = window.__sopLog || (window.__sopLog = {{
    follow: true, pos: 0, height: 0, freeze: 0, el: null, tries: 0,
  }});
  const attach = () => {{
    const el = document.querySelector(sel);
    // The full-log panel sits inside a collapsed expansion, so the scroll
    // container may not exist yet. Give up after ~30 s; the next update()
    // re-arms it.
    if (!el) {{ if (++s.tries < 60) setTimeout(attach, 500); return; }}
    s.tries = 0;
    if (s.el === el) return;
    // A rebuilt panel brings a NEW container: re-bind, keeping follow/pos.
    s.el = el;
    el.addEventListener("scroll", () => {{
      if (Date.now() < s.freeze) return;
      // A cleared list reports 0 with no scrollable range: that is our own
      // rebuild, not the reader moving.
      if (el.scrollHeight - el.clientHeight < 8) return;
      if (el.scrollTop <= 8) {{ s.follow = true; s.pos = 0; }}
      else {{ s.follow = false; s.pos = el.scrollTop; }}
      s.height = el.scrollHeight;
    }}, {{passive: true}});
  }};
  attach();
}})()
"""


def _freeze_js(ms: int = 600) -> str:
    """Silence the scroll listener while the list is rebuilt below it.

    Without this the DOM update emits scroll events at position 0, which the
    tracker would read as "the reader wants to follow" - wiping the very
    position the restore needs. Kept short: the live tick arrives every 2 s,
    and a longer freeze would leave the reader's real scrolling unobserved.
    """
    return (
        "(() => { const s = window.__sopLog;"
        f" if (s) s.freeze = Date.now() + {int(ms)}; }})()"
    )


def _restore_scroll_js(force_follow: bool = False) -> str:
    """Keep the reader where they were; only a follower snaps to the top."""
    forced = "true" if force_follow else "false"
    return f"""
(() => {{
  const s = window.__sopLog;
  if (!s) return;
  const el = document.querySelector({_LOG_SCROLL_SELECTOR!r});
  if (!el) return;
  if ({forced}) {{ s.follow = true; s.pos = 0; }}
  // Newest-first: new events land ABOVE the reader, so the same event keeps
  // its screen position only when the growth is added to scrollTop.
  const target = s.follow ? 0 : s.pos + Math.max(0, el.scrollHeight - s.height);
  const apply = () => {{
    const max = Math.max(0, el.scrollHeight - el.clientHeight);
    el.scrollTop = s.follow ? 0 : Math.min(target, max);
    s.pos = el.scrollTop;
    s.height = el.scrollHeight;
  }};
  apply(); setTimeout(apply, 120); setTimeout(apply, 400);
}})()
"""


class TailStrip:
    """Fixed strip showing exactly the newest K events, one line each.

    Plain labels in a plain column — deliberately NOT ui.log, whose box
    scrolls. The strip always fits its content (at most K rows), so there is
    no scrollbar: when a new event arrives the oldest row simply falls off.

    Each row carries its level as a colour class: a warning in the strip has
    to be findable at a glance, or the strip is just a small log.
    """

    _LEVEL_CLASS: ClassVar[dict[str, str]] = {
        "warn": "log-warn",
        "error": "log-error",
        "success": "log-success",
        "tool": "log-tool",
        "blocked": "log-blocked",
        "supervisor_response": "log-supervisor",
        "opencode_prompt": "log-prompt",
        "opencode_output": "log-output",
    }

    def __init__(self, keep: int = 5):
        self.keep = keep
        self._signature = None
        self.container = ui.column().classes("sop-tail")

    def update(self, logs: list[dict]) -> None:
        logs = [e for e in (logs or []) if isinstance(e, dict)]
        # Noise never reaches the strip — it exists to answer "what's
        # happening right now", not to be exhaustive.
        visible = [
            e for e in logs if (e.get("level") or "info") not in DEFAULT_HIDDEN_LEVELS
        ]
        start = start_ts(logs)
        shown = visible[-self.keep :]
        signature = tuple(
            (e.get("level") or "info", _one_line(e, start)) for e in shown
        )
        if signature == self._signature:
            return
        self._signature = signature
        self.container.clear()
        with self.container:
            for event in shown:
                level = event.get("level") or "info"
                # w-full: the column aligns children flex-start, so without
                # it the label sizes to its text and the ellipsis never cuts.
                # Errors wrap instead of ellipsizing: a cause that is cut off
                # mid-sentence is the same as no cause at all.
                label = ui.label(_one_line(event, start)).classes(
                    "mono text-xs w-full " + self._LEVEL_CLASS.get(level, ""),
                )
                if level in {"error", "blocked"}:
                    label.classes("sop-tail__wrap")
                else:
                    label.classes("whitespace-nowrap overflow-hidden text-ellipsis")


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
            self.search_input = (
                ui.input(
                    placeholder=i18n.t("Search logs…"),
                    on_change=lambda _e: self._refresh(),
                )
                .classes("mono w-56")
                .props("debounce=300")
            )
            self.counts_label = ui.label("").classes("sop-hint mono")
            self.speech_switch = (
                ui.switch(
                    i18n.t("Agent speech"),
                    value=True,
                    on_change=lambda _e: self._refresh(),
                )
                .props("dense")
                .tooltip(
                    "The agent's plain-text prose and reasoning summaries "
                    "(💭 thoughts). Tool I/O stays out; tool markers have their "
                    "own toggle.",
                )
            )
            self.tool_switch = (
                ui.switch(
                    i18n.t("Tool use"),
                    value=True,
                    on_change=lambda _e: self._refresh(),
                )
                .props("dense")
                .tooltip(
                    i18n.t("Shell / edit / mcp tool markers from the agent."),
                )
            )
            # Never let the trailing icons wrap under the filters: they are
            # one control group and split apart at larger type sizes.
            with ui.row().classes("items-center gap-1 ml-auto flex-nowrap"):
                if detail_fetcher is not None:
                    self.ref_input = (
                        ui.input(
                            placeholder=i18n.t("event #"),
                        )
                        .classes("mono w-24")
                        .props("dense")
                    )
                    ui.button(
                        icon="open_in_new",
                        on_click=self._show_ref,
                    ).props("flat dense").tooltip(
                        i18n.t(
                            "Fetch one headline event's full body from the job "
                            "sidecar.",
                        ),
                    )
                if download_fetcher is not None:
                    ui.button(
                        icon="download",
                        on_click=self._download,
                    ).props("flat dense").tooltip(
                        i18n.t("Download the full log as text."),
                    )
        self.log = ui.log(max_lines=max_lines).classes(
            "log-box log-panel-newest w-full h-[520px]",
        )
        # Arm the reader-position tracker before the first update.
        ui.run_javascript(_TRACKER_JS)

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
            ui.notify(
                i18n.t("Enter an event # from a headline line, e.g. 12"),
                type="warning",
            )
            return
        detail = self._detail_fetcher(ref)
        if not detail:
            ui.notify(
                i18n.tr("No sidecar record for event #{ref}", ref=ref),
                type="negative",
            )
            return
        body = sanitize_event_message(detail.get("body") or "")
        ts = fmt_ts(detail)
        with ui.dialog() as dialog, ui.card().classes("w-[900px] h-[70vh]"):
            ui.label(
                i18n.tr(
                    "Event #{ref} · {level} · {chars} chars · {ts}",
                    ref=ref,
                    level=detail.get("level", "?"),
                    chars=f"{len(body):,}",
                    ts=ts,
                ),
            ).classes("text-xs text-[#8b949e]")
            with ui.scroll_area().classes("w-full flex-grow"):
                ui.markdown(f"```\n{body}\n```")
            ui.button(i18n.t("Close"), on_click=dialog.close).props("flat")
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
            i18n.tr(
                "{err} err · {warn} warn · {tool} tool",
                err=counts["error"],
                warn=counts["warn"],
                tool=counts["tool"],
            ),
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
        # max_lines in reverse. Rebuild only when the visible content actually
        # changes (new events, search, or a category toggle) so an idle stream
        # costs nothing.
        #
        # Scroll: a rebuild used to pin scrollTop = 0 for 0.6 s on every new
        # event, so reading anything below the first screenful was impossible
        # while a job ran. The position now belongs to the reader: follow the
        # top only while they are at the top, otherwise keep the lines they
        # were looking at in place.
        signature = (
            len(filtered),
            filtered[-1].get("ts") if filtered else 0,
            search,
            bool(self.tool_switch.value),
            bool(self.speech_switch.value),
        )
        if signature == self._signature:
            return
        # Search/toggle changes are a deliberate re-read: start at the top.
        # Pure event arrival keeps the reader's position.
        re_read = (
            (
                search != self._signature[2]
                or bool(self.tool_switch.value) != self._signature[3]
                or bool(self.speech_switch.value) != self._signature[4]
            )
            if self._signature
            else True
        )
        self._signature = signature
        start = start_ts(logs)
        visible = filtered[-self.max_lines :] if self.max_lines else filtered
        # Re-arm the tracker: the panel may have been rendered inside a
        # collapsed expansion, so no scroll container existed at bind time.
        ui.run_javascript(_TRACKER_JS)
        ui.run_javascript(_freeze_js())
        self.log.clear()
        for event in reversed(visible):
            self.log.push(self._format_line(event, start))
        # ui.log re-renders slower than one round trip on a big list, so the
        # restore re-applies itself a few times over ~0.4 s.
        ui.run_javascript(_restore_scroll_js(force_follow=re_read))
