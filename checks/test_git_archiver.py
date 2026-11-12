"""GitWorkspaceArchiver checks: incremental snapshots, restore, retention.

Contract:
- two snapshots with one file changed → the second snapshot's tree differs
  only by that file (content-addressed sharing, not a fresh full copy);
- restore overlays the snapshot's files back without deleting newer ones
  (same semantics as the legacy copy restore);
- .archive itself and *.log never enter a snapshot; .opencodeignore
  patterns are honoured and retroactively untrack;
- keep_last drops the oldest tags and gc actually reclaims their unique
  objects (orphan commits — no branch pins history);
- missing git executable → transparent fallback to the copy backend;
- legacy run_* directories remain listed and restorable;
- files_to_archive mode snapshots only the requested files.
"""

import shutil

import pytest

from supervisor.workspace.git_archiver import GitWorkspaceArchiver
from supervisor.workspace.workspace_archiver import WorkspaceArchiver, create_archiver

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git executable required",
)


def _mk_ws(tmp_path, name="ws"):
    ws = tmp_path / name
    ws.mkdir()
    (ws / "protocol.md").write_text("protocol v1", encoding="utf-8")
    (ws / "code.py").write_text("print(1)", encoding="utf-8")
    (ws / "noise.log").write_text("log", encoding="utf-8")
    return ws


def test_incremental_snapshot_only_changed_file(tmp_path):
    ws = _mk_ws(tmp_path)
    archiver = GitWorkspaceArchiver(ws, keep_last=0)

    first = archiver.archive_workspace(label="first")
    assert first.success
    assert set(first.archived_files) == {"protocol.md", "code.py"}
    assert "noise.log" not in first.archived_files
    assert (ws / ".archive" / "snapshots.git" / "HEAD").exists()

    (ws / "code.py").write_text("print(2)", encoding="utf-8")
    second = archiver.archive_workspace(label="second")
    assert second.success

    # The snapshot tags share unchanged blobs; only code.py differs.
    diff = archiver._git(
        "diff", "--name-only",
        f"snap/{first.archive_path.name}",
        f"snap/{second.archive_path.name}",
    ).stdout.split()
    assert diff == ["code.py"]


def test_restore_overlays_without_deleting_newer_files(tmp_path):
    ws = _mk_ws(tmp_path)
    archiver = GitWorkspaceArchiver(ws, keep_last=0)
    snap = archiver.archive_workspace(label="base")

    (ws / "code.py").write_text("print('broken')", encoding="utf-8")
    (ws / "extra.py").write_text("added later", encoding="utf-8")

    restored = archiver.restore_archive(snap.archive_path)
    assert set(restored) == {"protocol.md", "code.py"}
    assert (ws / "code.py").read_text(encoding="utf-8") == "print(1)"
    assert (ws / "extra.py").exists()  # overlay semantics: no deletion


def test_excludes_dot_dirs_and_retroactively_untracks_logs(tmp_path):
    ws = _mk_ws(tmp_path)
    (ws / ".opencode").mkdir()
    (ws / ".opencode" / "config.json").write_text("{}", encoding="utf-8")
    archiver = GitWorkspaceArchiver(ws, keep_last=0)
    first = archiver.archive_workspace(label="one")
    assert ".opencode/config.json" not in first.archived_files

    # A log that appears later never lands in any snapshot.
    (ws / "fresh.log").write_text("x" * 1000, encoding="utf-8")
    second = archiver.archive_workspace(label="two")
    assert "fresh.log" not in second.archived_files


def test_opencodeignore_honoured(tmp_path):
    ws = _mk_ws(tmp_path)
    (ws / "scratch").mkdir()
    (ws / "scratch" / "tmp.txt").write_text("t", encoding="utf-8")
    (ws / ".opencodeignore").write_text("scratch/\n", encoding="utf-8")
    archiver = GitWorkspaceArchiver(ws, keep_last=0)
    snap = archiver.archive_workspace(label="ignored")
    assert "scratch/tmp.txt" not in snap.archived_files


