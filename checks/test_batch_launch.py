"""Parallel-study batch provisioning checks.

Contract: N replicas of one protocol means N distinct workspaces (the
same-workspace guard stays intact), copy-mode excludes run artifacts,
protocol-mode seeds nothing else, and every enqueued job carries the batch
metadata the board groups by.
"""

from pathlib import Path

import pytest

from services.webui.batch_launch import (
    REPLICA_SKIP_DIRS,
    launch_study_batch,
    provision_replica,
)


class RecordingManager:
    """Fake JobManager: records enqueues, never starts workers."""

    def __init__(self):
        self.enqueued: list[tuple[str, object, dict]] = []

    def enqueue_job(self, job_type, config, meta=None):
        self.enqueued.append((job_type, config, dict(meta or {})))
        return f"job_{len(self.enqueued)}"


def _make_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "study_ws"
    (ws / "logs").mkdir(parents=True)
    (ws / ".archive").mkdir()
    (ws / ".opencode").mkdir()
    (ws / "sub").mkdir(parents=True)
    (ws / "protocol.md").write_text("## INPUT\nx\n## TARGET\ny\n## RESTRICTIONS\nz", encoding="utf-8")
    (ws / "code.py").write_text("print(1)", encoding="utf-8")
    (ws / "sub" / "data.csv").write_text("a,b", encoding="utf-8")
    (ws / "TASK_STATE.md").write_text("old journal", encoding="utf-8")
    (ws / "summary.md").write_text("old summary", encoding="utf-8")
    (ws / "run.log").write_text("noise", encoding="utf-8")
    return ws


def _session_state(ws: Path) -> dict:
    return {
        "max_retries": 3,
        "context_threshold": 60,
        "engine": "opencode",
        "codex_base_url": "",
        "codex_api_key": "",
        "opencode_model": "m",
        "opencode_model_backup": "",
        "opencode_executable": "opencode",
        "supervisor_model": "gpt-4o",
        "supervisor_model_backup": "",
        "supervisor_reasoning": "",
        "agent_reasoning": "",
        "timeout": 10,
        "protected_files": [],
        "max_tokens": 100000,
        "enable_python_scanner": True,
        "enable_occam_razor": False,
        "enable_supervisor_skills": True,
        "supervisor_skills_dir": "",
        "openai_key": "",
        "base_url": "",
    }


def test_copy_mode_excludes_artifacts_and_enqueues_per_replica(tmp_path):
    ws = _make_workspace(tmp_path)
    manager = RecordingManager()
    batch_id, job_ids, _configs = launch_study_batch(
        _session_state(ws), manager, ws, 3, init_mode="copy",
        plan_mode_rounds=1,
    )

    assert len(job_ids) == 3
    assert len(manager.enqueued) == 3
    workspaces = [Path(c.workspace) for _, c, _ in manager.enqueued]
    # Every replica is its own workspace under <ws>_studies/<batch>_rN —
    # distinct paths, so the JobManager workspace guard still holds.
    assert len({str(w) for w in workspaces}) == 3
    parent = tmp_path / "study_ws_studies"
    assert workspaces == [
        parent / f"{batch_id}_r1",
        parent / f"{batch_id}_r2",
        parent / f"{batch_id}_r3",
    ]
    for index, (_, config, meta) in enumerate(manager.enqueued, start=1):
        assert meta["batch_id"] == batch_id
        assert meta["replica_index"] == index
        assert meta["replica_count"] == 3
        assert config.protocol_path == Path(config.workspace) / "protocol.md"

    r1 = workspaces[0]
    assert (r1 / "protocol.md").exists()
    assert (r1 / "code.py").exists()
    assert (r1 / "sub" / "data.csv").exists()
    for skipped in [*REPLICA_SKIP_DIRS, "TASK_STATE.md", "summary.md", "run.log"]:
        assert not (r1 / skipped).exists(), skipped


def test_protocol_mode_seeds_only_protocol(tmp_path):
    ws = _make_workspace(tmp_path)
    replica = ws.parent / "study_ws_studies" / "batch_r1"
    provision_replica(ws, replica, "protocol")
    assert (replica / "protocol.md").exists()
    assert list(replica.iterdir()) == [replica / "protocol.md"]


def test_missing_protocol_raises(tmp_path):
    ws = tmp_path / "empty"
    ws.mkdir()
    with pytest.raises(ValueError, match="protocol.md"):
        provision_replica(ws, ws.parent / "r1", "protocol")


def test_partial_failure_cleans_unenqueued_replicas(tmp_path):
    ws = _make_workspace(tmp_path)
    parent = tmp_path / "study_ws_studies"

    class FailingManager(RecordingManager):
        def enqueue_job(self, job_type, config, meta=None):
            if len(self.enqueued) >= 1:
                raise RuntimeError("enqueue exploded")
            return super().enqueue_job(job_type, config, meta)

    with pytest.raises(RuntimeError, match="enqueue exploded"):
        launch_study_batch(_session_state(ws), FailingManager(), ws, 3, init_mode="copy")
    # Only the enqueued replica's dir remains; r2/r3 were rolled back.
    leftovers = sorted(p.name for p in parent.iterdir())
    assert len(leftovers) == 1 and leftovers[0].endswith("_r1")
