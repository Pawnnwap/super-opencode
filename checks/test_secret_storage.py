"""Persisted credentials are encrypted at rest and round-trip safely."""

from __future__ import annotations

import json

from services.config import settings
from services.jobs.job_manager import JobManager
from supervisor.security.secrets import decrypt_secrets, protect_secrets
from supervisor.utils.config import SupervisorConfig


def test_secret_mapping_round_trip_without_plaintext(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPER_OPENCODE_KEY_FILE", str(tmp_path / "secret.key"))

    protected = protect_secrets(
        {
            "openai_api_key": "super-secret",
            "codex_api_key": "codex-secret",
            "workspace": "/work",
        }
    )
    restored = decrypt_secrets(protected)

    assert "super-secret" not in json.dumps(protected)
    assert "codex-secret" not in json.dumps(protected)
    assert "openai_api_key" not in protected
    assert protected["openai_api_key_enc"]
    assert restored["openai_api_key"] == "super-secret"
    assert restored["codex_api_key"] == "codex-secret"
    assert restored["workspace"] == "/work"


def test_settings_file_does_not_contain_plaintext_keys(tmp_path, monkeypatch):
    key_file = tmp_path / "secret.key"
    settings_file = tmp_path / "settings.json"
    monkeypatch.setenv("SUPER_OPENCODE_KEY_FILE", str(key_file))
    monkeypatch.setattr(settings, "_SETTINGS_FILE", settings_file)

    settings.save_settings(
        {
            "openai_key": "super-secret",
            "codex_api_key": "codex-secret",
            "workspace": "/work",
        }
    )
    loaded = settings.load_settings()
    raw = settings_file.read_text(encoding="utf-8")

    assert "super-secret" not in raw
    assert "codex-secret" not in raw
    assert loaded["openai_key"] == "super-secret"
    assert loaded["codex_api_key"] == "codex-secret"


def test_job_state_encrypts_and_restores_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPER_OPENCODE_KEY_FILE", str(tmp_path / "secret.key"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = SupervisorConfig(
        protocol_path=workspace / "protocol.md",
        workspace=workspace,
        openai_api_key="super-secret",
        codex_api_key="codex-secret",
    )
    manager = JobManager(str(tmp_path / "store"))
    state = config.to_state_dict()
    manager.store.save_job_state("run_secret", {"state": "PENDING", "config": state})
    raw = manager.store._get_job_path("run_secret").read_text(encoding="utf-8")

    assert "super-secret" not in raw
    assert "codex-secret" not in raw
    restored = manager.config_from_state("run_secret")
    assert restored.openai_api_key == "super-secret"
    assert restored.codex_api_key == "codex-secret"
