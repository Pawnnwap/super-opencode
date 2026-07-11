from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from supervisor.analyzers.codebase_analyzer import snapshot_codebase
from supervisor.core.loop_base import LoopState
from supervisor.core.self_evolution_loop import SelfEvolutionLoop
from supervisor.core.target_state import initialise_target_state, read_staged_improvements
from supervisor.runners.test_runner import RunTestResult
from supervisor.workspace.workspace_archiver import WorkspaceArchiver


class _RegressionTests:
    def run(self) -> RunTestResult:
        return RunTestResult(0, 1, 0, 0.01, "1 failed", 1)


class _Guard:
    def sanitize_message(self, message: str):
        return message, []


class _Runner:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, message: str):
        self.messages.append(message)
        return iter(())


def test_regression_candidate_is_archived_and_recorded_before_rollback(tmp_path):
    (tmp_path / "candidate.py").write_text("value = 1\n", encoding="utf-8")
    accepted_snapshot = snapshot_codebase(tmp_path)
    (tmp_path / "candidate.py").write_text("value = 2\n", encoding="utf-8")
    initialise_target_state(tmp_path, target="Fix candidate.py and require pytest to pass.")

    loop = SelfEvolutionLoop.__new__(SelfEvolutionLoop)
    loop.config = SimpleNamespace(workspace=tmp_path)
    loop.protocol = SimpleNamespace(target_section="Fix candidate.py and require pytest to pass.")
    loop.test_runner = _RegressionTests()
    loop._baseline = RunTestResult(1, 0, 0, 0.01, "1 passed", 0)
    loop._last_result = None
    loop._accepted_snapshot = accepted_snapshot
    loop._iteration = 1
    loop.archiver = WorkspaceArchiver(tmp_path)
    loop.guard = _Guard()
    loop.runner = _Runner()
    loop._pending_cross_loop = ""
    loop._rollback = lambda: iter(())

    progress = SimpleNamespace(current_step=1, total_steps_estimate=2)
    list(loop._pre_judge("patched candidate implementation", progress))

    records = read_staged_improvements(tmp_path)
    assert len(records) == 1
    assert records[0]["archive_path"]
    assert Path(records[0]["archive_path"]).is_dir()
    assert "DIRECTION DIVERSITY REQUIRED" in loop.runner.messages[0]


def test_self_evolution_rejects_vague_target_before_agent_starts(tmp_path):
    loop = SelfEvolutionLoop.__new__(SelfEvolutionLoop)
    loop.config = SimpleNamespace(workspace=tmp_path)
    loop.protocol = SimpleNamespace(
        target_section="Improve reliability.",
        restrictions_section="- Do not edit files outside supervisor/.",
    )
    loop._cached_snapshot = snapshot_codebase(tmp_path)
    loop._state = LoopState.RUNNING

    events = list(loop._run())

    assert loop._state == LoopState.ENDED_FAILURE
    assert any(event["level"] == "error" and "TARGET rejected" in event["msg"] for event in events)
