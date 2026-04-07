from __future__ import annotations

import logging
import time
from collections.abc import Generator
from enum import Enum, auto
from pathlib import Path

from supervisor.utils.experience_tracker import update_experience
from supervisor.utils.text_utils import (sanitize_event_message,
                                         strip_thinking_blocks)

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


def _ev(level: str, msg: object, **kwargs) -> Event:
    event = {"level": level, "msg": sanitize_event_message(msg)}
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
        self._last_feedback: str = ""
        self._cached_snapshot = None
        self._python_scanner_ran: bool = False

    def _setup_core_services(self, agent: str = ""):
        from supervisor.analyzers.codebase_analyzer import snapshot_codebase
        from supervisor.protocols.protocol import load_protocol
        from supervisor.utils.gitignore_utils import update_gitignore_files
        from supervisor.workspace.workspace_archiver import WorkspaceArchiver

        # Update .gitignore files
        modified_gitignores = update_gitignore_files(self.config.workspace)
        if modified_gitignores:
            logger.info(
                f"Modified {len(modified_gitignores)} .gitignore file(s): {[str(p) for p in modified_gitignores]}",
            )

        self.protocol = load_protocol(self.config.protocol_path)
        self._cached_snapshot = snapshot_codebase(self.config.workspace)
        self.archiver = WorkspaceArchiver(self.config.workspace)
        self._init_components(agent=agent)

    def _init_components(self, agent: str = ""):
        from supervisor.analyzers.opencode_step_detector import \
            OpencodeStepDetector
        from supervisor.monitoring.session_tracker import SessionTracker
        from supervisor.runners.opencode_runner import OpencodeRunner
        from supervisor.workspace.workspace_guard import WorkspaceGuard

        self.runner = OpencodeRunner.from_config(
            self.config,
            agent=agent,
        )
        self.ctx_monitor = SessionTracker(
            self.config.context_threshold,
            self.config.max_tokens,
            self.config.truncation_enabled,
        )
        self.guard = WorkspaceGuard(self.config.workspace, self.config.protected_files)
        self._step_detector = OpencodeStepDetector()

    def _run_python_scanner(self) -> Generator[Event, None, None]:
        """Run python_scanner.py on workspace if .py files exist and not yet run."""
        import os

        if self._python_scanner_ran:
            return
        if not self.config or not self.config.workspace:
            return

        workspace = self.config.workspace.resolve()
        if not workspace.exists():
            return

        py_files = []
        for root, dirs, files in os.walk(workspace):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if f.endswith(".py"):
                    py_files.append(f)
                    break
            if py_files:
                break

        if not py_files:
            return

        self._python_scanner_ran = True
        yield _ev(
            "info",
            f"Running python_scanner on {len(py_files)} Python file(s)...",
        )

        try:
            from supervisor.vulnerability.python_scanner import scan

            scan(
                target=str(workspace),
                autofix_first=True,
                print_output=False,
            )
            yield _ev("info", "python_scanner completed")
        except Exception as e:
            yield _ev("warn", f"python_scanner failed: {e}")

        return

    def run_streaming(self) -> Generator[Event, None, None]:
        if self.config and getattr(self.config, "enable_python_scanner", True):
            yield from self._run_python_scanner()
        try:
            yield from self._run()
        except KeyboardInterrupt:
            if self.runner:
                self.runner.stop()
            yield _ev("warn", "Interrupted by user.")
        except Exception:
            if self.runner:
                self.runner.stop()
            import traceback

            yield _ev("error", f"Unhandled exception:\n{traceback.format_exc()}")

    def _refresh_codebase_snapshot(self) -> bool:
        """Re-snapshot the workspace and update cached snapshot if changed.

        Returns True only if the codebase has changed since the last snapshot.
        """
        from supervisor.analyzers.codebase_analyzer import snapshot_codebase

        if self._cached_snapshot is None:
            return False

        new_snapshot = snapshot_codebase(self.config.workspace)
        changed = self._cached_snapshot.changed_files(new_snapshot)
        if changed:
            logger.info(
                "Codebase evolved: %d changed file(s): %s",
                len(changed),
                changed,
            )
            self._cached_snapshot = new_snapshot
            return True
        return False

    def _check_and_update_snapshot(self) -> Generator[Event, None, None]:
        """Check if codebase changed and update supervisor if so."""
        if self._refresh_codebase_snapshot():
            yield _ev("info", "Codebase has evolved; updating context...")
            new_preamble = self._codebase_preamble()
            self.supervisor.update_system_prompt(new_preamble)
        yield from []

    def _run(self) -> Generator[Event, None, None]:
        raise NotImplementedError("Subclasses must implement _run()")

    def _apply_protection(self) -> Generator[Event, None, None]:
        yield _ev("info", "🔒  Setting read-only protection on critical files…")
        protected_dirs = [
            str(self.config.workspace / d)
            for d in self.guard.get_protected_dirs()
            if (self.config.workspace / d).exists()
        ]
        protected_files = [
            str(self.config.workspace / f)
            for f in self.guard.get_user_protected_files()
            if (self.config.workspace / f).exists()
        ]
        self._all_protected = protected_dirs + protected_files
        if self._all_protected:
            protected = self.guard.set_readonly_protection(self._all_protected)
            yield _ev("info", f"Read-only protection set on {len(protected)} paths")
        else:
            yield _ev("info", "No protected paths found to lock")

    def _remove_protection(self) -> Generator[Event, None, None]:
        yield _ev("info", "🔓  Removing read-only protection from critical files…")
        if hasattr(self, "_all_protected") and self._all_protected:
            unprotected = self.guard.remove_readonly_protection(self._all_protected)
            yield _ev(
                "info",
                f"Read-only protection removed from {len(unprotected)} paths",
            )
            self._all_protected = []
        else:
            yield _ev("info", "No protected paths to unlock")

    def _on_successful_output(self, output: str) -> Generator[Event, None, None]:
        """Hook for subclasses to do something before context monitor updates."""
        yield from []

    def _handle_failure(self, output: str) -> Generator[Event, None, None]:
        self._failures += 1
        retries_remaining = max(0, self.config.max_retries - self._failures)

        yield _ev(
            "warn",
            f"opencode returned empty/timeout (failure {self._failures}/{self.config.max_retries}, "
            f"{retries_remaining} {'retry' if retries_remaining == 1 else 'retries'} remaining).",
        )
        yield from self._forced_summary(output)

        if self._failures >= self.config.max_retries:
            yield from self._on_final_failure(output)
            self._state = LoopState.ENDED_FAILURE
            return

        yield _ev(
            "info",
            f"Retrying… (attempt {self._failures}/{self.config.max_retries})",
        )
        self.runner.start(self._restart_prompt())

    def _on_final_failure(self, output: str) -> Generator[Event, None, None]:
        failure_reason = self._last_feedback if self._last_feedback else "Reached max retries"
        update_experience(self.config.workspace, failed=[failure_reason])
        yield from []
        yield from []

    def _get_step_context(self, progress) -> StepContext:
        from supervisor.core.llm_supervisor import StepContext

        return StepContext(
            current_step=progress.current_step,
            total_steps_estimate=progress.total_steps_estimate,
            phase=progress.phase.name.lower(),
            completed_phases=list(progress.completed_phases),
        )

    def _pre_judge(
        self,
        output: str,
        progress,
    ) -> Generator[Event, None, tuple[str | None, bool]]:
        """Hook before judging. Returns (augmented_output, abort_turn)."""
        yield _ev("info", "Supervisor judging…")
        return output, False

    def _get_verdict(self, output: str, progress) -> SupervisorVerdict:
        raise NotImplementedError

    def _post_judge_feedback(
        self,
        safe_msg: str,
        output: str,
    ) -> Generator[Event, None, str]:
        """Hook to append alignment warnings etc."""
        return safe_msg

    def _do_judgement(self, output: str) -> Generator[Event, None, None]:
        progress = self.runner.get_step_progress()

        augmented_output, abort = yield from self._pre_judge(output, progress)
        if abort:
            return

        actual_output = augmented_output if augmented_output is not None else output

        verdict = self._get_verdict(actual_output, progress)
        yield _ev("supervisor_response", verdict.raw)

        yield from self._emit_token_warnings()

        if verdict.all_targets_met:
            self._state = LoopState.ENDED_SUCCESS
            lesson = self._extract_lesson_from_verdict(verdict.raw)
            if self._failures == 0:
                update_experience(self.config.workspace, worked=[lesson])
                return
            update_experience(self.config.workspace, worked=["Successfully met all targets"])
            return

        vuln_scan = self.scan_for_vulnerabilities()
        feedback_text = verdict.feedback + (vuln_scan or "")
        safe_msg = yield from self._sanitize_feedback(feedback_text)

        safe_msg = yield from self._post_judge_feedback(safe_msg, actual_output)

        self._last_feedback = safe_msg
        yield _ev("opencode_prompt", safe_msg)
        self.runner.send(safe_msg)

        yield from self._yield_suggestions(
            actual_output,
            self._get_step_context(progress),
        )

    def _run_loop(
        self,
        initial_output: str,
        initial_timed_out: bool,
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

            yield from self._handle_session_continuity()

            output, timed_out = self.runner.read_output()

    def _handle_session_continuity(self) -> Generator[Event, None, None]:
        """Decide whether to continue the current opencode session or restart.

        When context is below the continuation threshold, enable --continue
        to maintain session continuity. When context exceeds the threshold,
        write summary.md and start a fresh session.
        """
        if self.ctx_monitor.can_continue_session:
            if self.runner.is_continuation_enabled() or self.runner._session_active:
                self.runner.enable_continuation(True)
                yield _ev("info", "Continuing opencode session (--continue).")
            else:
                self.runner.mark_session_active()
        else:
            yield _ev(
                "info",
                "Context limit approaching — writing summary.md and restarting session.",
            )
            yield from self._forced_summary("")
            self.runner.stop()
            self.runner.reset_session()
            self.runner.reset_context_counter()
            self.ctx_monitor.reset()
            restart_prompt = self._get_restart_prompt_for_continuation()
            yield _ev("opencode_prompt", restart_prompt)
            self.runner.start(restart_prompt)
            self.runner.mark_session_active()

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
            "warn",
            f"Context at {self.ctx_monitor.fraction * 100:.0f}% — compacting.",
        )
        candidates = self.runner.identify_cleanup_candidates()
        if candidates:
            yield _ev(
                "info",
                f"Identified {len(candidates)} files for potential cleanup.",
            )
        deletion_permission = self.supervisor.ask_for_deletion_permission(
            candidates,
            self.config.workspace,
        )
        yield _ev("supervisor_response", deletion_permission.raw)
        msg, _ = self.guard.sanitize_message(
            strip_thinking_blocks(deletion_permission.feedback),
        )
        yield _ev("opencode_prompt", msg)
        self.runner.send(msg)
        self.ctx_monitor.reset()
        yield _ev("info", "Compaction prompt with deletion permissions sent.")

    def _update_context_monitor(self) -> Generator[Event, None, None]:
        files_read = self.runner.get_files_read()
        self.ctx_monitor.update(
            self.runner.estimated_context_tokens,
            files_read=files_read,
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
        )
        (self.config.workspace / "summary.md").write_text(report, encoding="utf-8")
        yield _ev("info", "summary.md written.")

    def _sanitize_feedback(self, feedback: str) -> Generator[Event, None, str]:
        safe_msg, violations = self.guard.sanitize_message(feedback)
        if violations:
            yield _ev("warn", f"Blocked out-of-workspace paths: {violations}")
        return safe_msg

    def _yield_suggestions(
        self,
        opencode_output: str,
        step_context=None,
    ) -> Generator[Event, None, None]:
        suggestions, chosen_paths = self.supervisor.generate_suggestions(
            opencode_output=opencode_output,
            step_context=step_context,
        )
        if chosen_paths:
            yield _ev(
                "supervisor_read_files",
                "\n".join(f"• {p}" for p in chosen_paths),
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

    def _get_restart_prompt_for_continuation(self) -> str:
        """Build a prompt for restarting opencode after context limit is reached.

        Reads the summary.md that was just written and combines it with the
        protocol to give opencode full context in a fresh session.
        """
        from supervisor.prompts import RESTART_PROMPT_TEMPLATE

        summary, protocol_text = self._get_restart_context()
        ws = self.config.workspace.resolve()
        return RESTART_PROMPT_TEMPLATE.format(
            summary=summary,
            protocol_text=protocol_text,
            workspace=ws,
        )

    def _codebase_preamble(self) -> str:
        """Return a digest of the current codebase for the system prompt."""
        return "\n\n## Live codebase\n" + self._cached_snapshot.digest_for_prompt(
            max_files=15,
        )

    @staticmethod
    def _strip_done_phrases(text: str) -> str:
        """Remove supervisor completion phrases from text."""
        from supervisor.core.llm_supervisor import _DONE_PHRASES

        cleaned = text
        for phrase in _DONE_PHRASES:
            cleaned = cleaned.replace(phrase, "")
        import re

        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        cleaned = cleaned.strip()
        return cleaned

    def _extract_lesson_from_verdict(self, raw: str) -> str:
        lines = raw.strip().splitlines()
        for line in lines:
            line_lower = line.lower().strip()
            if not line_lower:
                continue
            if line_lower.startswith(("lesson:", "strategy:", "summary:")):
                return line.split(":", 1)[1].strip()
        content = raw.strip()
        if len(content) > 100:
            content = content[:100] + "..."
        return content

    def _write(self, text: str, filename: str) -> None:
        """Write text to a file in the workspace."""
        (self.config.workspace / filename).write_text(text, encoding="utf-8")

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

            if any(part.startswith(".") for part in Path(rel_root).parts):
                dirs[:] = []
                continue

            dirs[:] = [
                d
                for d in dirs
                if not d.startswith(".")
                and not ignore_matcher.matches(
                    f"{rel_root}/{d}" if rel_root != "." else d,
                )
            ]
            for f in files:
                if f.endswith(".py"):
                    fp = f"{rel_root}/{f}" if rel_root != "." else f
                    if not ignore_matcher.matches(fp):
                        py_files.append(fp)

        if not py_files:
            logger.info(
                "No Python files found in workspace, skipping vulnerability scan",
            )
            return None

        logger.info("Found %d Python file(s) to scan", len(py_files))
        try:
            findings = _vuln_scan(
                target=str(workspace),
                min_severity="HIGH",
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
            if any(part.startswith(".") for part in Path(rel).parts):
                return False
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
