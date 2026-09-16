"""Detect goal-level stagnation across judged turns.

The intra-turn :class:`~supervisor.analyzers.loop_detector.LoopDetector` watches
tool markers inside one streamed turn; the cross-turn detector watches for
repeated outputs/feedback. This detector adds the missing third signal:
*environment stagnation* — the workspace fingerprint (file-content hashes) does
not change across judged turns, so no matter how busy the agent looks, nothing
it does lands in the codebase.

Pure and structural (no LLM), one instance per run. Following browser-use's
nudge ladder it never blocks directly: it escalates through
nudge -> strategy change -> restart so legitimately long read/test phases are
not punished.
"""

from __future__ import annotations

from dataclasses import dataclass

from supervisor.analyzers.loop_detector import LoopSignal, _normalize_turn


@dataclass
class StagnationSignal:
    """Escalating guidance demand for a stagnant goal loop."""

    stage: int  # 1 = nudge, 2 = force strategy change, 3 = escalate/restart
    stagnant_turns: int
    reason: str

    def as_loop_signal(self) -> LoopSignal:
        return LoopSignal(reason=self.reason, marker=f"stage-{self.stage}")


class WorkspaceStagnationDetector:
    """Track consecutive judged turns with an unchanged workspace fingerprint.

    ``worktree_sig`` is a stable hash of workspace file content; ``output_sig``
    is the agent's normalized prose. A turn counts as stagnant only when the
    worktree is unchanged; an unchanged worktree *plus* a near-identical
    output is flagged explicitly in the reason because it means the agent is
    replaying the same turn without touching files.
    """

    def __init__(
        self,
        *,
        nudge_after: int = 2,
        strategy_after: int = 3,
        escalate_after: int = 4,
    ):
        if not nudge_after < strategy_after < escalate_after:
            raise ValueError("thresholds must be strictly increasing")
        self.nudge_after = nudge_after
        self.strategy_after = strategy_after
        self.escalate_after = escalate_after
        self._last_worktree_sig: str = ""
        self._last_output_sig: str = ""
        self._stagnant_turns = 0
        self._highest_stage_fired = 0

    @property
    def stagnant_turns(self) -> int:
        return self._stagnant_turns

    def reset(self) -> None:
        """Clear the streak after external progress (restart, acceptance)."""
        self._stagnant_turns = 0
        self._last_worktree_sig = ""
        self._last_output_sig = ""
        self._highest_stage_fired = 0

    def record(self, worktree_sig: str, output_sig: str) -> StagnationSignal | None:
        """Record one judged turn; return a signal when a threshold is crossed.

        Each escalation stage fires at most once per stagnation streak (like
        OpenHands' one-time nudge); the streak resets as soon as the workspace
        changes again.
        """
        worktree_sig = (worktree_sig or "").strip()
        output_sig = _normalize_turn(output_sig or "")

        changed = bool(worktree_sig) and worktree_sig != self._last_worktree_sig
        self._last_worktree_sig = worktree_sig
        replayed = (
            not changed
            and bool(output_sig)
            and output_sig == self._last_output_sig
        )
        self._last_output_sig = output_sig

        if changed:
            self._stagnant_turns = 0
            self._highest_stage_fired = 0
            return None

        if not worktree_sig:
            # No fingerprint available (e.g. empty workspace) — stay silent
            # rather than nudging on missing instrumentation.
            return None

        self._stagnant_turns += 1

        stage = 0
        if self._stagnant_turns >= self.escalate_after:
            stage = 3
        elif self._stagnant_turns >= self.strategy_after:
            stage = 2
        elif self._stagnant_turns >= self.nudge_after:
            stage = 1
        if stage == 0 or stage <= self._highest_stage_fired:
            return None
        self._highest_stage_fired = stage

        suffix = (
            " The agent also produced a near-identical output — it is replaying"
            " the same turn without touching files."
            if replayed
            else ""
        )
        reasons = {
            1: (
                f"made no file changes for {self._stagnant_turns} consecutive turns"
                + suffix
            ),
            2: (
                f"changed no files for {self._stagnant_turns} consecutive turns —"
                " the current approach is not landing"
            ),
            3: (
                f"produced no workspace change for {self._stagnant_turns} consecutive"
                " turns — restarting with fixed context"
            ),
        }
        return StagnationSignal(
            stage=stage,
            stagnant_turns=self._stagnant_turns,
            reason=reasons[stage],
        )
