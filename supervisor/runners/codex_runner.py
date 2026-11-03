"""supervisor/runners/codex_runner.py

Drives the open-source Codex CLI via its non-interactive subcommand:

    codex exec <flags> -- "<prompt>"

Cross-platform (Windows + Linux). Thin engine subclass of
:class:`BaseRunner` — the full runner surface (turn lifecycle, session
pinning, step detection, archiving) lives there, and the base ``start()``
default already matches codex: the session id arrives on the event stream
(``thread.started``), so turn one opens a fresh session and later turns
``resume`` by the captured id. Only the engine-specific seams remain:

  * executable location  -> codex_support.locator.find_codex
  * command building      -> codex_support.command_builder.build_cmd
  * subprocess driving    -> codex_support.process.run_prompt (shared driver)

The ``opencode_*`` config fields are reused generically as the codex agent's
model / executable / fallback model. The optional ``codex_base_url`` /
``codex_api_key`` configure an external OpenAI-compatible (Responses API)
endpoint, injected per-run via codex ``-c`` overrides.
"""

from __future__ import annotations

import logging
from collections.abc import Generator

from supervisor.runners.base_runner import BaseRunner
from supervisor.runners.codex_support.locator import find_codex as _find_codex
from supervisor.runners.codex_support.process import (
    run_prompt as _codex_run_prompt_impl,
)
from supervisor.runners.opencode_support.result import RunResult
from supervisor.utils.text_utils import coerce_str

logger = logging.getLogger(__name__)


def find_codex(explicit: str = "") -> str:
    return _find_codex(explicit)


class CodexRunner(BaseRunner):
    """One send()/start() call = one ``codex exec "<prompt>"`` subprocess."""

    _engine = "codex"

    def __init__(
        self,
        *args,
        codex_base_url: str = "",
        codex_api_key: str = "",
        codex_context_window: int = 0,
        codex_reasoning_effort: str = "",
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.codex_base_url = coerce_str(codex_base_url, "codex_base_url") or ""
        self.codex_api_key = coerce_str(codex_api_key, "codex_api_key") or ""
        # Reasoning effort passed as `-c model_reasoning_effort` ("" = codex
        # config default). Set from SupervisorConfig.agent_reasoning.
        self.codex_reasoning_effort = (
            coerce_str(codex_reasoning_effort, "codex_reasoning_effort") or ""
        )
        # Per-subprocess env additions (e.g. isolated CODEX_HOME for probes).
        self.codex_extra_env: dict[str, str] = {}
        # Extra `-c` config overrides (e.g. the connectivity probe disables
        # the plugins feature).
        self.codex_extra_config_flags: list[str] | None = None
        # Local model's context window (from config.max_tokens). Forwarded as
        # a codex `-c model_context_window` override for external providers so
        # codex auto-compacts before the local model's cache overflows.
        self.codex_context_window = self._coerce_window(codex_context_window)

    @staticmethod
    def _coerce_window(value) -> int:
        try:
            window = int(value)
        except (TypeError, ValueError):
            return 0
        return max(0, window)

    @classmethod
    def from_config(cls, config, agent: str = "") -> CodexRunner:
        runner = super().from_config(config, agent=agent)
        runner.codex_base_url = coerce_str(
            getattr(config, "codex_base_url", ""), "codex_base_url"
        ) or ""
        runner.codex_api_key = coerce_str(
            getattr(config, "codex_api_key", ""), "codex_api_key"
        ) or ""
        runner.codex_context_window = cls._coerce_window(
            getattr(config, "max_tokens", 0)
        )
        runner.codex_reasoning_effort = (
            coerce_str(
                getattr(config, "agent_reasoning", ""), "agent_reasoning (codex)",
            ) or ""
        )
        return runner

    def _run_prompt(self, prompt: str) -> Generator[dict]:
        yield from _codex_run_prompt_impl(self, prompt, find_codex_fn=find_codex)


__all__ = ["CodexRunner", "RunResult", "find_codex"]
