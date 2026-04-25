"""Occam Razor post-success pass for live runs.

This stage never edits the live workspace. It identifies the final code,
copies that code into an archive-owned workspace, and lets opencode reduce
only redundant code/logic inside that copy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from collections.abc import Generator
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from supervisor.core.loop_base import Event, _ev
from supervisor.protocols.protocol import load_protocol
from supervisor.runners.opencode_runner import OpencodeRunner
from supervisor.utils.config import SupervisorConfig
from supervisor.utils.filesystem.path_filters import should_skip_path
from supervisor.workspace.ignore_patterns import IgnoreMatcher

logger = logging.getLogger(__name__)

_CODE_EXTENSIONS = {
    ".bash",
    ".bat",
    ".c",
    ".cfg",
    ".cmd",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".kt",
    ".kts",
    ".md",
    ".php",
    ".ps1",
    ".py",
    ".pyi",
    ".rb",
    ".rs",
    ".rst",
    ".sass",
    ".scala",
    ".scss",
    ".sh",
    ".sql",
    ".svelte",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".vue",
    ".yaml",
    ".yml",
}

_CODE_FILENAMES = {
    "Dockerfile",
    "Makefile",
    "Pipfile",
    "Procfile",
    "Taskfile",
    "bun.lock",
    "constraints.txt",
    "package-lock.json",
    "pnpm-lock.yaml",
    "poetry.lock",
    "requirements-dev.txt",
    "requirements.txt",
    "uv.lock",
    "yarn.lock",
}

_GENERATED_FILENAMES = {
    "evolution_report.md",
    "failure_report.md",
    "meta_protocol.md",
    "occam_manifest.json",
    "occam_report.md",
    "protocol.md",
    "summary.md",
}


@dataclass(frozen=True)
class CodeMetrics:
    file_count: int
    total_bytes: int
    total_lines: int


@dataclass(frozen=True)
class FileDelta:
    added_files: list[str]
    deleted_files: list[str]
    changed_files: list[str]


def _rel_posix(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def is_final_code_file(
    path: Path,
    workspace: Path,
    ignore_matcher: IgnoreMatcher | None = None,
) -> bool:
    """Return True when *path* belongs to the final-code snapshot."""
    if not path.is_file() or path.is_symlink():
        return False

    workspace = workspace.resolve()
    try:
        rel = path.resolve().relative_to(workspace)
    except ValueError:
        return False

    rel_posix = rel.as_posix()
    if should_skip_path(rel):
        return False
    if ignore_matcher and ignore_matcher.matches(rel_posix):
        return False

    name = path.name
    if name.lower() in _GENERATED_FILENAMES:
        return False
    if name in _CODE_FILENAMES:
        return True
    return path.suffix.lower() in _CODE_EXTENSIONS


def identify_final_code_files(workspace: Path) -> list[str]:
    """List final code/config/docs files in stable relative-path order."""
    workspace = workspace.resolve()
    ignore_matcher = IgnoreMatcher(workspace)
    ignore_matcher.load_from_workspace(workspace)

    files: list[str] = []
    for path in sorted(workspace.rglob("*")):
        if is_final_code_file(path, workspace, ignore_matcher):
            files.append(_rel_posix(path, workspace))
    return files


def measure_code_metrics(workspace: Path, files: list[str] | None = None) -> CodeMetrics:
    """Measure bytes and newline-based line count for code files."""
    workspace = workspace.resolve()
    files = files if files is not None else identify_final_code_files(workspace)
    total_bytes = 0
    total_lines = 0
    counted = 0

    for rel in files:
        path = workspace / rel
        if not path.is_file():
            continue
        data = path.read_bytes()
        total_bytes += len(data)
        total_lines += data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
        counted += 1

    return CodeMetrics(
        file_count=counted,
        total_bytes=total_bytes,
        total_lines=total_lines,
    )


def copy_final_code_snapshot(source: Path, target: Path, files: list[str]) -> list[str]:
    """Copy identified final code into *target*, preserving relative paths."""
    source = source.resolve()
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    for rel in files:
        src = (source / rel).resolve()
        try:
            src.relative_to(source)
        except ValueError:
            continue
        if not src.is_file() or src.is_symlink():
            continue
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel)
    return copied


def _hash_code_files(workspace: Path, files: list[str]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for rel in files:
        path = workspace / rel
        if path.is_file():
            hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def diff_file_hashes(before: dict[str, str], after: dict[str, str]) -> FileDelta:
    before_paths = set(before)
    after_paths = set(after)
    return FileDelta(
        added_files=sorted(after_paths - before_paths),
        deleted_files=sorted(before_paths - after_paths),
        changed_files=sorted(
            rel for rel in before_paths & after_paths if before[rel] != after[rel]
        ),
    )


class OccamRazorStage:
    """Run optional code-reduction pass against an isolated copy."""

    def __init__(self, config: SupervisorConfig, protocol_text: str):
        self.config = config
        self.protocol_text = protocol_text

    def run(self) -> Generator[Event]:
        workspace = self.config.workspace.resolve()
        files = identify_final_code_files(workspace)
        before_metrics = measure_code_metrics(workspace, files)
        before_hashes = _hash_code_files(workspace, files)

        if not files:
            yield _ev("warn", "[occam] No final code files identified; skipping Occam Razor pass.")
            return

        run_dir = self._make_run_dir(workspace)
        copy_workspace = run_dir / "final_code"
        protocol_snapshot = run_dir / "protocol.md"
        manifest_path = run_dir / "occam_manifest.json"

        copied = copy_final_code_snapshot(workspace, copy_workspace, files)
        protocol_snapshot.write_text(self.protocol_text, encoding="utf-8")

        manifest: dict[str, Any] = {
            "stage": "occam_razor",
            "created_at": int(time.time()),
            "original_workspace": str(workspace),
            "copy_workspace": str(copy_workspace),
            "protocol_snapshot": str(protocol_snapshot),
            "final_code_files": copied,
            "before": asdict(before_metrics),
            "status": "prepared",
        }
        self._write_manifest(manifest_path, manifest)

        yield _ev(
            "info",
            "[occam] Final code identified: "
            f"{before_metrics.file_count} files, {before_metrics.total_lines} lines, "
            f"{before_metrics.total_bytes} bytes.",
        )
        yield _ev("info", f"[occam] Copy workspace: {copy_workspace}")
        yield _ev("info", f"[occam] Manifest: {manifest_path}")

        prompt = self._build_prompt(copied, before_metrics, copy_workspace)
        yield _ev("opencode_prompt", prompt)

        output = ""
        timed_out = False
        try:
            runner = OpencodeRunner.from_config(
                replace(
                    self.config,
                    workspace=copy_workspace,
                    protocol_path=protocol_snapshot,
                    protected_files=(),
                    enable_python_scanner=False,
                    enable_occam_razor=False,
                    plan_mode_rounds=0,
                ),
                agent="build",
            )
            try:
                yield from runner.start(prompt)
                output, timed_out = runner.read_output()
            finally:
                runner.stop()
        except Exception as exc:
            manifest.update({"status": "opencode_error", "error": str(exc)})
            self._write_manifest(manifest_path, manifest)
            logger.exception("Occam Razor opencode pass failed")
            yield _ev("warn", f"[occam] opencode pass failed: {exc}")
            return

        if output.strip():
            yield _ev("opencode_output", output)
        if timed_out:
            yield _ev("warn", "[occam] opencode timed out; measuring partial copy state.")

        after_files = identify_final_code_files(copy_workspace)
        after_metrics = measure_code_metrics(copy_workspace, after_files)
        after_hashes = _hash_code_files(copy_workspace, after_files)
        delta = diff_file_hashes(before_hashes, after_hashes)
        bytes_removed = before_metrics.total_bytes - after_metrics.total_bytes
        lines_removed = before_metrics.total_lines - after_metrics.total_lines

        manifest.update(
            {
                "status": "opencode_done",
                "timed_out": timed_out,
                "after": asdict(after_metrics),
                "delta": asdict(delta),
                "bytes_removed": bytes_removed,
                "lines_removed": lines_removed,
            },
        )
        self._write_manifest(manifest_path, manifest)

        yield _ev(
            "info",
            "[occam] Reduction result: "
            f"{lines_removed} lines, {bytes_removed} bytes "
            f"({len(delta.changed_files)} changed, {len(delta.deleted_files)} deleted, "
            f"{len(delta.added_files)} added files in copy).",
        )

        yield from self._verify_reduced_copy(
            copy_workspace=copy_workspace,
            protocol_snapshot=protocol_snapshot,
            opencode_output=output,
            manifest_path=manifest_path,
            manifest=manifest,
        )

    def _make_run_dir(self, workspace: Path) -> Path:
        archive_root = workspace / ".archive"
        stamp = time.strftime("%Y%m%d_%H%M%S")
        base = archive_root / f"run_{stamp}_occam_razor"
        run_dir = base
        counter = 1
        while run_dir.exists():
            counter += 1
            run_dir = archive_root / f"run_{stamp}_{counter:02d}_occam_razor"
        run_dir.mkdir(parents=True, exist_ok=False)
        return run_dir

    def _build_prompt(
        self,
        files: list[str],
        before_metrics: CodeMetrics,
        copy_workspace: Path,
    ) -> str:
        listed_files = "\n".join(f"- {rel}" for rel in files[:300])
        if len(files) > 300:
            listed_files += (
                f"\n- ... {len(files) - 300} more copied final-code files "
                "(full list recorded in supervisor manifest)."
            )

        return (
            "OCCAM RAZOR PASS. You are working in an isolated COPY of final code.\n\n"
            f"Current cwd / only editable root: {copy_workspace.resolve()}\n"
            "Original final code is preserved elsewhere. Do not read, write, move, "
            "or delete anything outside cwd.\n\n"
            "Purpose: reduce extra code/logic only. The live run already met all targets.\n"
            "Find the smallest safe reduction that still satisfies the protocol.\n\n"
            "Hard rules:\n"
            "1. Add no new feature, dependency, behavior, target, or protocol rule.\n"
            "2. Add no new files. Existing lock/cache files may update only if needed by a check.\n"
            "3. Prefer deleting dead code, duplicate helpers, redundant wrappers, and needless branches.\n"
            "4. If unsure whether code is needed, keep it.\n"
            "5. Keep public behavior required by protocol unchanged.\n"
            "6. Run only existing relevant checks/tests if available.\n\n"
            "Final code files identified before this pass:\n"
            f"{listed_files}\n\n"
            "Baseline final-code size:\n"
            f"- files: {before_metrics.file_count}\n"
            f"- lines: {before_metrics.total_lines}\n"
            f"- bytes: {before_metrics.total_bytes}\n\n"
            "Protocol that reduced copy must still meet:\n"
            f"{self.protocol_text}\n\n"
            "Return concise OCCAM SUMMARY with files changed, lines/bytes reduced estimate, "
            "checks run, and why protocol still passes."
        )

    def _verify_reduced_copy(
        self,
        *,
        copy_workspace: Path,
        protocol_snapshot: Path,
        opencode_output: str,
        manifest_path: Path,
        manifest: dict[str, Any],
    ):
        yield _ev("info", "[occam] Supervisor verifying reduced copy against protocol...")
        try:
            from supervisor.analyzers.codebase_analyzer import snapshot_codebase
            from supervisor.core.llm_supervisor import LLMSupervisor

            protocol = load_protocol(protocol_snapshot)
            extra_system = "\n\n## Occam copy codebase\n" + snapshot_codebase(
                copy_workspace,
            ).digest_for_prompt(max_files=15)
            supervisor = LLMSupervisor(
                protocol=protocol,
                workspace=copy_workspace,
                model=self.config.supervisor_model,
                extra_system=extra_system,
                read_external_feedback=False,
                max_tokens=self.config.max_tokens,
                truncation_enabled=self.config.truncation_enabled,
                max_history_turns=self.config.max_history_turns,
                compact_intermediate_steps=False,
                model_backup=self.config.supervisor_model_backup,
                api_key=self.config.openai_api_key or None,
                base_url=self.config.openai_base_url or None,
            )
            verdict = supervisor.judge(opencode_output or "Occam pass produced no output.")
        except Exception as exc:
            manifest.update({"verification": {"status": "error", "error": str(exc)}})
            self._write_manifest(manifest_path, manifest)
            logger.exception("Occam Razor verification failed")
            yield _ev("warn", f"[occam] Supervisor verification failed: {exc}")
            return

        manifest.update(
            {
                "verification": {
                    "status": "done",
                    "all_targets_met": verdict.all_targets_met,
                    "raw": verdict.raw,
                },
            },
        )
        self._write_manifest(manifest_path, manifest)
        yield _ev("supervisor_response", verdict.raw)

        if verdict.all_targets_met:
            yield _ev("info", "[occam] Reduced copy still meets protocol.")
        else:
            yield _ev(
                "warn",
                "[occam] Reduced copy did not pass supervisor verification. "
                "Original final code remains untouched.",
            )

    @staticmethod
    def _write_manifest(path: Path, data: dict[str, Any]) -> None:
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
