"""Supervisor job IPC must not be readable or executable through workspace input."""

from __future__ import annotations

import subprocess

import pytest

from services.jobs.state_store import JobStateStore
from supervisor.runners.locator_common import find_executable
from supervisor.security.access_policy import build_policy


def test_job_store_is_denied_even_inside_the_workspace(tmp_path):
    store = tmp_path / ".job_store"
    store.mkdir()
    secret = store / "ui.token"
    secret.write_text("supervisor-only", encoding="utf-8")
    policy = build_policy(tmp_path)

    assert policy.check_read(secret).allowed is False
    assert policy.check_write(secret).allowed is False


@pytest.mark.parametrize(
    "job_id", ["../victim", "..\\victim", "/tmp/victim", "a/b", "a\\b", ".."]
)
def test_job_id_path_traversal_is_refused(tmp_path, job_id):
    store = JobStateStore(str(tmp_path / "store"))

    with pytest.raises(ValueError, match="invalid job id"):
        store.get_job_state(job_id)


def test_untrusted_explicit_executable_is_ignored(tmp_path, monkeypatch):
    untrusted = tmp_path / "opencode.exe"
    untrusted.write_bytes(b"not trusted")

    def _no_lookup(*_args, **_kwargs):
        raise FileNotFoundError("not on PATH")

    monkeypatch.setattr(subprocess, "run", _no_lookup)
    with pytest.raises(FileNotFoundError):
        find_executable(
            "opencode",
            names=["opencode.exe"],
            windows_extra_dirs=[],
            unix_extra_dirs=[],
            not_found_message="untrusted executable refused",
            explicit=str(untrusted),
        )


def test_trusted_explicit_executable_is_accepted(tmp_path, monkeypatch):
    trusted_root = tmp_path / "trusted"
    trusted_root.mkdir()
    executable = trusted_root / "opencode.exe"
    executable.write_bytes(b"trusted")
    monkeypatch.setenv("SOP_TRUSTED_EXECUTABLE_ROOTS", str(trusted_root))

    def _no_lookup(*_args, **_kwargs):
        raise FileNotFoundError("not on PATH")

    monkeypatch.setattr(subprocess, "run", _no_lookup)
    resolved = find_executable(
        "opencode",
        names=["opencode.exe"],
        windows_extra_dirs=[],
        unix_extra_dirs=[],
        not_found_message="missing",
        explicit=str(executable),
    )

    assert resolved == str(executable.resolve())
