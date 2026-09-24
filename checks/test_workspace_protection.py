"""Protection invariants around .archive/ and the runner shell policy.

Regression coverage for the sop_indicator_study_0923_1 incident: a restarted
session received a compaction prompt inviting it to remove "backup copies"
with no protected-path reminder in context, the agent deleted the whole
.archive/ directory, and codex's Remove-Item exec-policy rejections only
added friction afterwards (258 rejections in two days, archive lost anyway).
"""

from __future__ import annotations

from pathlib import Path

from supervisor.prompts.templates import (
    INIT_PROMPT_TEMPLATE,
    RESTART_PROMPT_TEMPLATE,
    SELF_EVOLUTION_INIT_PROMPT_TEMPLATE,
)
from supervisor.runners.codex_support.command_builder import build_cmd
from supervisor.utils.filesystem.file_permissions import set_file_readonly
from supervisor.workspace.cleanup_candidates import identify_cleanup_candidates
from supervisor.workspace.workspace_archiver import WorkspaceArchiver
from supervisor.workspace.workspace_guard import WorkspaceGuard


# --------------------------------------------------------------------------- #
# Prompt invariants: the agent must see the archive rule and the shell policy
# in every session-start template, including post-compaction restarts (the
# restarted session is exactly where the incident happened).
# --------------------------------------------------------------------------- #


def test_restart_template_carries_archive_and_shell_policy():
    assert ".archive/" in RESTART_PROMPT_TEMPLATE
    assert "do not modify it" in RESTART_PROMPT_TEMPLATE
    assert "SHELL POLICY" in RESTART_PROMPT_TEMPLATE
    assert "Remove-Item" in RESTART_PROMPT_TEMPLATE


def test_init_templates_carry_shell_policy():
    for template in (INIT_PROMPT_TEMPLATE, SELF_EVOLUTION_INIT_PROMPT_TEMPLATE):
        assert ".archive/" in template
        assert "SHELL POLICY" in template
        assert "shutil.rmtree" in template


def test_codex_runner_uses_yolo_flag():
    cmd = build_cmd(
        exe="codex",
        prompt="p",
        agent="a",
        codex_model=None,
        use_continue=False,
        session_id=None,
    )
    assert "--yolo" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd


# --------------------------------------------------------------------------- #
# Guard: protection reminder injection.
# --------------------------------------------------------------------------- #


def test_sanitize_with_protection_always_inject(tmp_path: Path):
    guard = WorkspaceGuard(tmp_path)
    msg, ws_v, prot_v = guard.sanitize_with_protection(
        "keep exactly one copy of every file", always_inject=True,
    )
    assert ws_v == [] and prot_v == []
    assert "CRITICAL PROTECTION" in msg


def test_sanitize_with_protection_default_skips_clean_message(tmp_path: Path):
    guard = WorkspaceGuard(tmp_path)
    msg, ws_v, prot_v = guard.sanitize_with_protection(
        "keep exactly one copy of every file",
    )
    assert ws_v == [] and prot_v == []
    assert "CRITICAL PROTECTION" not in msg


def test_sanitize_with_protection_flags_protected_delete(tmp_path: Path):
    guard = WorkspaceGuard(tmp_path)
    msg, ws_v, prot_v = guard.sanitize_with_protection(
        "please delete .archive snapshots",
    )
    assert ws_v == []
    assert prot_v
    assert "CRITICAL PROTECTION" in msg


# --------------------------------------------------------------------------- #
# Cleanup candidates must never propose anything inside archive trees.
# --------------------------------------------------------------------------- #


def test_cleanup_candidates_exclude_archive_trees(tmp_path: Path):
    (tmp_path / "common.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "common.py.bak").write_text("x = 0\n", encoding="utf-8")
    for rel in (
        ".archive/run_1/code/old.py",
        ".archive/run_2/code/old.py.bak",
        "archive/notes.py",
    ):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("# old\n", encoding="utf-8")
    pycache = tmp_path / "__pycache__"
    pycache.mkdir()
    (pycache / "common.cpython-311.pyc").write_bytes(b"\x00")

    candidates = identify_cleanup_candidates(tmp_path)

    assert all(
        not c.startswith((".archive", "archive")) for c in candidates
    ), candidates
    assert "common.py.bak" in candidates
    assert "__pycache__" in candidates


# --------------------------------------------------------------------------- #
# Archiver retention: supervisor prunes its own snapshots, never the agent.
# --------------------------------------------------------------------------- #


def _make_snapshot(archive_root: Path, name: str, readonly: bool = False) -> Path:
    d = archive_root / name
    d.mkdir(parents=True)
    f = d / "f.txt"
    f.write_text("x", encoding="utf-8")
    if readonly:
        set_file_readonly(str(f))
    return d


def test_archiver_prunes_oldest_beyond_keep_last(tmp_path: Path):
    archive_root = tmp_path / ".archive"
    _make_snapshot(archive_root, "run_1000000000_0000_old", readonly=True)
    _make_snapshot(archive_root, "run_1000000001_0001_old")
    _make_snapshot(archive_root, "run_1000000002_0002_old")

    archiver = WorkspaceArchiver(tmp_path, keep_last=3)
    result = archiver.archive_workspace(label="t")

    assert result.success
    names = sorted(p.name for p in archive_root.iterdir() if p.is_dir())
    # Oldest (with its read-only file) gone; the other two + the new one stay.
    assert "run_1000000000_0000_old" not in names
    assert "run_1000000001_0001_old" in names
    assert "run_1000000002_0002_old" in names
    assert len(names) == 3
    assert "pruned" in result.message


def test_archiver_keep_last_zero_disables_pruning(tmp_path: Path):
    archive_root = tmp_path / ".archive"
    _make_snapshot(archive_root, "run_1000000000_0000_old")
    _make_snapshot(archive_root, "run_1000000001_0001_old")

    archiver = WorkspaceArchiver(tmp_path, keep_last=0)
    result = archiver.archive_workspace(label="t")

    assert result.success
    names = [p.name for p in archive_root.iterdir() if p.is_dir()]
    assert len(names) == 3
    assert "pruned" not in result.message
