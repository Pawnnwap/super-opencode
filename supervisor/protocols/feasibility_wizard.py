"""supervisor/protocols/feasibility_wizard.py

LLM-assisted drafting for the pre-execution feasibility gate.

Mirrors protocol_wizard: one bounded streamed request through the supervisor
model. The prompt is TRANSCRIBE-ONLY — gate counts come from the protocol's
TARGET, ceilings only from run artifacts of previous attempts (status.json,
summary.md, REPORT.md, TASK_STATE.md, DESIGN.md) found in the workspace. The
result is a draft that fills the key-value form for operator review;
feasibility_gate.py itself stays fully deterministic.
"""

from __future__ import annotations

import logging
from pathlib import Path

from supervisor.protocols.feasibility_gate import (
    GATE_STATUSES,
    parse_llm_facts,
)
from supervisor.utils.llm_stream import (
    GenerationCancelled,
    make_stream_client,
    stream_chat_text,
)

logger = logging.getLogger(__name__)

MAX_PROTOCOL_CHARS = 6000
MAX_EVIDENCE_CHARS = 6000
_PER_FILE_CAP = 1600

# Prior-attempt artifacts, most authoritative first. Tails are excerpted so
# the latest state (best passing gate count, current status) survives the cap.
_EVIDENCE_FILES = (
    "status.json",
    "summary.md",
    "REPORT.md",
    "TASK_STATE.md",
    "DESIGN.md",
)

_FEASIBILITY_SYSTEM = """\
You transcribe acceptance gates for a coding task into strict JSON.

Given a protocol.md and optional evidence from previous attempts, produce a
feasibility facts object with EXACTLY this shape:

{
  "gates": [
    {
      "name": "<short gate name>",
      "definition": "<pass condition in <=60 characters: the essential test only>",
      "ref": <int, the TARGET item number this gate derives from>,
      "status": "open" | "passing" | "blocked",
      "evidence": "<one line: where this status is evidenced>",
      "check": <optional machine check object, see below>
    }
  ],
  "notes": "<optional one-line overall note>"
}

Machine check ("check") — include ONLY when the TARGET item literally
names it, never invent one:
  runnable command -> {"kind": "command", "cmd": "<the exact command>",
                       "expect_exit": 0}
  numeric threshold with a stated artifact ->
              {"kind": "metric", "metric": "<metric name>",
               "op": ">\" | \">=\" | \"<\" | \"<=\" | \"==",
               "value": <number>,
               "source": "<file.json>:<dotted.key>"}

Hard rules:
1. One gate per numbered acceptance criterion in the protocol's TARGET.
2. COMPRESS definitions to the essential test phrase — e.g.
   "page.shrp > 3.0", "corr within allowed band", "all pytest pass".
   Never copy the full protocol sentence; keep numbers and thresholds
   exactly as stated.
2. TRANSCRIBE ONLY — every name, definition and status must be supported by
   the provided text. If the material does not support a field, use "".
3. status meanings: "passing" = the evidence shows it passed in at least one
   earlier attempt; "blocked" = it failed in EVERY recorded attempt;
   "open" = not attempted or unknown.
4. Never invent a status or evidence. With no prior-attempt evidence, every
   gate is "open" with empty evidence.
5. Output ONLY the JSON object — no markdown fences, no commentary.
"""


class FeasibilityWizard:
    """One-shot drafter for feasibility facts; the operator saves or edits."""

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        self._client = make_stream_client(api_key=api_key, base_url=base_url)
        self._model = model

    def draft(
        self,
        protocol_text: str,
        evidence_text: str = "",
        *,
        on_progress=None,
        stop_event=None,
    ) -> dict:
        user_msg = (
            "### PROTOCOL\n"
            f"{_bounded_tail(protocol_text, MAX_PROTOCOL_CHARS)}\n\n"
            "### EVIDENCE FROM PREVIOUS ATTEMPTS\n"
            f"{str(evidence_text).strip() or '(none found)'}"
        )
        messages = [
            {"role": "system", "content": _FEASIBILITY_SYSTEM},
            {"role": "user", "content": user_msg},
        ]
        # Transcribing gates needs no chain-of-thought; a thinking model at
        # high effort otherwise streams minutes of reasoning before the JSON.
        # Endpoints that reject the parameter fall back to one plain call.
        try:
            raw = stream_chat_text(
                self._client,
                self._model,
                messages,
                temperature=0.0,
                field_name="feasibility facts draft",
                on_progress=on_progress,
                stop_event=stop_event,
                extra_body={"reasoning_effort": "none"},
            )
        except (GenerationCancelled, TimeoutError):
            raise
        except Exception as exc:  # noqa: BLE001 — parameter rejection
            logger.info(
                "Feasibility draft: reasoning suppression rejected (%s); "
                "retrying without it", exc,
            )
            raw = stream_chat_text(
                self._client,
                self._model,
                messages,
                temperature=0.0,
                field_name="feasibility facts draft",
                on_progress=on_progress,
                stop_event=stop_event,
            )
        return parse_llm_facts(raw)


