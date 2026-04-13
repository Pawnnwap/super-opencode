"""supervisor/utils/experience_tracker.py - Track build experience across iterations.
Experience data informs future evolution decisions and step planning.

Maintains an experience.md file in the workspace with structured sections:
- What Worked
- What Failed
- Evolution Summaries (structured)

Experience data informs future evolution decisions and step planning.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_HEADER_WORKED = "## What Worked"
_HEADER_FAILED = "## What Failed"
_HEADER_SUMMARIES = "## Evolution Summaries"
_CACHE_DIR = ".opencode"
_CACHE_FILENAME = "experience_cache.json"

_EXPERIENCE_CACHE: dict[str, dict[str, Any]] = {}


def _get_cache_path(workspace: Path) -> Path:
    return workspace / _CACHE_DIR / _CACHE_FILENAME


def _ensure_cache_dir(workspace: Path) -> None:
    cache_dir = workspace / _CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)


def _load_cache_from_file(workspace: Path) -> dict[str, Any] | None:
    cache_path = _get_cache_path(workspace)
    if not cache_path.exists():
        return None
    try:
        with cache_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load experience cache from %s: %s", cache_path, e)
        return None


def _save_cache_to_file(workspace: Path, cache: dict[str, Any]) -> None:
    _ensure_cache_dir(workspace)
    cache_path = _get_cache_path(workspace)
    try:
        with cache_path.open("w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
    except OSError as e:
        logger.warning("Failed to save experience cache to %s: %s", cache_path, e)


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
    def from_dict(cls, data: dict[str, Any]) -> EvolutionSummary:
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


def _get_cache(workspace: Path) -> dict[str, Any]:
    key = str(workspace)
    if key not in _EXPERIENCE_CACHE:
        loaded = _load_cache_from_file(workspace)
        if loaded:
            summaries = []
            for s in loaded.get("summaries", []):
                if isinstance(s, dict):
                    summaries.append(EvolutionSummary.from_dict(s))
                elif isinstance(s, EvolutionSummary):
                    summaries.append(s)
            loaded["summaries"] = summaries
            _EXPERIENCE_CACHE[key] = loaded
        else:
            _EXPERIENCE_CACHE[key] = {
                "worked": [],
                "failed": [],
                "summaries": [],
            }
    return _EXPERIENCE_CACHE[key]


def _build_markdown_from_cache(workspace: Path) -> str:
    cache = _get_cache(workspace)
    parts = [_HEADER_WORKED]
    parts.extend("- " + item for item in cache["worked"])
    parts.append("")
    parts.append(_HEADER_FAILED)
    parts.extend("- " + item for item in cache["failed"])
    parts.append("")
    parts.append(_HEADER_SUMMARIES)
    for summary in cache["summaries"]:
        parts.append(summary.to_markdown())
    return "\n".join(parts) + "\n"


def init_experience_file(workspace: Path) -> Path:
    _get_cache(workspace)
    return _get_cache_path(workspace)


def _serialize_cache_for_save(cache: dict[str, Any]) -> dict[str, Any]:
    summaries = []
    for s in cache.get("summaries", []):
        if isinstance(s, EvolutionSummary):
            summaries.append(s.to_dict())
        elif isinstance(s, dict):
            summaries.append(s)
    return {
        "worked": cache.get("worked", []),
        "failed": cache.get("failed", []),
        "summaries": summaries,
    }


def update_experience(
    workspace: Path,
    worked: list[str] | None = None,
    failed: list[str] | None = None,
) -> None:
    cache = _get_cache(workspace)
    if worked:
        cache["worked"].extend(worked)
    if failed:
        cache["failed"].extend(failed)
    _save_cache_to_file(workspace, _serialize_cache_for_save(cache))
    logger.info("Updated experience: worked=%s, failed=%s", worked, failed)


def log_evolution_summary(workspace: Path, summary: EvolutionSummary) -> None:
    cache = _get_cache(workspace)
    if not summary.timestamp:
        summary.timestamp = datetime.now(UTC).isoformat()
    cache["summaries"].append(summary)
    _save_cache_to_file(workspace, _serialize_cache_for_save(cache))
    logger.info("Logged evolution summary: %s [%s]", summary.goal or "unnamed", summary.outcome)


def read_summaries(workspace: Path, max_count: int = 10) -> list[EvolutionSummary]:
    cache = _get_cache(workspace)
    summaries = cache["summaries"]
    return summaries[-max_count:] if len(summaries) > max_count else list(summaries)


def _parse_summary_block(goal: str, lines: list[str]) -> EvolutionSummary:
    summary = EvolutionSummary(goal=goal)
    return summary


def extract_insights(workspace: Path) -> list[ExperienceInsight]:
    summaries = read_summaries(workspace, max_count=50)
    if not summaries:
        return []

    insights = []
    failure_counts: dict[str, int] = {}
    success_patterns: dict[str, int] = {}
    violation_counts: dict[str, int] = {}
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
    cache = _get_cache(workspace)
    if not cache["worked"] and not cache["failed"] and not cache["summaries"]:
        return ""
    return _build_markdown_from_cache(workspace)


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
