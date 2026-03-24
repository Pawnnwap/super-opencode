from __future__ import annotations

import time
from typing import Generator

Event = dict

def _ev(level: str, msg: str, **kwargs) -> Event:
    event = {"level": level, "msg": msg}
    event.update(kwargs)
    return event

class BaseLoop:
    def __init__(self):
        # These should be initialized by subclasses, but we declare them here
        # to ensure the common methods can use them.
        self.config = None
        self.runner = None
        self.supervisor = None
        self.ctx_monitor = None
        self.guard = None
        
        self._last_step_time: float = 0.0
        self._active_progress_steps: int = 0
        self._timeout_extension_count: int = 0
        self._max_timeout_extensions: int = 3
        self._step_history: list[dict] = []

    def run_streaming(self) -> Generator[Event, None, None]:
        try:
            yield from self._run()
        except KeyboardInterrupt:
            self.runner.stop()
            yield _ev("warn", "Interrupted by user.")
        except Exception as exc:
            import traceback
            self.runner.stop()
            yield _ev("error", f"Unhandled exception:\n{traceback.format_exc()}")

    def _run(self) -> Generator[Event, None, None]:
        raise NotImplementedError("Subclasses must implement _run()")

    def _should_extend_timeout(self, progress) -> bool:
        if self._timeout_extension_count >= self._max_timeout_extensions:
            return False
        activity_state = self.runner.get_activity_state()
        if activity_state in ("active_progress", "waiting_for_output"):
            time_since_last_step = time.time() - self._last_step_time
            return time_since_last_step < (self.config.timeout * 0.8)
        if progress.current_step > 0 and progress.phase.name != "UNKNOWN":
            time_since_last_step = time.time() - self._last_step_time
            return time_since_last_step < (self.config.timeout * 0.8)
        return False

    def _handle_active_progress_timeout(
        self, progress
    ) -> Generator[Event, None, None]:
        ext_count = self._timeout_extension_count + 1
        activity_state = self.runner.get_activity_state()
        wait_msg = " (may be waiting for output)" if activity_state == "waiting_for_output" else ""
        yield _ev(
            "info",
            f"opencode is actively working (step {progress.current_step}, "
            f"phase: {progress.phase.name.lower()}, state: {activity_state}{wait_msg}). "
            f"Timeout extension {ext_count}/{self._max_timeout_extensions} — continuing...",
        )

    def _emit_heartbeat(self, progress) -> Generator[Event, None, None]:
        heartbeat_data = {
            "current_step": progress.current_step,
            "total_steps_estimate": progress.total_steps_estimate,
            "percentage": progress.percentage,
            "phase": progress.phase.name.lower(),
        }
        yield _ev(
            "heartbeat",
            f"opencode active: step {progress.current_step}/{progress.total_steps_estimate} "
            f"({progress.phase.name.lower()}) — {progress.percentage:.0f}% complete",
            **heartbeat_data
        )

    def _emit_step_events(self, output: str) -> Generator[Event, None, None]:
        for event in self.runner.get_step_events(output):
            lvl = event.get("level", "info")
            if lvl == "step":
                yield _ev(
                    "step",
                    f"{event.get('phase_label', 'Step')} - {event.get('msg', '')[:100]}",
                )
                self._step_history.append(event)
            elif lvl == "phase_transition":
                yield _ev(
                    "phase_transition",
                    f"Phase transition: {event.get('from_phase', '?')} → {event.get('to_phase', '?')}",
                )
                self._step_history.append(event)
            elif lvl == "step_progress":
                progress = self.runner.get_step_progress()
                progress_event = {
                    "current_step": progress.current_step,
                    "total_steps_estimate": progress.total_steps_estimate,
                    "percentage": progress.percentage,
                    "phase": progress.phase.name.lower(),
                    "completed_phases": list(progress.completed_phases),
                }
                yield _ev(
                    "step_progress",
                    f"Step progress: {progress.current_step}/{progress.total_steps_estimate} ({progress.percentage:.0f}%) - {progress.phase.name.lower()}",
                    **progress_event
                )

    def _do_compaction(self) -> Generator[Event, None, None]:
        yield _ev(
            "warn", f"Context at {self.ctx_monitor.fraction * 100:.0f}% — compacting."
        )
        candidates = self.runner.identify_cleanup_candidates()
        if candidates:
            yield _ev("info", f"Identified {len(candidates)} files for potential cleanup.")
        deletion_permission = self.supervisor.ask_for_deletion_permission(
            candidates, self.config.workspace
        )
        yield _ev("supervisor_response", deletion_permission.raw)
        msg, _ = self.guard.sanitize_message(deletion_permission.feedback)
        yield _ev("opencode_prompt", msg)
        self.runner.send(msg)
        self.ctx_monitor.reset()
        yield _ev("info", "Compaction prompt with deletion permissions sent.")

    def _update_context_monitor(self) -> Generator[Event, None, None]:
        files_read = self.runner.get_files_read()
        self.ctx_monitor.update(self.runner.estimated_context_tokens, files_read=files_read)
        if self.ctx_monitor.approaching_limit:
            advice = self.ctx_monitor.get_reduction_advice()
            file_info = f"Files loaded: {', '.join(files_read)}" if files_read else "Files loaded: none"
            yield _ev(
                "warn",
                f"⚠️ Context usage high: {advice['current_tokens']}/{advice['max_tokens']} tokens "
                f"({self.ctx_monitor.fraction*100:.0f}%). {advice['recommendation']}.\n\n{file_info}"
            )

    def _emit_token_warnings(self) -> Generator[Event, None, None]:
        warnings = self.supervisor.get_token_warnings()
        if warnings:
            files_read = self.runner.get_files_read()
            file_info = f"Files loaded: {', '.join(files_read)}" if files_read else "Files loaded: none"
            for warning in warnings:
                yield _ev("warn", f"⚠️ Token warning: {warning}\n\n{file_info}")
            self.supervisor.clear_token_warnings()

