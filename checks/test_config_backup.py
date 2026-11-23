"""A backup model identical to the primary is not resilience."""

from __future__ import annotations

from pathlib import Path

from supervisor.utils.config import SupervisorConfig


def _config(**overrides) -> SupervisorConfig:
    values = {
        "protocol_path": Path("protocol.md"),
        "workspace": Path("."),
    }
    values.update(overrides)
    return SupervisorConfig(**values)


def test_identical_supervisor_backup_is_disabled():
    config = _config(
        supervisor_model="deepseek-v4.1-flash",
        supervisor_model_backup="deepseek-v4.1-flash",
    )
    assert config.supervisor_model_backup is None
    assert config.supervisor_model == "deepseek-v4.1-flash"


def test_identical_agent_backup_is_disabled():
    config = _config(
        opencode_model="deepseek-v4.1-flash",
        opencode_model_backup="deepseek-v4.1-flash",
    )
    assert config.opencode_model_backup is None
    assert config.opencode_model == "deepseek-v4.1-flash"


def test_distinct_backups_are_kept():
    config = _config(
        supervisor_model="primary",
        supervisor_model_backup="backup",
        opencode_model="agent-primary",
        opencode_model_backup="agent-backup",
    )
    assert config.supervisor_model_backup == "backup"
    assert config.opencode_model_backup == "agent-backup"


def test_blank_backup_is_disabled():
    config = _config(
        supervisor_model="primary",
        supervisor_model_backup="   ",
    )
    assert config.supervisor_model_backup is None