"""supervisor/llm_supervisor.py — OpenAI-powered judge."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from openai import OpenAI

from .protocol import Protocol

logger = logging.getLogger(__name__)


@dataclass
class ProtocolViolation:
    section: str
    description: str
    suggestion: str


@dataclass
class AlignmentResult:
    aligned: bool
    violations: list[ProtocolViolation] = field(default_factory=list)
    reinforcement_message: str = ""


_DONE_PHRASES = [
    "all targets met",
    "all targets are met",
    "targets achieved",
    "task complete",
    "task is complete",
    "objectives met",
    "protocol satisfied",
]


@dataclass
class SupervisorVerdict:
    raw: str
    all_targets_met: bool
    feedback: str


@dataclass
class StepContext:
    current_step: int = 0
    total_steps_estimate: int = 5
    phase: str = "unknown"
    completed_phases: list[str] = field(default_factory=list)


class LLMSupervisor:
    """
    Wraps an OpenAI chat client. Protocol is the system prompt;
    conversation history gives the supervisor persistent memory.
    """

    _MAX_HISTORY_TURNS = 40

    def __init__(
        self,
        protocol: Protocol,
        workspace: Path,
        model: str,
        extra_system: str = "",
    ):
        self._client = OpenAI()
        self._model = model
        self._system = protocol.as_system_prompt(workspace) + extra_system
        self._history: list[dict] = []

    def judge(self, opencode_output: str) -> SupervisorVerdict:
        msg = (
            "opencode just produced the following output. "
            "Evaluate it against the protocol.\n"
            "If ALL targets are met say 'all targets met'.\n"
            "Otherwise give clear, actionable feedback.\n\n"
            f"--- opencode output ---\n{opencode_output}\n--- end ---"
        )
        return self._chat(msg)

    def judge_with_step_context(
        self, opencode_output: str, step_context: StepContext
    ) -> SupervisorVerdict:
        phases_str = ", ".join(step_context.completed_phases) if step_context.completed_phases else "none"
        msg = (
            "opencode just produced the following output. "
            "Evaluate it against the protocol.\n"
            "If ALL targets are met say 'all targets met'.\n"
            "Otherwise give clear, actionable feedback.\n\n"
            "--- Step Context ---\n"
            f"Current step: {step_context.current_step}/{step_context.total_steps_estimate}\n"
            f"Current phase: {step_context.phase}\n"
            f"Completed phases: {phases_str}\n\n"
            "--- opencode output ---\n"
            f"{opencode_output}\n--- end ---\n\n"
            "Focus your feedback on the current phase and remaining work."
        )
        return self._chat(msg)

    def ask_for_compaction_instructions(self) -> SupervisorVerdict:
        msg = (
            "The opencode agent's context window is nearly full. "
            "Generate instructions for it to:\n"
            "1. Keep only the latest version of every file.\n"
            "2. Retain any foundational/fallback code it may reference.\n"
            "3. Write summary.md to the workspace with: current status, "
            "key decisions, remaining tasks and future directions.\n"
            "Output ONLY the instruction text to send to opencode."
        )
        return self._chat(msg)

    def ask_for_deletion_permission(
        self, candidates: list[str], workspace: Path
    ) -> SupervisorVerdict:
        if not candidates:
            msg = (
                "The opencode agent's context window is nearly full. "
                "Generate instructions for it to:\n"
                "1. Keep only the latest version of every file.\n"
                "2. Retain any foundational/fallback code it may reference.\n"
                "3. Write summary.md to the workspace with: current status, "
                "key decisions, remaining tasks and future directions.\n"
                "Output ONLY the instruction text to send to opencode."
            )
            return self._chat(msg)

        file_list = "\n".join(f"  - {f}" for f in candidates)
        msg = (
            "The opencode agent's context window is nearly full. "
            "Before compacting the context, you MUST address the following cleanup:\n\n"
            "## DELETION PERMISSION\n\n"
            "The supervisor has identified the following files as outdated, unused, or safe to delete:\n\n"
            f"{file_list}\n\n"
            "You are granted permission to DELETE these files ONLY if:\n"
            "1. The file is confirmed outdated (e.g., backup files, old versions, temp files)\n"
            "2. The file is unused by any current code\n"
            "3. The file is not a core module or essential configuration\n\n"
            "Generate instructions for the agent to:\n"
            "1. Review the file list above and DELETE only the confirmed outdated/unused files\n"
            "2. Keep all core modules and essential files\n"
            "3. Keep only the latest version of every file\n"
            "4. Retain any foundational/fallback code it may reference\n"
            "5. Write summary.md to the workspace with: current status, "
            "key decisions, remaining tasks and future directions\n\n"
            "Output ONLY the instruction text to send to opencode, "
            "including explicit permission to delete the listed files."
        )
        return self._chat(msg)

    def report_final_status(
        self, reason: str, opencode_output: str, workspace: Path
    ) -> str:
        msg = (
            f"Run ending. Reason: {reason}\n\n"
            "Write a final report:\n"
            "1. What has been completed.\n"
            "2. Last known bug / blocker.\n"
            "3. Remaining undone tasks.\n\n"
            f"Latest opencode output:\n{opencode_output}"
        )
        return self._chat(msg).raw

    def generate_suggestions(
        self,
        opencode_output: str,
        current_summary: str = "",
        step_context: StepContext | None = None,
    ) -> str:
        context_info = ""
        if step_context:
            phases_str = ", ".join(step_context.completed_phases) if step_context.completed_phases else "none"
            context_info = (
                f"Current step: {step_context.current_step}/{step_context.total_steps_estimate}\n"
                f"Current phase: {step_context.phase}\n"
                f"Completed phases: {phases_str}\n\n"
            )

        summary_context = (
            f"\n\nCurrent implementation summary:\n{current_summary}"
            if current_summary
            else ""
        )

        msg = (
            "Based on the opencode output below and the current implementation status,\n"
            "generate actionable suggestions for improving the code or approach.\n"
            "Focus on:\n"
            "1. Code quality improvements\n"
            "2. Potential bugs or edge cases\n"
            "3. Performance optimizations\n"
            "4. Better patterns or practices\n"
            "5. Missing tests or error handling\n\n"
            f"--- Step Context ---\n{context_info}"
            f"--- opencode output ---\n{opencode_output}\n--- end ---{summary_context}\n\n"
            "Output ONLY the suggestions in a clear, actionable format. "
            "If no suggestions are needed, output 'No suggestions at this time.'"
        )
        return self._chat(msg).raw

    def verify_protocol_alignment(
        self,
        opencode_output: str,
        protocol: Protocol,
    ) -> AlignmentResult:
        violations: list[ProtocolViolation] = []
        output_lower = opencode_output.lower()

        restriction_patterns = [
            (
                r"(do not delete|don't delete|never delete|avoid deleting)",
                "Attempting to delete code without permission",
                "Do not delete files unless explicitly instructed in the TARGET section.",
            ),
            (
                r"(rm\s+-rf|del\s+/[sqf]|\$\(\{|sudo\s+)",
                "Suspicious destructive command detected",
                "Destructive commands require explicit permission. Only proceed if the TARGET explicitly requires it.",
            ),
            (
                r"(\.\.\/|\.\.\\|\.\.\%|\.\.\.)",
                "Path traversal attempt detected",
                "All file operations must stay within the workspace directory. Do not access parent directories.",
            ),
            (
                r"(git\s+reset|git\s+rebase|git\s+push\s+--force|git\s+push\s+-f)",
                "Destructive git operation detected",
                "Do not perform destructive git operations (reset, rebase, force push) without explicit permission.",
            ),
            (
                r"(chmod\s+777|chmod\s+000|\$\(whoami\)|eval\s+\$\{)",
                "Risky shell operation detected",
                "Avoid risky operations involving permissions, command substitution in eval, or dynamic code execution.",
            ),
        ]

        for pattern, description, suggestion in restriction_patterns:
            if re.search(pattern, output_lower):
                violations.append(
                    ProtocolViolation(
                        section="RESTRICTIONS",
                        description=description,
                        suggestion=suggestion,
                    )
                )

        if protocol.target_section:
            target_keywords = self._extract_keywords(protocol.target_section)
            found_keywords = sum(
                1 for kw in target_keywords if kw.lower() in output_lower
            )
            if target_keywords and found_keywords == 0:
                violations.append(
                    ProtocolViolation(
                        section="TARGET",
                        description="No evidence of target-related activity in output",
                        suggestion=f"Your output should address these target keywords: {', '.join(target_keywords[:5])}",
                    )
                )

        if protocol.restrictions_section:
            restriction_keywords = self._extract_keywords(protocol.restrictions_section)
            for kw in restriction_keywords:
                if kw.lower() in output_lower and any(
                    word in output_lower
                    for word in ["ignore", "skip", "bypass", "violate"]
                ):
                    violations.append(
                        ProtocolViolation(
                            section="RESTRICTIONS",
                            description=f"Potential attempt to ignore restriction keyword: {kw}",
                            suggestion="You must comply with all restrictions listed in the protocol.",
                        )
                    )
                    break

        aligned = len(violations) == 0
        reinforcement_message = (
            self._generate_reinforcement_message(violations) if violations else ""
        )

        return AlignmentResult(
            aligned=aligned,
            violations=violations,
            reinforcement_message=reinforcement_message,
        )

    def _extract_keywords(self, text: str) -> list[str]:
        words = re.findall(r"\b[a-zA-Z]{4,}\b", text)
        stopwords = {
            "that",
            "this",
            "with",
            "from",
            "have",
            "will",
            "been",
            "were",
            "they",
            "their",
            "what",
            "when",
            "your",
            "must",
            "only",
            "also",
            "into",
            "than",
            "then",
            "should",
            "could",
            "would",
            "which",
            "about",
            "after",
            "before",
            "being",
        }
        return [w for w in words if w.lower() not in stopwords][:10]

    def _generate_reinforcement_message(
        self, violations: list[ProtocolViolation]
    ) -> str:
        if not violations:
            return ""

        lines = [
            "\n--- PROTOCOL VIOLATION DETECTED ---\n",
            "The following protocol violations were detected in your output:\n",
        ]

        for i, v in enumerate(violations, 1):
            lines.append(f"{i}. [{v.section}] {v.description}")
            lines.append(f"   Correction: {v.suggestion}\n")

        lines.extend(
            [
                "Please review the protocol sections above and correct your approach.\n",
                "Reminder:\n",
                "  - INPUT section describes the task context you must understand\n",
                "  - TARGET section lists the objectives you must achieve\n",
                "  - RESTRICTIONS section defines boundaries you must not cross\n",
                "Re-read these sections and adjust your actions accordingly.\n",
                "--- END VIOLATION NOTICE ---\n",
            ]
        )

        return "".join(lines)

    # ------------------------------------------------------------------ #

    def _chat(self, user_content: str) -> SupervisorVerdict:
        self._history.append({"role": "user", "content": user_content})

        if len(self._history) > self._MAX_HISTORY_TURNS * 2:
            self._history = self._history[:2] + self._history[4:]

        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": self._system},
                *self._history,
            ],
        )
        reply = response.choices[0].message.content or ""
        self._history.append({"role": "assistant", "content": reply})

        all_met = any(p in reply.lower() for p in _DONE_PHRASES)
        return SupervisorVerdict(raw=reply, all_targets_met=all_met, feedback=reply)
