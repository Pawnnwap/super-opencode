from __future__ import annotations

import logging
import time
from enum import Enum, auto
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)

try:
    from supervisor.vulnerability.python_scanner import scan as _vuln_scan

    logger.info("Vulnerability scanner imported successfully")
except ImportError:
    logger.warning("Python vulnerability scanner not available")
    _vuln_scan = None

Event = dict


class LoopState(Enum):
    RUNNING = auto()
    ENDED_SUCCESS = auto()
    ENDED_FAILURE = auto()


def _ev(level: str, msg: str, **kwargs) -> Event:
    event = {"level": level, "msg": msg}
    event.update(kwargs)
    return event


class BaseLoop:
    def __init__(self, config=None):
        self.config = config
        self.runner = None
        self.supervisor = None
        self.ctx_monitor = None
        self.guard = None

        self._last_step_time: float = 0.0
        self._active_progress_steps: int = 0
        self._timeout_extension_count: int = 0
        self._max_timeout_extensions: int = 3
        self._step_history: list[dict] = []
        self._state = LoopState.RUNNING
        self._failures = 0

    def _init_components(self, agent: str = ""):
        from supervisor.analyzers.opencode_step_detector import \
            OpencodeStepDetector
        from supervisor.monitoring.context_monitor import ContextMonitor
        from supervisor.runners.opencode_runner import OpencodeRunner
        from supervisor.workspace.workspace_guard import WorkspaceGuard

        self.runner = OpencodeRunner(
            self.config.workspace,
            self.config.opencode_model,
            self.config.opencode_executable,
            self.config.timeout,
            agent=agent,
        )
        self.ctx_monitor = ContextMonitor(
            self.config.context_threshold,
            self.config.max_tokens,
            self.config.truncation_enabled,
        )
        self.guard = WorkspaceGuard(self.config.workspace, self.config.protected_files)
        self._step_detector = OpencodeStepDetector()

    def run_streaming(self) -> Generator[Event, None, None]:
        try:
            yield from self._run()
        except KeyboardInterrupt:
            self.runner.stop()
            yield _ev("warn", "Interrupted by user.")
        except Exception:
            import traceback

            self.runner.stop()
            yield _ev("error", f"Unhandled exception:\n{traceback.format_exc()}")

    def _run(self) -> Generator[Event, None, None]:
        raise NotImplementedError("Subclasses must implement _run()")

    def _on_successful_output(self, output: str) -> Generator[Event, None, None]:
        """Hook for subclasses to do something before context monitor updates."""
        yield from []

    def _handle_failure(self, output: str) -> Generator[Event, None, None]:
        raise NotImplementedError()

    def _do_judgement(self, output: str) -> Generator[Event, None, None]:
        raise NotImplementedError()

    def _run_loop(
        self, initial_output: str, initial_timed_out: bool
    ) -> Generator[Event, None, None]:
        output = initial_output
        timed_out = initial_timed_out

        while self._state == LoopState.RUNNING:
            current_progress = self.runner.get_step_progress()

            if timed_out or not output.strip():
                if self._should_extend_timeout(current_progress):
                    yield from self._handle_active_progress_timeout(current_progress)
                    self._timeout_extension_count += 1
                    output, timed_out = self.runner.read_output()
                    continue

                diag = self.runner.last_diagnostic()
                yield _ev("warn", f"opencode returned no output. Diagnostic:\n{diag}")
                yield from self._handle_failure(output)
                if self._state != LoopState.RUNNING:
                    break
                output, timed_out = self.runner.read_output()
                continue

            self._failures = 0
            yield from self._on_successful_output(output)
            yield from self._update_context_monitor()

            previous_step = self._active_progress_steps
            yield from self._emit_step_events(output)
            yield _ev("opencode_output", output)

            current_progress = self.runner.get_step_progress()
            if current_progress.current_step > previous_step:
                self._last_step_time = time.time()
                self._active_progress_steps = current_progress.current_step
                self._timeout_extension_count = 0
                yield from self._emit_heartbeat(current_progress)

            if self.ctx_monitor.should_compact:
                self.supervisor.compact_history()
                yield from self._do_compaction()
                output, timed_out = self.runner.read_output()
                continue

            yield from self._do_judgement(output)
            if self._state != LoopState.RUNNING:
                break

            output, timed_out = self.runner.read_output()

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

    def _handle_active_progress_timeout(self, progress) -> Generator[Event, None, None]:
        ext_count = self._timeout_extension_count + 1
        activity_state = self.runner.get_activity_state()
        wait_msg = (
            " (may be waiting for output)"
            if activity_state == "waiting_for_output"
            else ""
        )
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
            **heartbeat_data,
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
                    **progress_event,
                )

    def _do_compaction(self) -> Generator[Event, None, None]:
        yield _ev(
            "warn", f"Context at {self.ctx_monitor.fraction * 100:.0f}% — compacting."
        )
        candidates = self.runner.identify_cleanup_candidates()
        if candidates:
            yield _ev(
                "info", f"Identified {len(candidates)} files for potential cleanup."
            )
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
        self.ctx_monitor.update(
            self.runner.estimated_context_tokens, files_read=files_read
        )
        if self.ctx_monitor.approaching_limit:
            advice = self.ctx_monitor.get_reduction_advice()
            file_info = (
                f"Files loaded: {', '.join(files_read)}"
                if files_read
                else "Files loaded: none"
            )
            yield _ev(
                "warn",
                f"⚠️ Context usage high: {advice['current_tokens']}/{advice['max_tokens']} tokens "
                f"({self.ctx_monitor.fraction * 100:.0f}%). {advice['recommendation']}.\n\n{file_info}",
            )

    def _emit_token_warnings(self) -> Generator[Event, None, None]:
        warnings = self.supervisor.get_token_warnings()
        if warnings:
            files_read = self.runner.get_files_read()
            file_info = (
                f"Files loaded: {', '.join(files_read)}"
                if files_read
                else "Files loaded: none"
            )
            for warning in warnings:
                yield _ev("warn", f"⚠️ Token warning: {warning}\n\n{file_info}")
            self.supervisor.clear_token_warnings()

    def _forced_summary(self, last_output: str) -> Generator[Event, None, None]:
        yield _ev("info", "Writing summary.md…")
        report = self.supervisor.report_final_status(
            reason="forced summarization",
            opencode_output=last_output,
            workspace=self.config.workspace,
        )
        (self.config.workspace / "summary.md").write_text(report, encoding="utf-8")
        yield _ev("info", "summary.md written.")

    def _sanitize_feedback(self, feedback: str) -> Generator[Event, None, str]:
        safe_msg, violations = self.guard.sanitize_message(feedback)
        if violations:
            yield _ev("warn", f"Blocked out-of-workspace paths: {violations}")
        return safe_msg

    def _yield_suggestions(
        self, opencode_output: str, step_context=None
    ) -> Generator[Event, None, None]:
        suggestions, chosen_paths = self.supervisor.generate_suggestions(
            opencode_output=opencode_output,
            step_context=step_context,
        )
        if chosen_paths:
            yield _ev(
                "supervisor_read_files", "\n".join(f"• {p}" for p in chosen_paths)
            )

        if suggestions and "no suggestions" not in suggestions.lower():
            yield _ev("supervisor_suggestions", suggestions)

    def _get_restart_context(self) -> tuple[str, str]:
        summary_path = self.config.workspace / "summary.md"
        summary = (
            summary_path.read_text(encoding="utf-8")
            if summary_path.exists()
            else "(none)"
        )
        text = self.config.protocol_path.read_text(encoding="utf-8")
        return summary, text

    def scan_for_vulnerabilities(self) -> str | None:
        """Run vulnerability scan on workspace Python files, return formatted results or None."""
        import os

        if _vuln_scan is None:
            logger.debug("Vulnerability scanner not available, skipping scan")
            return None

        from supervisor.workspace.ignore_patterns import IgnoreMatcher

        workspace = self.config.workspace.resolve()
        logger.info("Starting vulnerability scan on workspace: %s", workspace)

        ignore_matcher = IgnoreMatcher(workspace)
        ignore_matcher.load_from_workspace(workspace)

        py_files = []
        for root, dirs, files in os.walk(workspace):
            rel_root = str(Path(root).resolve().relative_to(workspace))

            # Skip hidden directories (those starting with ".") entirely
            if any(part.startswith(".") for part in Path(rel_root).parts):
                dirs[:] = []
                continue

            # Filter out directories matching ignore patterns (e.g. .gitignore rules)
            dirs[:] = [
                d
                for d in dirs
                if not d.startswith(".")  # prune hidden subdirs before descent
                and not ignore_matcher.matches(
                    f"{rel_root}/{d}" if rel_root != "." else d
                )
            ]
            for f in files:
                if f.endswith(".py"):
                    fp = f"{rel_root}/{f}" if rel_root != "." else f
                    if not ignore_matcher.matches(fp):
                        py_files.append(fp)

        if not py_files:
            logger.info(
                "No Python files found in workspace, skipping vulnerability scan"
            )
            return None

        logger.info("Found %d Python file(s) to scan", len(py_files))
        try:
            findings = _vuln_scan(
                target=str(workspace),
                min_severity="MEDIUM",
                autofix_first=True,
                scan_deps=False,
                print_output=False,
            )
            logger.info("Vulnerability scan returned %d finding(s)", len(findings))
        except Exception:
            logger.exception("Vulnerability scan failed with exception")
            return None

        def _should_include(finding) -> bool:
            fpath = finding.file.replace("\\", "/")
            try:
                rel = str(Path(finding.file).resolve().relative_to(workspace))
            except (ValueError, OSError):
                rel = fpath
            # Skip findings in any hidden directory (starting with ".")
            if any(part.startswith(".") for part in Path(rel).parts):
                return False
            # Skip findings matching ignore patterns (protected files, etc.)
            if ignore_matcher.matches(rel):
                return False
            return True

        findings = [f for f in findings if _should_include(f)]
        logger.debug("After filtering: %d finding(s) remain", len(findings))

        if not findings:
            logger.info("No relevant vulnerability findings after filtering")
            return None

        by_severity = {}
        for f in findings:
            by_severity.setdefault(f.severity, []).append(f)

        lines = [
            "\n\n--- vulnerability scan ---",
            f"Found {len(findings)} issue(s) (MEDIUM+ severity, dependencies excluded):",
        ]
        for sev in ("CRITICAL", "HIGH", "MEDIUM"):
            if sev in by_severity:
                lines.append(f"\n{sev} ({len(by_severity[sev])}):")
                for f in by_severity[sev]:
                    lines.append(f"  - {f.file}:{f.line} [{f.tool}] {f.message}")
                    if f.suggestion:
                        lines.append(f"    Fix: {f.suggestion}")

        result = "\n".join(lines)
        logger.info("Vulnerability scan results formatted: %d issue(s)", len(findings))
        return result
