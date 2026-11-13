"""Run-scoped process containment: registry, job scope, and the kill gate.

Every process a run starts is recorded at spawn time in an append-only
ledger, and every descendant that process creates is contained in that run's
job object (a Windows Job Object, or a POSIX session/process group). A kill is
therefore only ever an operation on one of those two sets — never on a process
name, a command-line keyword, or a PID observed somewhere else.

Modules:

* :mod:`supervisor.runtime.process_registry` — the append-only ownership ledger;
* :mod:`supervisor.runtime.job_object` — the kernel containment scope;
* :mod:`supervisor.runtime.process_control` — the ONLY place that signals a
  process (verifies ownership first, records refusals);
* :mod:`supervisor.runtime.processes` — the base class plus the per-language
  spawners (engine / MATLAB / Python / Rust / service).
"""

from __future__ import annotations

from supervisor.runtime.process_base import OwnedProcess, ProcResult
from supervisor.runtime.process_control import (
    KillRefused,
    spawn_tracked,
    terminate_pid,
    terminate_run,
    terminate_tree,
    verify_owned,
)
from supervisor.runtime.process_registry import ProcessRegistry, ProcRecord
from supervisor.runtime.processes import (
    EngineProcess,
    MatlabProcess,
    PythonProcess,
    RustProcess,
    ServiceProcess,
)

__all__ = [
    "EngineProcess",
    "KillRefused",
    "MatlabProcess",
    "OwnedProcess",
    "ProcRecord",
    "ProcResult",
    "ProcessRegistry",
    "PythonProcess",
    "RustProcess",
    "ServiceProcess",
    "spawn_tracked",
    "terminate_pid",
    "terminate_run",
    "terminate_tree",
    "verify_owned",
]
