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

# Reached a final state; never resumes on its own.
TERMINAL_JOB_STATES = frozenset({"SUCCESS", "FAILED", "CANCELLED", "BLOCKED"})

# Terminal states that represent unfinished work rather than a clean outcome.
ATTENTION_JOB_STATES = frozenset({"FAILED", "CANCELLED"})

__all__ = [
    "ACTIVE_JOB_STATES",
    "ATTENTION_JOB_STATES",
    "JOB_STORE_DIR",
    "TERMINAL_JOB_STATES",
    "WORKER_SETTINGS_FILE",
]
