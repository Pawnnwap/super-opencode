"""CLI auto-upgrade must be a repair path, not a startup step.

Every UI restart used to run `npm install -g <pkg>@latest` for the selected
engine. When the process was tree-killed mid install (2026-11-06) npm left the
package unpacked but WITHOUT bin shims, and every queued run then failed with
"not found". codex got an "already resolves -> skip" guard; opencode and pi
kept reinstalling on every boot until they got the same one.
"""

from __future__ import annotations

import services.runtime.app_bootstrap as boot


def _no_install(monkeypatch):
    """Fail loudly if any npm install is attempted."""
    def _explode(*_a, **_k):
        raise AssertionError("npm install must not run when the CLI resolves")

    monkeypatch.setattr(boot.subprocess, "Popen", _explode)


def test_opencode_upgrade_skips_when_the_cli_resolves(monkeypatch, tmp_path):
    monkeypatch.setattr(boot, "_installed_cli", lambda name: f"C:/bin/{name}.exe")
    _no_install(monkeypatch)
    boot.auto_upgrade_opencode(settings_file=tmp_path / "settings.json")


def test_pi_upgrade_skips_when_the_cli_resolves(monkeypatch, tmp_path):
    monkeypatch.setattr(boot, "_installed_cli", lambda name: f"C:/bin/{name}.exe")
    _no_install(monkeypatch)
    boot.auto_upgrade_pi(settings_file=tmp_path / "settings.json")


def test_missing_cli_still_gets_the_repair_install(monkeypatch, tmp_path):
    """A CLI that does NOT resolve must still be installed (the repair path)."""
    monkeypatch.setattr(boot, "_installed_cli", lambda _name: "")
    seen: list[str] = []

    class _Proc:
        returncode = 0

        def communicate(self, timeout=None):
            return "", ""

    def _fake_popen(cmd, **_kwargs):
        seen.append(cmd)
        return _Proc()

    monkeypatch.setattr(boot.subprocess, "Popen", _fake_popen)
    boot.auto_upgrade_opencode(settings_file=tmp_path / "settings.json")
    assert seen and "opencode-ai" in seen[0]


def test_skip_switch_still_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCODE_SKIP_UPGRADE", "1")
    monkeypatch.setattr(boot, "_installed_cli", lambda _name: "")
    _no_install(monkeypatch)
    boot.auto_upgrade_opencode(settings_file=tmp_path / "settings.json")
