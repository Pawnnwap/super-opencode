"""
supervisor/opencode_step_detector.py

Monitors opencode execution output and detects inner step boundaries,
phase transitions, and progress indicators.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Generator, Optional


class StepPhase(Enum):
    UNKNOWN = auto()
    PLANNING = auto()
    CODING = auto()
    TESTING = auto()
    REVIEW = auto()
    COMPLETING = auto()


@dataclass
class Step:
    step_number: int
    phase: StepPhase
    content: str
    timestamp_offset: int = 0

    @property
    def label(self) -> str:
        phase_map = {
            StepPhase.PLANNING: "Planning",
            StepPhase.CODING: "Coding",
            StepPhase.TESTING: "Testing",
            StepPhase.REVIEW: "Review",
            StepPhase.COMPLETING: "Completing",
            StepPhase.UNKNOWN: "Unknown",
        }
        return f"[Step {self.step_number}] {phase_map.get(self.phase, 'Unknown')}"

    def to_event(self) -> dict:
        return {
            "level": "step",
            "step_number": self.step_number,
            "phase": self.phase.name.lower(),
            "phase_label": self.label,
            "msg": self.content,
        }


@dataclass
class PhaseTransition:
    from_phase: StepPhase
    to_phase: StepPhase
    content: str
    step_number: int

    def to_event(self) -> dict:
        return {
            "level": "phase_transition",
            "from_phase": self.from_phase.name.lower(),
            "to_phase": self.to_phase.name.lower(),
            "step_number": self.step_number,
            "msg": f"Transition: {self.from_phase.name} → {self.to_phase.name}",
            "detail": self.content,
        }


@dataclass
class StepProgress:
    current_step: int = 0
    total_steps_estimate: int = 5
    phase: StepPhase = StepPhase.UNKNOWN
    completed_phases: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    transitions: list[PhaseTransition] = field(default_factory=list)

    @property
    def percentage(self) -> float:
        if self.total_steps_estimate <= 0:
            return 0.0
        return min((self.current_step / self.total_steps_estimate) * 100, 100.0)

    def to_event(self) -> dict:
        return {
            "level": "step_progress",
            "current_step": self.current_step,
            "total_steps_estimate": self.total_steps_estimate,
            "percentage": self.percentage,
            "phase": self.phase.name.lower(),
            "completed_phases": self.completed_phases,
        }


class OpencodeStepDetector:
    PLANNING_PATTERNS = [
        re.compile(
            r"(?:^|\n)\s*(?:plan|thinking|analyzing|considering|let me|i'll need to)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:^|\n)\s*[-*]\s*(?:first|step \d+|let me|start|begin)", re.IGNORECASE
        ),
        re.compile(
            r"(?:^|\n)\s*(?:\d+[\.\)]\s*(?:first|then|next|after|before|step \d+))",
            re.IGNORECASE,
        ),
        re.compile(r"(?:^|\n)\s*(?:plan:|steps?:|approach:)", re.IGNORECASE),
        re.compile(
            r"i(?:'ll| will| am going to)\s+(?:analyze|review|examine|check)",
            re.IGNORECASE,
        ),
        re.compile(
            r"here(?:'s| is) (?:my|the) (?:plan|approach|strategy)", re.IGNORECASE
        ),
    ]

    CODING_PATTERNS = [
        re.compile(
            r"(?:^|\n)\s*(?:creating|writing|modifying|editing|updating)", re.IGNORECASE
        ),
        re.compile(r"(?:^|\n)\s*(?:file:|path:)", re.IGNORECASE),
        re.compile(
            r"(?:^|\n)\s*(?:```[\w]*|def |class |import |from\s+\w)", re.IGNORECASE
        ),
        re.compile(
            r"(?:^|\n)\s*(?:open|read|write|create)\s+(?:file|directory)", re.IGNORECASE
        ),
        re.compile(
            r"(?:^|\n)\s*(?:will|going to)\s+(?:create|write|modify|edit|add|remove|change)",
            re.IGNORECASE,
        ),
        re.compile(r"((?:^|\n)\s*```[\s\S]*?```)", re.IGNORECASE),
    ]

    TESTING_PATTERNS = [
        re.compile(
            r"(?:^|\n)\s*(?:running|executing|performing)\s+(?:test)", re.IGNORECASE
        ),
        re.compile(
            r"(?:^|\n)\s*(?:test result|passed|failed|error|fail)", re.IGNORECASE
        ),
        re.compile(r"(?:^|\n)\s*pytest", re.IGNORECASE),
        re.compile(
            r"(?:^|\n)\s*(?:assert|verify|check)\s+(?:that|if|=|==)", re.IGNORECASE
        ),
        re.compile(r"(?:^|\n)\s*(?:test|spec)\s*(?::|for)", re.IGNORECASE),
        re.compile(
            r"(?:^|\n)\s*(?:running|executing)\s+(?:command|script)", re.IGNORECASE
        ),
    ]

    REVIEW_PATTERNS = [
        re.compile(
            r"(?:^|\n)\s*(?:review|verify|validate|check)\s+(?:the|this|my)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:^|\n)\s*(?:looks good|verified|confirmed|validated)", re.IGNORECASE
        ),
        re.compile(r"(?:^|\n)\s*(?:all done|complete|finished|done)", re.IGNORECASE),
        re.compile(r"(?:^|\n)\s*(?:final|summary|conclusion)", re.IGNORECASE),
    ]

    STEP_INDICATOR_PATTERNS = [
        re.compile(r"(?:^|\n)\s*(?:step|turn|iteration)\s*(\d+)", re.IGNORECASE),
        re.compile(
            r"(?:^|\n)\s*(?:moving to|now |next )?(?:step|phase)\s*(\d+)", re.IGNORECASE
        ),
        re.compile(r"(?:^|\n)\s*(?:\[ ?\d+ ?/ ?\d+ ?\])", re.IGNORECASE),
        re.compile(
            r"(?:^|\n)\s*(?:progress|advance|continue)\s*:?\s*(?:step|phase)?\s*(\d+)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:^|\n)\s*(?:begin|starting)\s+(?:step|phase)\s*(\d+)", re.IGNORECASE
        ),
    ]

    def __init__(
        self,
        step_callback: Optional[Callable[[Step], None]] = None,
        transition_callback: Optional[Callable[[PhaseTransition], None]] = None,
        progress_callback: Optional[Callable[[StepProgress], None]] = None,
    ):
        self._step_callback = step_callback
        self._transition_callback = transition_callback
        self._progress_callback = progress_callback
        self._progress = StepProgress()
        self._last_phase = StepPhase.UNKNOWN
        self._buffer = ""
        self._char_offset = 0

    @property
    def progress(self) -> StepProgress:
        return self._progress

    def detect_phase(self, text: str) -> StepPhase:
        text_lower = text.lower()

        if self._matches_patterns(text, self.PLANNING_PATTERNS):
            return StepPhase.PLANNING
        if self._matches_patterns(text, self.CODING_PATTERNS):
            return StepPhase.CODING
        if self._matches_patterns(text, self.TESTING_PATTERNS):
            return StepPhase.TESTING
        if self._matches_patterns(text, self.REVIEW_PATTERNS):
            return StepPhase.REVIEW

        return StepPhase.UNKNOWN

    def _matches_patterns(self, text: str, patterns: list[re.Pattern]) -> bool:
        for pattern in patterns:
            if pattern.search(text):
                return True
        return False

    def detect_step_number(self, text: str) -> Optional[int]:
        for pattern in self.STEP_INDICATOR_PATTERNS:
            match = pattern.search(text)
            if match and match.group(1):
                try:
                    return int(match.group(1))
                except (ValueError, IndexError):
                    pass
        return None

    def detect_steps(self, output: str) -> Generator[Step, None, None]:
        lines = output.split("\n")
        current_step = self._progress.current_step
        current_phase = self._progress.phase
        phase_started = False

        for i, line in enumerate(lines):
            self._char_offset += len(line) + 1

            step_num = self.detect_step_number(line)
            if step_num is not None:
                current_step = step_num
                phase_started = True

            detected_phase = self.detect_phase(line)

            if detected_phase != StepPhase.UNKNOWN and detected_phase != current_phase:
                if phase_started and current_phase != StepPhase.UNKNOWN:
                    self._progress.completed_phases.append(current_phase.name.lower())

                self._progress.current_step = current_step
                self._progress.phase = detected_phase

                if self._last_phase != detected_phase:
                    transition = PhaseTransition(
                        from_phase=self._last_phase,
                        to_phase=detected_phase,
                        content=line.strip(),
                        step_number=current_step,
                    )
                    self._progress.transitions.append(transition)
                    if self._transition_callback:
                        self._transition_callback(transition)
                    yield Step(
                        step_number=current_step,
                        phase=detected_phase,
                        content=line.strip(),
                        timestamp_offset=self._char_offset,
                    ).to_event()

                current_phase = detected_phase
                self._last_phase = detected_phase

            if current_phase != StepPhase.UNKNOWN and self._matches_patterns(
                line, self.CODING_PATTERNS
            ):
                if (
                    current_phase == StepPhase.UNKNOWN
                    or current_phase == StepPhase.PLANNING
                ):
                    current_phase = StepPhase.CODING
                    self._progress.phase = StepPhase.CODING

        self._progress.current_step = current_step
        self._progress.phase = current_phase

    def process_output(self, output: str) -> Generator[dict, None, None]:
        self._buffer += output

        for step_event in self.detect_steps(output):
            step = Step(
                step_number=step_event.get("step_number", 0),
                phase=StepPhase[step_event.get("phase", "unknown").upper()],
                content=step_event.get("msg", ""),
                timestamp_offset=step_event.get("timestamp_offset", 0),
            )
            self._progress.steps.append(step)

            if self._step_callback:
                self._step_callback(step)

            yield step_event

        yield self._progress.to_event()
        if self._progress_callback:
            self._progress_callback(self._progress)

    def is_progressing(self) -> bool:
        return (
            self._progress.current_step > 0
            and self._progress.phase != StepPhase.UNKNOWN
        )

    def is_waiting_for_output(self) -> bool:
        return (
            self._progress.phase in (StepPhase.CODING, StepPhase.TESTING)
            and len(self._progress.steps) > 0
            and self._progress.steps[-1].phase == StepPhase.CODING
        )

    def get_activity_state(self) -> str:
        if self._progress.phase == StepPhase.UNKNOWN:
            return "initializing"
        if self.is_waiting_for_output():
            return "waiting_for_output"
        if self.is_progressing():
            return "active_progress"
        return "unknown"

    def reset(self) -> None:
        self._progress = StepProgress()
        self._last_phase = StepPhase.UNKNOWN
        self._buffer = ""
        self._char_offset = 0


def create_step_detector(
    on_step: Optional[Callable[[Step], None]] = None,
    on_transition: Optional[Callable[[PhaseTransition], None]] = None,
    on_progress: Optional[Callable[[StepProgress], None]] = None,
) -> OpencodeStepDetector:
    return OpencodeStepDetector(
        step_callback=on_step,
        transition_callback=on_transition,
        progress_callback=on_progress,
    )
