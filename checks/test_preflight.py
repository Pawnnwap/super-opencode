"""Preflight is the single answer to "can I launch?".

These tests exist because the three answers used to disagree: the button was
enabled while ``launch_job`` would fail, and the wizard listed blockers the Run
page had never heard of.
"""

from __future__ import annotations

import pytest

from services.webui import preflight
from supervisor.protocols.protocol import PROTOCOL_JSONL_NAME


@pytest.fixture
def patch_probes(monkeypatch):
    def _patch(ok: bool) -> None:
        from services.webui.state import app_state

        monkeypatch.setattr(app_state, "tests_ok", lambda: ok)

    return _patch


def test_missing_workspace_is_the_only_blocker():
    blockers = preflight.blockers(None)
    assert [b.code for b in blockers] == ["no_workspace"]


def test_missing_protocol_blocks_even_when_probes_pass(patch_probes, tmp_path):
    patch_probes(True)
    codes = [b.code for b in preflight.blockers(tmp_path)]
    assert "no_protocol" in codes
    assert "probes_failed" not in codes


def test_probe_failure_is_forgiven_only_on_request(patch_probes, tmp_path):
    patch_probes(False)
    (tmp_path / PROTOCOL_JSONL_NAME).write_text("{}", encoding="utf-8")

    codes = [b.code for b in preflight.blockers(tmp_path)]
    assert "probes_failed" in codes

    forgiven = [
        b.code for b in preflight.hard_blockers(tmp_path, forgive_probes=True)
    ]
    assert "probes_failed" not in forgiven


def test_protocol_alone_does_not_block_when_probes_pass(patch_probes, tmp_path):
    patch_probes(True)
    (tmp_path / PROTOCOL_JSONL_NAME).write_text("{}", encoding="utf-8")
    assert preflight.hard_blockers(tmp_path) == []


def test_every_blocker_carries_a_fix_destination(patch_probes, tmp_path):
    patch_probes(False)
    for blocker in preflight.blockers(tmp_path):
        assert blocker.fix_href.startswith("/#"), blocker.code
        assert blocker.label
