"""supervisor/runners/base_runner.py

Shared base for ALL execution-agent runners — one class, three engine
subclasses. ``OpencodeRunner``, ``CodexRunner``, and ``PiRunner`` inherit
this full public surface (turn lifecycle, session pinning, step detection,
context accounting, archiving, cleanup inquiry) and implement only their
engine seams:

  * ``_engine``             class attr naming the backend in logs/warnings
  * ``_run_prompt``         one subprocess turn (the engine's process adapter)
  * ``_prepare_workspace``  pre-run workspace setup (default: mkdir only)
  * session enumeration     inert by default; only engines that capture
                            sessions out-of-band (opencode's session-list
                            diffing) override ``start`` and these hooks

The default :meth:`start` fits engines whose event stream carries the
resumable session id (codex ``thread.started``, pi ``session``): the first
turn opens a fresh session, the process layer captures the id, and later
turns resume it.

``OcTestRunner`` (test_runner.py) also subclasses this for workspace +
lifecycle plumbing; it exercises none of the turn surface, so every
constructor argument after ``workspace`` carries a default.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Callable, Generator
from pathlib import Path

from supervisor.analyzers.opencode_step_detector import (
    OpencodeStepDetector,
    PhaseTransition,
    Step,
    StepProgress,
)
from supervisor.runners.command_common import validate_message
from supervisor.runners.opencode_support.inspection import extract_file_refs
from supervisor.runners.opencode_support.result import RunResult
from supervisor.runners.process_utils import kill_process_tree
from supervisor.utils.text_utils import coerce_str, strip_thinking_blocks
from supervisor.workspace.cleanup_candidates import (
    build_cleanup_inquiry,
)
from supervisor.workspace.cleanup_candidates import (
    identify_cleanup_candidates as discover_cleanup_candidates,
)
from supervisor.workspace.workspace_archiver import (
    ArchiveResult,
    WorkspaceArchiver,
    create_archiver,
)

logger = logging.getLogger(__name__)


class BaseRunner:
    """One send()/start() call = one engine subprocess turn."""

    # Backend name used in logs, event text, and validation warnings.
    # ("opencode" | "codex" | "pi"; base default is a generic label.)
    _engine = "agent"

    def __init__(
        self,
        workspace: Path,
        timeout: int = 300,
        opencode_model: str | None = None,
        opencode_executable: str = "",
        opencode_pure: bool = False,
        opencode_variant: str = "",
        agent: str = "",
        opencode_model_backup: str | None = None,
        archive_keep_last: int = 5,
        archive_backend: str = "git",
        step_detector: OpencodeStepDetector | None = None,
        on_step: Callable[[Step], None] | None = None,
        on_transition: Callable[[PhaseTransition], None] | None = None,
        on_progress: Callable[[StepProgress], None] | None = None,
        run_id: str = "",
    ):
        logger.debug(
            "__init__ raw inputs - opencode_model=%r (type=%s) opencode_executable=%r (type=%s) "
            "agent=%r (type=%s) opencode_model_backup=%r (type=%s) timeout=%r (type=%s)",
            opencode_model,
            type(opencode_model).__name__,
            opencode_executable,
            type(opencode_executable).__name__,
            agent,
            type(agent).__name__,
            opencode_model_backup,
            type(opencode_model_backup).__name__,
            timeout,
            type(timeout).__name__,
        )

        self.workspace = workspace
        # Ownership id of the run this runner serves. Empty when the caller is
        # a bare test double; every real run passes its job id so each spawned
        # engine tree is ledgered and contained (see supervisor.runtime).
        self.run_id = str(run_id or "")
        self._alive = False
        # The opencode_* field names are the persisted config vocabulary,
        # reused generically by every engine as model / executable / backup.
        self.opencode_model = coerce_str(opencode_model, "opencode_model") or None
        self.opencode_model_backup = (
            coerce_str(opencode_model_backup, "opencode_model_backup") or None
        )
        self.opencode_executable = coerce_str(
            opencode_executable,
            "opencode_executable",
        )
        self.opencode_pure = bool(opencode_pure)
        # Thinking variant (opencode `--variant` on 1.x / folded into
        # `--model provider/model#variant` on 2.x, pi `--thinking`, codex
        # `-c model_reasoning_effort`). "" = model default. Only values from
        # the engine's own naming should be set here.
        self.opencode_variant = coerce_str(opencode_variant, "opencode_variant")
        self.agent = coerce_str(agent, "agent")
        if not isinstance(timeout, int):
            logger.warning(
                "Type coercion: 'timeout' received %r (type=%s) - casting to int.",
                timeout,
                type(timeout).__name__,
            )
        self.timeout = int(timeout)

        logger.info(
            "__init__ coerced values - opencode_model=%r opencode_model_backup=%r "
            "agent=%r timeout=%d workspace=%s",
            self.opencode_model,
            self.opencode_model_backup,
            self.agent,
            self.timeout,
            self.workspace,
        )

        self._last_result: RunResult | None = None
        self._chars_exchanged: int = 0
        # Operator cancellation. The process layer checks this every 0.5 s
        # while consuming the engine's event stream, so a STOP exits the job
        # thread even when a detached descendant still holds the stdout pipe
        # open (the OS tree-kill alone cannot close such an inherited handle).
        self._cancel_event = threading.Event()
        # Real cumulative context tokens from the engine's event stream
        # (0 until the stream reports them). Preferred over the chars/4
        # estimate when available.
        self._last_tokens_total: int = 0
        self._process: subprocess.Popen | None = None
        self._session_active = False
        self._use_continue = False
        self._session_id: str | None = None
        self._archive_keep_last = int(archive_keep_last)
        self._archive_backend = archive_backend
        self.__archiver: WorkspaceArchiver | None = None

        self._step_detector = step_detector or OpencodeStepDetector(
            step_callback=on_step,
            transition_callback=on_transition,
            progress_callback=on_progress,
        )

    @property
    def _archiver(self) -> WorkspaceArchiver:
        # Created lazily: OcTestRunner shares this base and never archives,
        # so constructing the backend (which may touch the workspace) waits
        # for the first archive call.
        if self.__archiver is None:
            self.__archiver = create_archiver(
                self.workspace,
                keep_last=self._archive_keep_last,
                backend=self._archive_backend,
            )
        return self.__archiver

    @classmethod
    def from_config(cls, config, agent: str = "") -> BaseRunner:
        return cls(
            workspace=config.workspace,
            timeout=config.timeout,
            opencode_model=config.opencode_model,
            opencode_executable=config.opencode_executable,
            opencode_pure=getattr(config, "opencode_pure", False),
            opencode_variant=str(getattr(config, "agent_reasoning", "") or ""),
            agent=agent,
            opencode_model_backup=config.opencode_model_backup,
            archive_keep_last=getattr(config, "archive_keep_last", 5),
            archive_backend=getattr(config, "archive_backend", "git"),
            run_id=str(getattr(config, "run_id", "") or ""),
        )

    # -- lifecycle ----------------------------------------------------- #

    @property
    def is_alive(self) -> bool:
        """Return True if the runner is currently active."""
        return self._alive

    @property
    def cancel_event(self) -> threading.Event:
        """Set by :meth:`stop`; the engine stream exits promptly on it."""
        return self._cancel_event

    def stop(self) -> None:
        self._alive = False
        self._session_active = False
        # Signal FIRST: the stream consumer may be blocked on a pipe held by
        # a detached descendant, and the event is what frees the job thread.
        self._cancel_event.set()
        if self._process is not None:
            # Tree-kill: on Windows the direct child is the cmd.exe shim of a
            # `.cmd` install; killing only it orphans the real CLI process,
            # which then holds the binary lock and keeps hitting the provider.
            # The ownership gate is inside process_control: only this run's
            # own tree can be reached, never another run's or an unrelated one.
            # The run id travels on the process object (stamped at spawn), so
            # this stays a one-argument call.
            kill_process_tree(self._process)

    # -- engine seams (the only parts a backend must implement) --------- #

    def _run_prompt(self, prompt: str) -> Generator[dict]:
        """Run one prompt turn through the engine CLI (engine seam)."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement _run_prompt()"
        )

    def _prepare_workspace(self) -> None:
        """Pre-run workspace setup (engine seam).

        Default: the workspace directory only. Engines needing in-workspace
        state (opencode's project permission marker, pi's session dir)
        override this.
        """
        self.workspace.mkdir(parents=True, exist_ok=True)

    def _list_all_session_ids(self) -> set[str]:
        """Enumerate the engine's current session ids (engine seam).

        Inert by default: engines that report the session id on the event
        stream (codex, pi) never enumerate. opencode overrides this for its
        before/after diff capture.
        """
        return set()

    def _capture_new_session_id(
        self,
        before: set[str],
        attempts: int = 10,
        delay_seconds: float = 0.5,
    ) -> str | None:
        """Find the session id created since *before* (engine seam)."""
        return None

    # -- turn lifecycle ------------------------------------------------- #

    def start(self, initial_prompt: str) -> Generator[dict]:
        """Default first-turn flow for stream-reported session ids.

        Turn one opens a fresh session (no resume flags); the process layer
        captures the session id from the event stream, and later turns pass
        it explicitly. Engines that capture sessions out-of-band (opencode)
        override this with their diffing flow.
        """
        if self._cancel_event.is_set():
            yield {
                "level": "warn",
                "msg": f"{self._engine} start skipped: run cancelled.",
            }
            return
        validated = validate_message(
            initial_prompt, "initial_prompt (start)", engine=self._engine
        )
        if validated is None:
            logger.warning(
                "Empty initial prompt in %s start(). Falling back to continue.",
                self._engine,
            )
            validated = "Continue based on the current context and proceed with the task."

        self._alive = True
        if self._session_active:
            yield from self._run_prompt(validated)
            return

        logger.info("New %s session (no resume).", self._engine)
        yield {"level": "info", "msg": f"New {self._engine} session (no resume)."}
        # The process layer captures the session id from the stream so the
        # next turn can resume precisely.
        self._session_id = None
        self.enable_continuation(False)
        yield from self._run_prompt(validated)
        self._session_active = True
        self.enable_continuation(True)

    def send(self, message: str) -> Generator[dict]:
        if self._cancel_event.is_set():
            yield {
                "level": "warn",
                "msg": f"{self._engine} turn skipped: run cancelled.",
            }
            return
        validated = validate_message(message, "message (send)", engine=self._engine)
        resolved_message = (
            validated
            if validated is not None
            else "Continue based on the current context and proceed."
        )
        # Single-threaded driver: nothing flips _alive back to True mid-call, so
        # a stopped runner can never recover here. Fail fast instead of sleeping
        # through a doomed exponential backoff before the inevitable raise.
        if not self._alive:
            raise RuntimeError(
                f"{type(self).__name__} has been stopped. "
                f"(Operation: send, alive={self._alive}, session_active={self._session_active})",
            )

        if self._session_active:
            self.enable_continuation(True)
        logger.info("send() - message length=%d chars", len(resolved_message))
        yield from self._run_prompt(resolved_message)

    def read_output(self, timeout: int | None = None) -> tuple[str, bool]:
        if self._last_result is None:
            return "", False
        return strip_thinking_blocks(self._last_result.output), self._last_result.timed_out

    def read_prose(self) -> str:
        """Model prose of the most recent run (text events only).

        Unlike :meth:`read_output` this excludes tool markers, stderr and
        exit-code notes — the reply text itself, e.g. a finished report.
        """
        if self._last_result is None:
            return ""
        return self._last_result.prose

    @property
    def last_looped(self) -> tuple[bool, str]:
        """(looped, reason) from the most recent run; (False, "") if none."""
        if self._last_result is None:
            return False, ""
        return (
            getattr(self._last_result, "looped", False),
            getattr(self._last_result, "loop_reason", ""),
        )

    def last_diagnostic(self) -> str:
        return self._last_result.diagnostic() if self._last_result else "(no run yet)"

    # -- session pinning ------------------------------------------------- #

    def enable_continuation(self, enabled: bool = True) -> None:
        # Bare continue flags (`opencode --continue`, pi `--continue`) attach
        # to the engine's globally-last session, which on a multi-project
        # machine can belong to a different workspace. Continuation is
        # therefore only allowed once a captured session id pins the target;
        # without one each turn runs fresh (prompts carry protocol +
        # feedback, so this stays correct). Engines whose fallback resume is
        # workspace-scoped (codex resume, pi --session-dir) re-enable here
        # from start() once an id is captured.
        if enabled and not self._session_id:
            logger.debug(
                "Continuation requested without a captured session id; "
                "staying session-less."
            )
            return
        self._use_continue = enabled

    def is_continuation_enabled(self) -> bool:
        return self._use_continue

    def is_session_pinned(self) -> bool:
        """True when a captured session id pins the conversation.

        Session-less turns (capture failure, fresh fallback) have no
        conversation memory, so callers must send self-contained prompts.
        """
        return bool(self._session_id)

    def mark_session_active(self) -> None:
        self._session_active = True

    def reset_session(self) -> None:
        self._session_active = False
        self._use_continue = False
        self._session_id = None
        # Context compaction stops the old session's process and immediately
        # starts a fresh one; that cleanup is not an operator cancel.
        self._cancel_event.clear()

    def reset_context_counter(self) -> None:
        self._chars_exchanged = 0
        self._last_tokens_total = 0

    # -- context accounting ---------------------------------------------- #

    @property
    def estimated_context_tokens(self) -> int:
        # Prefer the engine's reported token total (exact) over the
        # heuristic chars/4 estimate, which is all non-json output has.
        if self._last_tokens_total > 0:
            return self._last_tokens_total
        return self._chars_exchanged // 4

    # -- step detection --------------------------------------------------- #

    @property
    def step_detector(self) -> OpencodeStepDetector:
        return self._step_detector

    def get_step_progress(self) -> StepProgress:
        return self._step_detector.progress

    def reset_step_detector(self) -> None:
        self._step_detector.reset()

    def process_step_detection(self, output: str) -> Generator[dict]:
        yield from self._step_detector.process_output(output)

    def get_step_events(self, output: str) -> list[dict]:
        return list(self._step_detector.process_output(output))

    def get_current_phase(self) -> str:
        return self._step_detector.progress.phase.name.lower()

    def is_progressing(self) -> bool:
        return self._step_detector.is_progressing()

    def is_waiting_for_output(self) -> bool:
        return self._step_detector.is_waiting_for_output()

    def get_activity_state(self) -> str:
        return self._step_detector.get_activity_state()

    def get_step_summary(self) -> dict:
        progress = self._step_detector.progress
        return {
            "current_step": progress.current_step,
            "total_steps": progress.total_steps_estimate,
            "percentage": progress.percentage,
            "phase": progress.phase.name.lower(),
            "completed_phases": list(progress.completed_phases),
            "step_count": len(progress.steps),
        }

    # -- workspace cleanup + archiving ------------------------------------ #

    def send_cleanup_inquiry(self, candidates: list[str]) -> None:
        inquiry = build_cleanup_inquiry(self.workspace, candidates)
        if not inquiry:
            return
        logger.info(
            "Sending cleanup inquiry to %s for %d candidates",
            self._engine,
            len(candidates),
        )
        for _ in self.send(inquiry):
            pass

    def identify_cleanup_candidates(self) -> list[str]:
        candidates = discover_cleanup_candidates(self.workspace)
        if candidates:
            self.send_cleanup_inquiry(candidates)
        return candidates

    def archive_files(self, files: list[str]) -> ArchiveResult:
        return self._archiver.archive_workspace(label="cleanup", files_to_archive=files)

    def archive_before_new_run(self) -> ArchiveResult:
        return self._archiver.archive_before_new_run()

    def get_archiver(self) -> WorkspaceArchiver:
        return self._archiver

    def list_archives(self) -> list[dict]:
        return self._archiver.list_archives()

    def get_archive_stats(self) -> dict:
        return self._archiver.get_archive_stats()

    def get_files_read(self) -> list[str]:
        if not self._last_result or not self._last_result.output:
            return []
        return extract_file_refs(self._last_result.output)
