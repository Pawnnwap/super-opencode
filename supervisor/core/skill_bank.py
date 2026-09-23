"""Supervisor skill bank: limited, supervisor-decided skill loading.

The supervisor is a plain chat-completions judge, not an agent. Its only
agency over skills is a structured marker in its verdict: every prompt shows
a *catalog* (name + one-line description per skill), and the judge loads a
body on request by adding ``LOAD_SKILL: <name>`` — the same protocol the
evidence harness uses for ``NEED_EVIDENCE``. The loop resolves the request
deterministically: validate the name, read one allow-listed ``.md`` file
from a bank directory, keep the body as resident judge context.

Control boundaries (why this cannot wander):

* only the catalog is auto-rendered — bodies never enter a prompt unless
  the judge explicitly requested them;
* load requests are capped per turn, the resident set is bounded with LRU
  eviction, and one resolution round per judged turn — requests seen in a
  re-judge are queued for the next turn, preventing load chains;
* names must match ``_NAME_RE``, so only bank-directory ``*.md`` files are
  reachable (path traversal is structurally impossible);
* skill content is judge-context only — it never reaches the runner;
  ``strip_skill_markers`` removes the marker lines from agent feedback.
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from supervisor.utils.filesystem.file_ops import safe_read_text

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CATALOG_MAX_ENTRIES = 12
_CATALOG_CHAR_BUDGET = 2000  # chars for the whole catalog listing
_DESCRIPTION_FALLBACK_CHARS = 120
_DESCRIPTION_MAX_CHARS = 200
_KNOWN_NAMES_SHOWN = 8


@dataclass(frozen=True)
class Skill:
    """A single supervisor skill discovered in a bank directory."""

    name: str
    description: str
    body: str
    source: str  # bank label the skill came from ("builtin"|"workspace"|"extra")


@dataclass
class SkillLoadResult:
    """Outcome of a load round: what became resident and why not the rest."""

    loaded: list[Skill] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class SkillBank:
    """Discover skills, render the catalog, and bound what the judge loads.

    Directories are given in shadowing order: later directories override
    earlier ones on name clash (workspace skills override built-ins). The
    bank owns the resident set — loaded bodies stay in the judge's context
    for the rest of the run, evicted least-recently-used when over budget.
    """

    def __init__(
        self,
        builtin_dir: Path | None = None,
        workspace_dir: Path | None = None,
        extra_dir: str | Path | None = None,
        *,
        body_budget: int = 6000,
        max_resident: int = 5,
        max_total_chars: int = 16000,
        max_loads_per_turn: int = 2,
    ):
        self._body_budget = max(500, body_budget)
        self._max_resident = max(1, max_resident)
        self._max_total_chars = max(self._body_budget, max_total_chars)
        self._max_loads_per_turn = max(1, max_loads_per_turn)
        self._dirs: list[tuple[str, Path]] = []
        for label, directory in (
            ("builtin", builtin_dir),
            ("workspace", workspace_dir),
            ("extra", extra_dir),
        ):
            if directory:
                self._dirs.append((label, Path(directory)))
        self._skills: dict[str, Skill] = {}
        self._resident: OrderedDict[str, Skill] = OrderedDict()
        self._pending: list[str] = []
        self.discover()

    # ------------------------------------------------------------------ #
    # Discovery                                                          #
    # ------------------------------------------------------------------ #

    def discover(self) -> dict[str, Skill]:
        """Re-scan bank directories; later dirs shadow earlier on clash."""
        discovered: dict[str, Skill] = {}
        for label, directory in self._dirs:
            for path in sorted(directory.glob("*.md")):
                name = path.stem.lower()
                if not _NAME_RE.match(name):
                    logger.warning(
                        "skill bank: ignoring %s (name must match %s)",
                        path.name,
                        _NAME_RE.pattern,
                    )
                    continue
                skill = self._parse_skill_file(path, name, label)
                if skill is None:
                    continue
                discovered[name] = skill
        self._skills = discovered
        return discovered

    def _parse_skill_file(self, path: Path, name: str, label: str) -> Skill | None:
        raw = safe_read_text(path, default="")
        if not raw.strip():
            return None
        description = ""
        body = raw
        lines = raw.splitlines()
        if lines and lines[0].strip() == "---":
            close = None
            for i in range(1, len(lines)):
                if lines[i].strip() == "---":
                    close = i
                    break
            if close is not None:
                for line in lines[1:close]:
                    key, _, value = line.partition(":")
                    if key.strip().lower() == "description":
                        description = " ".join(value.split())[:_DESCRIPTION_MAX_CHARS]
                body = "\n".join(lines[close + 1 :]).lstrip("\n")
        if not description:
            description = _first_paragraph(body)[:_DESCRIPTION_FALLBACK_CHARS]
        if len(body) > self._body_budget:
            body = body[: self._body_budget] + "\n…(skill body truncated)"
        return Skill(
            name=name,
            description=description or name,
            body=body,
            source=label,
        )

    def has_skills(self) -> bool:
        return bool(self._skills)

    def names(self) -> list[str]:
        return sorted(self._skills)

    def skills(self) -> list[Skill]:
        return [self._skills[name] for name in sorted(self._skills)]

    # ------------------------------------------------------------------ #
    # Prompt rendering                                                   #
    # ------------------------------------------------------------------ #

    def catalog_block(self) -> str:
        """Catalog + load instruction; the only skill text auto-rendered."""
        if not self._skills:
            return ""
        lines = [
            "--- Supervisor Skills (bank) ---",
            "Optional know-how you may load into your context before judging:",
        ]
        budget = _CATALOG_CHAR_BUDGET
        hidden = 0
        for skill in self.skills():
            entry = f"- {skill.name}: {skill.description}"
            if len(lines) - 2 >= _CATALOG_MAX_ENTRIES or len(entry) > budget:
                hidden += 1
                continue
            lines.append(entry)
            budget -= len(entry) + 1
        if hidden:
            lines.append(f"(+{hidden} more not listed)")
        lines.append(
            "Load only skills relevant to the current judgement — do not "
            "load them all. To load one, add a line `LOAD_SKILL: <name>` to "
            "your verdict; it stays in your context for the rest of the run.",
        )
        return "\n".join(lines) + "\n"

    def resident_block(self) -> str:
        """Bodies of loaded skills, for injection into judge prompts."""
        if not self._resident:
            return ""
        parts = []
        for skill in self._resident.values():
            self._resident.move_to_end(skill.name)
            parts.append(f"[skill: {skill.name}] {skill.description}\n{skill.body}")
        return (
            "--- Loaded Supervisor Skills (apply their guidance) ---\n"
            + "\n\n".join(parts)
            + "\n"
        )

    def context_block(self) -> str:
        """Catalog + resident bodies in one block, for the provider registry."""
        catalog = self.catalog_block()
        resident = self.resident_block()
        if catalog and resident:
            return catalog + "\n" + resident
        return catalog or resident

    # ------------------------------------------------------------------ #
    # Loading (bounded)                                                  #
    # ------------------------------------------------------------------ #

    def load(self, requested: list[str]) -> SkillLoadResult:
        """Load at most ``max_loads_per_turn`` skills; defer the rest."""
        result = SkillLoadResult()
        for raw in requested[: self._max_loads_per_turn]:
            name = (raw or "").strip().lower()
            if not name:
                continue
            skill = self._skills.get(name)
            if skill is None:
                result.notes.append(
                    f"skill '{name}' not in bank (known: {self._known_names()})",
                )
                continue
            if name in self._resident:
                self._resident.move_to_end(name)
                result.notes.append(f"skill '{name}' already loaded")
                continue
            self._resident[name] = skill
            result.loaded.append(skill)
        deferred = requested[self._max_loads_per_turn :]
        if deferred:
            result.notes.append(
                f"deferred {len(deferred)} load request(s) to the next turn",
            )
            self.queue_pending(deferred)
        self._evict_over_budget()
        return result

    def _evict_over_budget(self) -> None:
        while len(self._resident) > self._max_resident:
            evicted = self._resident.popitem(last=False)
            logger.info("skill bank: evicted '%s' (resident cap)", evicted[0])
        total = sum(len(skill.body) for skill in self._resident.values())
        while total > self._max_total_chars and len(self._resident) > 1:
            evicted = self._resident.popitem(last=False)
            total -= len(evicted[1].body)
            logger.info("skill bank: evicted '%s' (char budget)", evicted[0])

    def loaded_names(self) -> list[str]:
        return list(self._resident)

    # ------------------------------------------------------------------ #
    # Chain-free queuing                                                 #
    # ------------------------------------------------------------------ #

    def queue_pending(self, names: list[str]) -> None:
        """Hold requests for the next turn (no load chains within a turn)."""
        for raw in names:
            name = (raw or "").strip().lower()
            if not name or name in self._pending or name in self._resident:
                continue
            self._pending.append(name)

    def drain_pending(self) -> list[str]:
        pending, self._pending = self._pending[:], []
        return pending

    def _known_names(self) -> str:
        names = self.names()
        return ", ".join(names[:_KNOWN_NAMES_SHOWN]) + (
            "..." if len(names) > _KNOWN_NAMES_SHOWN else ""
        )


def builtin_bank_dir() -> Path:
    """Directory of the built-in skills shipped with this package."""
    return Path(__file__).resolve().parent.parent / "skills" / "builtin"


def _first_paragraph(text: str) -> str:
    for paragraph in (text or "").split("\n\n"):
        cleaned = " ".join(paragraph.split())
        if cleaned:
            return cleaned
    return ""


def strip_skill_markers(text: str) -> str:
    """Remove LOAD_SKILL lines from text bound for the runner.

    The judge's full reply becomes agent-facing feedback (chat.py sets
    ``feedback=reply``), so this keeps supervisor-side request markers from
    leaking into the coding agent's prompt. NEED_EVIDENCE lines are left
    untouched — that is pre-existing behavior outside this feature.
    """
    marker_re = re.compile(r"^\s*LOAD_SKILL\s*:.*$", re.IGNORECASE)
    return "\n".join(
        line for line in (text or "").splitlines() if not marker_re.match(line)
    )
