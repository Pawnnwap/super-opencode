"""supervisor/protocol_analyzer.py — Analyze protocol files and provide quality feedback."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from .protocol import Protocol, parse_protocol_text


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class ValidationIssue:
    severity: Severity
    section: str
    message: str
    suggestion: str = ""


@dataclass
class SectionScore:
    clarity: float
    testability: float
    completeness: float

    @property
    def overall(self) -> float:
        return round((self.clarity + self.testability + self.completeness) / 3, 2)


@dataclass
class ProtocolAnalysis:
    input_score: SectionScore
    target_score: SectionScore
    restrictions_score: SectionScore
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def overall_score(self) -> float:
        return round(
            (self.input_score.overall + self.target_score.overall + self.restrictions_score.overall) / 3,
            2,
        )

    @property
    def quality_rating(self) -> str:
        s = self.overall_score
        if s >= 0.9:
            return "excellent"
        elif s >= 0.75:
            return "good"
        elif s >= 0.5:
            return "fair"
        else:
            return "poor"

    def to_dict(self) -> dict:
        return {
            "overall_score": self.overall_score,
            "quality_rating": self.quality_rating,
            "input": {
                "clarity": self.input_score.clarity,
                "testability": self.input_score.testability,
                "completeness": self.input_score.completeness,
                "overall": self.input_score.overall,
            },
            "target": {
                "clarity": self.target_score.clarity,
                "testability": self.target_score.testability,
                "completeness": self.target_score.completeness,
                "overall": self.target_score.overall,
            },
            "restrictions": {
                "clarity": self.restrictions_score.clarity,
                "testability": self.restrictions_score.testability,
                "completeness": self.restrictions_score.completeness,
                "overall": self.restrictions_score.overall,
            },
            "issues": [
                {
                    "severity": issue.severity.value,
                    "section": issue.section,
                    "message": issue.message,
                    "suggestion": issue.suggestion,
                }
                for issue in self.issues
            ],
        }

    def to_markdown(self) -> str:
        lines = [
            "# Protocol Quality Analysis",
            "",
            f"**Overall Score:** {self.overall_score:.0%} ({self.quality_rating})",
            "",
            "## Section Scores",
            "",
            f"| Section | Clarity | Testability | Completeness | Overall |",
            f"|---------|---------|-------------|--------------|---------|",
            f"| INPUT | {self.input_score.clarity:.0%} | {self.input_score.testability:.0%} | {self.input_score.completeness:.0%} | {self.input_score.overall:.0%} |",
            f"| TARGET | {self.target_score.clarity:.0%} | {self.target_score.testability:.0%} | {self.target_score.completeness:.0%} | {self.target_score.overall:.0%} |",
            f"| RESTRICTIONS | {self.restrictions_score.clarity:.0%} | {self.restrictions_score.testability:.0%} | {self.restrictions_score.completeness:.0%} | {self.restrictions_score.overall:.0%} |",
            "",
        ]

        if self.issues:
            lines.append("## Issues Found")
            lines.append("")
            for issue in self.issues:
                icon = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}[issue.severity.value]
                lines.append(f"{icon} **[{issue.section}]** {issue.message}")
                if issue.suggestion:
                    lines.append(f"   - *Suggestion:* {issue.suggestion}")
                lines.append("")

        return "\n".join(lines)


_VAGUE_WORDS = {
    "somehow",
    "maybe",
    "possibly",
    "perhaps",
    "etc",
    "and so on",
    "something",
    "things",
    "stuff",
    "some",
    "any",
    "various",
    "appropriate",
    "suitable",
    "reasonable",
    "good",
    "nice",
    "better",
    "improve",
    "enhance",
}

_ACTION_VERBS = {
    "create",
    "build",
    "add",
    "implement",
    "write",
    "develop",
    "generate",
    "design",
    "set up",
    "configure",
    "install",
    "update",
    "modify",
    "change",
    "fix",
    "refactor",
    "optimize",
    "test",
    "verify",
    "validate",
    "ensure",
    "check",
    "remove",
    "delete",
    "replace",
    "integrate",
}

_TESTABILITY_KEYWORDS = {
    "test",
    "pass",
    "assert",
    "verify",
    "validate",
    "check",
    "must",
    "should",
    "required",
    "expected",
    "success",
    "error",
    "fail",
    "criteria",
    "output",
    "result",
    "run",
    "pytest",
    "unittest",
}


class ProtocolAnalyzer:
    """Analyzes protocol files and provides structured quality feedback."""

    def __init__(self, min_section_length: int = 20, strict_mode: bool = False):
        self._min_section_length = min_section_length
        self._strict_mode = strict_mode

    def analyze(self, protocol: Protocol) -> ProtocolAnalysis:
        issues: list[ValidationIssue] = []

        input_score = self._score_section("INPUT", protocol.input_section, issues)
        target_score = self._score_section("TARGET", protocol.target_section, issues)
        restrictions_score = self._score_section("RESTRICTIONS", protocol.restrictions_section, issues)

        self._validate_cross_section(protocol, issues)

        return ProtocolAnalysis(
            input_score=input_score,
            target_score=target_score,
            restrictions_score=restrictions_score,
            issues=issues,
        )

    def analyze_text(self, text: str) -> ProtocolAnalysis:
        protocol = parse_protocol_text(text)
        return self.analyze(protocol)

    def _score_section(
        self, section_name: str, text: str, issues: list[ValidationIssue]
    ) -> SectionScore:
        if not text.strip():
            issues.append(
                ValidationIssue(
                    severity=Severity.ERROR,
                    section=section_name,
                    message=f"{section_name} section is empty.",
                    suggestion=f"Add content describing {section_name.lower()} information.",
                )
            )
            return SectionScore(clarity=0.0, testability=0.0, completeness=0.0)

        clarity = self._score_clarity(section_name, text, issues)
        testability = self._score_testability(section_name, text, issues)
        completeness = self._score_completeness(section_name, text, issues)

        return SectionScore(clarity=clarity, testability=testability, completeness=completeness)

    def _score_clarity(
        self, section_name: str, text: str, issues: list[ValidationIssue]
    ) -> float:
        score = 1.0
        text_lower = text.lower()

        # Penalize vague words
        vague_found = [w for w in _VAGUE_WORDS if w in text_lower]
        if vague_found:
            penalty = min(0.15 * len(vague_found), 0.5)
            score -= penalty
            issues.append(
                ValidationIssue(
                    severity=Severity.WARNING,
                    section=section_name,
                    message=f"Contains vague language: {', '.join(vague_found[:5])}",
                    suggestion="Replace vague terms with specific, concrete language.",
                )
            )

        # Penalize very long sentences (>50 words)
        sentences = re.split(r"[.!?\n]+", text)
        long_sentences = [s for s in sentences if len(s.split()) > 50]
        if long_sentences:
            penalty = min(0.1 * len(long_sentences), 0.3)
            score -= penalty
            issues.append(
                ValidationIssue(
                    severity=Severity.WARNING,
                    section=section_name,
                    message=f"Contains {len(long_sentences)} sentence(s) over 50 words.",
                    suggestion="Break long sentences into shorter, more focused statements.",
                )
            )

        # Reward structured content (bullet points, numbered lists)
        bullet_count = len(re.findall(r"^[\s]*[-*]\s", text, re.MULTILINE))
        numbered_count = len(re.findall(r"^[\s]*\d+[.)]\s", text, re.MULTILINE))
        if bullet_count + numbered_count > 0:
            score = min(score + 0.05, 1.0)

        # Penalize if section is too short
        if len(text) < self._min_section_length:
            penalty = 0.3 if self._strict_mode else 0.2
            score -= penalty
            issues.append(
                ValidationIssue(
                    severity=Severity.WARNING,
                    section=section_name,
                    message=f"{section_name} section is very short ({len(text)} chars).",
                    suggestion=f"Add more detail to the {section_name.lower()} section (minimum {self._min_section_length} chars).",
                )
            )

        return round(max(score, 0.0), 2)

    def _score_testability(
        self, section_name: str, text: str, issues: list[ValidationIssue]
    ) -> float:
        text_lower = text.lower()

        # Count testability keywords
        test_kw_count = sum(1 for kw in _TESTABILITY_KEYWORDS if kw in text_lower)

        if section_name == "TARGET":
            # Target section should have high testability
            if test_kw_count == 0:
                issues.append(
                    ValidationIssue(
                        severity=Severity.ERROR,
                        section=section_name,
                        message="TARGET has no testability keywords (test, verify, pass, assert, etc.).",
                        suggestion="Add concrete acceptance criteria with measurable outcomes.",
                    )
                )
                return 0.1

            # Check for numbered deliverables
            numbered_items = re.findall(r"^\s*\d+[.)]\s", text, re.MULTILINE)
            if not numbered_items:
                issues.append(
                    ValidationIssue(
                        severity=Severity.WARNING,
                        section=section_name,
                        message="TARGET has no numbered deliverables.",
                        suggestion="Use numbered list items (1. 2. 3.) for clear deliverables.",
                    )
                )
                return min(0.5, test_kw_count * 0.1)

            return round(min(1.0, test_kw_count * 0.15 + len(numbered_items) * 0.1), 2)

        elif section_name == "RESTRICTIONS":
            # Restrictions should be clear prohibitions
            prohibition_patterns = [
                r"do not",
                r"don't",
                r"never",
                r"must not",
                r"shall not",
                r"avoid",
                r"no\s+\w+",
                r"forbid",
                r"prohibit",
            ]
            prohibition_count = sum(
                len(re.findall(p, text_lower)) for p in prohibition_patterns
            )

            if prohibition_count == 0:
                issues.append(
                    ValidationIssue(
                        severity=Severity.WARNING,
                        section=section_name,
                        message="RESTRICTIONS has no clear prohibition statements.",
                        suggestion='Use explicit prohibitions like "Do not ..." or "Never ...".',
                    )
                )
                return 0.3

            return round(min(1.0, prohibition_count * 0.15), 2)

        else:  # INPUT
            # INPUT testability is about specificity
            specificity_score = 0.5  # baseline
            if re.search(r"\d+", text):
                specificity_score += 0.2
            if re.search(r"(file|path|dir|folder|module)", text_lower):
                specificity_score += 0.15
            if re.search(r"(python|javascript|java|go|rust|c\+\+)", text_lower):
                specificity_score += 0.15

            return round(min(1.0, specificity_score), 2)

    def _score_completeness(
        self, section_name: str, text: str, issues: list[ValidationIssue]
    ) -> float:
        score = 1.0
        text_lower = text.lower()

        word_count = len(text.split())

        if section_name == "INPUT":
            # INPUT should describe existing state
            has_context = any(
                kw in text_lower
                for kw in ["exists", "given", "current", "has", "contains", "located", "directory", "file", "repo", "project"]
            )
            if not has_context:
                score -= 0.3
                issues.append(
                    ValidationIssue(
                        severity=Severity.INFO,
                        section=section_name,
                        message="INPUT may lack context about the existing state.",
                        suggestion="Describe what files/directories exist and the current state.",
                    )
                )

            if word_count < 5:
                score -= 0.3
                issues.append(
                    ValidationIssue(
                        severity=Severity.WARNING,
                        section=section_name,
                        message=f"INPUT is very brief ({word_count} words).",
                        suggestion="Provide more context about the existing codebase or environment.",
                    )
                )

        elif section_name == "TARGET":
            # TARGET should have multiple deliverables
            bullet_count = len(re.findall(r"^[\s]*[-*]\s", text, re.MULTILINE))
            numbered_count = len(re.findall(r"^[\s]*\d+[.)]\s", text, re.MULTILINE))
            total_items = bullet_count + numbered_count

            if total_items == 0 and word_count < 10:
                score -= 0.4
                issues.append(
                    ValidationIssue(
                        severity=Severity.ERROR,
                        section=section_name,
                        message="TARGET has no clear deliverable items.",
                        suggestion="List specific, numbered deliverables the agent must produce.",
                    )
                )
            elif total_items == 1:
                score -= 0.2
                issues.append(
                    ValidationIssue(
                        severity=Severity.INFO,
                        section=section_name,
                        message="TARGET has only one deliverable.",
                        suggestion="Consider adding more deliverables for a comprehensive protocol.",
                    )
                )

            # Check for vague targets
            if "improve" in text_lower and "test" not in text_lower:
                score -= 0.2
                issues.append(
                    ValidationIssue(
                        severity=Severity.WARNING,
                        section=section_name,
                        message='TARGET uses "improve" without measurable criteria.',
                        suggestion='Specify what "improved" means with concrete metrics or test criteria.',
                    )
                )

        elif section_name == "RESTRICTIONS":
            # RESTRICTIONS should cover key safety areas
            safety_areas = {
                "files": any(w in text_lower for w in ["file", "delete", "modify", "remove"]),
                "directories": any(w in text_lower for w in ["directory", "folder", "dir", "path"]),
                "packages": any(w in text_lower for w in ["package", "install", "dependency", "pip"]),
                "security": any(w in text_lower for w in ["security", "secret", "key", "password", "credential"]),
            }

            covered = sum(1 for v in safety_areas.values() if v)
            if covered < 2:
                score -= 0.2
                missing = [k for k, v in safety_areas.items() if not v]
                issues.append(
                    ValidationIssue(
                        severity=Severity.INFO,
                        section=section_name,
                        message=f"RESTRICTIONS could cover more safety areas (missing: {', '.join(missing)}).",
                        suggestion="Consider adding restrictions for file operations, directories, and dependencies.",
                    )
                )

            bullet_count = len(re.findall(r"^[\s]*[-*]\s", text, re.MULTILINE))
            if bullet_count == 0 and word_count < 10:
                score -= 0.2
                issues.append(
                    ValidationIssue(
                        severity=Severity.WARNING,
                        section=section_name,
                        message="RESTRICTIONS has no bullet-point rules.",
                        suggestion="List restrictions as clear bullet points for easy parsing.",
                    )
                )

        return round(max(score, 0.0), 2)

    def _validate_cross_section(
        self, protocol: Protocol, issues: list[ValidationIssue]
    ) -> None:
        """Validate consistency across protocol sections."""
        # Check if restrictions mention things from target (potential contradictions)
        target_lower = protocol.target_section.lower()
        restrict_lower = protocol.restrictions_section.lower()

        # If target says to delete/remove but restrictions forbid it
        target_deletes = bool(re.search(r"(delete|remove|rm)\b", target_lower))
        restrict_no_delete = bool(re.search(r"(do not|don't|never|must not|shall not)\s+.*?(delete|remove|rm)\b", restrict_lower))

        if target_deletes and restrict_no_delete:
            issues.append(
                ValidationIssue(
                    severity=Severity.ERROR,
                    section="RESTRICTIONS",
                    message="TARGET mentions deleting/removing but RESTRICTIONS forbid it.",
                    suggestion="Resolve this contradiction: either remove deletion from TARGET or allow it in RESTRICTIONS.",
                )
            )

        # Check if target says to install but restrictions forbid it
        target_installs = bool(re.search(r"(install|pip|npm|apt)\b", target_lower))
        restrict_no_install = bool(re.search(r"(do not|don't|never|must not|shall not)\s+.*?(install|add|use)\b", restrict_lower))

        if target_installs and restrict_no_install:
            issues.append(
                ValidationIssue(
                    severity=Severity.ERROR,
                    section="RESTRICTIONS",
                    message="TARGET mentions installing but RESTRICTIONS forbid it.",
                    suggestion="Resolve this contradiction: either remove installation from TARGET or allow it in RESTRICTIONS.",
                )
            )

    def validate_text(self, text: str) -> list[ValidationIssue]:
        """Validate protocol text and return issues without full scoring."""
        issues: list[ValidationIssue] = []
        try:
            protocol = parse_protocol_text(text)
        except ValueError as e:
            issues.append(
                ValidationIssue(
                    severity=Severity.ERROR,
                    section="STRUCTURE",
                    message=str(e),
                    suggestion="Ensure protocol has ## INPUT, ## TARGET, and ## RESTRICTIONS headings.",
                )
            )
            return issues

        # Run validation checks
        self._score_section("INPUT", protocol.input_section, issues)
        self._score_section("TARGET", protocol.target_section, issues)
        self._score_section("RESTRICTIONS", protocol.restrictions_section, issues)
        self._validate_cross_section(protocol, issues)

        return issues
