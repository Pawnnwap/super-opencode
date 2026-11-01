"""Behaviour tests for the global Python edit gate (vulnerability/py_edit_gate.py).

Subprocess-based on purpose: the gate is a hook executable, and its contracts
are stdin payload -> exit code + stdout JSON, plus files it may rewrite.
Every run gets a throwaway HOME so the real ~/.codex logs are never touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from vulnerability import py_edit_gate as gate

GATE = Path(__file__).resolve().parents[1] / "vulnerability" / "py_edit_gate.py"


def _patch(paths: list[str], bodies: dict[str, str]) -> str:
    chunks = ["*** Begin Patch"]
    for path in paths:
        chunks.append(f"*** Update File: {path}")
        for line in bodies[path].splitlines():
            chunks.append(f" {line}")
    chunks.append("*** End Patch")
    return "\n".join(chunks)


def _payload(cwd: Path, command: str, tool: str = "apply_patch") -> str:
    return json.dumps(
        {
            "session_id": "test",
            "cwd": str(cwd),
            "hook_event_name": "PostToolUse",
            "tool_name": tool,
            "tool_input": {"command": command},
            "tool_use_id": "tu-1",
        },
    )


def _run_gate(
    payload: str,
    home: Path,
    cwd: Path,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["USERPROFILE"] = str(home)
    env["HOME"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("PY_EDIT_GATE", None)
    return subprocess.run(
        [sys.executable, str(GATE), *args],
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(cwd),
        env=env,
        timeout=120,
    )


def _context(result: subprocess.CompletedProcess[str]) -> str:
    if not result.stdout.strip():
        return ""
    return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


@pytest.fixture()
def sandbox(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    return ws, home


# ── Pure parsing ─────────────────────────────────────────────────────────────
def test_parse_patch_paths_reads_every_header():
    text = (
        "*** Begin Patch\n*** Add File: a.py\n+x = 1\n"
        "*** Update File: pkg/b.py\n*** Update File: spaced/my file.py\n*** End Patch"
    )
    assert gate.parse_patch_paths(text) == ["a.py", "pkg/b.py", "spaced/my file.py"]


def test_extract_targets_resolves_relative_and_drops_non_python(tmp_path):
    (tmp_path / "keep.py").write_text("x = 1", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "inner.py").write_text("y = 2", encoding="utf-8")
    payload = {
        "tool_input": {
            "command": _patch(
                ["keep.py", "notes.md", "sub/inner.py", "gone.py"],
                {
                    "keep.py": "x = 1",
                    "notes.md": "hi",
                    "sub/inner.py": "y = 2",
                    "gone.py": "z = 3",
                },
            ),
        },
    }
    found = gate.extract_targets(payload, tmp_path)
    names = sorted(p.name for p in found)
    # Extraction reports every referenced .py path; existence is skip_reason's job.
    assert names == ["gone.py", "inner.py", "keep.py"]
    by_name = {p.name: p for p in found}
    assert gate.skip_reason(by_name["gone.py"]) == "not a file"
    assert gate.skip_reason(by_name["keep.py"]) == ""


def test_skip_reason_covers_ignored_and_oversized(tmp_path):
    missing = tmp_path / "nope.py"
    assert gate.skip_reason(missing) == "not a file"
    venv = tmp_path / "venv" / "lib" / "x.py"
    venv.parent.mkdir(parents=True)
    venv.write_text("x = 1", encoding="utf-8")
    assert gate.skip_reason(venv) == "ignored dir"
    empty = tmp_path / "empty.py"
    empty.write_text("", encoding="utf-8")
    assert gate.skip_reason(empty) == "empty"
    big = tmp_path / "big.py"
    big.write_text("x = 1\n" * 200_000, encoding="utf-8")
    assert gate.skip_reason(big).startswith("too large")
    ok = tmp_path / "ok.py"
    ok.write_text("x = 1", encoding="utf-8")
    assert gate.skip_reason(ok) == ""


def test_compose_advisory_orders_sections_and_caps_length():
    text = gate.compose_advisory(
        fixed=["formatted"],
        imports_added=["import json"],
        syntax=["a.py:1:1 boom"],
        missing_imports=["Undefined name `foo` @L2"],
        lint=[f"E501 L{i} long" for i in range(40)],
        security=["B602 L3 [MEDIUM] shell=True"],
        notes=[],
    )
    assert (
        text.index("AUTO-FIXED")
        < text.index("MISSING IMPORT")
        < text.index("DOES NOT COMPILE")
    )
    assert text.index("lint:") < text.index("security:")
    assert "(+32 more)" in text
    long_form = gate.compose_advisory([], [], ["z" * 2000], [], [], [], [])
    assert len(long_form) <= gate.ADVISORY_MAX_CHARS


def test_import_statement_for_skips_unknown_and_future():
    assert gate.import_statement_for("json") == "import json"
    assert gate.import_statement_for("Path") == "from pathlib import Path"
    assert gate.import_statement_for("mythical_widget") is None
    gate.STDLIB_IMPORTS.setdefault(
        "__probe_future__",
        "from __future__ import annotations",
    )
    assert gate.import_statement_for("__probe_future__") is None
    gate.STDLIB_IMPORTS.pop("__probe_future__")


def test_import_insert_offset_follows_docstring_and_imports():
    source = '#!/usr/bin/env python\n# -*- coding: utf-8\n"""Doc."""\n\nimport os\nfrom sys import argv\n\n\ndef f():\n    pass\n'
    assert gate.import_insert_offset(source) == 6
    assert gate.import_insert_offset("import os\n") == 1
    assert gate.import_insert_offset("def f(:\n") == 0


def test_autofix_disabled_here_reads_marker(tmp_path):
    (tmp_path / "pkg").mkdir()
    target = tmp_path / "pkg" / "m.py"
    target.write_text("x = 1", encoding="utf-8")
    assert gate.autofix_disabled_here(target) is False
    (tmp_path / ".edit-gate-disable").write_text("", encoding="utf-8")
    assert gate.autofix_disabled_here(target) is True


# ── Fail-open payload handling ───────────────────────────────────────────────
@pytest.mark.parametrize(
    "junk",
    ["", "   ", "not json at all", "[1,2,3]", '{"tool_input": null}'],
)
def test_malformed_payload_is_a_silent_noop(sandbox, junk):
    ws, home = sandbox
    (ws / "x.py").write_text("import os\n", encoding="utf-8")
    result = _run_gate(junk, home, ws)
    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert (ws / "x.py").read_text(encoding="utf-8") == "import os\n"


def test_patch_without_file_headers_is_a_noop(sandbox):
    ws, home = sandbox
    (ws / "x.py").write_text("import os\n", encoding="utf-8")
    payload = _payload(ws, "*** Begin Patch\n*** End Patch")
    result = _run_gate(payload, home, ws)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_environment_kill_switch_disables_the_gate(sandbox, monkeypatch):
    ws, home = sandbox
    target = ws / "x.py"
    target.write_text("import os\nprint(1)\n", encoding="utf-8")
    env = dict(os.environ)
    env.update(PY_EDIT_GATE="0", USERPROFILE=str(home), HOME=str(home))
    result = subprocess.run(
        [sys.executable, str(GATE)],
        input=_payload(ws, _patch(["x.py"], {"x.py": "import os\nprint(1)"})),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(ws),
        env=env,
        timeout=120,
    )
    assert result.returncode == 0 and result.stdout.strip() == ""
    assert target.read_text(encoding="utf-8") == "import os\nprint(1)\n"


# ── Real checks on real files ───────────────────────────────────────────────
def test_syntax_error_is_reported_with_position(sandbox):
    ws, home = sandbox
    (ws / "broken.py").write_text("def f(:\n    return 1\n", encoding="utf-8")
    context = _context(
        _run_gate(
            _payload(ws, _patch(["broken.py"], {"broken.py": "def f(:\n    return 1"})),
            home,
            ws,
        ),
    )
    assert "DOES NOT COMPILE" in context
    assert "broken.py:1:" in context and "SyntaxError" in context


def test_unused_import_is_autofixed_and_announced(sandbox):
    ws, home = sandbox
    target = ws / "mod.py"
    target.write_text(
        "import os\nimport sys\n\n\ndef f():\n    return sys.argv\n",
        encoding="utf-8",
    )
    context = _context(
        _run_gate(
            _payload(
                ws,
                _patch(
                    ["mod.py"],
                    {
                        "mod.py": "import os\nimport sys\n\n\ndef f():\n    return sys.argv",
                    },
                ),
            ),
            home,
            ws,
        ),
    )
    assert "AUTO-FIXED" in context
    assert "import os" not in target.read_text(encoding="utf-8")
    assert "import sys" in target.read_text(encoding="utf-8")


def test_missing_stdlib_import_is_inserted(sandbox):
    ws, home = sandbox
    target = ws / "uses_json.py"
    body = "def f():\n    return json.dumps({})\n"
    target.write_text(body, encoding="utf-8")
    context = _context(
        _run_gate(
            _payload(ws, _patch(["uses_json.py"], {"uses_json.py": body})),
            home,
            ws,
        ),
    )
    assert "AUTO-FIXED" in context and "import json" in context
    text = target.read_text(encoding="utf-8")
    assert text.startswith("import json")
    compile(text, "uses_json.py", "exec")


def test_unknown_name_stays_advisory_and_leaves_file_byte_identical(sandbox):
    ws, home = sandbox
    target = ws / "unknown.py"
    body = "def f():\n    return frobnicate_the_thing(1)\n"
    target.write_bytes(body.encode("utf-8"))
    original = target.read_bytes()
    context = _context(
        _run_gate(_payload(ws, _patch(["unknown.py"], {"unknown.py": body})), home, ws),
    )
    assert "MISSING IMPORT" in context
    assert "not a stdlib name" in context
    assert target.read_bytes() == original


def test_crlf_file_keeps_its_line_endings(sandbox):
    ws, home = sandbox
    target = ws / "crlf.py"
    target.write_bytes(
        b"import os\r\nimport sys\r\n\r\n\r\ndef f():\r\n    return sys.argv\r\n",
    )
    _run_gate(
        _payload(ws, _patch(["crlf.py"], {"crlf.py": "import os\r\nimport sys"})),
        home,
        ws,
    )
    blob = target.read_bytes()
    assert b"\r\n" in blob
    assert b"import os" not in blob


def test_read_only_file_is_not_rewritten(sandbox):
    import stat

    ws, home = sandbox
    target = ws / "locked.py"
    body = "import os\nprint(1)\n"
    target.write_text(body, encoding="utf-8")
    os.chmod(target, stat.S_IREAD)
    try:
        context = _context(
            _run_gate(
                _payload(ws, _patch(["locked.py"], {"locked.py": body})),
                home,
                ws,
            ),
        )
    finally:
        os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
    assert "read-only" in context
    assert target.read_text(encoding="utf-8") == body


def test_repeat_verdict_is_suppressed_within_ttl(sandbox):
    ws, home = sandbox
    target = ws / "sec.py"
    # bandit B307 (eval) is MEDIUM, and no ruff rule in CHECK_SELECT touches it,
    # so this fixture stays byte-stable and the finding persists across runs.
    body = 'def f():\n    return eval("1 + 1")\n'
    target.write_text(body, encoding="utf-8")
    payload = _payload(ws, _patch(["sec.py"], {"sec.py": body}))
    first = _context(_run_gate(payload, home, ws))
    assert "B307" in first
    second = _context(_run_gate(payload, home, ws))
    assert "B307" not in second


def test_no_autofix_flag_keeps_findings_advisory(sandbox):
    ws, home = sandbox
    target = ws / "advisory.py"
    body = "import os\nprint(1)\n"
    target.write_text(body, encoding="utf-8")
    context = _context(
        _run_gate(
            _payload(ws, _patch(["advisory.py"], {"advisory.py": body})),
            home,
            ws,
            "--no-autofix",
        ),
    )
    assert "AUTO-FIXED" not in context
    assert "F401" in context
    assert target.read_text(encoding="utf-8") == body


def test_audit_log_records_each_fire(sandbox):
    ws, home = sandbox
    (ws / "logged.py").write_text("x = 1\n", encoding="utf-8")
    _run_gate(_payload(ws, _patch(["logged.py"], {"logged.py": "x = 1"})), home, ws)
    log = home / ".codex" / "logs" / "py_edit_gate.jsonl"
    assert log.is_file()
    record = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert record["tool"] == "apply_patch"
    assert record["files"] == [str((ws / "logged.py").resolve())]
    assert record["tier"]


def test_marker_file_makes_a_project_advisory_only(sandbox):
    ws, home = sandbox
    (ws / ".edit-gate-disable").write_text("", encoding="utf-8")
    target = ws / "quiet.py"
    body = "import os\nprint(1)\n"
    target.write_text(body, encoding="utf-8")
    context = _context(
        _run_gate(_payload(ws, _patch(["quiet.py"], {"quiet.py": body})), home, ws),
    )
    assert "AUTO-FIXED" not in context and "F401" in context
    assert target.read_text(encoding="utf-8") == body


def test_non_python_targets_are_ignored(sandbox):
    ws, home = sandbox
    (ws / "notes.md").write_text("hello", encoding="utf-8")
    result = _run_gate(
        _payload(ws, _patch(["notes.md"], {"notes.md": "hello"})),
        home,
        ws,
    )
    assert result.returncode == 0 and result.stdout.strip() == ""
    assert (ws / "notes.md").read_text(encoding="utf-8") == "hello"


# ── Churn control (whole-file rewrites are what break the agent's next patch) ─
LONG_CALL = (
    "def compute(a, b):\n"
    "    result = some_function(a, b, first_arg_value=1, second_arg_value=2, third_arg_value=3)\n"
    "    return result\n"
)


def test_formatter_is_opt_in_so_line_layout_survives(sandbox):
    ws, home = sandbox
    target = ws / "svc.py"
    target.write_text(LONG_CALL, encoding="utf-8")
    context = _context(
        _run_gate(_payload(ws, _patch(["svc.py"], {"svc.py": LONG_CALL})), home, ws)
    )
    assert "AUTO-FIXED" not in context
    assert "E501" not in context  # long lines are never advised
    assert target.read_text(encoding="utf-8") == LONG_CALL


def test_long_lines_are_never_advised(sandbox):
    ws, home = sandbox
    body = "VALUE = " + " + ".join(str(i) for i in range(60)) + "\n"
    target = ws / "long.py"
    target.write_text(body, encoding="utf-8")
    result = _run_gate(_payload(ws, _patch(["long.py"], {"long.py": body})), home, ws)
    assert result.stdout.strip() == ""  # clean: silence
    assert target.read_text(encoding="utf-8") == body


def test_format_flag_reflows_when_explicitly_enabled(sandbox):
    ws, home = sandbox
    target = ws / "svc.py"
    target.write_text(LONG_CALL, encoding="utf-8")
    context = _context(
        _run_gate(
            _payload(ws, _patch(["svc.py"], {"svc.py": LONG_CALL})),
            home,
            ws,
            "--format",
        ),
    )
    assert "formatted" in context
    assert "some_function(\n" in target.read_text(encoding="utf-8")


def test_mass_fix_over_the_churn_ceiling_is_reverted(sandbox):
    ws, home = sandbox
    target = ws / "many.py"
    imports = "".join(f"import mod{i}\n" for i in range(25))
    body = imports + "\ndef f():\n    return 1\n"
    target.write_text(body, encoding="utf-8")
    context = _context(
        _run_gate(_payload(ws, _patch(["many.py"], {"many.py": body})), home, ws)
    )
    assert "AUTO-FIXED" not in context
    assert target.read_text(encoding="utf-8") == body  # 25 line removals > ceiling


def test_deprecated_typing_alias_is_never_rewritten(sandbox):
    ws, home = sandbox
    target = ws / "alias.py"
    body = "from typing import List\n\n\ndef f(x: List[int]) -> list:\n    return list(x)\n"
    target.write_text(body, encoding="utf-8")
    _run_gate(_payload(ws, _patch(["alias.py"], {"alias.py": body})), home, ws)
    assert target.read_text(encoding="utf-8") == body


def test_header_blanks_repaired_and_agent_told_to_reread(sandbox):
    ws, home = sandbox
    target = ws / "hdr.py"
    body = "import os\n\n\ndef f():\n    pass\n"
    target.write_text(body, encoding="utf-8")
    context = _context(
        _run_gate(_payload(ws, _patch(["hdr.py"], {"hdr.py": body})), home, ws)
    )
    text = target.read_text(encoding="utf-8")
    assert text.startswith("def f()"), text  # no blank-file header left behind
    assert "RE-READ" in context
    assert "do not resubmit the whole file" in context


def test_changed_line_count_and_churn_budget():
    before = b"a\nb\nc\n"
    assert gate._changed_line_count(before, before) == 0
    assert gate._changed_line_count(before, b"a\nB\nc\n") == 1
    assert gate._changed_line_count(before, b"a\nb\nc\nd\n") == 1
    assert gate._churn_budget(b"\n".join([b"x"] * 5)) == gate.MAX_FIX_LINES
    assert gate._churn_budget(b"\n".join([b"x"] * 500)) > gate.MAX_FIX_LINES
