"""Parallel-study batch provisioning checks.

Contract: N replicas of one protocol means N distinct workspaces (the
same-workspace guard stays intact), copy-mode excludes run artifacts,
protocol-mode seeds nothing else, and every enqueued job carries the batch
metadata the board groups by.
"""

from pathlib import Path

import pytest

from services.webui import batch_launch as batch_module
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
    (ws / "protocol.md").write_text(
        "## INPUT\nx\n## TARGET\ny\n## RESTRICTIONS\nz", encoding="utf-8"
    )
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
        _session_state(ws),
        manager,
        ws,
        3,
        init_mode="copy",
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


def test_jsonl_protocol_replica(tmp_path):
    from supervisor.protocols.protocol import parse_protocol_text, save_protocol

    ws = _make_workspace(tmp_path)
    save_protocol(
        ws, parse_protocol_text((ws / "protocol.md").read_text(encoding="utf-8"))
    )

    replica = ws.parent / "study_ws_studies" / "batch_r1"
    provision_replica(ws, replica, "protocol")
    names = {path.name for path in replica.iterdir()}
    assert "protocol.jsonl" in names
    assert "protocol.md" not in names

    manager = RecordingManager()
    _batch, _ids, configs = launch_study_batch(
        _session_state(ws),
        manager,
        ws,
        1,
        init_mode="protocol",
    )
    assert configs[0].protocol_path.name == "protocol.jsonl"


def test_matlab_runtime_dirs_are_never_copied(tmp_path):
    """A MATLAB workspace dir is the batch launch's #1 copy failure.

    MATLAB writes runtime state (``matlab/graphics/.../matlabsettings.xml``,
    ``MathWorks/MATLAB Profiles/...``) into the cwd it was launched from and
    holds files in it open while running, so ``copytree`` dies with
    ``shutil.Error: [WinError 32]``. The names are skipped case-insensitively
    because Windows spells them ``MATLAB`` / ``MathWorks``.
    """
    ws = _make_workspace(tmp_path)
    (ws / "matlab" / "graphics").mkdir(parents=True)
    (ws / "matlab" / "graphics" / "matlabsettings_2221d.xml").write_text(
        "locked",
        encoding="utf-8",
    )
    (ws / "MathWorks" / "MATLAB Profiles").mkdir(parents=True)
    (ws / "MathWorks" / "MATLAB Profiles" / "matlab.settings").write_text(
        "x",
        encoding="utf-8",
    )

    replica = ws.parent / "study_ws_studies" / "batch_r1"
    provision_replica(ws, replica, "copy")

    assert not (replica / "matlab").exists()
    assert not (replica / "MathWorks").exists()
    # Real research data still travels.
    assert (replica / "code.py").exists()
    assert (replica / "sub" / "data.csv").exists()


def test_locked_file_copy_failure_propagates(tmp_path, monkeypatch):
    """A copy failure must reach the caller, not vanish.

    The Run page surfaces it; had the copy error been swallowed the operator
    saw a Launch button that "did nothing".
    """
    import shutil as _shutil

    ws = _make_workspace(tmp_path)

    def boom(_src, _dst, **_kw):
        raise _shutil.Error(["[WinError 32] file in use by another process"])

    monkeypatch.setattr(_shutil, "copytree", boom)
    with pytest.raises(_shutil.Error, match="WinError 32"):
        launch_study_batch(
            _session_state(ws),
            RecordingManager(),
            ws,
            2,
            init_mode="copy",
        )


def test_partial_failure_cancels_enqueued_jobs_and_reports_compensation(tmp_path):
    ws = _make_workspace(tmp_path)
    parent = tmp_path / "study_ws_studies"

    class FailingManager(RecordingManager):
        def __init__(self):
            super().__init__()
            self.cancelled: list[str] = []

        def enqueue_job(self, job_type, config, meta=None):
            if len(self.enqueued) >= 1:
                raise RuntimeError("enqueue exploded")
            return super().enqueue_job(job_type, config, meta)

        def cancel_job(self, job_id):
            self.cancelled.append(job_id)
            return True

    manager = FailingManager()
    with pytest.raises(RuntimeError) as caught:
        launch_study_batch(_session_state(ws), manager, ws, 3, init_mode="copy")

    error = caught.value
    assert type(error).__name__ == "BatchLaunchError"
    assert error.batch_id.startswith("study_")
    assert error.job_ids == ["job_1"]
    assert error.cancelled_job_ids == ["job_1"]
    assert error.cancel_failures == {}
    assert manager.cancelled == ["job_1"]
    # The cancelled job keeps its workspace for inspection; only replicas
    # that never reached the queue are removed.
    leftovers = sorted(p.name for p in parent.iterdir())
    assert len(leftovers) == 1 and leftovers[0].endswith("_r1")


def test_partial_failure_reports_every_compensation_failure(tmp_path):
    ws = _make_workspace(tmp_path)
    parent = tmp_path / "study_ws_studies"

    class FailingManager(RecordingManager):
        def __init__(self):
            super().__init__()
            self.cancel_attempts: list[str] = []

        def enqueue_job(self, job_type, config, meta=None):
            if len(self.enqueued) >= 2:
                raise RuntimeError("enqueue exploded")
            return super().enqueue_job(job_type, config, meta)

        def cancel_job(self, job_id):
            self.cancel_attempts.append(job_id)
            if job_id == "job_1":
                raise OSError("cancel store unavailable")
            return False

    manager = FailingManager()
    with pytest.raises(batch_module.BatchLaunchError) as caught:
        launch_study_batch(_session_state(ws), manager, ws, 3, init_mode="copy")

    error = caught.value
    assert manager.cancel_attempts == ["job_1", "job_2"]
    assert error.cancelled_job_ids == []
    assert error.cancel_failures == {
        "job_1": "OSError: cancel store unavailable",
        "job_2": "cancel request was not accepted",
    }
    leftovers = sorted(p.name for p in parent.iterdir())
    assert len(leftovers) == 2
    assert {name.rsplit("_", 1)[-1] for name in leftovers} == {"r1", "r2"}


def test_batch_failure_message_explains_compensation_and_retry():
    cause = RuntimeError("enqueue exploded")
    compensated = batch_module.BatchLaunchError(
        "study_test",
        ["job_1"],
        cause,
        cancelled_job_ids=["job_1"],
    )
    message = batch_module.batch_launch_failure_message(compensated)
    assert "cancellation was requested for 1 started job(s)" in message
    assert "job_1" in message
    assert "Retry after they reach a terminal state" in message
    assert "enqueue exploded" in message

    uncompensated = batch_module.BatchLaunchError(
        "study_test",
        ["job_1", "job_2"],
        cause,
        cancelled_job_ids=["job_1"],
        cancel_failures={"job_2": "cancel request was not accepted"},
    )
    message = batch_module.batch_launch_failure_message(uncompensated)
    assert "job_2" in message
    assert "could not be cancelled" in message
    assert "Stop them before retrying" in message
