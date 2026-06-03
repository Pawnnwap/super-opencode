"""supervisor/runners/codex_runner.py

Drives the open-source Codex CLI via its non-interactive subcommand:

    codex exec <flags> -- "<prompt>"

Cross-platform (Windows + Linux). Implemented as a thin subclass of
``OpencodeRunner`` so the entire supervisor loop integration (step detection,
context accounting, archiving, cleanup, session continuity) is reused
unchanged. Only the engine-specific seams differ:

  * executable location  -> codex_support.locator.find_codex
  * command building      -> codex_support.command_builder.build_cmd
  * subprocess driving    -> codex_support.process.run_prompt
  * session handling      -> codex `resume`/`--last` instead of opencode
                             session-id list diffing

Continuation: codex tracks the most recent rollout, so the first turn inlines
the brevity rules and subsequent turns ``resume`` (by captured session id when
available, else ``--last``). No session-list enumeration is required.
"""

from __future__ import annotations

import logging
from collections.abc import Generator

from supervisor.runners.codex_support.command_builder import (
    fresh_session_prompt as _codex_fresh_session_prompt_impl,
    validate_message as _codex_validate_message_impl,
)
from supervisor.runners.codex_support.locator import find_codex as _find_codex
from supervisor.runners.codex_support.process import run_prompt as _codex_run_prompt_impl
from supervisor.runners.opencode_runner import OpencodeRunner
from supervisor.runners.opencode_support.result import RunResult
from supervisor.utils.text_utils import coerce_str

logger = logging.getLogger(__name__)


def find_codex(explicit: str = "") -> str:
    return _find_codex(explicit)


def _validate_message(message: str, context: str = "message") -> str | None:
    return _codex_validate_message_impl(message, context)


class CodexRunner(OpencodeRunner):
    """One send()/start() call = one ``codex exec "<prompt>"`` subprocess.

    Inherits the full public runner surface from ``OpencodeRunner``; the
    ``opencode_*`` config fields are reused generically as the codex agent's
    model / executable / fallback model. The optional ``codex_base_url`` /
    ``codex_api_key`` configure an external OpenAI-compatible (Responses API)
    endpoint, injected per-run via codex ``-c`` overrides.
    """

    def __init__(self, *args, codex_base_url: str = "", codex_api_key: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.codex_base_url = coerce_str(codex_base_url, "codex_base_url") or ""
        self.codex_api_key = coerce_str(codex_api_key, "codex_api_key") or ""

    @classmethod
    def from_config(cls, config, agent: str = "") -> "CodexRunner":
        runner = super().from_config(config, agent=agent)
        runner.codex_base_url = coerce_str(
            getattr(config, "codex_base_url", ""), "codex_base_url"
        ) or ""
        runner.codex_api_key = coerce_str(
            getattr(config, "codex_api_key", ""), "codex_api_key"
        ) or ""
        return runner

    def _prepare_workspace(self) -> None:
        """Codex needs no in-workspace marker file; just ensure the dir exists."""
        self.workspace.mkdir(parents=True, exist_ok=True)

    def start(self, initial_prompt: str) -> Generator[dict]:
        validated = _validate_message(initial_prompt, "initial_prompt (start)")
        if validated is None:
            logger.warning(
                "Empty initial prompt in codex start(). Falling back to continue.",
            )
            validated = "Continue based on the current context and proceed with the task."

        self._alive = True
        if self._session_active:
            yield from self._run_prompt(validated)
            return

        logger.info("New codex session — inlining brevity rules into first prompt.")
        yield {
            "level": "info",
            "msg": "New codex session — inlining brevity rules into first prompt.",
        }
        # Fresh session: no resume, brevity inlined. The process layer captures
        # the codex session id from output (when present) so the next turn can
        # resume precisely.
        self._session_id = None
        self.enable_continuation(False)
        yield from self._run_prompt(self._fresh_session_prompt(validated))
        self._session_active = True
        self.enable_continuation(True)

    def _run_prompt(self, prompt: str) -> Generator[dict]:
        yield from _codex_run_prompt_impl(self, prompt, find_codex_fn=find_codex)

    def _fresh_session_prompt(self, prompt: str) -> str:
        return _codex_fresh_session_prompt_impl(prompt)

    # Codex has no opencode-style session-list command. Continuation relies on
    # `resume <id|--last>`, so these enumeration hooks are intentionally inert.
    def _list_all_session_ids(self) -> set[str]:
        return set()

    def _capture_new_session_id(
        self,
        before: set[str],
        attempts: int = 4,
        delay_seconds: float = 0.25,
    ) -> str | None:
        return None


__all__ = ["CodexRunner", "RunResult", "find_codex"]
