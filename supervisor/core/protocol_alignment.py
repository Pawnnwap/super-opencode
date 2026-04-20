from __future__ import annotations

import re
from dataclasses import dataclass, field

from supervisor.protocols.protocol import Protocol


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


def verify_protocol_alignment(
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
                ),
            )

    if protocol.target_section:
        target_keywords = extract_keywords(protocol.target_section)
        found_keywords = sum(1 for kw in target_keywords if kw.lower() in output_lower)
        if target_keywords and found_keywords == 0:
            violations.append(
                ProtocolViolation(
                    section="TARGET",
                    description="No evidence of target-related activity in output",
                    suggestion=(
                        "Your output should address these target keywords: "
                        + ", ".join(target_keywords[:5])
                    ),
                ),
            )

    if protocol.restrictions_section:
        restriction_keywords = extract_keywords(protocol.restrictions_section)
        for keyword in restriction_keywords:
            if keyword.lower() in output_lower and any(
                word in output_lower
                for word in ["ignore", "skip", "bypass", "violate"]
            ):
                violations.append(
                    ProtocolViolation(
                        section="RESTRICTIONS",
                        description=(
                            "Potential attempt to ignore restriction keyword: "
                            f"{keyword}"
                        ),
                        suggestion="You must comply with all restrictions listed in the protocol.",
                    ),
                )
                break

    return AlignmentResult(
        aligned=not violations,
        violations=violations,
        reinforcement_message=(
            generate_reinforcement_message(violations) if violations else ""
        ),
    )


def extract_keywords(text: str) -> list[str]:
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
    return [word for word in words if word.lower() not in stopwords][:10]


def generate_reinforcement_message(violations: list[ProtocolViolation]) -> str:
    if not violations:
        return ""

    violations_text = ""
    for index, violation in enumerate(violations, 1):
        violations_text += f"{index}. [{violation.section}] {violation.description}\n"
        violations_text += f"   Correction: {violation.suggestion}\n\n"

    from supervisor.prompts import PROTOCOL_VIOLATION_TEMPLATE

    return PROTOCOL_VIOLATION_TEMPLATE.format(violations=violations_text)
