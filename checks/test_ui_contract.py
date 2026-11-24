"""Regression guards for the two things that cannot fail loudly.

Cascade layers and the state contract are invisible at runtime: get them wrong
and the page still renders, just with every button the same colour and every
cancelled job counted as a failure. Nothing in the existing suite could catch
either — 203 of 690 UI assertions are source substrings and exactly one touches
a style class.
"""

from __future__ import annotations

import re
from pathlib import Path

from services.jobs.job_states import (
    ATTENTION_JOB_STATES,
    PILL_TONE_CLASS,
    STATE_CONTRACT,
)
from services.webui import theme


def _important_block() -> str:
    """The `@layer theme { ... }` block — everything before `@layer sop`."""
    css = theme.CSS
    start = css.index("@layer theme {")
    end = css.index("@layer sop {")
    return css[start:end]


def test_css_declares_both_layers_in_the_right_order():
    css = theme.CSS
    assert css.index("@layer theme {") < css.index("@layer sop {")
    assert css.count("{") == css.count("}"), "unbalanced braces in theme.CSS"


def test_normal_rules_live_in_the_last_layer():
    # `sop` must not be declared before it is used, or NiceGUI's own layer
    # statement would place it somewhere in the middle and utilities would win.
    css = theme.CSS
    assert css.startswith("@layer theme {")
    assert "@layer sop-important {" not in css
    assert re.search(r"\n\.sop-panel \{", css), "component rules expected in sop"


def test_quasar_fighting_rules_sit_in_the_first_layer():
    block = _important_block()
    for selector in (".q-btn--flat", ".q-btn--outline", ".mono"):
        assert selector in block, selector
    assert "!important" in block


def test_long_lists_keep_only_their_vertical_scrollbar():
    assert ".sop-scroll .q-scrollarea__content { width: 100%; min-width: 0; }" in theme.CSS
    assert ".sop-scroll .q-scrollarea__thumb--h { display: none; }" in theme.CSS
    harvest = (
        Path(__file__).resolve().parents[1]
        / "services" / "webui" / "pages" / "harvest.py"
    ).read_text(encoding="utf-8")
    assert 'props("scrollbar-x=false")' in harvest


def test_accent_fill_token_is_readable_with_white_text():
    assert "--sop-accent-fill: #3370e0;" in theme.CSS
    # The Quasar palette must use the same fill, or bg-primary buttons keep
    # the low-contrast blue.
    from inspect import getsource

    assert 'primary="#3370e0"' in getsource(theme.apply)


def test_surface_ladder_and_text_floor():
    assert "--sop-surface-2: #1b2430;" in theme.CSS
    assert "--sop-text-3: #8794a8;" in theme.CSS
    # text-3 must never be used on surface-3 (measured 4.34:1).
    assert "--sop-surface-3: #232c3a;" in theme.CSS


def test_state_contract_covers_every_job_state():
    assert set(STATE_CONTRACT) == {
        "PENDING",
        "RUNNING",
        "STOPPING",
        "SUCCESS",
        "FAILED",
        "BLOCKED",
        "ABANDONED",
        "CANCELLED",
    }
    for state, style in STATE_CONTRACT.items():
        assert style.tone in PILL_TONE_CLASS, state
        assert style.label


def test_cancelled_is_neutral_and_not_counted_as_attention():
    assert STATE_CONTRACT["CANCELLED"].tone == "neutral"
    assert not STATE_CONTRACT["CANCELLED"].needs_attention
    assert "CANCELLED" not in ATTENTION_JOB_STATES
    # Blocked did stop short of its target, so it does need attention.
    assert "BLOCKED" in ATTENTION_JOB_STATES


def test_pills_are_derived_from_the_contract():
    from services.webui.layout import PILL_MAP

    for state, style in STATE_CONTRACT.items():
        html = PILL_MAP[state]
        assert PILL_TONE_CLASS[style.tone] in html, state
        assert f">{style.label}<" in html, state
        assert 'role="status"' in html, state
