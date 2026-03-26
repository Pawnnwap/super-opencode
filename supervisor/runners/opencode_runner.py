"""
supervisor/opencode_runner.py

Drives opencode via its non-interactive CLI:

    opencode run "<prompt>" [--model <model>]

- stdin=DEVNULL  → guarantees no TTY, no interactive prompts
- All permissions auto-approved in `run` mode
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Generator, Optional

from supervisor.analyzers.opencode_step_detector import (
    OpencodeStepDetector,
    Step,
    PhaseTransition,
    StepProgress,
)
from supervisor.workspace.workspace_archiver import WorkspaceArchiver, ArchiveResult

logger = logging.getLogger(__name__)

# ── Executable resolution ─────────────────────────────────────────────────── #

_NAMES = ["opencode", "opencode.exe", "opencode.cmd", "opencode.bat"]

_WINDOWS_EXTRA_DIRS = [
    Path.home() / "AppData" / "Local" / "opencode",
    Path.home() / "AppData" / "Local" / "Programs" / "opencode",
    Path.home() / "AppData" / "Roaming" / "npm",
    Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / ".bin",
    Path.home() / ".local" / "bin",
    Path.home() / "bin",
    Path("C:/Program Files/opencode"),
    Path("C:/Program Files (x86)/opencode"),
    Path("C:/tools/opencode"),
]

_DOT_PATH_FILE = Path(__file__).parent.parent / ".opencode_path"
_DOT_MODEL_FILE = Path(__file__).parent.parent / ".opencode_model"


def find_opencode(explicit: str = "") -> str:
    explicit = str(explicit) if explicit is not None else ""
    if explicit.strip():
        return explicit.strip()

    if _DOT_PATH_FILE.exists():
        val = _DOT_PATH_FILE.read_text(encoding="utf-8").strip()
        if val:
            return val

    for name in _NAMES:
        found = shutil.which(name)
        if found:
            return found

    if sys.platform == "win32":
        for d in _WINDOWS_EXTRA_DIRS:
            if not d.exists():
                continue
            for name in _NAMES:
                c = d / name
                if c.exists():
                    return str(c)

    raise FileNotFoundError(
        "Cannot find the opencode executable.\n"
        "Run  python diagnose_opencode.py  to auto-detect it,\n"
        "or paste the full path into the 'opencode executable' field in the UI.\n"
        r"Common Windows location: C:\Users\<you>\AppData\Local\opencode\opencode.exe"
    )


# ── Result container ─────────────────────────────────────────────────────── #


class RunResult:
    def __init__(
        self,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        timed_out: bool = False,
        exception: str = "",
    ):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.timed_out = timed_out
        self.exception = exception

    @property
    def output(self) -> str:
        """Combined output surfaced to the supervisor loop."""
        parts = []
        if self.exception:
            parts.append(f"[EXCEPTION] {self.exception}")
        if self.timed_out:
            parts.append("[TIMED OUT]")
        # opencode run prints work output to stdout; progress/errors to stderr
        if self.stdout.strip():
            parts.append(self.stdout.strip())
        if self.stderr.strip():
            parts.append(f"[stderr]\n{self.stderr.strip()}")
        if self.returncode not in (0, None):
            parts.append(f"[exit {self.returncode}]")
        return "\n".join(parts)

    @property
    def ok(self) -> bool:
        return not self.timed_out and not self.exception and self.returncode == 0

    def diagnostic(self) -> str:
        lines = [
            f"exit_code : {self.returncode}",
            f"timed_out : {self.timed_out}",
            f"exception : {self.exception or '(none)'}",
            f"stdout    : {len(self.stdout)} chars",
            f"stderr    : {len(self.stderr)} chars",
        ]
        if self.stdout.strip():
            lines.append(f"--- stdout ---\n{self.stdout[:1200]}")
        if self.stderr.strip():
            lines.append(f"--- stderr ---\n{self.stderr[:1200]}")
        return "\n".join(lines)


# ── Runner ────────────────────────────────────────────────────────────────── #


class OpencodeRunner:
    """
    One send()/start() call = one  opencode run "<prompt>"  subprocess.
    stdin is always DEVNULL so opencode never tries to open a TUI or wait for input.
    """

    def __init__(
        self,
        workspace: Path,
        opencode_model: Optional[str] = None,
        opencode_executable: str = "",
        timeout: int = 300,
        agent: str = "",
        step_detector: Optional[OpencodeStepDetector] = None,
        on_step: Optional[Callable[[Step], None]] = None,
        on_transition: Optional[Callable[[PhaseTransition], None]] = None,
        on_progress: Optional[Callable[[StepProgress], None]] = None,
    ):
        self.workspace = workspace
        self.opencode_model = opencode_model
        self.opencode_executable = opencode_executable
        self.timeout = timeout
        self.agent = agent

        self._last_result: Optional[RunResult] = None
        self._chars_exchanged: int = 0
        self._alive: bool = False
        self._process: Optional[subprocess.Popen] = None
        self._archiver = WorkspaceArchiver(workspace)

        if step_detector is not None:
            self._step_detector = step_detector
        else:
            self._step_detector = OpencodeStepDetector(
                step_callback=on_step,
                transition_callback=on_transition,
                progress_callback=on_progress,
            )

    @property
    def step_detector(self) -> OpencodeStepDetector:
        return self._step_detector

    def get_step_progress(self) -> StepProgress:
        return self._step_detector.progress

    def reset_step_detector(self) -> None:
        self._step_detector.reset()

    # ------------------------------------------------------------------ #

    def start(self, initial_prompt: str) -> None:
        self._alive = True
        self._prepare_workspace()
        self._run_prompt(initial_prompt)

    def send(self, message: str) -> None:
        if not self._alive:
            raise RuntimeError("OpencodeRunner has been stopped.")
        self._run_prompt(message)

    def read_output(self, timeout: Optional[int] = None) -> tuple[str, bool]:
        if self._last_result is None:
            return "", False
        return self._last_result.output, self._last_result.timed_out

    def last_diagnostic(self) -> str:
        return self._last_result.diagnostic() if self._last_result else "(no run yet)"

    def stop(self) -> None:
        self._alive = False
        if self._process is not None:
            # Force kill the process to ensure immediate termination
            try:
                self._process.kill()
            except Exception:
                # Ignore errors if the process has already terminated
                pass

    @property
    def is_alive(self) -> bool:
        return self._alive

    @property
    def estimated_context_tokens(self) -> int:
        return self._chars_exchanged // 4

    # ------------------------------------------------------------------ #

    def _prepare_workspace(self) -> None:
        """
        Ensure the workspace exists and contains an opencode project marker
        so opencode anchors its project root here instead of walking up the
        directory tree to a parent folder.
        """
        self.workspace.mkdir(parents=True, exist_ok=True)

        # opencode looks for .opencode/ as its project root marker.
        # Create it if missing so opencode doesn't escape the workspace.
        oc_dir = self.workspace / ".opencode"
        oc_dir.mkdir(exist_ok=True)

        # Minimal config.json that tells opencode this is the project root
        # and disables permission prompts that would block non-interactive use.
        config_path = oc_dir / "config.json"
        if not config_path.exists():
            import json

            config_path.write_text(
                json.dumps({"autoapprove": True}, indent=2),
                encoding="utf-8",
            )
            logger.info("Created .opencode/config.json in workspace")

    def _run_prompt(self, prompt: str) -> None:
        exe = find_opencode(self.opencode_executable)
        cmd = self._build_cmd(exe, prompt)
        logger.info("CMD: %s", " ".join(cmd))

        # .cmd/.bat on Windows need shell=True
        use_shell = sys.platform == "win32" and exe.lower().endswith(
            (".cmd", ".bat", ".ps1")
        )

        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,  # ← kills TUI / interactive prompts
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(self.workspace),
                env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
                shell=use_shell,
            )

            # Wait for process to complete with timeout
            try:
                stdout, stderr = self._process.communicate(timeout=self.timeout)
                returncode = self._process.returncode
            except subprocess.TimeoutExpired:
                # Timeout occurred
                stdout_val = self._process.stdout.read() if self._process.stdout else ""
                stderr_val = self._process.stderr.read() if self._process.stderr else ""

                stdout_val = (
                    stdout_val.decode("utf-8", errors="replace")
                    if isinstance(stdout_val, bytes)
                    else (stdout_val or "")
                )
                stderr_val = (
                    stderr_val.decode("utf-8", errors="replace")
                    if isinstance(stderr_val, bytes)
                    else (stderr_val or "")
                )

                self._last_result = RunResult(
                    stdout=stdout_val,
                    stderr=stderr_val,
                    returncode=-1,
                    timed_out=True,
                )
                logger.warning("opencode timed out after %ds", self.timeout)
                self._chars_exchanged += len(prompt) + len(self._last_result.output)
                return

            stdout = stdout or ""
            stderr = stderr or ""

            # Detect known fatal errors
            combined_lower = (stdout + stderr).lower()
            if (
                "unable to connect" in combined_lower
                or "is the computer able to access" in combined_lower
            ):
                stderr = (
                    "[OPENCODE CONFIG ERROR] opencode cannot reach the AI provider.\n"
                    "Fix: run 'opencode' interactively → configure a working provider,\n"
                    "or set the model in the UI 'opencode model' field.\n\n"
                    "Raw error:\n" + (stdout + stderr).strip()
                )
                stdout = ""

            self._last_result = RunResult(
                stdout=stdout,
                stderr=stderr,
                returncode=returncode,
            )
            logger.info(
                "opencode exit=%d  stdout=%d  stderr=%d",
                returncode,
                len(stdout),
                len(stderr),
            )
            if stderr.strip():
                logger.info("stderr: %s", stderr[:400])

        except Exception as exc:
            self._last_result = RunResult(exception=str(exc), returncode=-1)
            logger.error("opencode launch error: %s", exc)

        self._chars_exchanged += len(prompt) + len(self._last_result.output)

    def _build_cmd(self, exe: str, prompt: str) -> list[str]:
        # opencode run [--agent <agent>] "<prompt>" [--model <model>]
        cmd = [exe, "run"]

        agent = str(self.agent or "").strip()
        if agent:
            cmd += ["--agent", agent]

        cmd.append(prompt)

        # Resolve model: explicit UI field > .opencode_model file
        model = str(self.opencode_model or "").strip()
        if not model and _DOT_MODEL_FILE.exists():
            model = _DOT_MODEL_FILE.read_text(encoding="utf-8").strip()
        if model:
            cmd += ["--model", model]

        return cmd

    def process_step_detection(self, output: str) -> Generator[dict, None, None]:
        for event in self._step_detector.process_output(output):
            yield event

    def get_step_events(self, output: str) -> list[dict]:
        events = []
        for event in self._step_detector.process_output(output):
            events.append(event)
        return events

    def get_current_phase(self) -> str:
        return self._step_detector.progress.phase.name.lower()

    def is_active(self) -> bool:
        return self._step_detector.is_progressing()

    def is_progressing(self) -> bool:
        return self._step_detector.is_progressing()

    def is_waiting_for_output(self) -> bool:
        return self._step_detector.is_waiting_for_output()

    def get_activity_state(self) -> str:
        return self._step_detector.get_activity_state()

    def get_step_summary(self) -> dict:
        progress = self._step_detector.progress
        return {
            "current_step": progress.current_step,
            "total_steps": progress.total_steps_estimate,
            "percentage": progress.percentage,
            "phase": progress.phase.name.lower(),
            "completed_phases": list(progress.completed_phases),
            "step_count": len(progress.steps),
        }

    def send_cleanup_inquiry(self, candidates: list[str]) -> None:
        """
        Send an inquiry to opencode about the identified cleanup candidates.
        Opencode will evaluate the files and respond with its recommendations.
        Files will be archived instead of deleted to preserve history.
        """
        if not candidates:
            return

        workspace_rel = (
            self.workspace.relative_to(self.workspace)
            if self.workspace.is_absolute()
            else self.workspace
        )
        inquiry = (
            f"You are working in workspace: {workspace_rel}\n\n"
            f"I have identified the following files that may be outdated or unused:\n"
        )
        for i, candidate in enumerate(candidates, 1):
            inquiry += f"  {i}. {candidate}\n"

        inquiry += (
            "\nPlease analyze these files and respond with a JSON list of file paths "
            "that should be archived. These files will be moved to .archive/ "
            "instead of being deleted, preserving historical versions.\n"
            "Consider:\n"
            "- Files that are clearly temporary, backup, or cache files\n"
            "- Files that are not referenced by other code\n"
            "- Files that appear to be duplicate or superseded versions\n"
            "- Any __pycache__ directories\n\n"
            "IMPORTANT: Never select protected paths (.opencode/, .checkpoints/, .archive/) "
            "for archiving.\n\n"
            "Respond ONLY with a JSON array of file paths to archive, nothing else. "
            'Example: ["file1.bak", "file2.tmp"]'
        )

        logger.info(
            "Sending cleanup inquiry to opencode for %d candidates", len(candidates)
        )
        self.send(inquiry)

    def identify_cleanup_candidates(self) -> list[str]:
        """
        Identify files that might be outdated or unused, then send an inquiry to
        opencode for its recommendation on what should be deleted.
        Returns opencode's response parsed as a list of file paths.
        """
        import re

        candidates: list[str] = []
        workspace = self.workspace

        _VERSION_PATTERNS = [
            re.compile(r"\.bak$"),
            re.compile(r"\.backup$"),
            re.compile(r"\.old$"),
            re.compile(r"\.orig$"),
            re.compile(r"\.tmp$"),
            re.compile(r"~\d+$"),
            re.compile(r"\.v\d+$"),
            re.compile(r"_backup_\d+$"),
            re.compile(r"_old_\d+$"),
            re.compile(r"\.\d+$"),
        ]

        _SOURCE_EXTS = {
            ".py",
            ".pyc",
            ".pyo",
            ".pyd",
            ".md",
            ".txt",
            ".rst",
            ".json",
            ".yaml",
            ".yml",
            ".toml",
            ".cfg",
            ".ini",
            ".js",
            ".ts",
            ".jsx",
            ".tsx",
            ".css",
            ".scss",
            ".html",
            ".xml",
            ".sh",
            ".bat",
            ".ps1",
        }

        def should_ignore(path: Path) -> bool:
            if not path.is_file():
                if not (path.is_dir() and path.name == "__pycache__"):
                    return True
            rel = path.relative_to(workspace)
            if ".checkpoints" in rel.parts:
                return True
            if path == workspace / ".checkpoints":
                return True
            ignore_dirs = {
                ".git",
                ".venv",
                "venv",
                "node_modules",
                ".mypy_cache",
                ".opencode",
            }
            if any(part in ignore_dirs for part in rel.parts):
                return True
            return False

        def is_versioned_backup(name: str) -> bool:
            for pattern in _VERSION_PATTERNS:
                if pattern.search(name):
                    return True
            return False

        def get_base_name(path: Path) -> str:
            name = path.name
            base = name
            changed = True
            while changed:
                changed = False
                for pattern in _VERSION_PATTERNS:
                    new_base = pattern.sub("", base)
                    if new_base != base:
                        base = new_base
                        changed = True
                        break
            return base

        candidates.extend(
            self._identify_versioned_backups(
                workspace, should_ignore, is_versioned_backup, get_base_name
            )
        )
        candidates.extend(
            self._identify_orphaned_files(workspace, should_ignore, _SOURCE_EXTS)
        )

        if candidates:
            self.send_cleanup_inquiry(candidates)

        return candidates

    def _identify_versioned_backups(
        self,
        workspace: Path,
        should_ignore,
        is_versioned_backup,
        get_base_name,
    ) -> list[str]:
        candidates: list[str] = []
        backup_groups: dict[str, list[Path]] = {}
        all_files: dict[str, Path] = {}

        for path in workspace.rglob("*"):
            if should_ignore(path):
                continue
            all_files[path.name] = path
            if is_versioned_backup(path.name):
                base = get_base_name(path)
                if base not in backup_groups:
                    backup_groups[base] = []
                backup_groups[base].append(path)

        for base_name, backups in backup_groups.items():
            if base_name in all_files:
                backups.append(all_files[base_name])
            backups_sorted = sorted(backups, key=lambda p: len(p.name))
            for backup in backups_sorted[1:]:
                candidates.append(str(backup.relative_to(workspace)))

        return candidates

    def _identify_orphaned_files(
        self,
        workspace: Path,
        should_ignore,
        source_exts: set,
    ) -> list[str]:
        candidates: list[str] = []
        import re

        import_patterns = [
            (re.compile(r"^(?:from|import)\s+([\w.]+)", re.MULTILINE), "py"),
            (
                re.compile(r'require\s*\(\s*["\']([^"\']+)["\']\s*\)', re.MULTILINE),
                "js",
            ),
            (re.compile(r'import\s+.*?from\s+["\']([^"\']+)["\']', re.MULTILINE), "js"),
            (re.compile(r'#include\s*["<]([^">]+)[">]', re.MULTILINE), "c"),
        ]

        referenced_paths: set[str] = set()
        for path in workspace.rglob("*"):
            if should_ignore(path):
                continue
            if path.suffix not in source_exts and not path.name.endswith(".h"):
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
                for pattern, ptype in import_patterns:
                    for match in pattern.finditer(content):
                        ref = match.group(1)
                        if ptype == "py":
                            ref = ref.replace(".", "/")
                            if not ref.endswith(".py"):
                                ref += ".py"
                        referenced_paths.add(ref)
            except Exception:
                pass

        for path in workspace.rglob("*"):
            if should_ignore(path):
                continue
            rel_str = str(path.relative_to(workspace))

            is_pycache_dir = path.is_dir() and path.name == "__pycache__"
            if is_pycache_dir:
                candidates.append(rel_str)
                continue

            is_cache_file = path.suffix in {
                ".pyc",
                ".pyo",
                ".pyc.tmp",
            } or path.name.endswith(".pyc")
            if is_cache_file:
                candidates.append(rel_str)
                continue

        return candidates

    def archive_files(self, files: list[str]) -> ArchiveResult:
        """
        Archive specified files instead of deleting them.
        This preserves historical versions while cleaning up the workspace.
        """
        return self._archiver.archive_workspace(label="cleanup", files_to_archive=files)

    def archive_before_new_run(self) -> ArchiveResult:
        """
        Archive the current workspace state before starting a new run.
        Called at the beginning of a supervisor loop execution.
        """
        return self._archiver.archive_before_new_run()

    def get_archiver(self) -> WorkspaceArchiver:
        """Return the workspace archiver instance."""
        return self._archiver

    def list_archives(self) -> list[dict]:
        """List all available archives."""
        return self._archiver.list_archives()

    def get_archive_stats(self) -> dict:
        """Get archive statistics."""
        return self._archiver.get_archive_stats()

    def get_files_read(self) -> list[str]:
        """
        Extract file references from opencode output.
        Looks for patterns like file paths, file operations, and file references.
        """
        import re

        if not self._last_result or not self._last_result.output:
            return []

        output = self._last_result.output
        files: set[str] = set()

        # Patterns for file references in opencode output
        file_patterns = [
            # File paths with common extensions
            re.compile(
                r"(?:^|\s)([a-zA-Z_][\w./\\-]*\.(?:py|js|ts|jsx|tsx|json|yaml|yml|toml|md|txt|rst|cfg|ini|sh|bat|ps1|html|css|xml))(?:\s|$)",
                re.MULTILINE,
            ),
            # "file:" or "path:" prefixed paths
            re.compile(r"(?:file|path):\s*([a-zA-Z_][\w./\\-]+)", re.IGNORECASE),
            # File operations like "Reading file X" or "Creating file Y"
            re.compile(
                r"(?:reading|creating|writing|modifying|editing|updating|opening)\s+(?:file\s+)?([a-zA-Z_][\w./\\-]+)",
                re.IGNORECASE,
            ),
            # File paths in code blocks
            re.compile(
                r"```[\w]*\s*\n\s*(?:#\s*)?([a-zA-Z_][\w./\\-]+\.(?:py|js|ts|json|yaml|yml|toml|md|txt))",
                re.MULTILINE,
            ),
        ]

        for pattern in file_patterns:
            for match in pattern.finditer(output):
                file_path = match.group(1)
                if file_path and len(file_path) > 2:  # Filter out very short matches
                    files.add(file_path)

        return sorted(files)
