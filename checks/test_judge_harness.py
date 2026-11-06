"""Judge evidence harness: ``read_file`` arguments and directory requests.

Regression cover for the recurring production warning burst:

    Permission denied reading D:\\...\\<workspace>: [Errno 13] ...

Root cause (observed 2026-11-02 .. 2026-11-06 across ten study workspaces):
``parse_verdict_structure`` kept only the first token of a NEED_EVIDENCE line,
so ``read_file <path>`` reached the harness as ``read_file`` with an empty
argument. The harness then called ``safe_read_text`` on the workspace ROOT,
which is a directory: Windows raises PermissionError, ``safe_read_text``
retried four times and logged each attempt, and the judge received
"empty or missing" instead of the file it asked for.

The fix preserves the full request text, lists directories (including the
root) instead of opening them as files, and makes ``safe_read_text`` refuse
directories before its retry ladder.
"""

from __future__ import annotations

import logging
from pathlib import Path

from supervisor.core.judge_harness import (
    JudgeHarness,
    JudgeResource,
    _read_file_block,
)
from supervisor.core.llm_support.models import parse_verdict_structure
from supervisor.utils.filesystem.file_ops import safe_read_text


def _harness(workspace: Path) -> JudgeHarness:
    harness = JudgeHarness()
    harness.register(
        JudgeResource(
            name="read_file",
            provider=lambda arg: _read_file_block(workspace, arg),
            description="read_file <path>",
            on_demand=True,
        ),
    )
    return harness


def _workspace(tmp_path: Path) -> Path:
    (tmp_path / "note.txt").write_text("hello\n", encoding="utf-8")
    (tmp_path / "compute_indicator.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "dev").mkdir()
    (tmp_path / "dev" / "probe.py").write_text("print(1)\n", encoding="utf-8")
    return tmp_path


def test_parse_verdict_preserves_read_file_argument():
    reply = (
        "CRITERIA:\n"
        "- [UNMET] target — not done\n"
        "NEED_EVIDENCE: read_file docs/note.md\n"
    )

    _, _, _, requests = parse_verdict_structure(reply)

    assert requests == ["read_file docs/note.md"]


def test_read_file_argument_reads_the_file(tmp_path):
    workspace = _workspace(tmp_path)

    out = _harness(workspace).fulfill(["read_file note.txt"])

    assert "hello" in out
    assert "empty or missing" not in out


def test_read_file_without_argument_lists_workspace_root(tmp_path, caplog):
    workspace = _workspace(tmp_path)

    with caplog.at_level(logging.WARNING):
        out = _harness(workspace).fulfill(["read_file"])

    assert "is a directory; entries:" in out
    assert "compute_indicator.py" in out
    assert "dev/" in out
    assert "Permission denied" not in caplog.text


def test_read_file_directory_argument_lists_entries(tmp_path):
    workspace = _workspace(tmp_path)

    out = _harness(workspace).fulfill(["read_file dev"])

    assert "dev" in out
    assert "is a directory; entries:" in out
    assert "probe.py" in out


def test_read_file_outside_workspace_is_rejected(tmp_path):
    workspace = _workspace(tmp_path)

    out = _harness(workspace).fulfill(["read_file .."])

    assert "path outside workspace" in out


def test_read_file_missing_file_reports_missing(tmp_path):
    workspace = _workspace(tmp_path)

    out = _harness(workspace).fulfill(["read_file nope.txt"])

    assert "empty or missing" in out


def test_safe_read_text_refuses_a_directory_without_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        text = safe_read_text(tmp_path, default="DEFAULT")

    assert text == "DEFAULT"
    assert "Permission denied" not in caplog.text
    assert "OS error reading" not in caplog.text


def test_read_file_root_listing_is_bounded(tmp_path):
    workspace = _workspace(tmp_path)
    for index in range(120):
        (workspace / f"bulk_{index:03d}.txt").write_text("x", encoding="utf-8")

    out = _harness(workspace).fulfill(["read_file"])

    assert "... (+" in out
    assert len(out) <= 2000  # JudgeHarness._FULFILL_BUDGET
