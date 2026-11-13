"""Access & Protection's auto-generator must be gated on a SAVED protocol.

The generator's first step asks the supervisor LLM to propose read-only
reference directories FROM the workspace protocol, and it reads that protocol
from disk. An unsaved draft in the Protocol editor is invisible to it, so
running the generator before saving proposed paths from nothing - with no
error and no explanation of why the suggestions were useless.

Contract under test:
- the Protocol section renders ABOVE Access & Protection (the document the
  generator depends on is written first);
- with no saved protocol the auto-generate button is disabled and a standing
  notice says what to do;
- the gate is re-evaluated at click time, so saving the protocol above and
  clicking again works without a page reload.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.webui.pages import wizard

_REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def fake_workspace(tmp_path, monkeypatch):
    """A workspace path that resolves, with no protocol file in it."""
    monkeypatch.setattr(wizard, "_workspace", lambda: tmp_path)
    monkeypatch.setattr(wizard, "_profile_sync", lambda *a, **k: {})
    return tmp_path


def _write_protocol(workspace: Path) -> None:
    from supervisor.protocols.protocol import (
        Protocol,
        save_protocol,
    )

    text = (
        "## INPUT\n\none input artifact\n\n"
        "## TARGET\n\n1. reach the target\n\n"
        "## RESTRICTIONS\n\nno network\n"
    )
    save_protocol(workspace, Protocol(
        raw=text,
        input_section="one input artifact",
        target_section="1. reach the target",
        restrictions_section="no network",
    ))


def test_no_saved_protocol_blocks_the_generator(fake_workspace):
    ready, reason = wizard._protocol_ready(fake_workspace)
    assert ready is False
    assert "Accept & Save" in reason, (
        "the notice must name the action that lifts the lock"
    )
    assert "protocol" in reason.lower()


def test_saved_protocol_unlocks_the_generator(fake_workspace):
    _write_protocol(fake_workspace)
    ready, reason = wizard._protocol_ready(fake_workspace)
    assert ready is True
    assert reason == ""


def test_empty_saved_protocol_still_blocks(fake_workspace):
    from supervisor.protocols.protocol import PROTOCOL_JSONL_NAME

    (fake_workspace / PROTOCOL_JSONL_NAME).write_text("", encoding="utf-8")
    ready, reason = wizard._protocol_ready(fake_workspace)
    assert ready is False
    assert "empty" in reason.lower()


def test_section_order_is_advanced_then_protocol_then_protection():
    """Advanced settings stay first of the three; the protocol still precedes
    the panel that generates from it; the hand-off button closes the page."""
    src = (_REPO / "services/webui/pages/wizard.py").read_text(encoding="utf-8")
    render_body = src.split("def render() -> None:")[1]
    order = [
        line.strip().removeprefix("_").removesuffix("()")
        for line in render_body.splitlines()
        if line.strip().startswith("_") and line.strip().endswith("()")
    ]
    assert order.index("advanced_section") < order.index("protocol_section")
    assert order.index("protocol_section") < order.index("protection_section")
    # The hand-off to Live Run is the last thing on the page.
    assert order[-1] == "ready_section"
    assert order.index("protection_section") < order.index("ready_section")


def test_saving_the_protocol_stays_on_the_page():
    """Save must not navigate away from the panel it just unlocked."""
    src = (_REPO / "services/webui/pages/wizard.py").read_text(encoding="utf-8")
    accept = src.split("def _accept_protocol")[1].split("\ndef ", 1)[0]
    assert "_protection_section.refresh()" in accept
    assert 'ui.navigate.to("/run")' not in accept


def test_access_completion_does_not_navigate_on_its_own():
    """Finishing the generator leaves the operator on the page to review it.

    The only hand-off is the "Ready for Live Run!" button at the page bottom.
    """
    src = (_REPO / "services/webui/pages/wizard.py").read_text(encoding="utf-8")
    gen = src.split("async def _auto_generate")[1].split("\n        def ready_now", 1)[0]
    assert 'ui.navigate.to("/run")' not in gen, (
        "auto-generate must not jump to Live Run by itself"
    )
    assert "mark_configured(workspace)" in gen, (
        "a completed run is what unlocks the hand-off button"
    )
    assert "_ready_section.refresh()" in gen


def test_ready_button_is_the_only_hand_off_and_is_gated():
    src = (_REPO / "services/webui/pages/wizard.py").read_text(encoding="utf-8")
    body = src.split("def _ready_section")[1].split("\n# ──", 1)[0]
    assert '"Ready for Live Run!"' in body
    assert 'ui.navigate.to("/run")' in body
    # Greyed out until every setup step and test is complete...
    assert "button.set_enabled(False)" in body
    assert "_setup_blockers(" in body
    # ...and the lock says what is missing instead of a silent no-op.
    assert "for item in blockers:" in body


def test_ready_gate_covers_tests_protocol_access_and_drill():
    src = (_REPO / "services/webui/pages/wizard.py").read_text(encoding="utf-8")
    body = src.split("def _setup_blockers")[1].split("\n@ui.refreshable", 1)[0]
    assert "app_state.tests_ok()" in body
    assert "_protocol_ready(workspace)" in body
    assert 'profile.get("access_configured")' in body
    assert "run_policy_drill(workspace, policy)" in body


def test_auto_generate_button_is_gated_and_rechecked():
    src = (_REPO / "services/webui/pages/wizard.py").read_text(encoding="utf-8")
    body = src.split("def _protection_section")[1].split("\n@ui.refreshable", 1)[0]
    # Rendered disabled when the protocol is missing...
    assert "auto_button.set_enabled(False)" in body
    # ...and re-checked on every click instead of trusting the render.
    assert "def ready_now()" in body
    assert "if not ready_now():" in body
    # The standing notice is shown, not a silent no-op.
    assert "gate_label.set_visibility(not ok)" in body


def test_generator_reads_the_protocol_from_disk():
    """A draft that was never saved must not reach the LLM call."""
    src = (_REPO / "services/webui/pages/wizard.py").read_text(encoding="utf-8")
    gen = src.split("async def _auto_generate")[1].split("\n        def ready_now", 1)[0]
    assert "_protocol_text(workspace)" in gen
    assert "_compose_protocol(" not in gen, (
        "the generator must use the SAVED document, not the editor draft"
    )


def test_saving_the_protocol_unlocks_the_panel_in_place():
    """Save must not navigate away from the panel it just unlocked.

    The auto-generator sits directly below the protocol editor and only
    unlocks once the protocol is on disk; jumping to Live Run on save sent the
    operator away mid-setup.
    """
    src = (_REPO / "services/webui/pages/wizard.py").read_text(encoding="utf-8")
    accept = src.split("def _accept_protocol")[1].split("\ndef ", 1)[0]
    assert "_protection_section.refresh()" in accept
    assert 'ui.navigate.to("/run")' not in accept


# ── The "Ready for Live Run!" hand-off gate ──────────────────────────────────


def _configured_workspace(workspace: Path) -> None:
    from supervisor.security.access_profile import mark_configured

    _write_protocol(workspace)
    mark_configured(workspace)


def test_ready_gate_lists_every_missing_step(fake_workspace, monkeypatch):
    monkeypatch.setattr(
        wizard.app_state, "tests_ok", lambda: False,
    )
    blockers = wizard._setup_blockers(fake_workspace)
    joined = " ".join(blockers)
    assert "Connectivity Tests" in joined
    assert "Protocol" in joined
    assert "Access & Protection" in joined


def test_ready_gate_opens_only_when_everything_is_done(fake_workspace, monkeypatch):
    _configured_workspace(fake_workspace)
    monkeypatch.setattr(wizard.app_state, "tests_ok", lambda: True)
    assert wizard._setup_blockers(fake_workspace) == []


def test_ready_gate_stays_shut_without_the_connectivity_tests(
    fake_workspace, monkeypatch,
):
    _configured_workspace(fake_workspace)
    monkeypatch.setattr(wizard.app_state, "tests_ok", lambda: False)
    blockers = wizard._setup_blockers(fake_workspace)
    assert len(blockers) == 1 and "Connectivity Tests" in blockers[0]


def test_ready_gate_stays_shut_without_a_saved_protocol(fake_workspace, monkeypatch):
    from supervisor.security.access_profile import mark_configured

    mark_configured(fake_workspace)
    monkeypatch.setattr(wizard.app_state, "tests_ok", lambda: True)
    blockers = wizard._setup_blockers(fake_workspace)
    assert len(blockers) == 1 and "Protocol" in blockers[0]


def test_ready_gate_stays_shut_until_access_is_configured(
    fake_workspace, monkeypatch,
):
    _write_protocol(fake_workspace)
    monkeypatch.setattr(wizard.app_state, "tests_ok", lambda: True)
    blockers = wizard._setup_blockers(fake_workspace)
    assert len(blockers) == 1 and "Access & Protection" in blockers[0]


def test_ready_gate_reports_a_failed_policy_drill(fake_workspace, monkeypatch):
    """A configured step whose drill fails must not unlock the button."""
    from supervisor.security import preflight

    _configured_workspace(fake_workspace)
    monkeypatch.setattr(wizard.app_state, "tests_ok", lambda: True)
    monkeypatch.setattr(
        preflight, "run_policy_drill",
        lambda *_a, **_k: preflight.DrillReport(
            checks=[preflight.DrillCheck("cwd readable", False, "denied")],
        ),
    )
    blockers = wizard._setup_blockers(fake_workspace)
    assert len(blockers) == 1
    assert "policy drill failed" in blockers[0]
    assert "cwd readable" in blockers[0]


def test_ready_gate_needs_a_workspace(monkeypatch):
    monkeypatch.setattr(wizard.app_state, "tests_ok", lambda: True)
    assert wizard._setup_blockers(None) == [
        "Set a valid workspace path in Configuration.",
    ]
