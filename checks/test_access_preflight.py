"""Checks for the access-policy preflight drills (the "canary" layer)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from supervisor.security import engine_guards, preflight
from supervisor.security.access_policy import build_policy
from supervisor.security.engine_guards import install_access_guard
from supervisor.security.preflight import run_drill, run_policy_drill


@pytest.fixture(autouse=True)
def isolated_store(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(engine_guards, "POLICY_STORE_DIR", tmp_path / "store")


@pytest.fixture()
def layout(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    ref = tmp_path / "ref"
    ref.mkdir()
    return ws, ref


def test_policy_drill_passes(layout):
    ws, ref = layout
    report = run_policy_drill(ws, build_policy(ws, [ref]))
    assert report.ok, report.fail_text()
    assert report.passed == len(report.checks)
    assert all(check.line().startswith("[PASS]") for check in report.checks)


def test_policy_drill_fails_when_boundary_is_disabled(layout):
    ws, ref = layout
    report = run_policy_drill(ws, build_policy(ws, [ref], enabled=False))
    assert not report.ok
    names = {check.name for check in report.failures()}
    assert "outside read denied" in names
    assert "relative escape denied" in names


def test_policy_drill_detects_a_leaky_policy(layout):
    ws, ref = layout

    class _Leaky:
        """A policy whose checks always allow: the drill must reject it."""

        whitelist = (ref.resolve(),)

        @staticmethod
        def check_read(_path):
            return type("D", (), {"allowed": True})()

        @staticmethod
        def check_write(_path):
            return type("D", (), {"allowed": True})()

        @staticmethod
        def scan_command(_command):
            return [object()]

    report = run_policy_drill(ws, _Leaky())
    assert not report.ok
    names = {check.name for check in report.failures()}
    assert "outside read denied" in names
    assert "inside shell command allowed" in names


def test_policy_drill_passes_with_whitelisted_parent(tmp_path):
    # Study layout: the workspace sits inside a read-only reference tree.
    # The drill's old fixed fixtures (ws.parent, ws+"-evil") landed inside
    # the whitelist and turned legitimate read grants into false failures.
    root = tmp_path / "research"
    ws = root / "studies" / "study_r1"
    ws.mkdir(parents=True)
    policy = build_policy(ws, [root])
    report = run_policy_drill(ws, policy)
    assert report.ok, report.fail_text()
    sibling = Path(str(ws) + "-evil")
    assert policy.check_read(sibling / "x").allowed
    assert not policy.check_write(sibling / "x").allowed


def test_codex_drill_with_whitelisted_parent(tmp_path):
    root = tmp_path / "research"
    ws = root / "studies" / "study_r1"
    ws.mkdir(parents=True)
    policy = build_policy(ws, [root])
    install = install_access_guard(ws, policy, "codex", codex_home=tmp_path / "codex_home")
    report = run_drill("codex", ws, policy, install)
    assert report.ok, report.fail_text()
    names = {check.name for check in report.checks}
    assert "codex gate denies outside read" in names


def test_policy_drill_one_explicit_failure_without_outside_root(layout, monkeypatch):
    ws, ref = layout
    monkeypatch.setattr(preflight, "_outside_fixture", lambda ws, roots: None)
    report = run_policy_drill(ws, build_policy(ws, [ref]))
    assert not report.ok
    failures = report.failures()
    assert len(failures) == 1
    assert failures[0].name == "outside drill root"


def test_static_drill_opencode(layout):
    ws, ref = layout
    policy = build_policy(ws, [ref])
    install = install_access_guard(ws, policy, "opencode", codex_home=ws / "ch")
    report = run_drill("opencode", ws, policy, install)
    assert report.ok, report.fail_text()
    names = {check.name for check in report.checks}
    assert "opencode policy pointer into store" in names
    assert "opencode snapshot matches workspace" in names


def test_opencode_drill_rejects_pointer_outside_store(layout):
    ws, ref = layout
    policy = build_policy(ws, [ref])
    install = install_access_guard(ws, policy, "opencode", codex_home=ws / "ch")
    pointer = ws / ".supervisor" / "access_policy.json"
    data = json.loads(pointer.read_text(encoding="utf-8"))
    data["store"] = str(ws / "evil" / "policy-forged.json")
    pointer.write_text(json.dumps(data), encoding="utf-8")
    report = run_drill("opencode", ws, policy, install)
    assert not report.ok
    assert "opencode policy pointer into store" in {
        check.name for check in report.failures()
    }


def test_static_drill_pi(layout):
    ws, ref = layout
    policy = build_policy(ws, [ref])
    install = install_access_guard(ws, policy, "pi", codex_home=ws / "ch")
    report = run_drill("pi", ws, policy, install)
    assert report.ok, report.fail_text()


def test_codex_drill_end_to_end(layout, tmp_path):
    ws, ref = layout
    policy = build_policy(ws, [ref])
    install = install_access_guard(ws, policy, "codex", codex_home=tmp_path / "codex_home")
    report = run_drill("codex", ws, policy, install)
    assert report.ok, report.fail_text()
    names = {check.name for check in report.checks}
    assert "codex gate denies outside read" in names
    assert "codex gate allows cwd read" in names


def test_codex_drill_catches_missing_hook(layout, tmp_path):
    ws, ref = layout
    policy = build_policy(ws, [ref])
    home = tmp_path / "codex_home"
    install = install_access_guard(ws, policy, "codex", codex_home=home)
    hooks = home / "hooks.json"
    data = json.loads(hooks.read_text(encoding="utf-8"))
    data["hooks"]["PreToolUse"] = []
    hooks.write_text(json.dumps(data), encoding="utf-8")

    report = run_drill("codex", ws, policy, install)
    assert not report.ok
    assert "codex PreToolUse hook registered" in {c.name for c in report.failures()}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