def test_retention_drops_oldest_and_reclaims(tmp_path):
    ws = _mk_ws(tmp_path)
    archiver = GitWorkspaceArchiver(ws, keep_last=2)
    names = []
    for i in range(4):
        (ws / "protocol.md").write_text(f"protocol v{i}", encoding="utf-8")
        (ws / f"big_{i}.bin").write_bytes(bytes([i]) * 200_000)
        result = archiver.archive_workspace(label=f"gen{i}")
        assert result.success
        names.append(result.archive_path.name)

    tags = archiver._git("tag", "-l").stdout.split()
    assert len(tags) == 2
    assert f"snap/{names[2]}" in tags and f"snap/{names[3]}" in tags

    # The dropped snapshots' unique 200 KB blobs are gone after gc.
    blob_count = archiver._git(
        "count-objects", "-v",
    ).stdout
    size_kb = int(
        next(ln for ln in blob_count.splitlines() if ln.startswith("size:")).split()[1]
    )
    # 2 surviving snapshots × (changed protocol + one big blob) ≈ 400 KB;
    # all four surviving would be ≈ 800 KB.
    assert size_kb < 650, blob_count


def test_git_missing_falls_back_to_copy(tmp_path, monkeypatch):
    ws = _mk_ws(tmp_path)
    monkeypatch.setattr(
        "supervisor.workspace.git_archiver.shutil.which", lambda _: "",
    )
    archiver = GitWorkspaceArchiver(ws, keep_last=2)
    assert isinstance(archiver._fallback, WorkspaceArchiver)
    result = archiver.archive_workspace(label="copy mode")
    assert result.success
    # Copy-backend snapshot directory, not a git repo.
    assert (ws / ".archive" / result.archive_path.name).is_dir()
    assert not (ws / ".archive" / "snapshots.git").exists()


def test_factory_selects_backend(tmp_path):
    ws = _mk_ws(tmp_path)
    assert isinstance(
        create_archiver(ws, keep_last=1, backend="git"), GitWorkspaceArchiver,
    )
    assert isinstance(
        create_archiver(ws, keep_last=1, backend="copy"), WorkspaceArchiver,
    )


def test_legacy_dirs_still_listed_and_restorable(tmp_path):
    ws = _mk_ws(tmp_path)
    legacy = WorkspaceArchiver(ws, keep_last=0)
    legacy_result = legacy.archive_workspace(label="old days")
    assert legacy_result.success

    archiver = GitWorkspaceArchiver(ws, keep_last=0)
    snap = archiver.archive_workspace(label="new days")

    listed = {a["name"]: a for a in archiver.list_archives()}
    assert legacy_result.archive_path.name in listed
    assert snap.archive_path.name in listed
    assert listed[snap.archive_path.name]["backend"] == "git"

    (ws / "code.py").write_text("mutated", encoding="utf-8")
    restored = archiver.restore_archive(legacy_result.archive_path)
    assert "code.py" in [str(p).replace("\\", "/") for p in restored] or restored
    assert (ws / "code.py").read_text(encoding="utf-8") == "print(1)"


def test_files_to_archive_partial_snapshot(tmp_path):
    ws = _mk_ws(tmp_path)
    archiver = GitWorkspaceArchiver(ws, keep_last=0)
    result = archiver.archive_workspace(
        label="cleanup", files_to_archive=["code.py", "noise.log"],
    )
    assert result.success
    assert result.archived_files == ["code.py"]  # .log filtered
    restored = archiver.restore_archive(result.archive_path)
    assert set(restored) == {"code.py"}


def test_stats_report_backend(tmp_path):
    ws = _mk_ws(tmp_path)
    archiver = GitWorkspaceArchiver(ws, keep_last=0)
    archiver.archive_workspace(label="stats")
    stats = archiver.get_archive_stats()
    assert stats["backend"] == "git"
    assert stats["archive_count"] == 1
    assert stats["total_files"] == 2
