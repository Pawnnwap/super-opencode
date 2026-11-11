"""Framework-free planning and layout helpers for the run harvester.

One harvest turns several finished runs into one clean folder:

    <location>/harvest_<timestamp>/
        INDEX.md                every run, its state and collection status
        HARVEST.md              batch-level summary (best effort)
        <workspace>__<job_id>/
            run.json             provenance + collection status
            report.md            the run's stored final report (always written)
            harvest.md           the agent's distilled result (or a fallback)

The UI and the worker both import this module, so matching, folder naming and
the agent output contract have exactly one definition.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.jobs.job_states import TERMINAL_JOB_STATES

HARVEST_JOB_TYPE = "harvest"

# A finished run with a recorded outcome. FAILED/CANCELLED/ABANDONED runs are
# harvestable too (partial results are often the point), but the UI defaults
# to COMPLETED_STATES so a stray CANCELLED farm run must be chosen explicitly.
COMPLETED_STATES = frozenset({"SUCCESS", "BLOCKED"})
HARVESTABLE_STATES = frozenset(TERMINAL_JOB_STATES | {"ABANDONED"})

DEFAULT_HARVEST_PROMPT = (
    "Extract this run's durable results: the final outcome, the main findings "
    "or deliverables, the exact artifact paths worth keeping, and the next "
    "action or blocker. Quote concrete numbers and file paths; do not restate "
    "the protocol."
)

# Final-report files a run may have left in its workspace, best first. The
# job state's own report wins; these are the fallback when it is empty.
REPORT_FILENAMES = (
    "failure_report.md",
    "blocked_report.md",
    "summary.md",
    "report.md",
)

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_REPORT_INLINE_LIMIT = 8000


@dataclass
class HarvestEntry:
    """One selected run's folder and collection outcome."""

    job_id: str
    state: str
    workspace: str
    slug: str
    dest: Path
    harvest: str = "pending"  # pending | agent | fallback | skipped | cancelled
    detail: str = ""


def search_text(job_id: str, state: dict[str, Any]) -> str:
    """Lowercase haystack for the page's string auto-match."""
    config = state.get("config") or {}
    workspace = str(config.get("workspace") or "")
    parts = (
        job_id,
        str(state.get("state") or ""),
        Path(workspace).name,
        workspace,
        str(state.get("batch_id") or ""),
        str(state.get("report") or ""),
    )
    return "\n".join(parts).lower()


def match_query(query: str, haystack: str) -> bool:
    """Case-insensitive substring match; empty query matches everything."""
    needle = " ".join(str(query or "").lower().split())
    return not needle or needle in haystack


def run_slug(job_id: str, state: dict[str, Any]) -> str:
    """Folder name for one run: workspace basename plus the unique job id."""
    config = state.get("config") or {}
    workspace = str(config.get("workspace") or "")
    base = Path(workspace).name if workspace else "run"
    safe = _SLUG_RE.sub("_", base).strip("._-")[:64] or "run"
    return f"{safe}__{job_id}"


def unique_child(parent: Path, name: str) -> Path:
    """A non-existing child path, suffixed -2, -3, ... on collision."""
    candidate = parent / name
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = parent / f"{name}-{suffix}"
    return candidate


def make_batch_dir(location: str | Path, now: float | None = None) -> Path:
    """Create and return a fresh timestamped harvest folder under *location*.

    A harvest never writes into an existing batch folder: rerunning the same
    selection (or launching two harvests in one second) produces a new
    sibling, so results from two harvests can never be mixed.
    """
    root = Path(location).expanduser().resolve()
    stamp = time.strftime(
        "%Y%m%d_%H%M%S",
        time.localtime(time.time() if now is None else now),
    )
    candidate = root / f"harvest_{stamp}"
    suffix = 1
    while True:
        try:
            candidate.mkdir(parents=True)
            return candidate
        except FileExistsError:
            suffix += 1
            candidate = root / f"harvest_{stamp}-{suffix}"


def run_report(state: dict[str, Any]) -> str:
    """The run's stored final report, or the best report file on disk."""
    report = str(state.get("report") or "")
    if report.strip():
        return report
    workspace = str((state.get("config") or {}).get("workspace") or "")
    if not workspace:
        return ""
    root = Path(workspace)
    for name in REPORT_FILENAMES:
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if text.strip():
            return text
    return ""


def write_run_meta(dest: Path, meta: dict[str, Any]) -> None:
    (dest / "run.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def write_run_baseline(
    dest: Path, job_id: str, state: dict[str, Any],
) -> dict[str, Any]:
    """Write report.md + run.json before any agent runs.

    This baseline is the guarantee: even when the source workspace is gone or
    the agent fails, every selected run still leaves a report and provenance
    in its folder.
    """
    dest.mkdir(parents=True, exist_ok=True)
    report = run_report(state)
    (dest / "report.md").write_text(
        report
        if report.strip()
        else f"# {job_id}\n\nNo final report was recorded for this run.\n",
        encoding="utf-8",
    )
    config = state.get("config") or {}
    meta: dict[str, Any] = {
        "job_id": job_id,
        "type": state.get("type"),
        "state": state.get("state"),
        "workspace": config.get("workspace") or "",
        "batch_id": state.get("batch_id"),
        "replica_index": state.get("replica_index"),
        "engine": config.get("engine"),
        "model": config.get("opencode_model"),
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
        "report_chars": len(report),
        "harvested_at": time.time(),
        "harvest": "pending",
    }
    write_run_meta(dest, meta)
    return meta


