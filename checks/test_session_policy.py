"""Session policy: judge every turn, then rotate on cadence or high water.

The engines (opencode/codex/pi) compact their own context, so the supervisor
no longer compacts on the agent's behalf. What remains is a post-judgement
session policy. The regression this file pins: the old pre-judgement
compaction branch ran BEFORE ``_do_judgement`` and dropped the turn's output
unjudged whenever context crossed 60%.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from supervisor.core.loop_base import BaseLoop, LoopState
from supervisor.monitoring.session_tracker import SessionTracker
from supervisor.utils.config import SupervisorConfig


def _loop(tmp_path: Path, fraction: float) -> BaseLoop:
    protocol = tmp_path / "protocol.md"
    protocol.write_text("# PROTOCOL\n\n## TARGET\n\nReach 9 AB gates.\n", encoding="utf-8")
    loop = BaseLoop(SupervisorConfig(workspace=tmp_path, protocol_path=protocol))
    loop.ctx_monitor = SessionTracker(threshold=0.60, max_tokens=1000)
    loop.ctx_monitor.update(int(fraction * 1000))
    return loop


def _progress():
    return SimpleNamespace(
        current_step=0,
        total_steps_estimate=0,
        percentage=0.0,
        phase=SimpleNamespace(name="UNKNOWN"),
        completed_phases=[],
    )


class _Runner:
    """Minimal runner double: one turn of output, no loop, no steps."""

    last_looped = (False, "")
    estimated_context_tokens = 0
    _session_active = False

    def get_step_progress(self):
        return _progress()

    def read_output(self):
        return "", False

    def get_files_read(self):
        return []

    def get_step_events(self, output):
        return []

    def is_continuation_enabled(self):
        return False

    def enable_continuation(self, enabled):
        pass

    def mark_session_active(self):
        self._session_active = True


class _Supervisor:
    def __init__(self):
        self.rolled = 0
        self.compacted = 0

    def rollup_history(self):
        self.rolled += 1
        return 0

    def compact_history(self):
        self.compacted += 1


def test_turn_above_advisory_threshold_is_still_judged(tmp_path):
    """Regression: 60%+ context must not skip judgement (the dropped-output bug)."""
    loop = _loop(tmp_path, 0.70)
    loop.runner = _Runner()
    loop.supervisor = _Supervisor()
    judged: list[str] = []

    def _judge(output):
        judged.append(output)
        loop._state = LoopState.ENDED_SUCCESS
        yield from []

    loop._do_judgement = _judge
    loop._restart_from_summary_output = lambda last_output: iter(())

    list(loop._run_loop("the turn output", False))

    assert judged == ["the turn output"]


def test_cadence_mark_rotates_the_session(tmp_path):
    loop = _loop(tmp_path, 0.55)
    loop.runner = _Runner()
    loop.supervisor = _Supervisor()
    restarts: list[str] = []
    loop._restart_from_summary_output = lambda last_output: restarts.append(last_output) or iter(())

    list(loop._handle_session_continuity())

    assert restarts == [""]
    assert loop.supervisor.rolled == 1
    assert loop.supervisor.compacted == 0


def test_high_water_mark_compacts_then_rotates(tmp_path):
    loop = _loop(tmp_path, 0.92)
    loop.runner = _Runner()
    loop.supervisor = _Supervisor()
    restarts: list[str] = []
    loop._restart_from_summary_output = lambda last_output: restarts.append(last_output) or iter(())

    list(loop._handle_session_continuity())

    assert restarts == [""]
    assert loop.supervisor.compacted == 1


def test_below_cadence_mark_keeps_the_session(tmp_path):
    loop = _loop(tmp_path, 0.30)
    loop.runner = _Runner()
    loop.supervisor = _Supervisor()
    restarts: list[str] = []
    loop._restart_from_summary_output = lambda last_output: restarts.append(last_output) or iter(())

    list(loop._handle_session_continuity())

    assert restarts == []
    assert loop.runner._session_active is True
    assert loop.supervisor.rolled == 0
