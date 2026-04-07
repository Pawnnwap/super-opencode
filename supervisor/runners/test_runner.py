"""
supervisor/test_runner.py

Runs pytest (or a lightweight syntax/import check when pytest is absent)
against a target directory and returns structured results.

Used by the self-evolution loop to:
  1. Capture a baseline before opencode touches anything.
  2. Compare after each iteration to ensure the codebase is improving
     (or at least not regressing) before accepting a checkpoint.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RunTestResult:
    passed: int
    failed: int
    errors: int
    duration_s: float
    output: str  # full pytest / check output
    exit_code: int
    syntax_errors: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.syntax_errors is None:
            self.syntax_errors = []

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors

    @property
    def ok(self) -> bool:
        """True if no failures or errors."""
        return self.failed == 0 and self.errors == 0 and len(self.syntax_errors) == 0

    def summary(self) -> str:
        parts = [f"passed={self.passed}  failed={self.failed}  errors={self.errors}"]
        if self.syntax_errors:
            parts.append(f"syntax_errors={len(self.syntax_errors)}")
        parts.append(f"({self.duration_s:.1f}s)")
        return "  ".join(parts)

    def delta(self, baseline: RunTestResult) -> str:
        """Human-readable diff vs a baseline."""
        dp = self.passed - baseline.passed
        df = self.failed - baseline.failed
        de = self.errors - baseline.errors
        ds = len(self.syntax_errors) - len(baseline.syntax_errors)
        parts = []
        if dp:
            parts.append(f"passed {dp:+d}")
        if df:
            parts.append(f"failed {df:+d}")
        if de:
            parts.append(f"errors {de:+d}")
        if ds:
            parts.append(f"syntax_errors {ds:+d}")
        return ", ".join(parts) if parts else "no change"

    def is_regression_vs(self, baseline: RunTestResult) -> bool:
        """Return True if this result is strictly worse than baseline."""
        more_failures = (self.failed + self.errors) > (
            baseline.failed + baseline.errors
        )
        new_syntax = len(self.syntax_errors) > len(baseline.syntax_errors)
        return more_failures or new_syntax


class OcTestRunner:
    """
    Runs pytest in a subprocess and parses the exit code + stdout.
    Falls back to a per-file syntax check if pytest is not installed.
    """

    def __init__(self, workspace: Path, test_dir: str = "tests"):
        self.workspace = workspace
        self.test_dir = workspace / test_dir

    # ------------------------------------------------------------------ #
    # Public                                                               #
    # ------------------------------------------------------------------ #

    def run(self) -> RunTestResult:
        """Run tests and return a RunTestResult."""
        t0 = time.monotonic()

        if self._pytest_available():
            result = self._run_pytest(t0)
        else:
            result = self._run_syntax_check(t0)

        return result

    # ------------------------------------------------------------------ #
    # Internals                                                            #
    # ------------------------------------------------------------------ #

    def _pytest_available(self) -> bool:
        try:
            subprocess.run(
                [sys.executable, "-m", "pytest", "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def _run_pytest(self, t0: float) -> RunTestResult:
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            str(self.test_dir) if self.test_dir.exists() else str(self.workspace),
            "-v",
            "--tb=short",
            "--no-header",
            "-q",
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(self.workspace),
            timeout=300,
        )
        output = proc.stdout + proc.stderr
        passed, failed, errors = _parse_pytest_summary(output)
        return RunTestResult(
            passed=passed,
            failed=failed,
            errors=errors,
            duration_s=time.monotonic() - t0,
            output=output,
            exit_code=proc.returncode,
        )

    def _run_syntax_check(self, t0: float) -> RunTestResult:
        """Compile-check every .py file as a fallback."""
        errors: list[str] = []
        output_lines: list[str] = ["[syntax check mode — pytest not found]\n"]

        for py_file in sorted(self.workspace.rglob("*.py")):
            proc = subprocess.run(
                [sys.executable, "-m", "py_compile", str(py_file)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if proc.returncode != 0:
                msg = f"{py_file.relative_to(self.workspace)}: {proc.stderr.strip()}"
                errors.append(msg)
                output_lines.append(f"  SYNTAX ERROR: {msg}")
            else:
                output_lines.append(f"  ok: {py_file.relative_to(self.workspace)}")

        return RunTestResult(
            passed=0,
            failed=0,
            errors=0,
            duration_s=time.monotonic() - t0,
            output="\n".join(output_lines),
            exit_code=1 if errors else 0,
            syntax_errors=errors,
        )


# ------------------------------------------------------------------ #
# Pytest output parser                                                #
# ------------------------------------------------------------------ #


def _parse_pytest_summary(output: str) -> tuple[int, int, int]:
    """Extract (passed, failed, errors) from pytest -q output."""
    import re

    passed = failed = errors = 0
    # Look for lines like: "3 passed, 1 failed, 2 errors in 0.42s"
    pattern = re.compile(r"(\d+)\s+passed|(\d+)\s+failed|(\d+)\s+error", re.IGNORECASE)
    for m in pattern.finditer(output):
        if m.group(1):
            passed = int(m.group(1))
        if m.group(2):
            failed = int(m.group(2))
        if m.group(3):
            errors = int(m.group(3))
    return passed, failed, errors
