from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Generator
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING

from supervisor.core.goal_progress import GoalProgressTracker
from supervisor.core.llm_support.models import StepContext
from supervisor.utils.experience_tracker import (
    EvolutionSummary,
    log_evolution_summary,
    update_experience,
)
from supervisor.utils.filesystem.file_ops import safe_read_text
from supervisor.utils.text_utils import (
    head_tail_excerpt,
    sanitize_event_message,
    strip_thinking_blocks,
)

if TYPE_CHECKING:
    from supervisor.core.llm_support.models import SupervisorVerdict

logger = logging.getLogger(__name__)

try:
    from vulnerability.python_scanner import scan as _vuln_scan

    logger.info("Vulnerability scanner imported successfully")
except ImportError:
    logger.warning("Python vulnerability scanner not available")
    _vuln_scan = None

Event = dict


class LoopState(Enum):
    RUNNING = auto()
    ENDED_SUCCESS = auto()
    ENDED_FAILURE = auto()
    # Target demonstrably unreachable: terminal, but not an execution failure.
    ENDED_BLOCKED = auto()


@dataclass
class _ClosureNote:
    """Outcome of the BLOCKED→closed cross-check (see _closure_state_note)."""

    was_closed: bool
    markdown: str
    event_suffix: str
    # Durable status written for the target: "closed" (terminal) or
    # "blocked" (this run is over, but a relaunch may resume the target).
    status: str = "closed"


def _ev(level: str, msg: object, **kwargs) -> Event:
    event = {"level": level, "msg": sanitize_event_message(msg)}
    event.update(kwargs)
    return event


# A NEXT_ACTION that instructs the agent to stand down is a closed loop, not
# a plan. Kept deliberately narrow: leading "None"/"no action"/stand-down
# phrasing, or stop/terminate aimed at the run itself. "None of the gates
# pass; improve X" is a diagnosis, not a stand-down, and must not match.
_STANDDOWN_ACTION_RE = re.compile(
    r"^(?:"
    r"none(?:\s+(?:in|actionable|authori[sz]ed|admissible|executable)\b|\s*[.;:!]|$)"
    r"|nothing\b"
    r"|no\s+(?:further|action|admissible|in-workspace|executable|authori[sz]ed|more)\b"
    r"|(?:stand|remain|stay|hold)\b[^;]{0,40}\b"
    r"(?:stand\s*-?\s*down|down|standby|frozen|terminal)\b"
    r"|take\s+(?:no|zero)\b"
    r"|zero\b[^;]{0,30}\baction\b"
    r"|(?:stop|terminate|cease|halt)\b[^;]{0,60}\b"
    r"(?:workspace|lane|thread|run|loop|agent|activity|actions?|turns?)\b"
    r"|close\s+(?:this|the)\b[^;]{0,30}\b(?:thread|lane|study|run|workspace)\b"
    r"|await\b[^;]{0,80}\b"
    r"(?:supervisor|control[-\s]?channel|termination|ruling|operator|director|human)\b"
    r")",
    re.IGNORECASE,
)


def _is_standdown_action(action: str) -> bool:
    """True when the judge's NEXT_ACTION tells the agent to do nothing."""
    normalized = " ".join((action or "").split())
    normalized = normalized.lstrip("*_`#>~- ").strip()
    return bool(_STANDDOWN_ACTION_RE.match(normalized))


# A judge ordering the run to close is a terminal directive, not a hint. The
# failure-condition phrasing is the study protocol's own stop rule; "close the
# study" at a sentence start and "the study is closed/terminal" are the other
# unambiguous forms. Conditional plans ("if all arms fail, invoke ...") are
# excluded in _is_closure_directive so a pre-registration is not mistaken for
# an order.
_CLOSURE_DIRECTIVE_RE = re.compile(
    r"(?:"
    r"\b(?:invoke|trigger|execute|apply)\s+(?:the\s+)?protocol(?:'s)?\s+"
    r"failure\s+condition\b"
    r"|\bprotocol\s+failure\s+condition\s+(?:is\s+|has\s+been\s+)?"
    r"(?:invoked|triggered|met)\b"
    r"|\b(?:the\s+)?study\s+(?:is|has\s+been)\s+"
    r"(?:(?:terminally|finally|effectively)\s+)?(?:closed|terminal)\b"
    r"|(?:^|[.;!?]\s*)close\s+(?:out\s+)?(?:the\s+)?study\b"
    r"|\bdo\s+not\s+(?:burn|spend|use)\s+(?:additional|further|more)\s+"
    r"(?:slots|arms|submissions)\b"
    r")",
    re.IGNORECASE | re.MULTILINE,
)


def _is_closure_directive(action: str) -> bool:
    """True when the judge's NEXT_ACTION orders the study closed.

    A match preceded by an ``if`` clause within the same sentence is a
    pre-registered conditional, not a current order.
    """
    text = " ".join((action or "").split())
    for match in _CLOSURE_DIRECTIVE_RE.finditer(text):
        prefix = text[max(0, match.start() - 48):match.start()].lower()
        if re.search(r"\bif\b[^.;]{0,40}$", prefix):
            continue
        return True
    return False


def _has_positive_reachability_evidence(assessment) -> bool:
    """True when recorded facts contain measured evidence of attainability.

    "No gate is known-blocked" is not evidence: an all-open facts file is
    neutral (everything untested). Treating that as proof of reachability made
    an explicit terminal ruling write the reopenable `blocked` status instead
    of `closed` - the closure was contradicted by an absence of tests. A gate
    that actually passed, or a measured ceiling meeting the required count, is
    positive evidence.
    """
    gates = list(getattr(assessment, "gates", ()) or ())
    if gates:
        return any(gate.status == "passing" for gate in gates)
    ceiling = getattr(assessment, "known_ceiling", None)
    required = getattr(assessment, "required_gates", None)
    return ceiling is not None and required is not None and ceiling >= required


def loop_breaker_section(reason: str) -> str:
    """Restart-prompt section telling the agent how to break its loop.

    The original note only said "do NOT repeat it" and quoted the offending
    command verbatim — run_dd910ecc still looped through both restarts
    because the agent knew which call to avoid but not which *mechanism*
    was broken. Name the mechanism and the way out instead. ``reason`` is
    the loop detector's verdict ("repeated the same tool call (...)",
    "ran many tools without producing any output (...)", ...).
    """
    from supervisor.prompts import SHELL_HEREDOC_RULE

    return (
        "IMPORTANT: the previous session was killed because it got stuck in a loop:\n"
        f"{reason}\n"
        "Do not simply retry it. Break the loop:\n"
        "- If a shell command failed or returned nothing twice, change the "
        f"mechanism, not the wording: {SHELL_HEREDOC_RULE}\n"
        "- If you only need to inspect state, use a read-only command "
        "(cat, ls) instead of re-running the failing script.\n\n"
    )


