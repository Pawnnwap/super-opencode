"""supervisor/runners/opencode_runner.py

Drives opencode via its non-interactive CLI:

    opencode run "<prompt>" [--model <model>]

Thin engine subclass of :class:`BaseRunner` — the full runner surface (turn
lifecycle, session pinning, step detection, archiving) lives there. Only the
opencode-specific seams remain:

  * executable location  -> opencode_support.locator.find_opencode
  * workspace marker     -> .opencode/opencode.json (auto-approve permissions)
  * session capture      -> a supervisor-generated session id on opencode 2.x
                            (`--session <id>` creates the session when it does
                            not exist), or out-of-band `opencode session list`
                            diffing on 1.x. opencode does NOT report its
                            session id on the event stream (unlike codex/pi).
  * subprocess driving   -> opencode_support.process.run_prompt

- stdin=DEVNULL-equivalent: the prompt is piped, guaranteeing no TTY
- All permissions auto-approved in `run` mode
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Generator

from supervisor.runners.base_runner import BaseRunner
from supervisor.runners.command_common import validate_message
from supervisor.runners.opencode_support import catalog
from supervisor.runners.opencode_support.locator import find_opencode as _find_opencode
from supervisor.runners.opencode_support.process import run_prompt as _run_prompt_impl
from supervisor.runners.opencode_support.result import RunResult
from supervisor.runners.opencode_support.session import (
    SESSION_CAPTURE_LOCK as _SESSION_CAPTURE_LOCK,
)
from supervisor.runners.opencode_support.session import (
    capture_new_session_id as _capture_new_session_id_impl,
)
from supervisor.runners.opencode_support.session import (
    list_all_session_ids as _list_all_session_ids_impl,
)

logger = logging.getLogger(__name__)


def find_opencode(explicit: str = "") -> str:
    return _find_opencode(explicit)


def new_session_id() -> str:
    """A fresh opencode session id in the CLI's own ``ses_<token>`` shape.

    opencode 2.x accepts any ``ses_…`` id on ``--session`` and creates the
    session when it does not exist (verified on v2.2.7), so the supervisor can
    name its own session instead of diffing ``session list`` before/after a
    turn. That diff is what failed on every 2.x run ("Session ID capture
    failed; continuing session-less"), leaving each turn without conversation
    memory.
    """
    return "ses_" + secrets.token_urlsafe(18).replace("-", "").replace("_", "")


class OpencodeRunner(BaseRunner):
    """One send()/start() call = one `opencode run "<prompt>"` subprocess."""

    _engine = "opencode"

    def _prepare_workspace(self) -> None:
        """Ensure workspace exists and contains opencode project marker."""
        self.workspace.mkdir(parents=True, exist_ok=True)
        oc_dir = self.workspace / ".opencode"
        oc_dir.mkdir(exist_ok=True)
        config_path = oc_dir / "opencode.json"
        if not config_path.exists():
            import json

            config_path.write_text(
                json.dumps(
                    {
                        "$schema": "https://opencode.ai/config.json",
                        "permission": {
                            "read": "allow",
                            "edit": "allow",
                            "write": "allow",
                            "bash": "allow",
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            logger.info("Created .opencode/opencode.json in workspace")

    def start(self, initial_prompt: str) -> Generator[dict]:
        if self.cancel_event.is_set():
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
                "Empty initial prompt in start(). Using fallback to continue session.",
            )
            validated = "Continue based on the current context and proceed with the task."

        self._alive = True
        if self._session_active:
            yield from self._run_prompt(validated)
            return

        # opencode 2.x: name the session ourselves. `--session <id>` creates it
        # when missing, so the id is known BEFORE the first turn and every
        # later turn resumes it deterministically — no session-list diff, no
        # "capture failed, continuing session-less" degradation.
        major = catalog.cli_major(self._resolved_exe())
        if major is not None and major >= 2:
            self._session_id = new_session_id()
            self._session_active = True
            self.enable_continuation(True)
            logger.info("Session ID assigned: %s", self._session_id)
            yield {"level": "info", "msg": f"Session ID captured: {self._session_id}"}
            yield from self._run_prompt(validated)
            return

        # opencode 1.x does not report its session id on the event stream, so
        # the first turn runs under the session-capture lock and the new
        # session is found by diffing `opencode session list` before/after.
        #
        # Bootstrap + work in ONE turn: the first real prompt creates the
        # session, and the capture diff pins its ID. A standalone bootstrap
        # turn wastes a full model call and, with slow models, times out
        # before creating any session.
        with _SESSION_CAPTURE_LOCK:
            if self.cancel_event.is_set():
                return
            before = self._list_all_session_ids()
            yield from self._run_prompt(validated)
            if self.cancel_event.is_set():
                # The session-list diff can poll for seconds; a cancelled
                # turn must not wait for it.
                return
            self._session_id = self._capture_new_session_id(before)

        self._session_active = True
        if self._session_id:
            logger.info("Session ID captured: %s", self._session_id)
            yield {"level": "info", "msg": f"Session ID captured: {self._session_id}"}
            self.enable_continuation(True)
        else:
            logger.warning(
                "Could not capture the new session ID; continuing session-less. "
                "Later turns send self-contained prompts instead of risking "
                "bare --continue attaching to a foreign session.",
            )
            yield {
                "level": "warn",
                "msg": "Session ID capture failed; continuing session-less.",
            }
            self.enable_continuation(False)

    def _resolved_exe(self) -> str:
        try:
            return find_opencode(self.opencode_executable)
        except FileNotFoundError:
            return self.opencode_executable or "opencode"

    def _run_prompt(self, prompt: str) -> Generator[dict]:
        yield from _run_prompt_impl(self, prompt, find_opencode_fn=find_opencode)

    def _list_all_session_ids(self) -> set[str]:
        return _list_all_session_ids_impl(
            workspace=self.workspace,
            opencode_executable=self.opencode_executable,
            find_opencode_fn=find_opencode,
        )

    def _capture_new_session_id(
        self,
        before: set[str],
        attempts: int = 10,
        delay_seconds: float = 0.5,
    ) -> str | None:
        return _capture_new_session_id_impl(
            before,
            workspace=self.workspace,
            opencode_executable=self.opencode_executable,
            find_opencode_fn=find_opencode,
            attempts=attempts,
            delay_seconds=delay_seconds,
        )


__all__ = ["_SESSION_CAPTURE_LOCK", "OpencodeRunner", "RunResult", "find_opencode"]
