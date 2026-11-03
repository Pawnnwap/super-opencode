"""supervisor/runners/pi_runner.py

Drives the pi coding agent via its non-interactive print mode:

    pi -p --mode json --session-dir <workspace>/.pi-sessions [flags]

Cross-platform (Windows + Linux). Thin engine subclass of
:class:`BaseRunner` — the full runner surface (turn lifecycle, session
pinning, step detection, archiving) lives there, and the base ``start()``
default already matches pi: the session id arrives on the event stream
(``session`` event), so turn one opens a fresh session and later turns pass
``--session <id>``. Only the engine-specific seams remain:

  * executable location  -> pi_support.locator.find_pi
  * command building      -> pi_support.command_builder.build_cmd
  * subprocess driving    -> pi_support.process.run_prompt (shared driver)

The opencode_* config fields are reused generically as pi's model /
executable / fallback model (same convention as CodexRunner);
``agent_reasoning`` maps onto pi's ``--thinking`` ladder. pi providers are
configured in pi's own settings (~/.pi), so no external base_url/key fields
exist for this engine.
"""

from __future__ import annotations

import logging
from collections.abc import Generator

from supervisor.runners.base_runner import BaseRunner
from supervisor.runners.opencode_support.result import RunResult
from supervisor.runners.pi_support.command_builder import (
    sessions_dir_for,
)
from supervisor.runners.pi_support.locator import find_pi as _find_pi
from supervisor.runners.pi_support.process import run_prompt as _pi_run_prompt_impl

logger = logging.getLogger(__name__)


def find_pi(explicit: str = "") -> str:
    return _find_pi(explicit)


class PiRunner(BaseRunner):
    """One send()/start() call = one ``pi -p "<prompt over stdin>"`` subprocess."""

    _engine = "pi"

    @property
    def pi_sessions_dir(self) -> str:
        return sessions_dir_for(self.workspace)

    def _prepare_workspace(self) -> None:
        """Ensure the workspace and the pi session dir exist."""
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.workspace.joinpath(".pi-sessions").mkdir(exist_ok=True)

    def _run_prompt(self, prompt: str) -> Generator[dict]:
        yield from _pi_run_prompt_impl(self, prompt, find_pi_fn=find_pi)


__all__ = ["PiRunner", "RunResult", "find_pi"]
