"""Checks for the access-policy preflight drills (the "canary" layer)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import supervisor.security.engine_guards as engine_guards
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


def test_static_drill_opencode(layout):
    ws, ref = layout
    policy = build_policy(ws, [ref])
    install = install_access_guard(ws, policy, "opencode", codex_home=ws / "ch")
    report = run_drill("opencode", ws, policy, install)
    assert report.ok, report.fail_text()


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
