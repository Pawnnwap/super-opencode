from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

_AUTOFIX_TOOL_TIMEOUT = 30


def _run_fix(cmd: list[str]) -> tuple[str, str, int]:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_AUTOFIX_TOOL_TIMEOUT,
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "", f"timed out after {_AUTOFIX_TOOL_TIMEOUT}s", 1
    except Exception as exc:
        return "", str(exc), 1


def _ensure_fix_tool(package: str, binary: str) -> bool:
    if shutil.which(binary):
        return True
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", package, "-q"],
            capture_output=True,
            timeout=60,
        )
        return result.returncode == 0
    except Exception:
        return False


@dataclass
class _FixSummary:
    tool: str
    ok: bool
    note: str = ""


def _autofix_isort(filepath: str) -> _FixSummary:
    if not _ensure_fix_tool("isort", "isort"):
        return _FixSummary("isort", ok=False, note="not installed")
    _, stderr, returncode = _run_fix(["isort", "--quiet", filepath])
    return _FixSummary("isort", ok=(returncode == 0), note=stderr.strip() if returncode != 0 else "")


def _autofix_autopep8(filepath: str) -> _FixSummary:
    if not _ensure_fix_tool("autopep8", "autopep8"):
        return _FixSummary("autopep8", ok=False, note="not installed")
    safe_fixes = "E1,E2,E3,E4,W1,W2,W3,W6"
    _, stderr, returncode = _run_fix([
        "autopep8",
        "--in-place",
        f"--select={safe_fixes}",
        "--max-line-length=88",
        filepath,
    ])
    return _FixSummary("autopep8", ok=(returncode == 0), note=stderr.strip() if returncode != 0 else "")


def _autofix_pyupgrade(filepath: str) -> _FixSummary:
    if not _ensure_fix_tool("pyupgrade", "pyupgrade"):
        return _FixSummary("pyupgrade", ok=False, note="not installed")
    _, stderr, returncode = _run_fix(["pyupgrade", "--py38-plus", filepath])
    ok = returncode in (0, 1)
    return _FixSummary("pyupgrade", ok=ok, note=stderr.strip() if not ok else "")


def _autofix_ruff(filepath: str) -> _FixSummary:
    if not _ensure_fix_tool("ruff", "ruff"):
        return _FixSummary("ruff", ok=False, note="not installed")
    _, stderr, returncode = _run_fix([
        "ruff",
        "check",
        "--fix-only",
        "--select=ALL",
        "--ignore=F401,F811,F841,ERA,E,W,B,C4,ISC,N",
        filepath,
    ])
    ok = returncode in (0, 1)
    return _FixSummary("ruff", ok=ok, note=stderr.strip() if not ok else "")


_PARTIAL_FIXERS: list[tuple[str, Callable[[str], _FixSummary]]] = [
    ("isort", _autofix_isort),
    ("autopep8", _autofix_autopep8),
    ("pyupgrade", _autofix_pyupgrade),
    ("ruff", _autofix_ruff),
]

_SKIPPED_FIXERS_REASON = (
    "autoflake (unused imports/vars) and deadcode (unused functions/classes) are "
    "skipped: hashline operates on specific sections, not the whole codebase. "
    "Removing 'unused' symbols from a fragment may break callers elsewhere."
)


def _run_autofix(filepath: str) -> list[dict[str, Any]]:
    results = []
    for label, runner in _PARTIAL_FIXERS:
        try:
            summary = runner(filepath)
            results.append({
                "tool": summary.tool,
                "status": "ok" if summary.ok else "error",
                **({"note": summary.note} if summary.note else {}),
            })
        except Exception as exc:
            logger.warning("Autofix tool %r raised unexpectedly: %s", label, exc)
            results.append({"tool": label, "status": "error", "note": str(exc)})
    return results
