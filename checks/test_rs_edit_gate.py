"""Behaviour tests for the Rust edit gate (vulnerability/rs_edit_gate.py).

Real toolchain: each fixture crate has no dependencies, so `cargo check` runs
offline in well under a second once warm. Skipped entirely without cargo.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from vulnerability import rs_edit_gate as gate

pytestmark = pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")

GATE = Path(__file__).resolve().parents[1] / "vulnerability" / "rs_edit_gate.py"

CLEAN = "pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n"
TYPE_ERROR = (
    "pub fn add(a: i32, b: i32) -> i32 {\n    let s: String = a + b;\n    a\n}\n"
)
PARSE_ERROR = "pub fn f() -> i32 {\n    let x = 1 +\n    return x\n}\n"
DRIFT_ONLY = "pub fn g( ) -> i32 { 1 }\n"
CLIPPY_WARNING = "pub fn empty(v: &[i32]) -> bool {\n    v.len() == 0\n}\n"


def _crate(root: Path, source: str, name: str) -> Path:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "Cargo.toml").write_text(
        f'[package]\nname = "{name}"\nversion = "0.0.0"\nedition = "2021"\n',
        encoding="utf-8",
    )
    (root / "src" / "lib.rs").write_text(source, encoding="utf-8")
    return root


def _warm(crate: Path) -> None:
    subprocess.run(
        ["cargo", "check", "--offline", "--manifest-path", str(crate / "Cargo.toml")],
        capture_output=True,
        timeout=600,
        check=False,
    )


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
    # Redirect the gate's own log dir, NOT USERPROFILE: rustfmt is a rustup shim
    # and resolves its toolchain from the real user profile.
    env["RS_EDIT_GATE_LOG_DIR"] = str(home / "logs")
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("RS_EDIT_GATE", None)
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


def _message(result: subprocess.CompletedProcess[str]) -> str:
    """Stop events reply with systemMessage, not additionalContext."""
    if not result.stdout.strip():
        return ""
    return json.loads(result.stdout)["systemMessage"]


@pytest.fixture()
def sandbox(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return tmp_path, home


# ── Pure logic ──────────────────────────────────────────────────────────────
def test_targets_ignore_non_rust_files(tmp_path):
    (tmp_path / "a.rs").write_text(CLEAN, encoding="utf-8")
    (tmp_path / "b.md").write_text("hi", encoding="utf-8")
    patch = "*** Begin Patch\n*** Update File: a.rs\n*** Update File: b.md\n*** End Patch"
    payload = {"tool_input": {"command": patch}}
    found = gate.extract_targets(payload, tmp_path)
    assert [p.name for p in found] == ["a.rs"]


def test_edition_is_read_from_manifest(tmp_path):
    crate = _crate(tmp_path / "c2024", CLEAN, "c2024")
    (crate / "Cargo.toml").write_text(
        '[package]\nname = "c2024"\nedition = "2024"\n', encoding="utf-8"
    )
    assert gate.read_edition(crate) == "2024"
    assert gate.read_edition(_crate(tmp_path / "cplain", CLEAN, "cplain")) == "2021"


def test_cold_crate_is_never_checked(sandbox):
    tmp, home = sandbox
    crate = _crate(tmp / "coldcrate", CLEAN, "coldcrate")
    due, why = gate.check_policy({}, crate, None)
    assert (due, why) == (False, "cold")
    crate.joinpath("target").mkdir()
    assert gate.check_policy({}, crate, None) == (True, "first")
    recently = {"crate:" + str(crate).lower(): {"ts": __import__("time").time(), "seconds": 1.0}}
    assert gate.check_policy(recently, crate, 1.0)[0] is False
    old = {"crate:" + str(crate).lower(): {"ts": 0, "seconds": 30.0}}
    assert gate.check_policy(old, crate, 30.0)[0] is True


# ── Payload robustness ─────────────────────────────────────────────────────
@pytest.mark.parametrize("junk", ["", "  ", "not json", "{}", '{"tool_input": null}'])
def test_malformed_payload_is_a_silent_noop(sandbox, junk):
    tmp, home = sandbox
    (tmp / "x.rs").write_text(CLEAN, encoding="utf-8")
    result = _run_gate(junk, home, tmp)
    assert result.returncode == 0 and result.stdout.strip() == ""


def test_environment_kill_switch(sandbox):
    tmp, home = sandbox
    crate = _crate(tmp / "killcrate", TYPE_ERROR, "killcrate")
    _warm(crate)
    env = dict(os.environ)
    env.update(RS_EDIT_GATE="0", RS_EDIT_GATE_LOG_DIR=str(home / "logs"))
    result = subprocess.run(
        [sys.executable, str(GATE)],
        input=_payload(crate, "src/lib.rs"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=600,
    )
    assert result.returncode == 0 and result.stdout.strip() == ""


# ── The checks themselves ──────────────────────────────────────────────────
def test_parse_error_is_reported_with_position(sandbox):
    tmp, home = sandbox
    crate = _crate(tmp / "parsecrate", PARSE_ERROR, "parsecrate")
    context = _context(_run_gate(_payload(crate, "src/lib.rs"), home, crate))
    assert "DOES NOT PARSE" in context
    assert "lib.rs" in context


def test_type_error_is_reported_from_cargo(sandbox):
    tmp, home = sandbox
    crate = _crate(tmp / "typecrate", TYPE_ERROR, "typecrate")
    _warm(crate)
    context = _context(_run_gate(_payload(crate, "src/lib.rs"), home, crate))
    assert "COMPILE ERRORS" in context
    assert "E0308" in context
    assert "src/lib.rs:2" in context


def test_clean_crate_stays_silent(sandbox):
    tmp, home = sandbox
    crate = _crate(tmp / "cleanx", CLEAN, "cleanx")
    _warm(crate)
    result = _run_gate(_payload(crate, "src/lib.rs"), home, crate)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_formatting_drift_is_not_advised_by_default(sandbox):
    tmp, home = sandbox
    crate = _crate(tmp / "driftcrate", DRIFT_ONLY, "driftcrate")
    _warm(crate)
    context = _context(_run_gate(_payload(crate, "src/lib.rs"), home, crate))
    assert "formatted" not in context
    assert "Diff" not in context
    assert (crate / "src" / "lib.rs").read_text(encoding="utf-8") == DRIFT_ONLY


def test_format_flag_fixes_drift_in_place(sandbox):
    tmp, home = sandbox
    crate = _crate(tmp / "fmtcrate", DRIFT_ONLY, "fmtcrate")
    context = _context(_run_gate(_payload(crate, "src/lib.rs"), home, crate, "--format"))
    assert "formatted" in context
    assert (crate / "src" / "lib.rs").read_text(encoding="utf-8") == "pub fn g() -> i32 {\n    1\n}\n"


def test_repeated_check_is_throttled(sandbox):
    tmp, home = sandbox
    crate = _crate(tmp / "thrcrate", TYPE_ERROR, "thrcrate")
    _warm(crate)
    assert "COMPILE ERRORS" in _context(_run_gate(_payload(crate, "src/lib.rs"), home, crate))
    assert _context(_run_gate(_payload(crate, "src/lib.rs"), home, crate)) == ""


def test_file_outside_a_crate_is_noted(sandbox):
    tmp, home = sandbox
    stray = tmp / "stray"
    stray.mkdir()
    (stray / "loose.rs").write_text(CLEAN, encoding="utf-8")
    context = _context(_run_gate(_payload(stray, "loose.rs"), home, stray))
    assert "not inside a Cargo.toml crate" in context


def test_cold_crate_reports_warming_hint(sandbox):
    tmp, home = sandbox
    crate = _crate(tmp / "nocargo", CLEAN, "nocargo")
    context = _context(_run_gate(_payload(crate, "src/lib.rs"), home, crate))
    assert "no target/" in context


def test_stop_mode_runs_clippy_once(sandbox):
    tmp, home = sandbox
    crate = _crate(tmp / "clippycrate", CLIPPY_WARNING, "clippycrate")
    _warm(crate)
    payload = json.dumps({"cwd": str(crate), "hook_event_name": "Stop"})
    first = _message(_run_gate(payload, home, crate, "--stop"))
    assert "clippy" in first
    assert "len_zero" in first
    second = _message(_run_gate(payload, home, crate, "--stop"))
    assert second == ""  # throttled by STOP_CLIPPY_INTERVAL


def test_stop_mode_outside_a_crate_is_silent(sandbox):
    tmp, home = sandbox
    payload = json.dumps({"cwd": str(tmp), "hook_event_name": "Stop"})
    assert _run_gate(payload, home, tmp, "--stop").stdout.strip() == ""


def test_stop_mode_uses_the_stop_envelope_not_the_post_tool_one(sandbox):
    tmp, home = sandbox
    crate = _crate(tmp / "envcrate", TYPE_ERROR, "envcrate")
    _warm(crate)
    payload = json.dumps({"cwd": str(crate), "hook_event_name": "Stop"})
    result = _run_gate(payload, home, crate, "--stop")
    assert result.stdout.strip()
    body = json.loads(result.stdout)
    assert "systemMessage" in body
    assert "hookSpecificOutput" not in body  # Codex rejects it on Stop events


def test_edit_mode_uses_additional_context(sandbox):
    tmp, home = sandbox
    crate = _crate(tmp / "editenvcrate", TYPE_ERROR, "editenvcrate")
    _warm(crate)
    result = _run_gate(_payload(crate, "src/lib.rs"), home, crate)
    body = json.loads(result.stdout)
    assert body["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "COMPILE ERRORS" in body["hookSpecificOutput"]["additionalContext"]


def test_audit_log_records_modes(sandbox):
    tmp, home = sandbox
    crate = _crate(tmp / "auditcrate", TYPE_ERROR, "auditcrate")
    _warm(crate)
    _run_gate(_payload(crate, "src/lib.rs"), home, crate)
    log = home / "logs" / "rs_edit_gate.jsonl"
    assert log.is_file()
    record = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert record["mode"] == "edit"
    assert record["crates"][0]["crate"] == "auditcrate"
    assert record["crates"][0]["errors"] >= 1


def test_rustup_toolchain_noise_is_never_called_a_parse_error():
    blob = (
        "error: rustup could not choose a version of rustfmt to run, because one "
        "wasn't specified explicitly, and no default is configured."
    )
    parse, drift, note = gate.classify_rustfmt_output(blob, 1, "lib.rs")
    assert parse == []
    assert drift is False
    assert "rustup" in note


def test_real_rustfmt_parse_error_is_recognised():
    blob = "error: expected `;`, found `}\n  --> lib.rs:3:13\n  |\n3 |     return x\n"
    parse, drift, note = gate.classify_rustfmt_output(blob, 1, "lib.rs")
    assert len(parse) == 1
    assert drift is False and note == ""


def test_rustfmt_diff_counts_as_drift_not_error():
    blob = "Diff in lib.rs:1:\n-pub fn g( ) -> i32 { 1 }\n+pub fn g() -> i32 {\n"
    parse, drift, note = gate.classify_rustfmt_output(blob, 1, "lib.rs")
    assert parse == [] and drift is True and note == ""


def test_unrecognised_rustfmt_failure_becomes_a_note():
    parse, drift, note = gate.classify_rustfmt_output("some odd failure", 2, "lib.rs")
    assert parse == [] and drift is False and note
