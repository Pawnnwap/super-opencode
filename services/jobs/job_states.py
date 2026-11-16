"""Canonical job-state vocabulary, shared by the store, the worker and the UI.

These sets were previously re-declared in six places (``JobManager``,
``StoreJobClient``, the board, the live screen, the nav shell and the run page),
so a new state had to be added in every one and the copies could silently
disagree — the board once counted nothing because its keys were lowercase while
the store wrote uppercase.

Layering: this module is pure (no UI, no store), so both ``services.jobs`` and
``services.webui`` may import it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StateStyle:
    """How a job state is presented everywhere in the UI.

    ``tone`` is a semantic slot, not a colour: the stylesheet owns the hex.
    ``needs_attention`` is what the board counts — it used to include
    CANCELLED, so a workspace where the operator had stopped most runs showed
    57% "need attention" and the number stopped meaning anything.
    """

    tone: str          # accent | ok | warn | bad | neutral
    label: str         # operator-facing text
    needs_attention: bool


# The single source of truth for state → presentation. The board's rail, its
# pill and the attention counter all derive from this table; adding a state
# means editing one dict instead of six if/else chains.
STATE_CONTRACT: dict[str, StateStyle] = {
    "PENDING": StateStyle("accent", "PENDING", False),
    "RUNNING": StateStyle("accent", "RUNNING", False),
    "SUCCESS": StateStyle("ok", "SUCCESS", False),
    "FAILED": StateStyle("bad", "FAILED", True),
    "BLOCKED": StateStyle("warn", "BLOCKED", True),
    "ABANDONED": StateStyle("warn", "ABANDONED", True),
    # The operator's own action: neutral, never red, never counted as a fault.
    "CANCELLED": StateStyle("neutral", "CANCELLED", False),
}

# Tone → the pill class that already exists in theme.py.
PILL_TONE_CLASS: dict[str, str] = {
    "accent": "pill-running",
    "ok": "pill-success",
    "warn": "pill-blocked",
    "bad": "pill-failure",
    "neutral": "pill-cancelled",
}

# Where the job records live. The UI process, the worker daemon and the batch
# launcher must all agree on this path — it IS the IPC between them, so a
# mismatch silently strands jobs in a store nobody reads.
JOB_STORE_DIR = ".job_store"

# The UI writes its concurrency caps here; the daemon reads them at start.
# Same IPC rule as JOB_STORE_DIR: one spelling, or the daemon runs the caps
# the UI thought it replaced.
WORKER_SETTINGS_FILE = "worker_settings.json"

# Queued or executing: the job still owns its workspace.
ACTIVE_JOB_STATES = frozenset({"PENDING", "RUNNING"})

# Reached a final state; never resumes on its own. ABANDONED belongs here: the
# worker daemon writes it when it restarts over a RUNNING job, and a state that
# is not terminal is never pruned, never deletable from the board and never
# counted - so those records used to accumulate forever.
TERMINAL_JOB_STATES = frozenset(
    {"SUCCESS", "FAILED", "CANCELLED", "BLOCKED", "ABANDONED"},
)

# Terminal states that represent unfinished work rather than a clean outcome.
# Derived, so "what counts as attention" cannot drift from "how it looks":
# CANCELLED is excluded (the operator did it on purpose), BLOCKED is included
# (it stopped short of the target, which is exactly what attention means).
ATTENTION_JOB_STATES = frozenset(
    state for state, style in STATE_CONTRACT.items() if style.needs_attention
)

__all__ = [
    "ACTIVE_JOB_STATES",
    "ATTENTION_JOB_STATES",
    "JOB_STORE_DIR",
    "PILL_TONE_CLASS",
    "STATE_CONTRACT",
    "StateStyle",
    "TERMINAL_JOB_STATES",
    "WORKER_SETTINGS_FILE",
]