def collect_evidence(workspace: Path, *, total_cap: int = MAX_EVIDENCE_CHARS) -> str:
    """Bounded excerpts of prior-attempt artifacts; '' when none exist."""
    workspace = Path(workspace)
    parts: list[str] = []
    remaining = total_cap
    for name in _EVIDENCE_FILES:
        path = workspace / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not text:
            continue
        excerpt = _bounded_tail(text, _PER_FILE_CAP)
        if len(excerpt) > remaining:
            excerpt = excerpt[:remaining]
        parts.append(f"--- {name} ---\n{excerpt}")
        remaining -= len(excerpt)
        if remaining <= 0:
            break
    return "\n\n".join(parts)


def facts_to_gate_rows(facts: dict) -> list[dict]:
    """Draft (or saved facts) → editor rows; legacy shapes are converted."""
    if isinstance(facts, dict) and isinstance(facts.get("gates"), list):
        rows = []
        for gate in facts["gates"]:
            if not isinstance(gate, dict):
                continue
            name = str(gate.get("name", "")).strip()
            if not name:
                continue
            status = str(gate.get("status", "open")).strip().lower()
            check = gate.get("check")
            rows.append(
                {
                    "name": name,
                    "definition": str(gate.get("definition", "")).strip(),
                    "status": status if status in GATE_STATUSES else "open",
                    "evidence": str(gate.get("evidence", "")).strip(),
                    "ref": gate.get("ref") if isinstance(gate.get("ref"), int) else None,
                    "check": dict(check) if isinstance(check, dict) else None,
                },
            )
        return rows
    return legacy_facts_to_gate_rows(facts)


def legacy_facts_to_gate_rows(facts: dict) -> list[dict]:
    """Old count-based facts → best-effort gate rows (statuses preserved)."""
    facts = facts if isinstance(facts, dict) else {}
    thresholds = facts.get("gate_thresholds") or {}
    ceiling = facts.get("known_ceiling") or {}
    blocked = {str(g) for g in ceiling.get("blocked_gates", [])}
    names = list(dict.fromkeys([*thresholds, *sorted(blocked)]))
    return [
        {
            "name": name,
            "definition": str(thresholds.get(name, "")),
            "status": "blocked" if name in blocked else "open",
            "evidence": str(ceiling.get("evidence", "")) if name in blocked else "",
        }
        for name in names
        if str(name).strip()
    ]


def count_target_gates(protocol_text: str) -> int | None:
    """Number of numbered TARGET items in a protocol, None when unparseable.

    Same heuristic as protocol_wizard's target bookkeeping: lines starting
    with a digit between the "## TARGET" heading and the next section.
    """
    count = 0
    in_target = False
    for line in str(protocol_text).splitlines():
        stripped = line.strip()
        if stripped.lower() == "## target":
            in_target = True
            continue
        if in_target and stripped.startswith("## "):
            break
        if in_target and stripped and stripped[0].isdigit():
            count += 1
    return count or None


def workspace_sources(workspace: Path) -> dict:
    """What the gate can see in ``workspace``: protocol + prior artifacts."""
    workspace = Path(workspace)
    protocol = workspace / "protocol.md"
    protocol_text = ""
    if protocol.is_file():
        try:
            protocol_text = protocol.read_text(encoding="utf-8", errors="replace")
        except OSError:
            protocol_text = ""
    return {
        "protocol_found": bool(protocol_text),
        "target_gates": count_target_gates(protocol_text),
        "artifacts": [n for n in _EVIDENCE_FILES if (workspace / n).is_file()],
    }


def _bounded_tail(text: str, cap: int) -> str:
    text = str(text).strip()
    if len(text) <= cap:
        return text
    return "… " + text[-cap:]
