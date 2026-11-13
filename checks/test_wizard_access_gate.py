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
    the panel that generates from it."""
    src = (_REPO / "services/webui/pages/wizard.py").read_text(encoding="utf-8")
    render_body = src.split("def render() -> None:")[1]
    order = [
        line.strip().removeprefix("_").removesuffix("()")
        for line in render_body.splitlines()
        if line.strip().startswith("_") and line.strip().endswith("()")
    ]
    assert order.index("advanced_section") < order.index("protocol_section")
    assert order.index("protocol_section") < order.index("protection_section")
    # Nothing follows Access & Protection: it is the last setup step, which is
    # why the hand-off to Live Run happens there.
    assert order[-1] == "protection_section"


def test_saving_the_protocol_stays_on_the_page():
    """Save must not navigate away from the panel it just unlocked."""
    src = (_REPO / "services/webui/pages/wizard.py").read_text(encoding="utf-8")
    accept = src.split("def _accept_protocol")[1].split("\ndef ", 1)[0]
    assert "_protection_section.refresh()" in accept
    assert 'ui.navigate.to("/run")' not in accept


def test_access_completion_hands_off_to_live_run():
    """The jump to /run belongs to the END of setup, not the protocol save."""
    src = (_REPO / "services/webui/pages/wizard.py").read_text(encoding="utf-8")
    gen = src.split("async def _auto_generate")[1].split("\n        def ready_now", 1)[0]
    assert 'ui.navigate.to("/run")' in gen, (
        "finishing Access & Protection is what takes the operator to Live Run"
    )
    # Warnings keep the operator on the page so they can be read.
    assert "if not notes:" in gen.split('ui.navigate.to("/run")')[0][-80:]


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
