"""AI-proposed whitelist entries, validated against the access policy.

The model is good at reading a protocol and noticing "this task needs the
reference corpus in D:/datasets" or "the fixtures live next to the repo". It
is not trusted to widen the policy: every suggestion is re-checked here with
:func:`supervisor.security.access_policy.validate_whitelist_entry`, so a
hallucinated drive root, a UNC share, a system directory, or a path inside the
workspace never reaches the UI as an acceptable entry.

Only *path-like strings* are sent to the model - never file contents - which
keeps API keys and data rows out of the prompt while still surfacing the
directories a protocol refers to.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from supervisor.security.access_policy import (
    is_within,
    validate_whitelist_entry,
)

logger = logging.getLogger(__name__)

_ABS_PATH_RE = re.compile(
    r"""(?:[A-Za-z]:[\\/][^\s"'`|;&<>()\[\]{},]+|\\\\[^\s"'`|;&<>()\[\]{},]+|/[A-Za-z0-9_.\-]+/[^\s"'`|;&<>()\[\]{},]+)""",
)

_TEXT_SUFFIXES = {
    ".md",
    ".txt",
    ".json",
    ".jsonc",
    ".toml",
    ".yaml",
    ".yml",
    ".ini",
    ".cfg",
    ".py",
    ".ps1",
    ".sh",
    ".bat",
    ".cmd",
    ".ts",
    ".js",
}
_MAX_SCAN_BYTES = 96_000
_MAX_SCANNED_FILES = 40
_MAX_PROTOCOL_CHARS = 6_000


@dataclass(frozen=True)
class WhitelistSuggestion:
    path: Path
    reason: str
    confidence: float = 0.5

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "reason": self.reason,
            "confidence": self.confidence,
        }


@dataclass
class AdvisorResult:
    accepted: list[WhitelistSuggestion] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    raw_response: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.accepted)


def collect_context(
    workspace: Path,
    protocol_text: str = "",
    existing: Iterable[str] = (),
    on_progress: Callable[[str], None] | None = None,
) -> str:
    """Build a compact, path-only view of the run for the advisor prompt.

    ``on_progress(note)`` reports the pre-LLM phases. They scan the workspace
    (up to 40 files, 96 kB each) and used to be invisible: on a workspace whose
    tree sits on a slow disk the advisor looked identical whether it was
    scanning, waiting on the model, or hung.
    """
    workspace = Path(workspace)

    def _note(text: str) -> None:
        if on_progress is not None:
            try:
                on_progress(text)
            except Exception:
                logger.debug("context progress callback failed", exc_info=True)

    _note("Collecting context (protocol excerpt, top-level entries)…")
    lines: list[str] = [f"Workspace (cwd, full access): {workspace}"]

    existing_list = [str(item) for item in existing if str(item).strip()]
    lines.append(
        "Already whitelisted (read-only): " + (", ".join(existing_list) if existing_list else "none"),
    )

    if protocol_text.strip():
        excerpt = protocol_text.strip()[:_MAX_PROTOCOL_CHARS]
        lines.append("\nProtocol excerpt:\n" + excerpt)

    try:
        entries = sorted(workspace.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError:
        entries = []
    top = [f"{entry.name}/" if entry.is_dir() else entry.name for entry in entries[:60]]
    if top:
        lines.append("\nWorkspace top level: " + ", ".join(top))

    _note(f"Scanning workspace files for referenced paths (up to {_MAX_SCANNED_FILES})…")
    referenced = _referenced_paths(workspace)
    if referenced:
        lines.append("\nAbsolute paths already referenced by workspace files:")
        for path in referenced[:40]:
            lines.append(f"- {path}")

    lines.append(
        "\nRules: only existing absolute directories OUTSIDE the workspace; "
        "read-only with no exceptions; never drive roots, OS/program directories, "
        "the user-profile root, UNC shares, or anything inside the workspace.",
    )
    return "\n".join(lines)


def suggest_whitelist(
    workspace: Path,
    protocol_text: str,
    llm_call: Callable[[list[dict[str, str]]], str],
    *,
    existing: Iterable[str] = (),
    max_suggestions: int = 6,
    on_progress: Callable[[str], None] | None = None,
) -> AdvisorResult:
    """Ask the model for reference directories and validate every answer."""
    workspace = Path(workspace)
    existing_resolved = [Path(item) for item in existing if str(item).strip()]
    context = collect_context(workspace, protocol_text, existing, on_progress)

    messages = [
        {
            "role": "system",
            "content": (
                "You choose READ-ONLY reference directories for an autonomous coding "
                "agent. The agent can already read and write its workspace (cwd). "
                "You propose additional directories outside the workspace that the "
                "task genuinely needs to READ (datasets, reference repos, fixtures, "
                "docs). Never propose write targets: every whitelisted directory is "
                "read-only. Prefer the narrowest directory that covers the need; do "
                "not propose parents of several unrelated trees. Propose at most "
                f"{max_suggestions} entries. If nothing is needed, return [].\n\n"
                'Answer with JSON only, no prose and no code fence: '
                '[{"path": "D:/data/corpus", "reason": "why the task reads it", '
                '"confidence": 0.0}]'
            ),
        },
        {"role": "user", "content": context},
    ]
    raw = llm_call(messages)
    result = parse_suggestions(raw, workspace, existing=existing_resolved)
    result.raw_response = raw
    if len(result.accepted) > max_suggestions:
        result.accepted = result.accepted[:max_suggestions]
    return result


def parse_suggestions(
    raw: str,
    workspace: Path,
    *,
    existing: Iterable[str | Path] = (),
) -> AdvisorResult:
    """Validate a model response without trusting its formatting or paths."""
    result = AdvisorResult(raw_response=raw or "")
    payload = _extract_json_array(raw or "")
    if payload is None:
        result.rejected.append(("<response>", "response contained no JSON array"))
        return result

    existing_paths = [Path(item) for item in existing]
    seen: set[str] = set()
    for item in payload:
        if isinstance(item, str):
            raw_path, reason, confidence = item, "", 0.5
        elif isinstance(item, dict):
            raw_path = str(item.get("path") or item.get("dir") or "").strip()
            reason = str(item.get("reason") or "").strip()
            try:
                confidence = float(item.get("confidence", 0.5))
            except (TypeError, ValueError):
                confidence = 0.5
        else:
            result.rejected.append((repr(item), "not an object"))
            continue

        if not raw_path:
            result.rejected.append((raw_path or "<empty>", "missing path"))
            continue

        resolved, error = validate_whitelist_entry(raw_path, workspace)
        if resolved is None:
            result.rejected.append((raw_path, error))
            continue

        key = str(resolved).lower()
        if key in seen:
            continue
        if any(is_within(resolved, entry) for entry in existing_paths if str(entry)):
            result.rejected.append((raw_path, "already whitelisted"))
            continue

        seen.add(key)
        result.accepted.append(
            WhitelistSuggestion(
                path=resolved,
                reason=reason or "referenced by the task",
                confidence=max(0.0, min(1.0, confidence)),
            ),
        )

    result.accepted.sort(key=lambda item: item.confidence, reverse=True)
    return result


def _extract_json_array(raw: str) -> list | None:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def _referenced_paths(workspace: Path) -> list[str]:
    """Absolute paths mentioned by workspace files (path strings only)."""
    found: list[str] = []
    seen: set[str] = set()
    scanned = 0
    for path in workspace.rglob("*"):
        if scanned >= _MAX_SCANNED_FILES:
            break
        if not path.is_file() or path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        if any(part in {".git", "node_modules", ".venv", "venv", "__pycache__"} for part in path.parts):
            continue
        try:
            if path.stat().st_size > _MAX_SCAN_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1
        for match in _ABS_PATH_RE.finditer(text):
            candidate = match.group(0).rstrip(".,;:)")
            if len(candidate) < 4 or candidate.startswith(("http:", "https:")):
                continue
            key = candidate.lower()
            if key in seen:
                continue
            seen.add(key)
            found.append(candidate)
    return found


def suggest_with_supervisor_llm(
    workspace: Path,
    protocol_text: str,
    *,
    api_key: str,
    base_url: str | None,
    model: str,
    existing: Iterable[str] = (),
    max_suggestions: int = 6,
    on_progress: Callable[[int, int], None] | None = None,
    on_phase: Callable[[str], None] | None = None,
    stop_event: Any = None,
) -> AdvisorResult:
    """Convenience wrapper over the supervisor's OpenAI-compatible client.

    ``on_progress(answer_chars, reasoning_chars)`` streams the model's live
    counters; ``on_phase(note)`` covers the pre-LLM workspace scan. Both are
    optional and run on the calling (worker) thread: the UI marshals them
    onto its own loop.
    """
    from supervisor.utils.llm_stream import make_stream_client, stream_chat_text

    client = make_stream_client(api_key=api_key, base_url=base_url or None)

    def _call(messages: list[dict[str, str]]) -> str:
        def _stream_progress(content: int, reasoning: int, _chunks: int) -> None:
            if on_progress is not None:
                on_progress(content, reasoning)

        return stream_chat_text(
            client,
            model,
            messages,
            temperature=0.2,
            field_name="whitelist suggestions",
            on_progress=_stream_progress if on_progress is not None else None,
            stop_event=stop_event,
        )

    return suggest_whitelist(
        workspace,
        protocol_text,
        _call,
        existing=existing,
        max_suggestions=max_suggestions,
        on_progress=on_phase,
    )


__all__ = [
    "AdvisorResult",
    "WhitelistSuggestion",
    "collect_context",
    "parse_suggestions",
    "suggest_whitelist",
    "suggest_with_supervisor_llm",
]
