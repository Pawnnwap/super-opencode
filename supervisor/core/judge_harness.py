"""Judge evidence harness: open registry of resources the judge can use.

The judge used to see only the agent's prose. This harness lets anything —
core modules as easily as future plugins — register a *resource* that grounds
the judge in deterministic workspace facts:

* ``auto`` resources are rendered into every judge prompt (cheap signals:
  changed files, workspace tree, audit trail tail).
* on-demand resources are listed as a menu; the judge requests one by adding
  ``NEED_EVIDENCE: <name>`` to its structured verdict, and the loop resolves
  the request once before re-judging (expensive signals: test runs, file
  reads).

Registration is open — ``harness.register(JudgeResource(...))`` — no fixed
list, no framework lock-in. Everything is deterministic (no extra LLM calls
beyond the single bounded re-judge).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from supervisor.core.target_state import AUDIT_FILENAME
from supervisor.utils.filesystem.file_ops import safe_read_text

logger = logging.getLogger(__name__)

_AUTO_BLOCK_BUDGET = 2400  # chars per auto resource
_FULFILL_BUDGET = 2000  # chars per fulfilled resource
_MAX_REQUESTS_PER_TURN = 3
_AUDIT_TAIL_LINES = 8
_READ_FILE_BUDGET = 1500
_READ_DIR_MAX_ENTRIES = 80


@dataclass
class JudgeResource:
    """A single evidence resource the judge can consume."""

    name: str
    provider: Callable[[str], str]  # arg = request remainder ("" for auto)
    description: str
    on_demand: bool = False  # False -> rendered automatically every turn


@dataclass
class JudgeHarness:
    """Open registry; render auto evidence and fulfill judge requests."""

    resources: dict[str, JudgeResource] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # Open registry                                                      #
    # ------------------------------------------------------------------ #

    def register(self, resource: JudgeResource) -> None:
        self.resources[resource.name] = resource

    # ------------------------------------------------------------------ #
    # Auto evidence (every turn)                                          #
    # ------------------------------------------------------------------ #

    def render_auto_block(self) -> str:
        parts: list[str] = []
        for resource in self.resources.values():
            if resource.on_demand:
                continue
            try:
                rendered = str(resource.provider("") or "").strip()
            except Exception:
                logger.warning(
                    "auto resource %r failed", resource.name, exc_info=True,
                )
                continue
            if rendered:
                parts.append(rendered[:_AUTO_BLOCK_BUDGET])

        menu = self._menu()
        if not parts and not menu:
            return ""
        block = "--- Workspace Evidence (harness) ---\n" + "\n".join(parts)
        if menu:
            block += "\n" + menu
        return block + "\n"

    def _menu(self) -> str:
        on_demand = [r for r in self.resources.values() if r.on_demand]
        if not on_demand:
            return ""
        lines = ["On-demand resources (add `NEED_EVIDENCE: <name>` to your reply):"]
        lines.extend(f"- {r.name}: {r.description}" for r in on_demand)
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # On-demand fulfillment (one round per judged turn)                   #
    # ------------------------------------------------------------------ #

    def fulfill(self, requests: list[str]) -> str:
        parts: list[str] = []
        for request in requests[:_MAX_REQUESTS_PER_TURN]:
            name, _, arg = request.strip().partition(" ")
            resource = self.resources.get(name.strip())
            if resource is None or not resource.on_demand:
                parts.append(f"[{name}] unavailable (known: {self._known_names()})")
                continue
            try:
                rendered = str(resource.provider(arg.strip()) or "").strip()
            except Exception:
                logger.warning(
                    "resource %r failed", name, exc_info=True,
                )
                rendered = "(resource failed)"
            parts.append(
                f"[{name}]" + (f" ({arg.strip()})" if arg.strip() else "")
                + f"\n{rendered[:_FULFILL_BUDGET]}",
            )
        return "\n\n".join(parts)

    def _known_names(self) -> str:
        names = sorted(self.resources)
        return ", ".join(names[:8]) + ("..." if len(names) > 8 else "")


# ------------------------------------------------------------------------- #
# Built-in resources wired to loop state                                     #
# ------------------------------------------------------------------------- #


def make_builtin_resources(
    workspace: Path,
    *,
    changed_files: Callable[[], list[str]],
    workspace_tree: Callable[[], str],
    audit_tail: Callable[[], str],
    run_tests: Callable[[], str],
) -> list[JudgeResource]:
    """Build the default resource set bound to loop state via callables."""
    return [
        JudgeResource(
            name="changed_files",
            provider=lambda _arg: _changed_files_block(changed_files()),
            description="files changed since run start (auto)",
        ),
        JudgeResource(
            name="workspace_tree",
            provider=lambda _arg: workspace_tree(),
            description="compact file tree (auto)",
        ),
        JudgeResource(
            name="audit_tail",
            provider=lambda _arg: audit_tail(),
            description="recent goal-guard audit events (auto)",
        ),
        JudgeResource(
            name="test_report",
            provider=lambda _arg: run_tests(),
            description="run the workspace test suite now (expensive)",
            on_demand=True,
        ),
        JudgeResource(
            name="read_file",
            provider=lambda arg: _read_file_block(workspace, arg),
            description=(
                "read_file <path> — bounded file content; a directory path "
                "(or no path) returns a bounded entry listing"
            ),
            on_demand=True,
        ),
    ]


def _changed_files_block(changed: list[str]) -> str:
    if not changed:
        return (
            "Changed files since run start: NONE — no observable workspace "
            "change has landed yet."
        )
    shown = ", ".join(changed[:10])
    more = f" (+{len(changed) - 10} more)" if len(changed) > 10 else ""
    return f"Changed files since run start ({len(changed)}): {shown}{more}"


def _read_file_block(workspace: Path, arg: str) -> str:
    """Read a workspace file, or list a directory when the request names one.

    A judge can ask for ``read_file`` with no argument (or a directory path)
    when it wants to see what exists. That used to call ``safe_read_text`` on
    the workspace ROOT, which is a directory: Windows raises PermissionError,
    ``safe_read_text`` retried it four times, and the judge got
    "empty or missing" instead of evidence. The same four-warning burst
    recurred across ten workspaces (2026-11-02 .. 2026-11-06). Directories
    now return a bounded, deterministic entry listing.
    """
    workspace_root = Path(workspace).resolve()
    requested = str(arg or "").strip().strip("\"'")
    path = (
        (workspace_root / requested).resolve()
        if requested
        else workspace_root
    )
    try:
        path.relative_to(workspace_root)
    except ValueError:
        return f"[read_file] path outside workspace: {arg}"
    if path.is_dir():
        return _read_directory_block(workspace_root, path, requested)
    content = safe_read_text(path, default="")
    if not content:
        return f"[read_file] {requested or '(workspace root)'}: empty or missing"
    return f"[read_file] {requested}:\n{content[:_READ_FILE_BUDGET]}"


def _read_directory_block(
    workspace_root: Path,
    path: Path,
    requested: str,
) -> str:
    """Bounded directory listing for a ``read_file`` request that names a dir."""
    label = requested or "(workspace root)"
    try:
        entries = sorted(
            path.iterdir(),
            key=lambda item: (not item.is_dir(), item.name.lower()),
        )
    except PermissionError:
        return f"[read_file] {label}: permission denied listing directory"
    except OSError as exc:
        return f"[read_file] {label}: cannot list directory ({exc})"
    if not entries:
        return f"[read_file] {label}: empty directory"
    lines: list[str] = [f"[read_file] {label} is a directory; entries:"]
    used = len(lines[0])
    shown = 0
    for entry in entries:
        if shown >= _READ_DIR_MAX_ENTRIES:
            break
        try:
            rel = entry.relative_to(workspace_root)
        except ValueError:
            rel = entry
        if entry.is_dir():
            line = f"  {rel}/"
        else:
            try:
                size = entry.stat().st_size
            except OSError:
                size = -1
            line = f"  {rel} ({size} bytes)" if size >= 0 else f"  {rel}"
        # Keep room for the truncation marker instead of cutting it off.
        reserve = 40 if len(entries) - shown > 1 else 0
        if used + 1 + len(line) + reserve > _READ_FILE_BUDGET:
            break
        lines.append(line)
        used += 1 + len(line)
        shown += 1
    remaining = len(entries) - shown
    if remaining > 0:
        lines.append(f"  ... (+{remaining} more)")
    return "\n".join(lines)[:_READ_FILE_BUDGET]


def audit_tail_block(workspace: Path) -> str:
    audit = Path(workspace) / ".opencode" / AUDIT_FILENAME
    try:
        lines = audit.read_text(encoding="utf-8").strip().splitlines()
    except OSError:
        return ""
    if not lines:
        return ""
    tail = lines[-_AUDIT_TAIL_LINES:]
    return "Recent audit events:\n" + "\n".join(
        f"  {line[-160:]}" for line in tail
    )
