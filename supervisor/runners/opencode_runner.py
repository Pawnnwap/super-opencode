"""supervisor/runners/opencode_runner.py

Drives opencode via its non-interactive CLI:

    opencode run "<prompt>" [--model <model>]

Thin engine subclass of :class:`BaseRunner` — the full runner surface (turn
lifecycle, session pinning, step detection, archiving) lives there. Only the
opencode-specific seams remain:

  * executable location  -> opencode_support.locator.find_opencode
  * workspace marker     -> .opencode/config.json (auto-approve permissions)
  * session capture      -> out-of-band `opencode session list` diffing.
                            opencode does NOT report its session id on the
                            event stream (unlike codex/pi), so start() is
                            overridden and the enumeration hooks are real.
  * subprocess driving   -> opencode_support.process.run_prompt

- stdin=DEVNULL-equivalent: the prompt is piped, guaranteeing no TTY
- All permissions auto-approved in `run` mode
"""

from __future__ import annotations

import logging
from collections.abc import Generator

from supervisor.runners.base_runner import BaseRunner
from supervisor.runners.command_common import validate_message
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


class OpencodeRunner(BaseRunner):
    """One send()/start() call = one `opencode run "<prompt>"` subprocess."""

    _engine = "opencode"

    def _prepare_workspace(self) -> None:
        """Ensure workspace exists and contains opencode project marker."""
        self.workspace.mkdir(parents=True, exist_ok=True)
        oc_dir = self.workspace / ".opencode"
        oc_dir.mkdir(exist_ok=True)
        config_path = oc_dir / "config.json"
        if not config_path.exists():
            import json

            config_path.write_text(
                json.dumps({"autoapprove": True}, indent=2),
                encoding="utf-8",
            )
            logger.info("Created .opencode/config.json in workspace")

    def start(self, initial_prompt: str) -> Generator[dict]:
        # opencode does not report its session id on the event stream, so the
        # first turn runs under the session-capture lock and the new session
        # is found by diffing `opencode session list` before/after.
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

        # Bootstrap + work in ONE turn: the first real prompt creates the
        # session, and the capture diff pins its ID. A standalone bootstrap
        # turn wastes a full model call and, with slow models, times out
        # before creating any session.
        with _SESSION_CAPTURE_LOCK:
            before = self._list_all_session_ids()
            yield from self._run_prompt(validated)
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