class BaseLoop:
    def __init__(self, config=None):
        self.config = config
        self.runner = None
        self.supervisor = None
        self.ctx_monitor = None
        self.guard = None

        self._last_step_time: float = 0.0
        # Wall clock at loop construction. A closure document older than this
        # belongs to a previous cycle and must not close the new run.
        self._run_started_at: float = time.time()
        # Ruling lines already on disk at start belong to earlier cycles; any
        # later edit to the document must not make them "current" again.
        self._closure_baseline: frozenset[str] = frozenset()
        self._audit_baseline_offset: int = 0
        if config is not None:
            from supervisor.core.target_state import (
                audit_journal_size,
                closure_ruling_lines,
            )

            self._closure_baseline = closure_ruling_lines(config.workspace)
            self._audit_baseline_offset = audit_journal_size(config.workspace)
        # Structured decision service (jev), opt-in. It arbitrates textual
        # closure candidates only; hard evidence stays deterministic.
        self._jev = None
        self._jev_closeout_pending = False
        self._jev_notes: list[str] = []
        if config is not None and getattr(config, "enable_jev_decisions", False):
            jev_url = str(getattr(config, "jev_decision_url", "") or "").strip()
            if jev_url:
                from supervisor.core.decision_service import JevDecisionClient

                self._jev = JevDecisionClient(
                    jev_url,
                    timeout=float(
                        getattr(config, "jev_decision_timeout", 20.0) or 20.0,
                    ),
                    threshold=float(
                        getattr(config, "jev_decision_threshold", 0.80) or 0.80,
                    ),
                    api_key=str(
                        getattr(config, "jev_decision_api_key", "") or "",
                    ),
                )
        self._active_progress_steps: int = 0
        self._timeout_extension_count: int = 0
        self._max_timeout_extensions: int = 3
        self._step_history: list[dict] = []
        self._state = LoopState.RUNNING
        self._failures = 0
        self._task_turn: int = 0
        self._loop_restarts: int = 0
        self._max_loop_restarts: int = 2
        self._loop_breaker: str = ""
        # BLOCKED exit accounting (config.enable_blocked_exit): attainment
        # high-water marks (obligations ever MET, best passing-gate count) and
        # the feasibility-facts streak. Keyed on attainment, never on file
        # churn - an agent that rewrites summary.md every turn makes a diff
        # forever, which is exactly the loop this trigger must survive.
        self._progress = GoalProgressTracker()
        # Legacy identical-unmet-set rule, used only when the judge emits no
        # per-criterion verdict (see _unstructured_streak).
        self._unmet_streak: int = 0
        self._unmet_streak_sig: str = ""
        self._unreachable_streak: int = 0
        self._last_passing_gates: int | None = None
        # Highest passing-gate count ever seen, updated every judged turn.
        # Separate from _last_passing_gates, which the feasibility streak
        # needs as the *previous* turn's value.
        self._gate_high_water: int | None = None
        # Consecutive judged turns whose NEXT_ACTION is an explicit stand-down
        # ("None ... await supervisor termination"). This trigger is
        # independent of the criteria block, so a judge that emits no
        # structure cannot keep the loop alive by refusing to describe its
        # unmet set.
        self._standdown_streak: int = 0
        # Attainment verdict for the current judged turn, handed to the goal
        # guard so durable target state stops treating file churn as progress.
        self._last_attainment_progressed: bool | None = None
        # Why the run is closing as BLOCKED; also set by the agent-asserted
        # terminal-closure trigger.
        self._blocked_reason: str = ""
        # First-blocked rescue attempts consumed (config.max_blocked_rescues).
        # When jev is enabled the first blocked verdict is held while the judge
        # researches a recovery plan; jev rules on it. Any later blocked verdict
        # (or any failure in the rescue path) closes the run.
        self._blocked_rescues_used: int = 0
        # Why the run is aborting, when it is NOT the normal max-retries path
        # (e.g. loop-persists abort). Drives the final failure report wording;
        # empty = retries exhausted.
        self._abort_reason: str = ""
        from supervisor.analyzers.loop_detector import CrossTurnLoopDetector

        self._cross_turn_detector = CrossTurnLoopDetector()
        self._pending_cross_loop: str = ""
        self._last_feedback: str = ""
        self._cached_snapshot = None
        # Workspace evidence for the goal guard (book ch.8 exit validation):
        # refreshed once per judged turn by _refresh_codebase_snapshot.
        self._last_snapshot_signature: str = ""
        self._last_changed_files: list[str] = []
        self._run_changed_files: set[str] = set()
        # Per-file change markers for the current turn (path -> "+new" etc.),
        # consumed by the judge's file-selection listing.
        self._turn_deltas: dict[str, str] = {}
        self._python_scanner_ran: bool = False
        self._vuln_autofixed: bool = False
        # Supervisor skill bank (set up by _setup_skill_bank after the
        # supervisor exists); None = feature off or no skills discovered.
        self._skill_bank = None
        # Skill-load actions are queued as (level, msg) during _get_verdict
        # (a plain function) and yielded into the event stream by
        # _do_judgement, like every other supervisor action.
        self._skill_events: list[tuple[str, str]] = []

    @property
    def _engine_name(self) -> str:
        """Active execution engine name for user-facing log text ("opencode"|"codex")."""
        from supervisor.runners.factory import resolve_engine

        return resolve_engine(self.config)

    def request_stop(self) -> None:
        """Request cooperative loop shutdown without yielding cleanup events."""
        self._state = LoopState.ENDED_FAILURE
        if self.runner:
            self.runner.stop()

    def _setup_core_services(self, agent: str = ""):
        from supervisor.analyzers.codebase_analyzer import snapshot_codebase
        from supervisor.protocols.protocol import load_protocol
        from supervisor.utils.filesystem.gitignore_utils import update_gitignore_files
        from supervisor.workspace.workspace_archiver import create_archiver

        # Update .gitignore files
        modified_gitignores = update_gitignore_files(self.config.workspace)
        if modified_gitignores:
            logger.info(
                f"Modified {len(modified_gitignores)} .gitignore file(s): {[str(p) for p in modified_gitignores]}",
            )

        self.protocol = load_protocol(self.config.protocol_path)
        self._cached_snapshot = snapshot_codebase(self.config.workspace)
        self.archiver = create_archiver(
            self.config.workspace,
            keep_last=self.config.archive_keep_last,
            backend=getattr(self.config, "archive_backend", "git"),
        )
        self._init_components(agent=agent)

        # Pre-trust the workspace for non-interactive agent runs (opencode
        # auto-rejects permission prompts when no TTY is attached).
        from supervisor.runners.factory import resolve_engine

        engine = resolve_engine(self.config)
        if engine == "opencode":
            written = self.guard.ensure_agent_permissions(self.config.workspace)
            if written:
                logger.info("Project permission config written: %s", written)

        # Hard path policy: translate the whitelist into the active engine's
        # native gate before the first turn. The prompt block alone is advice;
        # these artifacts are what actually deny the call.
        self._install_access_guard(engine)

    def _init_components(self, agent: str = ""):
        from supervisor.analyzers.opencode_step_detector import OpencodeStepDetector
        from supervisor.monitoring.session_tracker import SessionTracker
        from supervisor.runners.factory import create_runner
        from supervisor.workspace.workspace_guard import WorkspaceGuard

        self.runner = create_runner(
            self.config,
            agent=agent,
        )
        self.ctx_monitor = SessionTracker(
            self.config.context_threshold,
            self.config.max_tokens,
            self.config.truncation_enabled,
        )
        self.access_policy = self._build_access_policy()
        self.guard = WorkspaceGuard(
            self.config.workspace,
            self.config.protected_files,
            access_policy=self.access_policy,
        )
        self.access_guard = None
        self._violation_offset = 0
        self._step_detector = OpencodeStepDetector()

    def _build_access_policy(self):
        """Compute the run's hard path policy from the supervisor config."""
        from supervisor.security.access_policy import AccessPolicy

        try:
            policy = AccessPolicy.from_config(self.config)
        except (OSError, ValueError) as exc:
            logger.warning("Access policy could not be built (%s); denying all", exc)
            policy = AccessPolicy(
                workspace=self.config.workspace, whitelist=(), enabled=True,
            )
        if policy.enabled:
            logger.info(
                "Access policy: writable=%s read-only=%s",
                policy.workspace,
                [str(item) for item in policy.whitelist] or "none",
            )
        return policy

    def _install_access_guard(self, engine: str) -> None:
        """Write the engine-specific gate and hand its flags to the runner."""
        from supervisor.security.engine_guards import install_access_guard

        try:
            result = install_access_guard(
                self.config.workspace,
                self.access_policy,
                engine,
            )
        except (OSError, ValueError) as exc:
            # Fail closed: keep the run from starting with a policy the engine
            # cannot enforce.
            logger.error("Access guard installation failed: %s", exc)
            raise RuntimeError(f"access guard installation failed: {exc}") from exc

        self.access_guard = result
        if result.failed:
            raise RuntimeError(
                "access guard could not be installed for "
                f"{engine}: {'; '.join(result.warnings) or 'unknown error'}",
            )
        from supervisor.security.preflight import run_drill

        drill = run_drill(engine, self.config.workspace, self.access_policy, result)
        self.access_drill = drill
        for check in drill.checks:
            logger.info("Access drill %s", check.line())
        if not drill.ok:
            # Cage's canary lesson: prove the boundary instead of trusting the
            # configuration. A gate that does not deny is a failed install.
            raise RuntimeError(
                f"access policy drill failed: {drill.fail_text()}",
            )
        if result.warnings:
            for warning in result.warnings:
                logger.warning("Access guard: %s", warning)
        if result.flags:
            setattr(self.runner, "access_guard_flags", list(result.flags))
        if result.env:
            # Every engine process layer merges this bag into the child env, so
            # the native gate can find the policy snapshot (which lives outside
            # the agent's writable cwd).
            setattr(self.runner, "access_guard_env", dict(result.env))
            merged = dict(getattr(self.runner, "codex_extra_env", None) or {})
            merged.update(result.env)
            setattr(self.runner, "codex_extra_env", merged)
        logger.info(
            "Access guard installed for %s: %s",
            engine,
            [str(path) for path in result.artifacts],
        )

    def _drain_access_violations(self) -> Generator[Event]:
        """Surface policy blocks recorded by the engine gate as job events."""
        import json

        guard = getattr(self, "access_guard", None)
        if not guard:
            return
        log_ref = (guard.env or {}).get("SOP_ACCESS_LOG", "")
        if not log_ref:
            return
        path = Path(log_ref)
        offset = getattr(self, "_violation_offset", 0)
        try:
            if not path.exists():
                return
            size = path.stat().st_size
            if size < offset:
                offset = 0  # rotated/truncated
            if size == offset:
                return
            with open(path, "r", encoding="utf-8") as handle:
                handle.seek(offset)
                lines = handle.readlines()
                self._violation_offset = handle.tell()
        except OSError:
            return

        for line in lines[-5:]:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            items = "; ".join(
                f"{item.get('path')} ({item.get('reason')})"
                for item in record.get("violations", [])
                if isinstance(item, dict)
            )
            yield _ev(
                "warn",
                f"ACCESS POLICY BLOCKED {record.get('tool') or 'tool'}: {items}",
            )

    def _create_supervisor(
        self,
        *,
        max_protected_files_for_suggestions: int | None = None,
    ):
        from supervisor.core.llm_supervisor import LLMSupervisor

        kwargs = dict(
            protocol=self.protocol,
            workspace=self.config.workspace,
            model=self.config.supervisor_model,
            extra_system=self._codebase_preamble(),
            read_external_feedback=self.config.read_external_feedback,
            max_tokens=self.config.max_tokens,
            truncation_enabled=self.config.truncation_enabled,
            max_history_turns=self.config.max_history_turns,
            compact_intermediate_steps=self.config.compact_intermediate_steps,
            history_verbatim_turns=getattr(
                self.config, "history_verbatim_turns", 1,
            ),
            history_budget_fraction=getattr(
                self.config, "history_budget_fraction", 0.35,
            ),
            reuse_identical_verdicts=getattr(
                self.config, "reuse_identical_verdicts", True,
            ),
            model_backup=self.config.supervisor_model_backup,
            api_key=getattr(self.config, "openai_api_key", "") or None,
            base_url=getattr(self.config, "openai_base_url", "") or None,
            reasoning=getattr(self.config, "supervisor_reasoning", "") or "",
        )
        if max_protected_files_for_suggestions is not None:
            kwargs["max_protected_files_for_suggestions"] = (
                max_protected_files_for_suggestions
            )
        return LLMSupervisor(**kwargs)

    def _run_python_scanner(self) -> Generator[Event]:
        """Run python_scanner.py on workspace if .py files exist and not yet run."""
        if self._python_scanner_ran:
            return
        if _vuln_scan is None:
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
            import concurrent.futures

            _SCAN_TIMEOUT = 300  # 5-minute hard ceiling for the whole scan

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _pool:
                _future = _pool.submit(
                    _vuln_scan,
                    target=str(workspace),
                    autofix_first=True,
                    print_output=False,
                )
                try:
                    _future.result(timeout=_SCAN_TIMEOUT)
                except concurrent.futures.TimeoutError:
                    yield _ev("warn", f"python_scanner timed out after {_SCAN_TIMEOUT}s, continuing")
                    return

            # Startup autofix already mutated the workspace; mark it so the
            # first judgement scan does NOT autofix a second time.
            self._vuln_autofixed = True
            yield _ev("info", "python_scanner completed")
        except Exception as e:
            yield _ev("warn", f"python_scanner failed: {e}")

        return

    def run_streaming(self) -> Generator[Event]:
        try:
            if self.config and getattr(self.config, "enable_python_scanner", True):
                yield from self._run_python_scanner()
            yield from self._run()
        except GeneratorExit:
            self._state = LoopState.ENDED_FAILURE
            self._cleanup_after_generator_close()
            raise
        except KeyboardInterrupt:
            if self.runner:
                self.runner.stop()
            self._state = LoopState.ENDED_FAILURE
            yield _ev("warn", "Interrupted by user.")
        except Exception:
            if self.runner:
                self.runner.stop()
            self._state = LoopState.ENDED_FAILURE
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
        self._last_changed_files = changed
        self._run_changed_files.update(changed)
        self._last_snapshot_signature = new_snapshot.signature()
        self._record_turn_deltas(self._cached_snapshot, new_snapshot)
        if changed:
            logger.info(
                "Codebase evolved: %d changed file(s): %s",
                len(changed),
                changed,
            )
            self._cached_snapshot = new_snapshot
            return True
        return False

    def _record_turn_deltas(self, before, after) -> None:
        """Store this turn's per-file change markers for the judge's file picker.

        The picker used to see only `path (size bytes)` in alphabetical order,
        so a deliverable rewritten this turn looked identical to a stale
        artifact. The delta is what makes picking files for verification
        meaningful; it is a no-op on backends without snapshot support.
        """
        self._turn_deltas = {}
        if before is None or after is None:
            return
        try:
            deltas = before.deltas(after)
        except Exception:
            logger.warning("Could not compute per-turn deltas", exc_info=True)
            return
        self._turn_deltas = {
            path: delta.render() for path, delta in deltas.items()
        }

    def _check_and_update_snapshot(self) -> Generator[Event]:
        """Check if codebase changed and update supervisor if so."""
        if self._refresh_codebase_snapshot():
            yield _ev("info", "Codebase has evolved; updating context...")
            new_preamble = self._codebase_preamble()
            self.supervisor.update_system_prompt(new_preamble)
        yield from []

    def _run(self) -> Generator[Event]:
        raise NotImplementedError("Subclasses must implement _run()")

    def _apply_protection(self) -> Generator[Event]:
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

    def _remove_protection(self) -> Generator[Event]:
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

    def _remove_protection_immediately(self) -> list[str]:
        """Remove read-only protection without yielding events."""
        if not hasattr(self, "_all_protected") or not self._all_protected:
            return []
        unprotected = self.guard.remove_readonly_protection(self._all_protected)
        self._all_protected = []
        return unprotected

    def _cleanup_after_generator_close(self) -> None:
        try:
            self._remove_protection_immediately()
        except Exception:
            logger.warning(
                "Failed to remove protection during generator close",
                exc_info=True,
            )
        if self.runner:
            self.runner.stop()

    def _archive_before_new_run(self) -> Generator[Event]:
        """Archive current workspace state before starting a fresh session."""
        if not hasattr(self, "archiver") or self.archiver is None:
            return
        yield _ev("info", "Archiving previous workspace state...")
        archive_result = self.archiver.archive_before_new_run()
        if archive_result.success:
            yield _ev(
                "info",
                f"Archived {len(archive_result.archived_files)} files to {archive_result.archive_path}",
            )
        else:
            yield _ev("warn", f"Archive warning: {archive_result.message}")

    def _on_successful_output(self, output: str) -> Generator[Event]:
        """Hook for subclasses to do something before context monitor updates."""
        yield from []

    def _refresh_supervisor_snapshot(self) -> Generator[Event]:
        if self.supervisor is not None:
            yield from self._check_and_update_snapshot()

    def _handle_failure(self, output: str) -> Generator[Event]:
        self._failures += 1
        retries_remaining = max(0, self.config.max_retries - self._failures)

        yield _ev(
            "warn",
            f"{self._engine_name} returned empty/timeout (failure {self._failures}/{self.config.max_retries}, "
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
        yield from self.runner.start(self._restart_prompt())
        yield from self._drain_access_violations()

    def _handle_loop(self, reason: str) -> Generator[Event]:
        """Agent got stuck looping mid-turn: it was killed; restart it clean.

        Resume from summary.md + the task-state journal in a fresh session, with
        a loop-breaker note telling the agent not to repeat the offending action.
        Bail out to a failure report if the loop survives repeated restarts.
        """
        self._loop_restarts += 1
        yield _ev(
            "warn",
            f"Loop detected — agent {reason}. Killed it; restarting with fixed "
            f"context (loop-restart {self._loop_restarts}/{self._max_loop_restarts}).",
        )
        if self._loop_restarts > self._max_loop_restarts:
            self._abort_reason = f"stuck in a tool loop: {reason}"
            yield _ev(
                "error",
                f"Loop persists after {self._max_loop_restarts} restart(s) — aborting run.",
            )
            yield from self._on_final_failure(f"Stuck in a loop: {reason}")
            self._state = LoopState.ENDED_FAILURE
            return
        self._loop_breaker = reason
        yield from self._restart_from_summary_output("")

    def _on_final_failure(self, output: str) -> Generator[Event]:
        failure_reason = (
            self._abort_reason or self._last_feedback or "Reached max retries"
        )
        update_experience(self.config.workspace, failed=[failure_reason])
        self._record_structured_failure(
            failure_reason,
            goal=self._memory_goal(),
            challenges=[failure_reason],
        )
        yield from []

    def _unstructured_streak(self, verdict) -> int:
        """Consecutive turns reporting the identical unmet set (legacy rule).

        Only used when the judge emits no per-criterion verdict, so there is
        no attainment to measure. The old implementation also required "no
        workspace change", which made the trigger unreachable for any agent
        that touched a file each turn - that condition is gone.
        """
        items = [
            str(getattr(c, "criterion", c)).strip()[:160]
            for c in (getattr(verdict, "unmet_criteria", None) or [])
            if str(getattr(c, "criterion", c)).strip()
        ]
        signature = "\n".join(sorted(items))
        if signature and signature == self._unmet_streak_sig:
            self._unmet_streak += 1
        else:
            self._unmet_streak = 1 if signature else 0
        self._unmet_streak_sig = signature
        return self._unmet_streak

    def _agent_closure_reason(self) -> str:
        """Non-empty when the agent itself declared the run terminally closed.

        Studies that hit a certified ceiling write their own closure into the
        audit trail (``.opencode/goal_audit.jsonl``: ``goal_terminal``,
        ``terminal_gap``, ``*_closed_*``). The harness used to ignore those
        lines, overwrite ``target_state.json`` back to ``running`` on every
        relaunch, and keep buying turns from an agent that had already
        declared the work finished - 271 judged turns in one case.

        An explicit terminal declaration is taken at face value regardless of
        the turn floor: it is the strongest evidence available that no further
        turn can change the outcome, and a long turn can contain many design
        iterations (the floor would otherwise force extra work after closure).
        """
        from supervisor.core.target_state import read_terminal_assertion

        assertion = read_terminal_assertion(
            self.config.workspace,
            since_ts=self._run_started_at,
            baseline=self._closure_baseline,
            offset=self._audit_baseline_offset,
        )
        if not assertion:
            return ""
        return (
            "the agent declared this run terminally closed "
            f"({assertion}); further turns cannot change the outcome"
        )

    def _feasibility_block_reason(self) -> str:
        """Deterministic 'target unreachable' reason from recorded facts, '' if reachable."""
        from pathlib import Path

        from supervisor.protocols.feasibility_gate import (
            check_feasibility,
            default_facts_path,
            load_feasibility_facts,
        )

        cfg_path = str(getattr(self.config, "feasibility_facts_path", "") or "")
        path = Path(cfg_path) if cfg_path else default_facts_path(self.config.workspace)
        facts = load_feasibility_facts(path)
        if not facts:
            return ""
        assessment = check_feasibility(facts, facts_source=str(path))
        if assessment.reachable:
            return ""
        return (
            f"recorded feasibility facts say the done condition is unreachable "
            f"(known ceiling {assessment.known_ceiling} of {assessment.required_gates} "
            f"gates; blocked gates: {', '.join(assessment.blocked_gates) or 'n/a'}): "
            f"{assessment.reason}"
        )

    def _passing_gate_count(self) -> int | None:
        """PASS count from the last gate harvest; None when there is none."""
        import json

        from supervisor.protocols.feasibility_gate import default_gate_results_path

        path = default_gate_results_path(self.config.workspace)
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            return None
        return sum(
            1
            for row in data["results"]
            if isinstance(row, dict)
            and str(row.get("conclusion", "")).upper() == "PASS"
        )

    def _arbitrate_close(
        self,
        verdict,
        passing_gates: int | None,
        output: str,
    ) -> tuple[str, bool]:
        """Ask the structured decision service about a textual closure candidate.

        Returns ``(block_reason, suppress_text_triggers)``. ``block_reason`` is
        non-empty only for a confident close. On service failure the caller
        falls back to the deterministic text heuristics.
        """
        client = self._jev
        if client is None:
            return "", False
        from supervisor.core.decision_service import build_close_evidence
        from supervisor.core.target_state import load_target_state

        state = load_target_state(self.config.workspace)
        protocol = getattr(self, "protocol", None)
        evidence = build_close_evidence(
            mandate=getattr(protocol, "target_section", ""),
            status=state.status if state else "unknown",
            iteration=self._task_turn,
            stagnation=state.stagnation_count if state else 0,
            passing_gates=passing_gates,
            verdict=verdict,
            output=output,
        )
        try:
            decision = client.decide_close(evidence)
        except Exception:
            logger.warning("jev closing decision crashed", exc_info=True)
            return "", False
        if decision is None:
            return "", False
        self._record_jev_note(f"Jev closing decision: {decision.summary}")
        if decision.verdict == "close":
            return (
                "the structured closing decision is terminal "
                f"({decision.summary}); the target remains unmet"
            ), False
        if decision.verdict == "grace":
            if self._jev_closeout_pending:
                return (
                    "the structured closing decision is terminal and the "
                    f"closeout grace turn is spent ({decision.summary})"
                ), False
            self._jev_closeout_pending = True
            return "", True
        self._jev_closeout_pending = False
        if decision.verdict == "escalate":
            self._record_jev_note(
                "Jev closing decision requested escalation; keeping the run alive",
            )
        return "", True

    def _arbitrate_protocol(self, alignment, output: str) -> tuple[bool, str]:
        """Return ``(suppress_warning, note)`` for a deterministic protocol flag.

        The regex scanner is intentionally broad; jev is the final yes/no on
        the flagged cases. On outage the warning stands (fail-safe).
        """
        if self._jev is None:
            return False, ""
        from supervisor.core.decision_service import CHOICE_MIN_CONFIDENCE

        protocol = getattr(self, "protocol", None)
        try:
            decision = self._jev.decide_protocol_violation(
                violations=[
                    getattr(v, "description", str(v))
                    for v in (getattr(alignment, "violations", None) or [])
                ],
                output=output,
                restrictions=getattr(protocol, "restrictions_section", ""),
            )
        except Exception:
            logger.warning("jev protocol review crashed", exc_info=True)
            return False, ""
        if decision is None:
            return False, ""
        note = f"Jev protocol review: {decision.summary}"
        suppress = (
            decision.action == "no_violation"
            and decision.probability >= CHOICE_MIN_CONFIDENCE
        )
        return suppress, note

    def _record_jev_note(self, note: str) -> None:
        if note:
            self._jev_notes.append(note)
            self._jev_notes = self._jev_notes[-8:]

    def _drain_jev_notes(self) -> Generator[Event]:
        notes, self._jev_notes = self._jev_notes[:], []
        for note in notes:
            yield _ev("info", note)

    def _apply_jev_contradictions(self, verdict) -> None:
        """Second-pass check: does a MET criterion's evidence contradict it?

        The deterministic regex in ``flag_contradicted_criteria`` has already
        downgraded obvious contradictions. This arbitrates the remaining
        ambiguous MET evidence; a confident contradiction downgrades the
        criterion and clears ``all_targets_met``.
        """
        if self._jev is None:
            return
        from supervisor.core.llm_support.models import CriterionResult

        criteria = getattr(verdict, "criteria_results", None) or []
        met_indices = [
            index
            for index, item in enumerate(criteria)
            if getattr(item, "met", False)
            and str(getattr(item, "evidence", "") or "").strip()
        ]
        if not met_indices:
            return
        protocol = getattr(self, "protocol", None)
        context = (
            "JUDGE VERDICT CONTRADICTION CHECK\n"
            f"- Mandate: {head_tail_excerpt(getattr(protocol, 'target_section', ''), 700)}\n"
            f"- Judge feedback: {head_tail_excerpt(getattr(verdict, 'feedback', ''), 700)}\n"
            f"- Judge NEXT_ACTION: {head_tail_excerpt(getattr(verdict, 'next_action', ''), 500)}"
        )
        pairs = [
            (
                str(getattr(criteria[index], "criterion", "")),
                str(getattr(criteria[index], "evidence", "")),
            )
            for index in met_indices
        ]
        try:
            flags = self._jev.decide_contradictions(pairs, context=context)
        except Exception:
            logger.warning("jev contradiction check crashed", exc_info=True)
            return
        if not flags:
            return
        downgraded: list[str] = []
        for pair_index, contradicted in enumerate(flags):
            if not contradicted or pair_index >= len(met_indices):
                continue
            index = met_indices[pair_index]
            item = criteria[index]
            criteria[index] = CriterionResult(
                criterion=item.criterion,
                met=False,
                evidence=item.evidence,
            )
            downgraded.append(item.criterion)
        if not downgraded:
            return
        verdict.all_targets_met = False
        notes = list(getattr(verdict, "validation_notes", None) or [])
        notes.extend(
            f"jev: evidence contradicts MET for '{name[:100]}'"
            for name in downgraded
        )
        verdict.validation_notes = notes
        self._record_jev_note(
            "Jev contradiction check downgraded "
            f"{len(downgraded)} MET claim(s): "
            + "; ".join(name[:80] for name in downgraded[:3]),
        )

    def _blocked_assessment(self, verdict, output: str = "") -> str:
        """Non-empty reason when this run must close as BLOCKED instead of looping.

        Five triggers:

        (a) attainment stagnation - N consecutive judged turns that satisfied
        no previously-unmet obligation and won no new gate. Deliberately keyed
        on *attainment*, not on workspace change: an agent that rewrites
        summary.md or deletes __pycache__ every turn produces a diff forever,
        so a change-based streak can never fire on the loop it must catch
        (measured: 0 Blocked exits across 549 judged turns of two studies).
        (b) the judge orders the study closed: it invokes the protocol's
        failure condition, says the study is closed/terminal, or tells the
        agent not to spend more slots. This is a terminal directive, so it is
        honored immediately - a study can finish several design iterations
        inside one long judged turn, and waiting for the turn floor is how a
        correctly-closed run kept re-triggering (run_c1b12a13).
        (c) the agent asserts a terminal closure in its own audit trail
        (goal_terminal / *_closed_*), or a run-scoped ruling appears in a
        closure document (`.opencode/closure.md`, `ESCALATION.md`). Explicit
        declarations are honored immediately, like the judge's.
        (d) the feasibility facts already declare the target unreachable and
        the passing-gate count has stopped improving.
        (e) the judge has told the agent to stand down for N consecutive
        turns while the targets stay unmet — the loop is waiting on an
        external decision, and more turns cannot produce one.

        Only the heuristic triggers (a), (d), (e) are gated by the turn floor;
        an explicit closure declaration (b), (c) is evidence in its own right.
        """
        # Which trigger fired — the agent's own closure assertion is the
        # weakest trust source, so _on_blocked cross-checks it before writing
        # the irreversible "closed" status.
        self._blocked_agent_asserted = False
        self._last_attainment_progressed = None
        if verdict.all_targets_met:
            self._progress.reset()
            self._unmet_streak, self._unmet_streak_sig = 0, ""
            self._standdown_streak = 0
            return ""
        passing = self._passing_gate_count()
        gate_gain = False
        if passing is not None:
            if self._gate_high_water is not None and passing > self._gate_high_water:
                gate_gain = True
            self._gate_high_water = max(self._gate_high_water or 0, passing)
        criteria = list(getattr(verdict, "criteria_results", None) or [])
        if criteria:
            signal = self._progress.record(criteria, passing_gates=passing)
            streak = signal.streak
            self._last_attainment_progressed = signal.progressed
            gate_gain = gate_gain or signal.progressed
        else:
            # Unstructured judge: no per-criterion MET/UNMET to track, so fall
            # back to the old identical-unmet-set rule. Attainment is
            # unmeasurable here, and treating that as stagnation would close
            # healthy runs whose judge simply omits the criteria block.
            streak = self._unstructured_streak(verdict)
        if gate_gain:
            # Real progress, even if the judge still sounds like it is
            # standing down; do not let a stall streak build over it.
            self._standdown_streak = 0
        elif _is_standdown_action(getattr(verdict, "next_action", "") or ""):
            self._standdown_streak += 1
        else:
            self._standdown_streak = 0
        # Attainment above is recorded even when the BLOCKED exit is disabled:
        # the goal guard's durable state still needs the measurement.
        if not getattr(self.config, "enable_blocked_exit", True):
            return ""
        # Agent-declared closures are explicit terminal evidence: honored
        # before the floor and never delegated to the decision service.
        closure = self._agent_closure_reason()
        if closure:
            self._blocked_agent_asserted = True
            return closure
        directive = str(getattr(verdict, "next_action", "") or "")
        text_closure = _is_closure_directive(directive)
        text_standdown = _is_standdown_action(directive)
        suppress_text = False
        if text_closure or text_standdown:
            jev_reason, suppress_text = self._arbitrate_close(
                verdict, passing, output,
            )
            if jev_reason:
                return jev_reason
        # Explicit closures are terminal evidence, not heuristics: honor them
        # before the turn floor. One judged turn can contain several design
        # iterations and dozens of submissions.
        if text_closure and not suppress_text:
            return (
                "the judge directed the run to close the study while the "
                f"target remains unmet: {head_tail_excerpt(directive, 200)}"
            )
        floor = int(getattr(self.config, "min_turns_before_blocked", 12))
        if self._task_turn < floor:
            return ""
        needed = max(2, int(getattr(self.config, "blocked_after_stagnant_verdicts", 6)))
        if streak >= needed:
            unmet = "; ".join(
                str(getattr(c, "criterion", c)).strip()[:160]
                for c in (getattr(verdict, "unmet_criteria", None) or [])[:4]
            )
            return (
                f"{streak} consecutive judged turns attained nothing new "
                f"(no obligation went UNMET -> MET and no gate newly passed; "
                f"{self._progress.satisfied} obligation(s) satisfied so far): "
                f"{unmet}"
            )
        if self._standdown_streak >= needed and not suppress_text:
            directive = head_tail_excerpt(
                str(getattr(verdict, "next_action", "") or ""), 160,
            )
            return (
                f"{self._standdown_streak} consecutive judged turns told the "
                f"agent to stand down while targets remain UNMET (last "
                f"directive: {directive}); the loop cannot advance without an "
                "operator decision"
            )
        reason = self._feasibility_block_reason()
        if not reason:
            self._unreachable_streak = 0
            return ""
        if (
            passing is None
            or self._last_passing_gates is None
            or passing > self._last_passing_gates
        ):
            # No evidence base yet, or gates are still being won: stay patient.
            self._unreachable_streak = 0
        else:
            self._unreachable_streak += 1
        self._last_passing_gates = passing
        needed = max(1, int(getattr(self.config, "blocked_after_unreachable_turns", 4)))
        if self._unreachable_streak >= needed:
            return (
                f"{reason}; no passing-gate improvement across "
                f"{self._unreachable_streak} judged turns."
            )
        return ""

    def _reset_blocked_accounting(self) -> None:
        """Fresh attainment baseline after an approved unblock plan.

        Clearing the streaks buys the executor a full stagnation window before
        the next blocked verdict, instead of re-closing on the counters that
        had already matured. The rescue itself is consumed, so a later blocked
        verdict still closes the run.
        """
        self._progress.reset()
        self._unmet_streak, self._unmet_streak_sig = 0, ""
        self._standdown_streak = 0
        self._unreachable_streak = 0
        self._last_passing_gates = None
        self._gate_high_water = None
        self._jev_closeout_pending = False
        self._blocked_agent_asserted = False

    def _attempt_blocked_rescue(
        self,
        verdict,
        reason: str,
        output: str,
    ) -> Generator[Event, None, bool]:
        """One web-researched recovery plan before the first BLOCKED exit.

        Returns True only when jev confidently rules the plan feasible and the
        plan has been sent to the executor. Every other outcome - feature off,
        no arbiter, budget spent, judge outage, no usable plan, jev rejection -
        returns False so the caller closes the run exactly as before.
        """
        if not getattr(self.config, "enable_blocked_rescue", True):
            return False
        if self._jev is None or self.supervisor is None:
            return False
        limit = max(0, int(getattr(self.config, "max_blocked_rescues", 1) or 0))
        if self._blocked_rescues_used >= limit:
            return False
        self._blocked_rescues_used += 1

        from supervisor.core import unblock_rescue
        from supervisor.core.decision_service import CHOICE_MIN_CONFIDENCE

        protocol = getattr(self, "protocol", None)
        target = str(getattr(protocol, "target_section", "") or "")
        restrictions = str(getattr(protocol, "restrictions_section", "") or "")
        # The executor's last output alone hides what it claimed earlier and
        # what the judge already told it. The durable summary and the verdict
        # journal are what expose misreports, weakened criteria, and
        # premature give-up.
        summary = safe_read_text(
            self.config.workspace / "summary.md", default="",
        )
        journal = self._restart_task_state()
        report = unblock_rescue.build_executor_record(
            output=output, summary=summary, journal=journal,
        )
        yield _ev(
            "warn",
            "Blocked exit held: forcing a supervisor recovery plan before "
            f"closing ({self._blocked_rescues_used}/{limit}).",
        )

        query_prompt = unblock_rescue.build_query_prompt(
            target=target,
            restrictions=restrictions,
            reason=reason,
            verdict=verdict,
            report=report,
        )
        try:
            raw_queries = self.supervisor._chat(
                query_prompt, is_intermediate=True, omit_history=True,
            ).raw
        except Exception:
            logger.warning("Unblock query planning crashed", exc_info=True)
            raw_queries = ""
        queries = unblock_rescue.parse_search_queries(raw_queries)
        if not queries:
            queries = unblock_rescue.fallback_queries(target, reason)
        if queries:
            yield _ev(
                "info",
                "Unblock research: searching the web for "
                + "; ".join(queries[: unblock_rescue.MAX_QUERIES]),
            )
        web_evidence, executed = unblock_rescue.gather_web_evidence(queries)
        yield _ev(
            "info",
            f"Unblock research: {len(executed)} web queries completed "
            f"({len(web_evidence)} chars of evidence).",
        )

        experience = ""
        try:
            experience = str(self.supervisor._build_experience_context() or "")
        except Exception:
            logger.warning("Unblock experience context failed", exc_info=True)
        plan_prompt = unblock_rescue.build_plan_prompt(
            target=target,
            restrictions=restrictions,
            reason=reason,
            verdict=verdict,
            report=report,
            experience=experience,
            web_evidence=web_evidence,
        )
        try:
            plan = str(
                self.supervisor._chat(
                    plan_prompt, is_intermediate=True, omit_history=True,
                ).raw
                or ""
            ).strip()
        except Exception:
            logger.warning("Unblock plan generation crashed", exc_info=True)
            plan = ""
        if not unblock_rescue.plan_is_usable(plan):
            yield _ev(
                "warn",
                "Supervisor produced no usable unblock plan; closing as BLOCKED.",
            )
            return False

        plan_path = ".opencode/unblock_plan.md"
        try:
            (self.config.workspace / ".opencode").mkdir(
                parents=True, exist_ok=True,
            )
        except OSError:
            logger.warning("Could not prepare .opencode for the unblock plan")
        self._write(
            "# Supervisor unblock plan\n\n"
            f"Blocked reason: {reason}\n\n"
            f"{plan}\n",
            plan_path,
        )
        yield _ev(
            "info",
            f"Unblock plan drafted (saved to {plan_path}):\n"
            + head_tail_excerpt(plan, 1200),
        )

        evidence = unblock_rescue.build_plan_evidence(
            reason=reason,
            target=target,
            verdict=verdict,
            queries=executed,
            web_evidence=web_evidence,
            report=report,
            plan=plan,
        )
        try:
            decision = self._jev.decide_unblock_plan(
                plan=plan, evidence=evidence,
            )
        except Exception:
            logger.warning("Jev unblock review crashed", exc_info=True)
            decision = None
        if decision is None:
            yield _ev(
                "warn",
                "Jev unblock review unavailable; closing as BLOCKED (fail-safe).",
            )
            return False
        note = f"Jev unblock review: {decision.summary}"
        self._record_jev_note(note)
        yield from self._drain_jev_notes()
        feasible = (
            decision.action == "feasible"
            and decision.probability >= CHOICE_MIN_CONFIDENCE
        )
        if not feasible:
            yield _ev(
                "warn",
                f"Unblock plan rejected ({decision.summary}); closing as BLOCKED.",
            )
            return False

        self._reset_blocked_accounting()
        prompt = unblock_rescue.build_executor_prompt(
            reason=reason,
            plan=plan,
            plan_path=plan_path,
        )
        if not self.runner.is_session_pinned():
            # Same reason as the normal feedback path: a session-less turn has
            # no conversation memory, so the plan alone would be an
            # unanswerable fragment.
            _, protocol_text = self._get_restart_context()
            journal_tail = self._restart_task_state()
            context_block = (
                "TASK CONTEXT (fresh session — no prior conversation memory):\n"
                f"PROTOCOL:\n{protocol_text}\n\n"
            )
            if journal_tail:
                context_block += f"RECENT PROGRESS JOURNAL:\n{journal_tail}\n\n"
            prompt = context_block + prompt
        safe_prompt = yield from self._sanitize_feedback(prompt)
        self._last_feedback = safe_prompt
        yield _ev(
            "warn",
            "Blocked exit cancelled: jev approved the supervisor's recovery "
            "plan; sending it to the executor.",
        )
        yield _ev("opencode_prompt", safe_prompt, prompt_kind="unblock")
        yield from self.runner.send(safe_prompt)
        yield from self._drain_access_violations()
        return True

    def _on_blocked(self, verdict, reason: str) -> Generator[Event]:
        """Write the blocked report + memory record; the operator decides next."""
        from supervisor.core.target_state import mark_target_closed

        agent_asserted = bool(getattr(self, "_blocked_agent_asserted", False))
        state_note = self._closure_state_note(agent_asserted)
        report = self.supervisor.report_final_status(
            reason=f"Target unreachable, run closed as BLOCKED: {reason}",
            opencode_output="",
        )
        self._write(report + state_note.markdown, "blocked_report.md")
        # Durable terminal status: without this the state file still read
        # "running" after the run closed, so a relaunch resumed it as if
        # nothing had happened. An agent-asserted closure is NOT taken at
        # face value for this irreversible write: when recorded feasibility
        # facts hold measured evidence that the target is reachable, the
        # closure is the supervised agent's word against the evidence base —
        # record it as "blocked", not "closed" (this run still ends BLOCKED, so no more
        # turns are bought; a relaunch is the operator's explicit choice).
        mark_target_closed(
            self.config.workspace,
            reason,
            status="closed" if state_note.was_closed else state_note.status,
        )
        update_experience(self.config.workspace, failed=[f"blocked: {reason}"])
        self._record_structured_failure(
            reason,
            goal=self._memory_goal(),
            challenges=[reason],
        )
        yield _ev(
            "warn",
            f"Blocked exit: {reason}\n\nWrote blocked_report.md. The run is closing "
            "as BLOCKED so the operator, not the loop, chooses the next move."
            + state_note.event_suffix,
        )

    def _closure_state_note(self, agent_asserted: bool) -> _ClosureNote:
        """Decide whether BLOCKED also writes the irreversible closed status.

        Returns a small record: the markdown block appended to
        ``blocked_report.md`` (always present — reopening must be a
        documented procedure, not archaeology), whether ``closed`` was
        written, and a suffix for the terminal event.
        """
        from supervisor.protocols.feasibility_gate import (
            check_feasibility,
            default_facts_path,
            load_feasibility_facts,
        )

        reopen_block = (
            "\n\n--- HOW TO REOPEN ---\n"
            "This run ended BLOCKED. The target's durable state lives in "
            "`.opencode/target_state.json` in the workspace.\n"
        )
        facts = load_feasibility_facts(default_facts_path(self.config.workspace))
        contradicted = False
        if agent_asserted and facts:
            assessment = check_feasibility(facts, facts_source="closure cross-check")
            contradicted = bool(
                assessment.reachable
                and _has_positive_reachability_evidence(assessment)
            )

        if agent_asserted and contradicted:
            return _ClosureNote(
                was_closed=False,
                markdown=(
                    reopen_block
                    + "The closure was the AGENT'S OWN assertion, and the recorded "
                    "feasibility facts still say the target is REACHABLE — so the "
                    "state was written as `blocked` (reopenable), not `closed`. "
                    "This run bought no more turns; a relaunch will resume the "
                    "target, and that is your explicit decision.\n"
                ),
                event_suffix=(
                    " Target state recorded as blocked: agent-asserted closure "
                    "contradicts recorded feasibility facts."
                ),
                status="blocked",
            )
        if agent_asserted:
            return _ClosureNote(
                was_closed=True,
                markdown=(
                    reopen_block
                    + "The closure was the AGENT'S OWN assertion; no recorded "
                    "feasibility facts contradict it, so the target was marked "
                    "`closed`.\n"
                    "To pursue this goal again: edit `.opencode/target_state.json` "
                    "and change `\"status\": \"closed\"` to `\"running\"`, or start "
                    "a fresh cycle with a revised protocol.md.\n"
                ),
                event_suffix="",
            )
        return _ClosureNote(
            was_closed=True,
            markdown=(
                reopen_block
                + "The target was marked `closed` on the harness's own evidence "
                "(stagnation or recorded unreachable facts).\n"
                "To pursue this goal again: edit `.opencode/target_state.json` and "
                "change `\"status\": \"closed\"` to `\"running\"`, or start a fresh "
                "cycle with a revised protocol.md.\n"
            ),
            event_suffix="",
        )

    def _memory_goal(self) -> str:
        """Stable task description stored with a durable memory decision."""
        protocol = getattr(self, "protocol", None)
        return str(getattr(protocol, "target_section", ""))

    def _record_task_state(self, verdict, progress) -> None:
        """Append a journal entry for this judge turn (no extra LLM call)."""
        from supervisor.core.task_state import append_task_state

        append_task_state(
            self.config.workspace,
            turn=self._task_turn,
            phase=progress.phase.name.lower(),
            step=progress.current_step,
            total_steps=progress.total_steps_estimate,
            targets_met=verdict.all_targets_met,
            feedback=verdict.feedback,
            next_action=getattr(verdict, "next_action", "") or "",
        )

    def _restart_task_state(self) -> str:
        """Tail of the progress journal, for injection into a restart prompt."""
        from supervisor.core.task_state import read_task_state_tail

        return read_task_state_tail(self.config.workspace)

    def _get_step_context(self, progress) -> StepContext:
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

    def _setup_skill_bank(self) -> None:
        """Wire the supervisor skill bank (catalog provider + load state).

        Called by loop subclasses right after the supervisor is created. The
        bank renders a catalog into every judge prompt via the context
        provider registry; bodies enter the context only when the judge asks
        for them with LOAD_SKILL (resolved in _resolve_skill_requests).
        """
        if not getattr(self.config, "enable_supervisor_skills", True):
            return
        from supervisor.core.skill_bank import SkillBank, builtin_bank_dir

        extra_dir = getattr(self.config, "supervisor_skills_dir", "") or None
        bank = SkillBank(
            builtin_dir=builtin_bank_dir(),
            workspace_dir=self.config.workspace / ".opencode" / "supervisor_skills",
            extra_dir=extra_dir,
        )
        if not bank.has_skills():
            logger.info("Supervisor skill bank empty — feature inert.")
            return
        self._skill_bank = bank
        self.supervisor.register_context_provider("skills", bank.context_block)
        logger.info(
            "Supervisor skill bank ready: %s", ", ".join(bank.names()),
        )

    def _fulfill_pending_skills(self) -> None:
        """Load skills queued by a previous turn's re-judge (no load chains)."""
        if self._skill_bank is None:
            return
        pending = self._skill_bank.drain_pending()
        if pending:
            self._load_and_record(pending)

    def _resolve_skill_requests(self, verdict: SupervisorVerdict) -> list[str]:
        """Load skills the judge asked for; return the names newly loaded."""
        if self._skill_bank is None or not verdict.skill_requests:
            return []
        return self._load_and_record(verdict.skill_requests)

    def _load_and_record(self, requested: list[str]) -> list[str]:
        """Load skills and record each action as a loop event + app log."""
        result = self._skill_bank.load(requested)
        if result.loaded:
            names = ", ".join(skill.name for skill in result.loaded)
            logger.info("Supervisor loaded skill(s): %s", names)
            self._skill_events.append(
                ("info", f"Supervisor loaded skill(s): {names}"),
            )
        for note in result.notes:
            logger.info("skill bank: %s", note)
            if "not in bank" in note:
                self._skill_events.append(
                    ("warn", f"Skill load rejected: {note}"),
                )
            elif "deferred" in note:
                self._skill_events.append(
                    ("info", f"Skill load deferred: {note}"),
                )
        return [skill.name for skill in result.loaded]

    def _drain_skill_events(self) -> Generator[Event]:
        """Yield queued skill-load events into the stream, then clear."""
        events, self._skill_events = self._skill_events[:], []
        for level, msg in events:
            yield _ev(level, msg)

    def _queue_deferred_skill_requests(self, verdict: SupervisorVerdict) -> None:
        """Hold LOAD_SKILL requests seen in a re-judge for the next turn."""
        if self._skill_bank is not None and verdict.skill_requests:
            self._skill_bank.queue_pending(verdict.skill_requests)

    def _post_judge_feedback(
        self,
        safe_msg: str,
        output: str,
    ) -> Generator[Event, None, str]:
        """Hook to append alignment warnings etc."""
        return safe_msg

    def _verify_success(
        self,
        verdict: SupervisorVerdict,
        output: str,
    ) -> str | None:
        """Validate a DONE proposal before the run may end (exit validation).

        Returns ``None`` to accept completion, or feedback text explaining why
        the completion claim was blocked (the loop continues with it).
        """
        _ = verdict, output
        return None

    def _turn_guidance(self, verdict: SupervisorVerdict, output: str) -> str:
        """Extra guidance prepended to feedback on a continue turn (nudges)."""
        _ = verdict, output
        return ""

    def _guard_restart_section(self) -> str:
        """Goal-guard context injected into restart prompts."""
        return ""

    def _do_judgement(self, output: str) -> Generator[Event]:
        progress = self.runner.get_step_progress()

        augmented_output, abort = yield from self._pre_judge(output, progress)
        if abort:
            return

        actual_output = augmented_output if augmented_output is not None else output

        verdict = self._get_verdict(actual_output, progress)
        yield _ev("supervisor_response", verdict.raw, digest=verdict.headline())
        validation_notes = getattr(verdict, "validation_notes", None)
        if validation_notes:
            yield _ev(
                "warn",
                "Judge verdict auto-corrected (evidence contradiction): "
                + " ".join(validation_notes),
            )
        # Skill-load actions (loads, rejections, deferrals) recorded during
        # _get_verdict, surfaced here like every other supervisor action.
        yield from self._drain_skill_events()

        self._task_turn += 1
        self._record_task_state(verdict, progress)

        blocked_reason = self._blocked_assessment(verdict, actual_output)
        yield from self._drain_jev_notes()
        if not (getattr(verdict, "criteria_results", None) or []) and not getattr(
            self, "_unstructured_warned", False
        ):
            # P3-2: an unstructured judge verdict silently downgrades the
            # blocked-exit assessment to the legacy identical-unmet-set rule.
            # 8.2.12 requires an explicit no-record signal, not a silent
            # fallback to the loosest branch. Warn once so the operator sees
            # it in the job log.
            self._unstructured_warned = True
            yield _ev(
                "warn",
                "Judge emitted no structured criteria — blocked-exit "
                "assessment falls back to the unstructured streak rule "
                "(no-record-per-rule).",
            )
        if blocked_reason:
            rescued = yield from self._attempt_blocked_rescue(
                verdict, blocked_reason, actual_output,
            )
            if rescued:
                return
            self._blocked_reason = blocked_reason
            self._state = LoopState.ENDED_BLOCKED
            yield from self._on_blocked(verdict, blocked_reason)
            return

        yield from self._emit_token_warnings()

        feedback_override: str | None = None
        if verdict.all_targets_met:
            blocked = self._verify_success(verdict, actual_output)
            if blocked is None:
                self._state = LoopState.ENDED_SUCCESS
                lesson = self._extract_lesson_from_verdict(verdict.raw)
                if self._failures == 0:
                    update_experience(self.config.workspace, worked=[lesson])
                else:
                    lesson = "Successfully met all targets"
                    update_experience(self.config.workspace, worked=[lesson])
                self._record_structured_success(
                    verdict.raw,
                    goal=self._memory_goal(),
                    solutions=[lesson],
                )
                yield from self._drain_jev_notes()
                return
            feedback_override = blocked
            yield _ev(
                "warn",
                "Completion blocked pending evidence — run continues.",
            )

        if feedback_override is not None:
            feedback_text = feedback_override
        else:
            feedback_text = verdict.feedback
            guidance = self._turn_guidance(verdict, actual_output)
            if guidance:
                feedback_text = guidance + "\n\n" + feedback_text
        yield from self._drain_jev_notes()

        vuln_scan = self.scan_for_vulnerabilities()
        feedback_text = feedback_text + (vuln_scan or "")
        safe_msg = yield from self._sanitize_feedback(feedback_text)

        safe_msg = yield from self._post_judge_feedback(safe_msg, actual_output)

        if not self.runner.is_session_pinned():
            # A session-less turn has no conversation memory — the feedback
            # alone would be an unanswerable fragment. Make it self-contained.
            _, protocol_text = self._get_restart_context()
            journal_tail = self._restart_task_state()
            context_block = (
                "TASK CONTEXT (fresh session — no prior conversation memory):\n"
                f"PROTOCOL:\n{protocol_text}\n\n"
            )
            if journal_tail:
                context_block += f"RECENT PROGRESS JOURNAL:\n{journal_tail}\n\n"
            safe_msg = context_block + safe_msg

        self._last_feedback = safe_msg

        if self._cross_turn_detector is not None:
            cross = self._cross_turn_detector.record(actual_output, safe_msg)
            if cross is not None:
                # Don't send the same feedback again — flag for the loop to
                # restart with a loop-breaker instead of oscillating further.
                self._pending_cross_loop = f"{cross.reason} ({cross.marker})"
                yield _ev("warn", f"Cross-turn loop: agent {cross.reason}.")
                return

        yield _ev("opencode_prompt", safe_msg, prompt_kind="turn")
        yield from self.runner.send(safe_msg)
        yield from self._drain_access_violations()

        yield from self._yield_suggestions(
            actual_output,
            self._get_step_context(progress),
        )

    def _run_loop(
        self,
        initial_output: str,
        initial_timed_out: bool,
    ) -> Generator[Event]:
        output = initial_output
        timed_out = initial_timed_out

        while self._state == LoopState.RUNNING:
            looped, loop_reason = self.runner.last_looped
            if looped:
                yield from self._handle_loop(loop_reason)
                if self._state != LoopState.RUNNING:
                    break
                output, timed_out = self.runner.read_output()
                continue

            current_progress = self.runner.get_step_progress()

            if timed_out or not output.strip():
                if self._should_extend_timeout(current_progress):
                    yield from self._handle_active_progress_timeout(current_progress)
                    self._timeout_extension_count += 1
                    output, timed_out = self.runner.read_output()
                    continue

                diag = self.runner.last_diagnostic()
                yield _ev("warn", f"{self._engine_name} returned no output. Diagnostic:\n{diag}")
                yield from self._handle_failure(output)
                if self._state != LoopState.RUNNING:
                    break
                output, timed_out = self.runner.read_output()
                continue

            self._failures = 0
            self._loop_restarts = 0  # genuine progress clears the loop counter
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
                yield from self._maybe_compact()
                output, timed_out = self.runner.read_output()
                continue

            yield from self._do_judgement(output)
            if self._state != LoopState.RUNNING:
                break

            if self._pending_cross_loop:
                reason = self._pending_cross_loop
                self._pending_cross_loop = ""
                yield from self._handle_loop(reason)
                if self._state != LoopState.RUNNING:
                    break
                output, timed_out = self.runner.read_output()
                continue

            yield from self._handle_session_continuity()

            output, timed_out = self.runner.read_output()

    def _handle_session_continuity(self) -> Generator[Event]:
        """Decide whether to continue the current opencode session or restart.

        When context is below the continuation threshold, enable --continue
        to maintain session continuity. When context exceeds the threshold,
        write summary.md and start a fresh session.
        """
        if self.ctx_monitor.can_continue_session:
            if self.runner.is_continuation_enabled() or self.runner._session_active:
                self.runner.enable_continuation(True)
                yield _ev("info", f"Continuing {self._engine_name} session.")
            else:
                self.runner.mark_session_active()
        else:
            yield _ev(
                "info",
                "Context limit approaching — writing summary.md and restarting session.",
            )
            yield from self._restart_from_summary_output("")

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

    def _handle_active_progress_timeout(self, progress) -> Generator[Event]:
        ext_count = self._timeout_extension_count + 1
        activity_state = self.runner.get_activity_state()
        wait_msg = (
            " (may be waiting for output)"
            if activity_state == "waiting_for_output"
            else ""
        )
        yield _ev(
            "info",
            f"{self._engine_name} is actively working (step {progress.current_step}, "
            f"phase: {progress.phase.name.lower()}, state: {activity_state}{wait_msg}). "
            f"Timeout extension {ext_count}/{self._max_timeout_extensions} — continuing...",
        )

    def _emit_heartbeat(self, progress) -> Generator[Event]:
        heartbeat_data = {
            "current_step": progress.current_step,
            "total_steps_estimate": progress.total_steps_estimate,
            "percentage": progress.percentage,
            "phase": progress.phase.name.lower(),
        }
        yield _ev(
            "heartbeat",
            f"{self._engine_name} active: step {progress.current_step}/{progress.total_steps_estimate} "
            f"({progress.phase.name.lower()}) — {progress.percentage:.0f}% complete",
            **heartbeat_data,
        )

    def _emit_step_events(self, output: str) -> Generator[Event]:
        for event in self.runner.get_step_events(output):
            lvl = event.get("level", "info")
            if lvl == "step":
                yield _ev(
                    "step",
                    f"{event.get('phase_label', 'Step')} - "
                    + head_tail_excerpt(
                        event.get("msg", ""), 100, end_ratio=0.5,
                    ),
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

    def _maybe_compact(self) -> Generator[Event]:
        """Two-stage compaction.

        Both stages first roll up the supervisor's own history (structural, no
        LLM). Then:
          * Stage 1 (elevated, < critical): light compaction — write summary.md
            and restart the agent in a fresh session, skipping the expensive
            agent cleanup/deletion turns.
          * Stage 2 (critical, >= 90%): full compaction — agent file-cleanup
            inquiry + deletion + summary + restart.
        """
        rolled = self.supervisor.rollup_history()
        if rolled:
            yield _ev("info", f"Condensed {rolled} older supervisor turn(s) to save context.")

        if self.ctx_monitor.is_critical:
            self.supervisor.compact_history()
            yield from self._do_compaction()
        else:
            yield from self._do_light_compaction()

    def _do_light_compaction(self) -> Generator[Event]:
        """Stage-1 compaction: summary + fresh restart, no agent cleanup turns."""
        yield _ev(
            "warn",
            f"Context at {self.ctx_monitor.fraction * 100:.0f}% — stage 1: "
            "writing summary.md and restarting session (light).",
        )
        yield from self._restart_from_summary_output("")

    def _do_compaction(self) -> Generator[Event]:
        yield _ev(
            "warn",
            f"Context at {self.ctx_monitor.fraction * 100:.0f}% — stage 2: full compaction.",
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
        yield _ev(
            "supervisor_response", deletion_permission.raw,
            digest=deletion_permission.headline(),
        )
        # always_inject: this prompt invites the agent to delete files, so the
        # protected-path reminder must be present even when the message itself
        # matches no protected pattern (a compaction prompt that omitted the
        # reminder once let the agent rationalize deleting the whole .archive/).
        msg, _, protected_hits = self.guard.sanitize_with_protection(
            strip_thinking_blocks(deletion_permission.feedback),
            always_inject=True,
        )
        if protected_hits:
            yield _ev(
                "warn",
                f"Protected-path references in compaction instructions: {protected_hits}",
            )
        yield _ev("opencode_prompt", msg, prompt_kind="compaction")
        yield from self.runner.send(msg)
        yield from self._drain_access_violations()
        compaction_output, _timed_out = self.runner.read_output()
        if compaction_output.strip():
            yield _ev("opencode_output", compaction_output)
        yield _ev(
            "info",
            "Compaction complete - writing summary.md and restarting session.",
        )
        yield from self._restart_from_summary_output(compaction_output)

    def _restart_from_summary_output(self, last_output: str) -> Generator[Event]:
        """Write summary.md, reset state, and start a fresh opencode session."""
        yield from self._forced_summary(last_output)
        yield from self._archive_before_new_run()
        self.runner.stop()
        self.runner.reset_session()
        self.runner.reset_context_counter()
        self.ctx_monitor.reset()
        restart_prompt = self._get_restart_prompt_for_continuation()
        yield _ev("opencode_prompt", restart_prompt, prompt_kind="restart")
        yield from self.runner.start(restart_prompt)
        yield from self._drain_access_violations()
        self.runner.mark_session_active()

    def _update_context_monitor(self) -> Generator[Event]:
        files_read = self.runner.get_files_read()
        self.ctx_monitor.update(
            self.runner.estimated_context_tokens,
            files_read=files_read,
        )
        # Structured token event so the UI token bar reads the real number
        # directly instead of regex-scraping a warning string. Hidden from the
        # log box by default (see log_ui._DEFAULT_HIDDEN_LEVELS).
        current_tokens = self.runner.estimated_context_tokens
        yield _ev(
            "tokens",
            f"Context: {current_tokens:,} tokens ({self.ctx_monitor.fraction * 100:.0f}%)",
            current=current_tokens,
            fraction=self.ctx_monitor.fraction,
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

    def _emit_token_warnings(self) -> Generator[Event]:
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

    def _forced_summary(self, last_output: str) -> Generator[Event]:
        yield _ev("info", "Writing summary.md…")
        report = self.supervisor.report_final_status(
            reason="forced summarization",
            opencode_output=last_output,
        )
        self._write(report, "summary.md")
        yield _ev("info", "summary.md written.")

    def _sanitize_feedback(self, feedback: str) -> Generator[Event, None, str]:
        from supervisor.core.skill_bank import strip_skill_markers

        # The judge's full reply can carry supervisor-side request markers;
        # they must never reach the runner (see strip_skill_markers).
        feedback = strip_skill_markers(feedback)
        safe_msg, violations, protected_violations = (
            self.guard.sanitize_with_protection(feedback)
        )
        if violations:
            yield _ev("warn", f"Blocked out-of-workspace paths: {violations}")
        if protected_violations:
            yield _ev("warn", f"Protected-path references: {protected_violations}")
        return safe_msg

    def _yield_suggestions(
        self,
        opencode_output: str,
        step_context=None,
    ) -> Generator[Event]:
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
        summary = safe_read_text(summary_path, default="(none)")
        text = safe_read_text(self.config.protocol_path)
        return summary, text

    def _get_restart_prompt_for_continuation(self) -> str:
        """Build a prompt for restarting opencode after context limit is reached.

        Reads the summary.md that was just written and combines it with the
        protocol to give opencode full context in a fresh session.
        """
        from supervisor.prompts import RESTART_PROMPT_TEMPLATE

        summary, protocol_text = self._get_restart_context()
        ws = self.config.workspace.resolve()
        tail = self._restart_task_state()
        task_state_section = (
            f"Recent progress journal (most recent turns):\n\n{tail}\n\n" if tail else ""
        )
        guard_section = self._guard_restart_section()
        loop_section = ""
        if self._loop_breaker:
            loop_section = loop_breaker_section(self._loop_breaker)
            self._loop_breaker = ""
        return RESTART_PROMPT_TEMPLATE.format(
            loop_section=guard_section + loop_section,
            summary=summary,
            task_state_section=task_state_section,
            protocol_text=protocol_text,
            workspace=ws,
        )

    def _codebase_preamble(self) -> str:
        """Structural digest of the workspace for the judge's system prompt.

        Signatures, not bodies. The system message is re-sent on EVERY judge
        call, so its size is a recurring cost, and full file bodies were 91%
        of it (measured: 47,562 of 52,061 chars on one run, re-sent 294
        times = 14.2 MB of duplicate payload for one run). The judge already
        has ``NEED_EVIDENCE: read_file <path>`` for the bodies it actually
        needs, so the always-on block only needs to say what exists.
        """
        return "\n\n## Live codebase\n" + self._cached_snapshot.skimmed_digest_for_prompt(
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

    def _build_evolution_summary(
        self,
        outcome: str,
        goal: str = "",
        key_changes: list[str] | None = None,
        challenges: list[str] | None = None,
        solutions: list[str] | None = None,
        violations: list[str] | None = None,
    ) -> EvolutionSummary:
        baseline = getattr(self, "_baseline", None)
        final = getattr(self, "_last_result", None)
        test_evidence: list[str] = []
        if baseline is not None:
            test_evidence.append("Baseline: " + baseline.summary())
        if final is not None:
            test_evidence.append("Final: " + final.summary())
            if baseline is not None:
                test_evidence.append("Delta: " + final.delta(baseline))

        changed_files: list[str] = []
        before = getattr(self, "_pre_snapshot", None)
        if before is not None:
            try:
                from supervisor.analyzers.codebase_analyzer import snapshot_codebase

                changed_files = before.changed_files(
                    snapshot_codebase(self.config.workspace),
                )
            except OSError:
                logger.warning("Could not capture changed files for memory summary")

        progress = self.runner.get_step_progress() if self.runner else None
        return EvolutionSummary(
            goal=goal,
            outcome=outcome,
            key_changes=key_changes or [],
            changed_files=changed_files,
            test_baseline=baseline.summary() if baseline is not None else "",
            test_final=final.summary() if final is not None else "",
            test_delta=final.delta(baseline) if final is not None and baseline is not None else "",
            test_evidence=test_evidence,
            regressions_count=0,
            iterations=getattr(self, "_iteration", 0),
            final_step=progress.current_step if progress else 0,
            total_steps=progress.total_steps_estimate if progress else 0,
            final_phase=progress.phase.name.lower() if progress else "",
            challenges=challenges or [],
            failure_patterns=(challenges or []) if outcome == "failure" else [],
            solutions=solutions or [],
            violations=violations or [],
            archive_path=str(getattr(self, "_best_archive", "")),
            confidence=1.0 if final is not None and final.ok else 0.7 if outcome == "success" else 0.4,
        )

    def _record_structured_success(
        self,
        verdict_raw: str,
        goal: str = "",
        key_changes: list[str] | None = None,
        challenges: list[str] | None = None,
        solutions: list[str] | None = None,
    ) -> None:
        summary = self._build_evolution_summary(
            outcome="success",
            goal=goal,
            key_changes=key_changes,
            challenges=challenges,
            solutions=solutions,
        )
        log_evolution_summary(self.config.workspace, summary)

    def _record_structured_failure(
        self,
        failure_reason: str,
        goal: str = "",
        challenges: list[str] | None = None,
    ) -> None:
        summary = self._build_evolution_summary(
            outcome="failure",
            goal=goal,
            challenges=challenges or [failure_reason],
        )
        log_evolution_summary(self.config.workspace, summary)

    def _extract_lesson_from_verdict(self, raw: str) -> str:
        from supervisor.utils.text_utils import head_tail_excerpt

        lines = raw.strip().splitlines()
        for line in lines:
            line_lower = line.lower().strip()
            if not line_lower:
                continue
            if line_lower.startswith(("lesson:", "strategy:", "summary:")):
                return line.split(":", 1)[1].strip()
        content = " ".join(raw.strip().split())
        # Lessons conclude at the end of the verdict — keep both ends.
        return head_tail_excerpt(content, 140, end_ratio=0.5)

    def _write(self, text: str, filename: str) -> bool:
        """Write text to a file in the workspace. Returns True if successful."""
        try:
            (self.config.workspace / filename).write_text(text, encoding="utf-8")
            return True
        except (PermissionError, OSError) as e:
            logger.warning(f"Failed to write {filename} to workspace: {e}")
            return False

    def scan_for_vulnerabilities(self) -> str | None:
        """Run vulnerability scan on workspace Python files, return formatted results or None."""
        if _vuln_scan is None:
            logger.debug("Vulnerability scanner not available, skipping scan")
            return None

        from supervisor.workspace.ignore_patterns import IgnoreMatcher

        workspace = self.config.workspace.resolve()
        logger.debug("Starting vulnerability scan on workspace: %s", workspace)

        ignore_matcher = IgnoreMatcher(workspace)
        ignore_matcher.load_from_workspace(workspace)

        has_py = False
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
            if any(
                f.endswith(".py")
                and not ignore_matcher.matches(
                    f"{rel_root}/{f}" if rel_root != "." else f,
                )
                for f in files
            ):
                has_py = True
                break

        if not has_py:
            logger.debug(
                "No Python files found in workspace, skipping vulnerability scan",
            )
            return None

        logger.debug("Python files present; running vulnerability scan")
        try:
            # Autofix is a workspace-mutating pass — run it only on the first
            # scan of a run; later judgements scan without re-applying autofixes.
            autofix_first = not self._vuln_autofixed
            self._vuln_autofixed = True
            findings = _vuln_scan(
                target=str(workspace),
                min_severity="HIGH",
                autofix_first=autofix_first,
                scan_deps=False,
                print_output=False,
            )
            logger.debug("Vulnerability scan returned %d finding(s)", len(findings))
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
            logger.debug("No relevant vulnerability findings after filtering")
            return None

        by_severity = {}
        for f in findings:
            by_severity.setdefault(f.severity, []).append(f)

        lines = [
            "\n\n--- vulnerability scan ---",
            f"Found {len(findings)} issue(s) (HIGH+ severity, dependencies excluded):",
        ]
        for sev in ("CRITICAL", "HIGH"):
            if sev in by_severity:
                lines.append(f"\n{sev} ({len(by_severity[sev])}):")
                for f in by_severity[sev]:
                    lines.append(f"  - {f.file}:{f.line} [{f.tool}] {f.message}")
                    if f.suggestion:
                        lines.append(f"    Fix: {f.suggestion}")

        result = "\n".join(lines)
        logger.debug("Vulnerability scan results formatted: %d issue(s)", len(findings))
        return result
