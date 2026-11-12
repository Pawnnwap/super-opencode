"""Parallel-study batch provisioning: one protocol, N replica workspaces.

"同一份 protocol 并行多开研究" — the protocol only ever reaches a run
through ``<workspace>/protocol.md`` and every run assumes exclusive
ownership of its workspace, so N parallel instances of one protocol are N
sibling workspace copies, each enqueued as its own job carrying batch
metadata (``batch_id`` / ``replica_index`` / ``replica_count``) for board
grouping and one-click batch cancel.

Replica layout: ``<workspace>_studies/<batch_id>_r1..rN`` — one parent per
source workspace, one timestamped subdir per batch, no collisions across
repeated launches.
"""

from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from services.config.supervisor_config_builder import build_supervisor_config
from supervisor.core.task_state import TASK_STATE_FILENAME
from supervisor.protocols.protocol import resolve_protocol_path
from supervisor.utils.config import SupervisorConfig

# Run artifacts that must NOT seed a fresh replica: snapshots and journals
# belong to the run that produced them, and dot-dirs (.opencode sessions,
# .archive snapshots) would race the replica's own versions.
REPLICA_SKIP_DIRS = {
    ".archive",
    ".opencode",
    ".job_store",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".checkpoints",
    ".git",
    ".venv",
    "venv",
    "node_modules",
    # MATLAB creates runtime-state dirs inside the cwd it was launched from
    # and keeps files in them open while it runs. Copying those throws
    # ``shutil.Error: [WinError 32]`` (file in use) and kills the whole batch
    # launch, so replicas never carry them.
    "matlab",
    "mathworks",
    "matlab settings",
}
REPLICA_SKIP_FILES = {
    TASK_STATE_FILENAME,
    "summary.md",
    "failure_report.md",
}

INIT_MODES = ("copy", "protocol")


def _replica_ignore(directory: str, names: list[str]) -> set[str]:
    # Dirs match case-insensitively: Windows spells the MATLAB dirs ``MATLAB`` /
    # ``MathWorks``, and a case-sensitive match would copy exactly the locked
    # files the skip exists to avoid (that filesystem ignores case). Skip *files*
    # stay case-sensitive — their spellings are ours, not the OS's.
    return {
        name
        for name in names
        if name.casefold() in REPLICA_SKIP_DIRS
        or name in REPLICA_SKIP_FILES
        or name.endswith(".log")
    }


def provision_replica(
    workspace: Path, replica: Path, init_mode: str,
) -> Path:
    """Create one replica workspace seeded per ``init_mode``.

    "copy": the whole workspace minus run artifacts (protocol included).
    "protocol": an empty dir holding only protocol.md — for research
    protocols that need no seeded code.
    """
    from supervisor.protocols.protocol import resolve_protocol_path

    protocol_src = resolve_protocol_path(workspace)
    if not protocol_src.is_file():
        raise ValueError(
            f"No protocol file (protocol.jsonl or protocol.md) in {workspace} — "
            "save one first.",
        )

    replica.parent.mkdir(parents=True, exist_ok=True)
    if init_mode == "protocol":
        replica.mkdir(parents=True)
        shutil.copy2(protocol_src, replica / protocol_src.name)
        # The acceptance gates travel with the protocol they were drafted
        # from; without them every replica restarts gate-blind.
        facts = workspace / "feasibility_facts.json"
        if facts.is_file():
            shutil.copy2(facts, replica / "feasibility_facts.json")
        profile = workspace / "access_protection.json"
        if profile.is_file():
            shutil.copy2(profile, replica / "access_protection.json")
    elif init_mode == "copy":
        if replica.exists():
            raise ValueError(f"Replica directory already exists: {replica}")
        shutil.copytree(workspace, replica, ignore=_replica_ignore)
    else:
        raise ValueError(f"Unknown replica init mode: {init_mode!r}")
    return replica


def launch_study_batch(
    session_state: dict[str, Any],
    manager,
    workspace: Path,
    replicas: int,
    init_mode: str = "copy",
    **overrides,
) -> tuple[str, list[str], list[SupervisorConfig]]:
    """Provision N replicas of ``workspace`` and enqueue one run per replica.

    Returns ``(batch_id, job_ids, configs)``. The caller decides navigation
    (usually to the first replica's live screen).
    """
    if replicas < 1:
        raise ValueError("replicas must be >= 1")
    if init_mode not in INIT_MODES:
        raise ValueError(f"init_mode must be one of {INIT_MODES}")

    batch_id = f"study_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"
    parent = workspace.parent / f"{workspace.name}_studies"

    job_ids: list[str] = []
    configs: list[SupervisorConfig] = []
    protocol_name = resolve_protocol_path(workspace).name
    try:
        for index in range(1, replicas + 1):
            replica = parent / f"{batch_id}_r{index}"
            provision_replica(workspace, replica, init_mode)
            config = build_supervisor_config(
                session_state,
                protocol_path=replica / protocol_name,
                workspace=replica,
                **overrides,
            )
            job_ids.append(manager.enqueue_job("run", config, meta={
                "batch_id": batch_id,
                "replica_index": index,
                "replica_count": replicas,
            }))
            configs.append(config)
    except Exception:
        # Partial provisioning: drop the replicas that never got enqueued
        # so a retry doesn't trip over stale directories.
        for index in range(len(job_ids) + 1, replicas + 1):
            stale = parent / f"{batch_id}_r{index}"
            shutil.rmtree(stale, ignore_errors=True)
        raise
    return batch_id, job_ids, configs
