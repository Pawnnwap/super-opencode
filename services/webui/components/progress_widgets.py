"""Progress widgets: phase breadcrumb + token usage bar (in-place updates)."""

from __future__ import annotations

from nicegui import ui

from services.webui.log_format import (
    latest_token_current,
    phase_breadcrumb,
    phase_state,
    regex_token_current,
)


class ProgressWidgets:
    """Breadcrumb + token bar that update element values (no rebuilds)."""

    def __init__(self, max_tokens: int, show_phase: bool = True):
        self.max_tokens = max_tokens
        # ui.html (not ui.label): the breadcrumb carries .phase-* markup that
        # a plain-text label would show literally. Harvest jobs have no
        # code/test/review pipeline, so they render without it.
        self.breadcrumb_label = None
        if show_phase:
            self.breadcrumb_label = ui.html("").classes("text-xs text-[#8b949e]")
        self._breadcrumb_html = ""
        self.status_label = ui.label("").classes("text-sm")
        with ui.row().classes("w-full items-center gap-2"):
            self.token_bar = ui.linear_progress(value=0, show_value=False).classes(
                "flex-grow",
            )
            self.token_label = ui.label("").classes("text-xs mono text-[#8b949e]")

    def update(self, logs: list[dict], state: str) -> None:
        logs = logs or []
        # The 2s poll rebuilds the DOM on every set_content, which restarts
        # the chip's CSS animations — push only genuine phase changes.
        if self.breadcrumb_label is not None:
            html = phase_breadcrumb(phase_state(logs, state)) if logs else ""
            if html != self._breadcrumb_html:
                self.breadcrumb_label.set_content(html)
                self._breadcrumb_html = html

        if state == "RUNNING":
            heartbeat_count = sum(
                1 for e in logs if isinstance(e, dict) and e.get("level") == "heartbeat"
            )
            step_count = sum(
                1 for e in logs
                if isinstance(e, dict) and e.get("level") in ("step", "phase_transition")
            )
            self.status_label.set_text(
                "🟢 Background process active"
                f" · 💓 {heartbeat_count} · 🧭 {step_count}",
            )
        else:
            self.status_label.set_text("")

        current = latest_token_current(logs)
        fraction = None
        if current is not None and self.max_tokens > 0:
            fraction = current / self.max_tokens
        elif current is None:
            fallback = regex_token_current(logs)
            if fallback is not None:
                current, fraction = fallback

        if current is not None and fraction is not None:
            color = "🔴" if fraction > 0.9 else "🟡" if fraction > 0.7 else "🟢"
            self.token_bar.set_value(min(fraction, 1.0))
            self.token_label.set_text(f"{color} {current:,} / {self.max_tokens:,} tokens")
