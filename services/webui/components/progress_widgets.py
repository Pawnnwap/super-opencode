"""Progress widgets: phase breadcrumb + token usage bar (in-place updates)."""

from __future__ import annotations

from nicegui import ui

from services.webui.log_format import (
    latest_token_current,
    phase_breadcrumb,
    regex_token_current,
)


class ProgressWidgets:
    """Breadcrumb + token bar that update element values (no rebuilds)."""

    def __init__(self, max_tokens: int):
        self.max_tokens = max_tokens
        # ui.html (not ui.label): the breadcrumb carries <strong>/<s> markup
        # that a plain-text label would show literally.
        self.breadcrumb_label = ui.html("").classes("text-xs text-[#8b949e]")
        self.status_label = ui.label("").classes("text-sm")
        with ui.row().classes("w-full items-center gap-2"):
            self.token_bar = ui.linear_progress(value=0, show_value=False).classes(
                "flex-grow",
            )
            self.token_label = ui.label("").classes("text-xs mono text-[#8b949e]")

    def update(self, logs: list[dict], state: str, is_evolution: bool = False) -> None:
        logs = logs or []
        progress_events = [
            e for e in logs if isinstance(e, dict) and e.get("level") == "step_progress"
        ]
        if progress_events:
            last = progress_events[-1]
            self.breadcrumb_label.set_content(
                phase_breadcrumb(
                    last.get("phase", ""),
                    last.get("completed_phases"),
                ),
            )
        else:
            self.breadcrumb_label.set_content("")

        if state == "RUNNING":
            heartbeat_count = sum(
                1 for e in logs if isinstance(e, dict) and e.get("level") == "heartbeat"
            )
            step_count = sum(
                1 for e in logs
                if isinstance(e, dict) and e.get("level") in ("step", "phase_transition")
            )
            self.status_label.set_text(
                f"🟢 {'Evolution' if is_evolution else 'Background'} process active"
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
