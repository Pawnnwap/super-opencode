"""Detect a stuck agent inside a single streamed turn.

Fed the live tool markers from the stream driver, this spots the agent
thrashing — calling the same tool over and over, cycling between a small set of
tool calls, or firing many tools without ever producing prose — so the
supervisor can kill the run and restart it with a fixed context instead of
burning the whole turn (and tokens) on a loop.

Pure and structural (no LLM): one detector per streamed turn. Tight matching
(same tool *including its args*, consecutive, no intervening text) keeps it from
flagging legitimately repeated work like rerunning the test suite across edits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class LoopSignal:
    reason: str  # human-readable why
    marker: str  # the offending marker / cycle


_WS = re.compile(r"\s+")
_DIGITS = re.compile(r"\d+")


def _normalize_turn(text: str) -> str:
    """Stable signature of a turn: lowercase, digits masked (so a varying
    count like '3 failing' vs '5 failing' matches), whitespace collapsed."""
    t = (text or "").lower()
    t = _DIGITS.sub("#", t)
    t = _WS.sub(" ", t).strip()
    return t[:400]


def _short(text: str, n: int = 80) -> str:
    s = " ".join((text or "").split())
    return s[:n] + ("…" if len(s) > n else "")


class LoopDetector:
    def __init__(
        self,
        *,
        max_consecutive: int = 6,
        max_cycle_repeats: int = 4,
        max_tools_without_text: int = 25,
        window: int = 40,
    ):
        self.max_consecutive = max_consecutive
        self.max_cycle_repeats = max_cycle_repeats
        self.max_tools_without_text = max_tools_without_text
        self.window = window
        self._recent: list[str] = []
        self._last: str | None = None
        self._consecutive = 0
        self._tools_since_text = 0
        self._fired = False

    def record_text(self) -> None:
        """A prose (text) event means the agent is making progress."""
        self._tools_since_text = 0

    def record_tool(self, marker: str) -> LoopSignal | None:
        """Record a tool marker; return a signal if a loop is detected (once)."""
        if self._fired:
            return None

        key = (marker or "").strip().lower()
        self._tools_since_text += 1

        if key == self._last:
            self._consecutive += 1
        else:
            self._consecutive = 1
            self._last = key

        self._recent.append(key)
        if len(self._recent) > self.window:
            self._recent.pop(0)

        if self._consecutive >= self.max_consecutive:
            return self._fire("repeated the same tool call", marker)

        cycle = self._detect_cycle()
        if cycle is not None:
            return self._fire("cycled between the same tool calls", cycle)

        if self._tools_since_text >= self.max_tools_without_text:
            return self._fire("ran many tools without producing any output", marker)

        return None

    def _detect_cycle(self) -> str | None:
        """Find a period-2..4 pattern repeated ``max_cycle_repeats`` times."""
        n = len(self._recent)
        for period in (2, 3, 4):
            need = period * self.max_cycle_repeats
            if n < need:
                continue
            window = self._recent[-need:]
            pattern = window[:period]
            if len(set(pattern)) <= 1:
                continue  # all identical -> the consecutive check owns this
            if all(window[i] == pattern[i % period] for i in range(need)):
                return " -> ".join(pattern)
        return None

    def _fire(self, reason: str, marker: str) -> LoopSignal:
        self._fired = True
        return LoopSignal(reason=reason, marker=marker)


class CrossTurnLoopDetector:
    """Detect supervisor<->agent oscillation ACROSS turns.

    Fed (agent output, supervisor feedback) once per judged turn. Fires when
    the same normalized output OR the same normalized feedback recurs
    ``max_repeats`` times within a sliding ``window`` — i.e. the agent keeps
    producing the same result, or the supervisor keeps giving the same fix and
    it never lands. Unlike the intra-turn detector this is long-lived (one per
    run) and re-arms after firing, so a loop that survives a restart fires
    again and eventually trips the restart cap.
    """

    def __init__(self, *, max_repeats: int = 3, window: int = 6, min_len: int = 8):
        self.max_repeats = max_repeats
        self.window = window
        self.min_len = min_len
        self._outputs: list[str] = []
        self._feedbacks: list[str] = []

    def record(self, output: str, feedback: str) -> LoopSignal | None:
        sig_o = _normalize_turn(output)
        sig_f = _normalize_turn(feedback)

        if len(sig_o) >= self.min_len:
            self._outputs.append(sig_o)
            if len(self._outputs) > self.window:
                self._outputs.pop(0)
        if len(sig_f) >= self.min_len:
            self._feedbacks.append(sig_f)
            if len(self._feedbacks) > self.window:
                self._feedbacks.pop(0)

        if len(sig_o) >= self.min_len and self._outputs.count(sig_o) >= self.max_repeats:
            return self._fire("kept producing the same result across turns", output)
        if len(sig_f) >= self.min_len and self._feedbacks.count(sig_f) >= self.max_repeats:
            return self._fire("kept getting the same feedback across turns", feedback)
        return None

    def _fire(self, reason: str, text: str) -> LoopSignal:
        self._outputs.clear()
        self._feedbacks.clear()
        return LoopSignal(reason=reason, marker=_short(text))
