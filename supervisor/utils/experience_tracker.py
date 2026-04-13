"""supervisor/utils/experience_tracker.py - Track build experience across iterations.

Maintains an experience.md file in the workspace with structured sections:
- What Worked
- What Failed
- Evolution Summaries (structured)

Experience data informs future evolution decisions and step planning.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_HEADER_WORKED = "## What Worked"
_HEADER_FAILED = "## What Failed"
_HEADER_SUMMARIES = "## Evolution Summaries"
_EXPERIENCE_FILE = "experience.md"


@dataclass
class EvolutionSummary:
    goal: str = ""
    outcome: str = ""
    key_changes: list[str] = field(default_factory=list)
    test_baseline: str = ""
    test_final: str = ""
    test_delta: str = ""
    regressions_count: int = 0
    iterations: int = 0
    final_step: int = 0
    total_steps: int = 0
    final_phase: str = ""
    challenges: list[str] = field(default_factory=list)
    solutions: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    archive_path: str = ""
    timestamp: str = ""

    def to_markdown(self) -> str:
        lines = [
            "### " + (self.goal or "Unnamed Evolution") + " [" + self.outcome + "]",
            "- **Iterations:** " + str(self.iterations) + " | **Steps:** " + str(self.final_step) + "/" + str(self.total_steps) + " | **Phase:** " + self.final_phase,
        ]
        if self.test_baseline:
            lines.append("- **Test Baseline:** " + self.test_baseline)
        if self.test_final:
            lines.append("- **Test Final:** " + self.test_final)
        if self.test_delta:
            lines.append("- **Test Delta:** " + self.test_delta)
        if self.regressions_count:
            lines.append("- **Regressions:** " + str(self.regressions_count))
        if self.key_changes:
            lines.append("- **Key Changes:**")
            for change in self.key_changes[:10]:
                lines.append("  - " + change)
        if self.challenges:
            lines.append("- **Challenges:**")
            for c in self.challenges[:5]:
                lines.append("  - " + c)
        if self.solutions:
            lines.append("- **Solutions:**")
            for s in self.solutions[:5]:
                lines.append("  - " + s)
        if self.violations:
            lines.append("- **Violations:**")
            for v in self.violations[:5]:
                lines.append("  - " + v)
        if self.archive_path:
            lines.append("- **Archive:** " + self.archive_path)
        lines.append("")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvolutionSummary":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ExperienceInsight:
    insight_type: str = ""
    description: str = ""
    frequency: int = 1
    context: str = ""
    recommendation: str = ""

    def to_markdown(self) -> str:
        return "- [" + self.insight_type + "] " + self.description + " (seen " + str(self.frequency) + "x). " + self.recommendation


def init_experience_file(workspace: Path) -> Path:
    path = workspace / _EXPERIENCE_FILE
    if not path.exists():
        initial_content = _HEADER_WORKED + "\n\n" + _HEADER_FAILED + "\n\n" + _HEADER_SUMMARIES + "\n"
        _atomic_write(path, initial_content)
        logger.info("Initialized experience file: %s", path)
    return path


def update_experience(
    workspace: Path,
    worked: list[str] | None = None,
    failed: list[str] | None = None,
) -> None:
    path = workspace / _EXPERIENCE_FILE
    content = read_experience(workspace)
    if not content:
        content = _HEADER_WORKED + "\n\n" + _HEADER_FAILED + "\n\n" + _HEADER_SUMMARIES + "\n"

    parts = content.split(_HEADER_FAILED, maxsplit=1)
    if len(parts) == 1:
        content = _HEADER_WORKED + "\n\n" + _HEADER_FAILED + "\n\n" + _HEADER_SUMMARIES + "\n"
        parts = content.split(_HEADER_FAILED, maxsplit=1)

    before_failed = parts[0]
    after_failed = parts[1] if len(parts) > 1 else "\n\n" + _HEADER_SUMMARIES + "\n"

    if worked:
        worked_items = "\n".join("- " + item for item in worked)
        before_failed = before_failed.rstrip() + "\n" + worked_items + "\n"

    if failed:
        failed_items = "\n".join("- " + item for item in failed)
        after_failed = after_failed.rstrip() + "\n" + failed_items + "\n"

    worked_body = before_failed.rstrip()
    failed_body = after_failed.strip()

    summaries_section = _HEADER_SUMMARIES
    if summaries_section in failed_body:
        idx = failed_body.index(summaries_section)
        failed_section = failed_body[:idx].strip()
        summaries_section_content = failed_body[idx:]
    else:
        failed_section = failed_body
        summaries_section_content = "\n\n" + summaries_section + "\n"

    new_content = worked_body + "\n\n" + _HEADER_FAILED + "\n\n" + failed_section + "\n\n" + summaries_section_content + "\n"
    _atomic_write(path, new_content)


def log_evolution_summary(workspace: Path, summary: EvolutionSummary) -> None:
    path = workspace / _EXPERIENCE_FILE
    content = read_experience(workspace)
    if not content:
        init_experience_file(workspace)
        content = read_experience(workspace)

    if _HEADER_SUMMARIES not in content:
        content = content.rstrip() + "\n\n" + _HEADER_SUMMARIES + "\n"

    parts = content.split(_HEADER_SUMMARIES, maxsplit=1)
    before_summaries = parts[0].rstrip()
    after_summaries = parts[1] if len(parts) > 1 else "\n"

    if not summary.timestamp:
        summary.timestamp = datetime.now(timezone.utc).isoformat()

    summary_markdown = summary.to_markdown()
    new_content = before_summaries + "\n\n" + _HEADER_SUMMARIES + "\n\n" + summary_markdown + after_summaries
    _atomic_write(path, new_content)
    logger.info("Logged evolution summary: %s [%s]", summary.goal or "unnamed", summary.outcome)


def read_summaries(workspace: Path, max_count: int = 10) -> list[EvolutionSummary]:
    content = read_experience(workspace)
    if not content or _HEADER_SUMMARIES not in content:
        return []

    parts = content.split(_HEADER_SUMMARIES, maxsplit=1)
    if len(parts) < 2:
        return []

    summary_text = parts[1].strip()
    summaries = []
    current_goal = ""
    current_lines = []

    for line in summary_text.splitlines():
        if line.startswith("### "):
            if current_goal and current_lines:
                summaries.append(_parse_summary_block(current_goal, current_lines))
            current_goal = line[4:].strip()
            current_lines = []
        else:
            current_lines.append(line)

    if current_goal and current_lines:
        summaries.append(_parse_summary_block(current_goal, current_lines))

    return summaries[-max_count:] if len(summaries) > max_count else summaries


def _parse_summary_block(goal: str, lines: list[str]) -> EvolutionSummary:
    summary = EvolutionSummary(goal=goal)
    in_key_changes = False
    in_challenges = False
    in_solutions = False
    in_violations = False

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("##"):
            in_key_changes = False
            in_challenges = False
            in_solutions = False
            in_violations = False
            continue

        if stripped.startswith("- **Iterations:**"):
            in_key_changes = False
            in_challenges = False
            in_solutions = False
            in_violations = False
            p = stripped.replace("- **Iterations:** ", "").split("|")
            if len(p) >= 3:
                try:
                    iter_part = p[0].strip()
                    summary.iterations = int(iter_part.split(":")[1].strip()) if ":" in iter_part else 0
                    step_part = p[1].strip()
                    step_nums = step_part.split(":")[1].strip().split("/")
                    if len(step_nums) == 2:
                        summary.final_step = int(step_nums[0].strip())
                        summary.total_steps = int(step_nums[1].strip())
                except (ValueError, IndexError):
                    pass
        elif stripped.startswith("- **Test Baseline:**"):
            in_key_changes = False
            in_challenges = False
            in_solutions = False
            in_violations = False
            summary.test_baseline = stripped.split(":", 2)[-1].strip()
        elif stripped.startswith("- **Test Final:**"):
            in_key_changes = False
            in_challenges = False
            in_solutions = False
            in_violations = False
            summary.test_final = stripped.split(":", 2)[-1].strip()
        elif stripped.startswith("- **Test Delta:**"):
            in_key_changes = False
            in_challenges = False
            in_solutions = False
            in_violations = False
            summary.test_delta = stripped.split(":", 2)[-1].strip()
        elif stripped.startswith("- **Regressions:**"):
            in_key_changes = False
            in_challenges = False
            in_solutions = False
            in_violations = False
            try:
                summary.regressions_count = int(stripped.split(":")[1].strip())
            except (ValueError, IndexError):
                pass
        elif stripped.startswith("- **Key Changes:**"):
            in_key_changes = True
            in_challenges = False
            in_solutions = False
            in_violations = False
            continue
        elif stripped.startswith("- **Challenges:**"):
            in_key_changes = False
            in_challenges = True
            in_solutions = False
            in_violations = False
            continue
        elif stripped.startswith("- **Solutions:**"):
            in_key_changes = False
            in_challenges = False
            in_solutions = True
            in_violations = False
            continue
        elif stripped.startswith("- **Violations:**"):
            in_key_changes = False
            in_challenges = False
            in_solutions = False
            in_violations = True
            continue
        elif stripped.startswith("- **Archive:**"):
            in_key_changes = False
            in_challenges = False
            in_solutions = False
            in_violations = False
            summary.archive_path = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("  - ") or stripped.startswith("- "):
            item_text = stripped.lstrip(" -")
            if in_key_changes:
                summary.key_changes.append(item_text)
            elif in_challenges:
                summary.challenges.append(item_text)
            elif in_solutions:
                summary.solutions.append(item_text)
            elif in_violations:
                summary.violations.append(item_text)

    return summary


def extract_insights(workspace: Path) -> list[ExperienceInsight]:
    summaries = read_summaries(workspace, max_count=50)
    if not summaries:
        return []

    insights = []
    failure_counts = {}
    success_patterns = {}
    violation_counts = {}
    regression_sums = 0
    runs_with_regressions = 0

    for s in summaries:
        if s.outcome == "failure":
            for ch in s.challenges:
                key = ch[:80]
                failure_counts[key] = failure_counts.get(key, 0) + 1

        if s.outcome == "success":
            for sol in s.solutions:
                key = sol[:80]
                success_patterns[key] = success_patterns.get(key, 0) + 1

        for v in s.violations:
            key = v[:80]
            violation_counts[key] = violation_counts.get(key, 0) + 1

        if s.regressions_count > 0:
            regression_sums += s.regressions_count
            runs_with_regressions += 1

    for pattern, count in failure_counts.items():
        if count >= 2:
            insights.append(ExperienceInsight(
                insight_type="failure_mode",
                description=pattern,
                frequency=count,
                recommendation="Avoid patterns that caused failures in prior evolutions",
            ))

    for pattern, count in success_patterns.items():
        if count >= 2:
            insights.append(ExperienceInsight(
                insight_type="success_pattern",
                description=pattern,
                frequency=count,
                recommendation="Apply this proven approach in similar contexts",
            ))

    if runs_with_regressions > 0:
        avg_reg = regression_sums / runs_with_regressions
        insights.append(ExperienceInsight(
            insight_type="regression_trend",
            description="Regressions occurred in " + str(runs_with_regressions) + " evolution(s)",
            frequency=runs_with_regressions,
            context="Average " + str(round(avg_reg, 1)) + " regressions per affected run",
            recommendation="Run tests frequently; rollback on regression detection",
        ))

    for violation, count in violation_counts.items():
        if count >= 2:
            insights.append(ExperienceInsight(
                insight_type="violation_trend",
                description=violation,
                frequency=count,
                recommendation="Ensure protocol compliance to avoid repeated violations",
            ))

    return insights


def get_experience_context(workspace: Path) -> str:
    summaries = read_summaries(workspace, max_count=5)
    insights = extract_insights(workspace)

    lines = ["--- Previous Experience ---"]

    if summaries:
        lines.append("")
        lines.append("Found " + str(len(summaries)) + " recent evolution(s):")
        lines.append("")
        for i, s in enumerate(summaries, 1):
            lines.append(str(i) + ". " + (s.goal or "Unnamed") + " [" + s.outcome + "] - " + str(s.iterations) + " iterations, " + str(s.final_step) + "/" + str(s.total_steps) + " steps")
            if s.test_delta:
                lines.append("   Test delta: " + s.test_delta)
            if s.regressions_count:
                lines.append("   Regressions: " + str(s.regressions_count))
            if s.challenges:
                lines.append("   Challenges: " + "; ".join(s.challenges[:3]))
            lines.append("")

    if insights:
        lines.append("")
        lines.append("Key Insights:")
        lines.append("")
        for insight in insights:
            lines.append("- " + insight.to_markdown())

    if not summaries and not insights:
        experience = read_experience_capped(workspace, max_chars=5000)
        if experience:
            lines.append("")
            lines.append(experience)
        else:
            lines.append("")
            lines.append("No previous experience data.")

    return "\n".join(lines)


def read_experience(workspace: Path) -> str:
    path = workspace / _EXPERIENCE_FILE
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("Failed to read experience file: %s", e)
        return ""


def read_experience_capped(workspace: Path, max_chars: int = 10000) -> str:
    content = read_experience(workspace)
    if not content or len(content) <= max_chars:
        return content

    lines = content.splitlines()
    if len(lines) <= 4:
        return content

    worked_idx = None
    failed_idx = None
    for i, line in enumerate(lines):
        if line.strip() == _HEADER_WORKED:
            worked_idx = i
        elif line.strip() == _HEADER_FAILED:
            failed_idx = i

    if worked_idx is None or failed_idx is None:
        return content[:max_chars]

    worked_header = lines[worked_idx]
    failed_header = lines[failed_idx]
    header_len = len(worked_header) + len(failed_header) + 2
    available = max_chars - header_len

    worked_budget = int(available * 0.4)
    failed_budget = int(available * 0.6)

    worked_body_start = worked_idx + 1
    worked_body_end = failed_idx
    worked_body = lines[worked_body_start:worked_body_end]
    kept_worked = []
    worked_len = 0
    for line in reversed(worked_body):
        line_len = len(line) + 1
        if worked_len + line_len > worked_budget:
            break
        kept_worked.append(line)
        worked_len += line_len
    kept_worked = list(reversed(kept_worked))

    failed_body_start = failed_idx + 1
    failed_body = lines[failed_body_start:]
    kept_failed = []
    failed_len = 0
    for line in reversed(failed_body):
        line_len = len(line) + 1
        if failed_len + line_len > failed_budget:
            break
        kept_failed.append(line)
        failed_len += line_len
    kept_failed = list(reversed(kept_failed))

    return "\n".join([worked_header] + kept_worked + [failed_header] + kept_failed)


def _atomic_write(path: Path, content: str) -> None:
    dir_path = path.parent
    dir_path.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), prefix=".experience_tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, str(path))
    except Exception as exc:
        logger.error("Failed to write experience file %s: %s", path, exc)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
