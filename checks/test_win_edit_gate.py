"""Behaviour tests for the Windows script edit gate (vulnerability/win_edit_gate.py).

PowerShell probes run against the real powershell.exe on this box (parse only,
never execute - asserted below). The batch and encoding checks are pure
functions and run with no shell at all.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from vulnerability import win_edit_gate as gate

GATE = Path(__file__).resolve().parents[1] / "vulnerability" / "win_edit_gate.py"
HAS_PS = gate.shell_exe() is not None
HAS_FORMATTER = HAS_PS and "FMT-UNAVAILABLE" not in subprocess.run(
    gate._ps_command(
        "if(Get-Module -ListAvailable -Name PSScriptAnalyzer){'FMT-PRESENT'}else{'FMT-UNAVAILABLE'}"
    ),
    capture_output=True,
    text=True,
    timeout=120,
).stdout

PS_CLEAN = 'param([string]$Name)\nif ($Name) {\n    Write-Output "hi $Name"\n}\n'
PS_SYNTAX_ERROR = "function Get-Thing {\n    $x = @{ a = 1\n}\n"
PS_EXEC_SENTINEL = "$s = Join-Path $PSScriptRoot 'EXECUTED.txt'\nSet-Content -Path $s -Value 'ran'\n"
BAT_CLEAN = "@echo off\r\nset FOO=1\r\necho %FOO%\r\n"
BAT_BARE_RUN = "call-me.bat\r\n"
BAT_CALLED = "@echo off\r\ncall call-me.bat\r\n"
BAT_DELAYED = "@echo off\r\nfor %%f in (*) do (\r\n  set NAME=%%f\r\n  echo !NAME!\r\n)\r\n"
BAT_DELAYED_OK = (
    "@echo off\r\nsetlocal EnableDelayedExpansion\r\nfor %%f in (*) do (\r\n  set NAME=%%f\r\n  echo !NAME!\r\n)\r\n"
)


def _write_bytes(root: Path, name: str, blob: bytes) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return path


def _payload(cwd: Path, target: str) -> str:
    patch = f"*** Begin Patch\n*** Update File: {target}\n-x\n*** End Patch"
    return json.dumps(
        {
            "cwd": str(cwd),
            "hook_event_name": "PostToolUse",
            "tool_name": "apply_patch",
            "tool_input": {"command": patch},
        }
    )


def _run_gate(payload: str, home: Path, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["WIN_EDIT_GATE_LOG_DIR"] = str(home / "logs")
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("WIN_EDIT_GATE", None)
    return subprocess.run(
        [sys.executable, str(GATE), *args],
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(cwd),
        env=env,
        timeout=600,
    )


def _context(result: subprocess.CompletedProcess[str]) -> str:
    if not result.stdout.strip():
        return ""
    return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


@pytest.fixture()
def sandbox(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return tmp_path, home


# 一 一  Pure: encoding 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一
def test_utf16_batch_is_flagged_but_powershell_accepts_it():
    blob = BAT_CLEAN.encode("utf-16")
    flagged = gate.encoding_findings(blob, Path("run.bat"))
    assert len(flagged) == 1 and "UTF-16" in flagged[0]
    assert gate.encoding_findings(blob, Path("run.ps1")) == []


def test_batch_bom_and_lf_only_endings_are_flagged():
    bom = b"\xef\xbb\xbf" + BAT_CLEAN.encode("utf-8")
    assert any("BOM" in f for f in gate.encoding_findings(bom, Path("run.cmd")))
    lf = BAT_CLEAN.replace("\r\n", "\n").encode("utf-8")
    assert any("LF-only" in f for f in gate.encoding_findings(lf, Path("run.cmd")))
    assert gate.encoding_findings(BAT_CLEAN.encode("utf-8"), Path("run.cmd")) == []


def test_powershell_line_endings_are_never_a_finding():
    assert gate.encoding_findings(PS_CLEAN.encode("utf-8"), Path("a.ps1")) == []
    crlf = PS_CLEAN.replace("\n", "\r\n").encode("utf-8")
    assert gate.encoding_findings(crlf, Path("a.ps1")) == []


def test_non_ascii_needs_a_bom_under_powershell_51():
    text = PS_CLEAN + "# 中文注释\n"
    assert any("PowerShell 5.1" in f for f in gate.encoding_findings(text.encode("utf-8"), Path("a.ps1")))
    assert gate.encoding_findings(text.encode("utf-8-sig"), Path("a.ps1")) == []


# 一 一  Pure: batch control flow 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一
def test_bare_script_run_is_reported_once():
    found = gate.batch_findings(BAT_BARE_RUN, Path("outer.bat"))
    assert len(found) == 1
    assert "without `call`" in found[0] and "outer.bat:1" in found[0]
    assert gate.batch_findings(BAT_CALLED, Path("outer.bat")) == []


def test_delayed_expansion_needs_setlocal():
    assert any("EnableDelayedExpansion" in f for f in gate.batch_findings(BAT_DELAYED, Path("loop.bat")))
    assert gate.batch_findings(BAT_DELAYED_OK, Path("loop.bat")) == []


def test_comments_and_lookalikes_are_not_reported():
    text = ":: run-me.bat\r\nrem run-me.bat\r\necho run-me.bat\r\nif exist run-me.bat (echo yes)\r\nset X=run-me.bat\r\n"
    assert gate.batch_findings(text, Path("quiet.bat")) == []


def test_quoted_path_with_arguments_is_still_checked():
    found = gate.batch_findings('@echo off\r\n"%~dp0sub\\helper.bat" arg1\r\n', Path("m.bat"))
    assert len(found) == 1 and "helper.bat" in found[0]


# 一 一  Pure: probe output + churn budget 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一
def test_probe_output_lines_keep_the_position_and_drop_the_counter():
    out = "12:5 MissingEndCurlyBrace\r\nERRORS=1\r\n"
    assert gate.parse_ps_parser_output(out) == ["12:5 MissingEndCurlyBrace"]
    assert gate.parse_ps_parser_output("ERRORS=0") == []


def test_analyzer_output_drops_the_sentinel():
    assert gate.classify_analyzer_output("3:9 [PSAvoidUsingWriteHost] Use-Output\r\nLINT-DONE") == [
        "3:9 [PSAvoidUsingWriteHost] Use-Output"
    ]


def test_churn_budget_is_20_lines_or_10_percent():
    small = b"a\n" * 10
    assert gate._churn_budget(small) == 20
    big = b"a\n" * 1000
    assert gate._churn_budget(big) == 100
    assert gate._changed_line_count(b"a\nb\nc\n", b"a\nB\nc\n") == 1


def test_targets_filter_by_suffix(tmp_path):
    patch = (
        "*** Begin Patch\n*** Update File: a.ps1\n*** Update File: b.bat\n"
        "*** Update File: c.md\n*** Update File: d.py\n*** End Patch"
    )
    found = gate.extract_targets({"tool_input": {"command": patch}}, tmp_path)
    assert [p.name for p in found] == ["a.ps1", "b.bat"]


# 一 一  Payload robustness 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一
@pytest.mark.parametrize("junk", ["", "  ", "not json", "{}", '{"tool_input": null}', "[1,2]"])
def test_malformed_payload_is_a_silent_noop(sandbox, junk):
    tmp, home = sandbox
    _write_bytes(tmp, "x.ps1", PS_CLEAN.encode("utf-8"))
    result = _run_gate(junk, home, tmp)
    assert result.returncode == 0 and result.stdout.strip() == ""


def test_environment_kill_switch(sandbox):
    tmp, home = sandbox
    path = _write_bytes(tmp, "bad.ps1", PS_SYNTAX_ERROR.encode("utf-8"))
    env = dict(os.environ)
    env.update(WIN_EDIT_GATE="0", WIN_EDIT_GATE_LOG_DIR=str(home / "logs"))
    result = subprocess.run(
        [sys.executable, str(GATE)],
        input=_payload(tmp, path.name),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=600,
    )
    assert result.returncode == 0 and result.stdout.strip() == ""


def test_unwatched_edit_writes_nothing_at_all(sandbox):
    tmp, home = sandbox
    (tmp / "notes.md").write_text("# hi\n", encoding="utf-8")
    result = _run_gate(_payload(tmp, "notes.md"), home, tmp)
    assert result.returncode == 0 and result.stdout.strip() == ""
    assert not (home / "logs" / "win_edit_gate.jsonl").exists()


# 一 一  End to end against the real shells 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一 一
@pytest.mark.skipif(not HAS_PS, reason="no PowerShell available")
def test_powershell_syntax_error_is_reported_with_position(sandbox):
    tmp, home = sandbox
    path = _write_bytes(tmp, "broken.ps1", PS_SYNTAX_ERROR.encode("utf-8"))
    context = _context(_run_gate(_payload(tmp, path.name), home, tmp))
    assert "DOES NOT PARSE" in context
    assert "broken.ps1" not in context  # positions come from the parser, keyed by line:col
    assert ":2" in context


@pytest.mark.skipif(not HAS_PS, reason="no PowerShell available")
def test_clean_powershell_stays_silent(sandbox):
    tmp, home = sandbox
    path = _write_bytes(tmp, "good.ps1", PS_CLEAN.encode("utf-8"))
    result = _run_gate(_payload(tmp, path.name), home, tmp)
    assert result.returncode == 0 and result.stdout.strip() == ""


@pytest.mark.skipif(not HAS_PS, reason="no PowerShell available")
def test_parser_messages_survive_the_console_codepage(sandbox):
    """powershell.exe prints in the OEM codepage; the gate must deliver readable text."""
    tmp, home = sandbox
    path = _write_bytes(tmp, "cjk.ps1", PS_SYNTAX_ERROR.encode("utf-8"))
    context = _context(_run_gate(_payload(tmp, path.name), home, tmp))
    assert "DOES NOT PARSE" in context
    assert "\ufffd" not in context  # the tell-tale of OEM bytes decoded as UTF-8
    assert "\xa1" not in context  # e.g. CP936 quotes arriving as latin-1 garbage
    assert context.split("DOES NOT PARSE:", 1)[1].strip()


@pytest.mark.skipif(not HAS_PS, reason="no PowerShell available")
def test_the_parse_probe_never_executes_the_script(sandbox):
    tmp, home = sandbox
    path = _write_bytes(tmp, "sideeffect.ps1", PS_EXEC_SENTINEL.encode("utf-8"))
    _run_gate(_payload(tmp, path.name), home, tmp)
    assert not (tmp / "EXECUTED.txt").exists()


@pytest.mark.skipif(not HAS_PS, reason="no PowerShell available")
def test_encoding_problem_is_advised_without_running_anything(sandbox):
    tmp, home = sandbox
    path = _write_bytes(tmp, "utf16.bat", BAT_CLEAN.encode("utf-16"))
    context = _context(_run_gate(_payload(tmp, path.name), home, tmp))
    assert "UTF-16" in context and "encoding" in context


@pytest.mark.skipif(not HAS_PS, reason="no PowerShell available")
def test_batch_control_flow_bug_is_advised(sandbox):
    tmp, home = sandbox
    path = _write_bytes(tmp, "outer.bat", BAT_BARE_RUN.encode("utf-8"))
    context = _context(_run_gate(_payload(tmp, path.name), home, tmp))
    assert "batch" in context and "without `call`" in context


@pytest.mark.skipif(not HAS_PS, reason="no PowerShell available")
def test_batch_with_call_and_setlocal_is_silent(sandbox):
    tmp, home = sandbox
    path = _write_bytes(tmp, "outer.bat", BAT_CALLED.encode("utf-8"))
    assert _context(_run_gate(_payload(tmp, path.name), home, tmp)) == ""
    path = _write_bytes(tmp, "loop.bat", BAT_DELAYED_OK.encode("utf-8"))
    assert _context(_run_gate(_payload(tmp, path.name), home, tmp)) == ""


@pytest.mark.skipif(not HAS_PS, reason="no PowerShell available")
def test_several_files_are_reported_in_one_advisory(sandbox):
    tmp, home = sandbox
    a = _write_bytes(tmp, "a.ps1", PS_SYNTAX_ERROR.encode("utf-8"))
    b = _write_bytes(tmp, "b.bat", BAT_BARE_RUN.encode("utf-8"))
    patch = f"*** Begin Patch\n*** Update File: {a.name}\n*** Update File: {b.name}\n*** End Patch"
    context = _context(_run_gate(json.dumps({"cwd": str(tmp), "tool_input": {"command": patch}}), home, tmp))
    assert "DOES NOT PARSE" in context and "batch" in context
    assert context.count("[win-edit-gate]") <= 4


@pytest.mark.skipif(not HAS_PS, reason="no PowerShell available")
def test_advisory_tells_the_agent_to_reread_and_not_to_resubmit(sandbox):
    tmp, home = sandbox
    path = _write_bytes(tmp, "broken2.ps1", PS_SYNTAX_ERROR.encode("utf-8"))
    _write_bytes(tmp, "u.bat", BAT_CLEAN.replace("\r\n", "\n").encode("utf-8"))
    patch = f"*** Update File: {path.name}\n*** Update File: u.bat"
    context = _context(_run_gate(json.dumps({"cwd": str(tmp), "tool_input": {"command": patch}}), home, tmp))
    assert "Re-read the file" in context
    assert "do not resubmit the whole file" in context


@pytest.mark.skipif(not HAS_PS, reason="no PowerShell available")
def test_lint_flag_reports_a_missing_module_as_a_note(sandbox):
    tmp, home = sandbox
    path = _write_bytes(tmp, "good2.ps1", PS_CLEAN.encode("utf-8"))
    context = _context(_run_gate(_payload(tmp, path.name), home, tmp, "--lint"))
    if HAS_FORMATTER:
        assert "PSScriptAnalyzer not installed" not in context
    else:
        assert "PSScriptAnalyzer" in context


@pytest.mark.skipif(not HAS_PS, reason="no PowerShell available")
def test_format_flag_never_mangles_a_file_it_cannot_verify(sandbox):
    tmp, home = sandbox
    path = _write_bytes(tmp, "good3.ps1", PS_CLEAN.encode("utf-8"))
    before = path.read_bytes()
    result = _run_gate(_payload(tmp, path.name), home, tmp, "--format")
    assert result.returncode == 0
    assert path.read_bytes() == before
    record = json.loads((home / "logs" / "win_edit_gate.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert record["fix_allowed"] is True


@pytest.mark.skipif(not HAS_PS, reason="no PowerShell available")
def test_audit_log_records_the_findings(sandbox):
    tmp, home = sandbox
    path = _write_bytes(tmp, "broken3.ps1", PS_SYNTAX_ERROR.encode("utf-8"))
    _run_gate(_payload(tmp, path.name), home, tmp)
    log = home / "logs" / "win_edit_gate.jsonl"
    assert log.is_file()
    record = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert record["event"] == "PostToolUse"
    assert record["counts"]["syntax"] >= 1
    assert record["files"][0].endswith("broken3.ps1")


def test_shell_exe_falls_back_to_the_system_powershell(tmp_path, monkeypatch):
    monkeypatch.setattr(gate.shutil, "which", lambda _name: None)
    monkeypatch.setattr(gate, "POWERShell_FALLBACK", str(_write_bytes(tmp_path, "powershell.exe", b"x")))
    assert gate.shell_exe() == str(tmp_path / "powershell.exe")
    monkeypatch.setattr(gate, "POWERShell_FALLBACK", str(tmp_path / "nope.exe"))
    assert gate.shell_exe() is None


def test_powershell_probe_reports_a_note_instead_of_false_cleanliness(sandbox, monkeypatch):
    tmp, home = sandbox
    path = _write_bytes(tmp, "unparsed.ps1", PS_SYNTAX_ERROR.encode("utf-8"))
    monkeypatch.setattr(gate, "shell_exe", lambda: None)
    errors, note = gate.powershell_parse_findings(path)
    assert errors == [] and "no PowerShell" in note
