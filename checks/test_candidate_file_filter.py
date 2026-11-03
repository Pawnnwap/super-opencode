"""Candidate-list filtering: cache dirs, cache/binary files, unreadable files.

The supervisor's LLM file-selection walk (context.read_protected_files_for_
suggestions) and the UI's workspace scans feed model context and human
droplists. Byte-compiled junk, dependency trees and binaries are pure noise
there — and a picked binary burns a selection slot on safe_read_text("").
The traversal default (should_skip_path with DEFAULT_IGNORE_DIRS) is
deliberately unchanged: snapshots and archives must still see the whole
workspace.
"""

from pathlib import Path

from supervisor.utils.filesystem.path_filters import (
    CANDIDATE_IGNORE_DIRS,
    is_candidate_file,
    is_probably_binary,
    should_skip_path,
)


def _make(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    return path


def _candidates(root: Path) -> set[str]:
    return {
        str(p.relative_to(root)).replace("\\", "/")
        for p in root.rglob("*")
        if is_candidate_file(p)
    }


def test_cache_and_dependency_dirs_excluded_from_candidates(tmp_path):
    keep = _make(tmp_path / "src" / "mod.py", "x = 1\n")
    junk = {
        _make(tmp_path / "__pycache__" / "mod.cpython-311.pyc", b"\x00\x01"),
        _make(tmp_path / "node_modules" / "pkg" / "index.js", "ok\n"),
        _make(tmp_path / "venv" / "lib" / "activate.py", "ok\n"),
        _make(tmp_path / "build" / "out.txt", "generated\n"),
        _make(tmp_path / "dist" / "bundle.js", "generated\n"),
        _make(tmp_path / "target" / "release" / "app.txt", "generated\n"),
        _make(tmp_path / "htmlcov" / "report.txt", "coverage\n"),
        _make(tmp_path / "src" / "pkg.egg-info" / "PKG-INFO", "metadata\n"),
    }

    candidates = _candidates(tmp_path)

    assert "src/mod.py" in candidates
    for j in junk:
        assert str(j.relative_to(tmp_path)).replace("\\", "/") not in candidates


def test_dot_dirs_still_excluded_and_archive_untouched(tmp_path):
    _make(tmp_path / ".pytest_cache" / "v" / "cache.txt", "cache\n")
    _make(tmp_path / ".mypy_cache" / "x.txt", "cache\n")
    _make(tmp_path / ".archive" / "old.txt", "archived\n")
    _make(tmp_path / "real.txt", "real\n")

    assert _candidates(tmp_path) == {"real.txt"}


def test_source_named_like_ignored_dir_is_kept(tmp_path):
    # A FILE named "build.py" must not be dropped because BUILD DIRS are
    # ignored — only path COMPONENTS match.
    assert is_candidate_file(_make(tmp_path / "src" / "build.py", "x = 1\n"))


def test_traversal_default_unchanged_build_dir_still_walked():
    # Snapshot/archiver behaviour must not change: DEFAULT_IGNORE_DIRS does
    # not know about build/dist/target — only the candidate layer does.
    assert not should_skip_path("build/out.txt")
    assert should_skip_path("build/out.txt", extra_dirs=CANDIDATE_IGNORE_DIRS)
    assert should_skip_path("src/__pycache__/x.pyc")  # canonical, in DEFAULT


def test_binary_content_sniffed_out(tmp_path):
    text = _make(tmp_path / "notes.txt", "plain text\n")
    binary = _make(tmp_path / "blob.dat", b"PK\x03\x04\x00\x00\x00\x00\x00")
    empty = _make(tmp_path / "empty.log", b"")

    assert not is_probably_binary(text)
    assert is_probably_binary(binary)
    assert not is_probably_binary(empty)  # empty file: trivially text
    assert "notes.txt" in _candidates(tmp_path)
    assert "blob.dat" not in _candidates(tmp_path)
    assert "empty.log" in _candidates(tmp_path)


def test_known_binary_suffixes_skipped_without_sniff(tmp_path):
    # Even ASCII-content files with binary extensions are not candidates.
    fake = _make(tmp_path / "docs" / "manual.pdf", "actually text\n")
    assert not is_candidate_file(fake)


def test_unreadable_or_missing_file_counts_as_binary(tmp_path):
    missing = tmp_path / "gone.txt"
    assert is_probably_binary(missing)
    assert not is_candidate_file(missing)
    assert not is_candidate_file(tmp_path)  # a directory is not a candidate


def test_generated_markdown_still_reachable(tmp_path):
    # The context walk only pre-filters junk; generated-markdown exclusion
    # stays the caller's job (it is instance state there).
    _make(tmp_path / "TASK_STATE.md", "# progress\n")
    assert "TASK_STATE.md" in _candidates(tmp_path)


def test_context_candidate_walk_uses_filter(tmp_path):
    """Integration: the supervisor's file-selection pool drops the junk."""
    from supervisor.core.llm_support.context import SupervisorContextManager
    from supervisor.workspace.ignore_patterns import IgnoreMatcher

    _make(tmp_path / "src" / "real.py", "x = 1\n")
    _make(tmp_path / "src" / "other.py", "y = 2\n")
    _make(tmp_path / "__pycache__" / "real.cpython.pyc", b"\x00\x01")
    _make(tmp_path / "node_modules" / "pkg" / "index.js", "ok\n")
    _make(tmp_path / "logo.png", b"\x89PNG\r\n\x1a\x00\x00")

    manager = SupervisorContextManager(
        workspace=tmp_path,
        max_tokens=2000,
        max_protected_files_for_suggestions=5,
        protocol_target="t",
        model="m",
        client=None,  # llm_select_files fails -> deterministic fallback order
        ignore_matcher=IgnoreMatcher(tmp_path),
        log_prompt=lambda *a, **k: None,
        skip_dirs=set(),
        skip_dir_prefixes=(),
        generated_markdown_files=set(),
    )

    _result, chosen = manager.read_protected_files_for_suggestions()
    lowered = [c.lower().replace("\\", "/") for c in chosen]
    assert all(not c.startswith("__pycache__") for c in lowered)
    assert all(not c.startswith("node_modules") for c in lowered)
    assert "logo.png" not in lowered
    # The readable sources still made the pool (fallback picks first-k sorted).
    assert "src/other.py" in lowered and "src/real.py" in lowered


def test_wizard_workspace_scan_uses_filter(tmp_path):
    """Integration: the UI protected-files droplist excludes the junk."""
    from services.webui.pages.wizard import _list_workspace_files

    _make(tmp_path / "app.py", "x = 1\n")
    _make(tmp_path / "__pycache__" / "app.cpython.pyc", b"\x00\x01")
    _make(tmp_path / "assets" / "logo.png", b"\x89PNG\r\n\x1a\x00\x00")

    files = _list_workspace_files(tmp_path, limit=100)
    assert files == ["app.py"]
