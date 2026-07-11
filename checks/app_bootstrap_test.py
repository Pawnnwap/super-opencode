from pathlib import Path

import pytest

from services.runtime import app_bootstrap


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(app_bootstrap.Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def test_codex_upgrade_preserves_existing_config(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = home / ".codex" / "config.toml"
    config.parent.mkdir()
    original = b'model = "user-model"\n'
    config.write_bytes(original)
    config.chmod(0o600)

    def destructive_upgrade(command: str, home_dir: str) -> tuple[int, str, str]:
        config.write_text('model = "startup-default"\n', encoding="utf-8")
        config.chmod(0o644)
        return 0, "", ""

    monkeypatch.setattr(app_bootstrap, "_run_codex_upgrade", destructive_upgrade)

    app_bootstrap.auto_upgrade_codex(home / "settings.json")

    assert config.read_bytes() == original
    assert config.stat().st_mode & 0o777 == 0o600


def test_codex_upgrade_restores_config_after_failure(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = home / ".codex" / "config.toml"
    config.parent.mkdir()
    original = b'approval_policy = "on-request"\n'
    config.write_bytes(original)

    def failing_upgrade(command: str, home_dir: str) -> tuple[int, str, str]:
        config.write_text("", encoding="utf-8")
        raise RuntimeError("upgrade failed")

    monkeypatch.setattr(app_bootstrap, "_run_codex_upgrade", failing_upgrade)

    app_bootstrap.auto_upgrade_codex(home / "settings.json")

    assert config.read_bytes() == original


def test_codex_upgrade_does_not_create_config(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app_bootstrap,
        "_run_codex_upgrade",
        lambda command, home_dir: (0, "", ""),
    )

    app_bootstrap.auto_upgrade_codex(home / "settings.json")

    assert not (home / ".codex" / "config.toml").exists()


def test_codex_upgrade_kills_process_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TimedOutProcess:
        returncode = None
        killed = False
        calls = 0

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
            self.calls += 1
            if self.calls == 1:
                raise app_bootstrap.subprocess.TimeoutExpired("npm", timeout)
            return "", ""

        def kill(self) -> None:
            self.killed = True

    process = TimedOutProcess()
    monkeypatch.setattr(
        app_bootstrap.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )

    with pytest.raises(app_bootstrap.subprocess.TimeoutExpired):
        app_bootstrap._run_codex_upgrade("npm command", "/tmp")

    assert process.killed
    assert process.calls == 2