def run_agent_prompt(
    operator_prompt: str,
    job_id: str,
    state: dict[str, Any],
    dest: Path,
    report: str,
    log_path: str = "",
) -> str:
    """Prompt for the per-run harvest agent (one run, one output file)."""
    config = state.get("config") or {}
    workspace = str(config.get("workspace") or "")
    batch = str(state.get("batch_id") or "(none)")
    excerpt = report.strip()
    if len(excerpt) > _REPORT_INLINE_LIMIT:
        excerpt = (
            excerpt[:_REPORT_INLINE_LIMIT]
            + f"\n\n[truncated - full report at {dest / 'report.md'}]"
        )
    if not excerpt:
        excerpt = "(no final report was recorded for this run)"
    updated = "unknown"
    if state.get("updated_at"):
        try:
            updated = time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(float(state["updated_at"])),
            )
        except (TypeError, ValueError, OSError):
            pass
    collected = [f"- final report: {dest / 'report.md'} (also copied below)"]
    if log_path:
        collected.append(f"- job event log (JSONL, optional): {log_path}")
    return "\n".join(
        [
            "You are harvesting ONE finished supervised run into a clean results folder.",
            "",
            "OPERATOR HARVEST FOCUS",
            operator_prompt.strip(),
            "",
            "RUN",
            f"- job id: {job_id}",
            f"- final state: {state.get('state') or 'UNKNOWN'}",
            f"- source workspace: {workspace}",
            f"- batch: {batch}",
            f"- last update: {updated}",
            "",
            "ALREADY COLLECTED",
            *collected,
            "",
            "OUTPUT CONTRACT - follow exactly",
            "1. Write the harvest as Markdown to exactly this file:",
            f"   {dest / 'harvest.md'}",
            "2. Treat the source workspace as read-only: do not create, edit, or delete",
            "   anything inside it.",
            f"3. Do not write anywhere outside {dest}.",
            "4. If the operator focus asks for artifacts, copy only those named files",
            f"   into {dest} and list them in harvest.md.",
            "5. Cite paths relative to the source workspace. Quote real numbers; if",
            '   evidence is missing, say "not available" instead of guessing.',
            "6. Keep harvest.md focused: aim for <= 200 lines unless the operator",
            "   focus requires a table or a copied artifact.",
            "",
            "FINAL REPORT (copy)",
            excerpt,
            "",
        ],
    )


def synthesis_prompt(
    operator_prompt: str,
    batch_dir: Path,
    entries: list[HarvestEntry],
) -> str:
    """Prompt for the batch-level summary over the per-run harvest notes."""
    lines = [
        (
            "You are writing the batch-level harvest for "
            f"{len(entries)} finished supervised run(s)."
        ),
        "",
        "OPERATOR HARVEST FOCUS",
        operator_prompt.strip(),
        "",
        "PER-RUN HARVEST NOTES (read them; all inside the harvest folder)",
    ]
    for entry in entries:
        workspace = entry.workspace or "(workspace missing)"
        lines.append(
            f"- {entry.job_id} ({entry.state}) at {workspace}; "
            f"note: {entry.dest / 'harvest.md'}",
        )
    lines += [
        "",
        "OUTPUT CONTRACT - follow exactly",
        "1. Write the batch harvest as Markdown to exactly this file:",
        f"   {batch_dir / 'HARVEST.md'}",
        "2. Lead with a table: run | state | workspace | headline result.",
        "3. Then synthesize the cross-run findings the focus asks for: the best",
        "   result(s), common blockers, and next actions.",
        "4. Quote concrete numbers and paths from the notes; never invent. Say",
        '   "not available" for gaps.',
        "5. Do not modify the per-run folders and do not write any other file.",
        "",
    ]
    return "\n".join(lines)


def index_markdown(
    batch_dir: Path,
    started: float,
    prompt: str,
    entries: list[HarvestEntry],
    has_summary: bool = False,
) -> str:
    """Root INDEX.md: one row per selected run, linking its folder."""
    lines = [
        "# Harvest - "
        + time.strftime("%Y-%m-%d %H:%M", time.localtime(started)),
        "",
        f"**Runs:** {len(entries)} · **Folder:** `{batch_dir}`",
        "",
        f"**Prompt:** {prompt.strip()}",
        "",
        "| run | state | workspace | harvest |",
        "| --- | --- | --- | --- |",
    ]
    for entry in entries:
        status = entry.harvest + (f" - {entry.detail}" if entry.detail else "")
        workspace = (entry.workspace or "-").replace("|", "\\|")
        lines.append(
            f"| [{entry.job_id}]({entry.slug}/) | {entry.state} | "
            f"{workspace} | {status} |",
        )
    lines += [
        "",
        (
            "Each run folder holds `run.json` (provenance), `report.md` (final "
            "report) and `harvest.md` (distilled result or fallback), plus any "
            "artifacts the harvest agent copied."
        ),
    ]
    if has_summary:
        lines += ["", "Batch summary: [HARVEST.md](HARVEST.md)"]
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "COMPLETED_STATES",
    "DEFAULT_HARVEST_PROMPT",
    "HARVESTABLE_STATES",
    "HARVEST_JOB_TYPE",
    "HarvestEntry",
    "index_markdown",
    "make_batch_dir",
    "match_query",
    "run_agent_prompt",
    "run_report",
    "run_slug",
    "search_text",
    "synthesis_prompt",
    "unique_child",
    "write_run_baseline",
    "write_run_meta",
]
