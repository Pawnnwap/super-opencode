"""Web UI formatting helpers: the phase breadcrumb and markdown previews.

The breadcrumb reads the loop's structural events, not the regex step
detector: `step_progress` keyed on prose, every agent turn ends in a summary,
so it showed "review" almost permanently while the real run cycled
code -> test -> review many times. Regression kept from the first version:
the markup is real tags, never markdown that a ui.html would show literally.
"""

from services.webui.log_format import (
    markdown_preview,
    phase_breadcrumb,
    phase_state,
    tool_phase,
)


def _ev(level: str, msg: str = "", **extra) -> dict:
    return {"level": level, "msg": msg, **extra}


PLAN = _ev("log-plan_phase", "[plan mode] Round 1/2 — sending prompt to codex…")
PLAN_PROMPT = _ev("opencode_prompt", "→ plan prompt (4,255 chars) — full text in event #1")
INITIAL = _ev("opencode_prompt", "→ initial prompt (29,991 chars) — full text in event #7")
TURN = _ev("opencode_prompt", "→ turn prompt (7,941 chars) — full text in event #12")
RESTART = _ev("opencode_prompt", "→ restart prompt (12,412 chars) — full text in event #16")
COMPACTION = _ev("opencode_prompt", "→ compaction prompt (3,838 chars) — full text in event #14")
OUTPUT = _ev("opencode_output", "← output (8,898 chars, 196 lines): Understood — iteration 3")
CONTINUE = _ev("supervisor_response", "⚖ CONTINUE — criteria 4/8 — full text in event #13")
DONE = _ev("supervisor_response", "⚖ DONE — criteria 8/8 — full text in event #40")
EDIT = _ev("tool", "[edit] services/webui/log_format.py")
PYTEST = _ev("tool", "[shell] cd E:\\proj && python -m pytest checks -q")
READ = _ev("tool", "[shell] rg -n phase services/webui")


# ----------------------------------------------------------- phase_state --


def test_plan_stage_holds_plan_until_the_initial_prompt():
    ps = phase_state([PLAN, PLAN_PROMPT, OUTPUT, CONTINUE])
    assert (ps.phase, ps.round) == ("plan", 0)
    ps = phase_state([PLAN, PLAN_PROMPT, OUTPUT, CONTINUE, INITIAL])
    assert (ps.phase, ps.round, ps.exact) == ("code", 1, True)


def test_first_turn_without_plan_stage_explores_before_coding():
    assert phase_state([INITIAL, READ]).phase == "plan"
    assert phase_state([INITIAL, READ, EDIT]).phase == "code"


def test_round_cycles_code_test_review_and_counts_verdicts():
    ps = phase_state([INITIAL, EDIT, PYTEST])
    assert (ps.phase, ps.round, ps.visited) == ("test", 1, frozenset({"code", "test"}))
    assert phase_state([INITIAL, EDIT, PYTEST, OUTPUT]).phase == "review"
    ps = phase_state([INITIAL, EDIT, PYTEST, OUTPUT, CONTINUE])
    assert (ps.phase, ps.round) == ("review", 1)
    ps = phase_state([INITIAL, EDIT, PYTEST, OUTPUT, CONTINUE, TURN])
    assert (ps.phase, ps.round, ps.visited) == ("code", 2, frozenset())


def test_done_verdict_and_success_state_finish_the_pipeline():
    assert phase_state([INITIAL, EDIT, OUTPUT, DONE]).phase == "done"
    ps = phase_state([INITIAL, EDIT, OUTPUT], "SUCCESS")
    assert (ps.phase, ps.end) == ("done", "success")
    ps = phase_state([INITIAL, EDIT, PYTEST], "FAILED")
    assert (ps.phase, ps.end) == ("test", "failed")
    assert phase_state([INITIAL, EDIT], "BLOCKED").end == "blocked"


def test_compaction_is_housekeeping_not_a_round():
    # Stage 2: the deletion-permission answer is logged as a supervisor_response
    # followed by the compaction prompt; the interrupted turn was never judged.
    events = [INITIAL, EDIT, OUTPUT, CONTINUE, COMPACTION, OUTPUT, RESTART, PYTEST]
    ps = phase_state(events)
    assert (ps.phase, ps.round) == ("test", 1)
    # Stage 1 (light): restart with no verdict at all resumes the round.
    assert phase_state([INITIAL, EDIT, OUTPUT, RESTART, EDIT]).round == 1
    # A restart right after a real verdict (cross-turn loop breaker) is a new round.
    assert phase_state([INITIAL, EDIT, OUTPUT, CONTINUE, RESTART]).round == 2


def test_truncated_window_marks_round_as_a_floor():
    ps = phase_state([PYTEST, OUTPUT, CONTINUE, TURN, EDIT])
    assert (ps.phase, ps.round, ps.exact) == ("code", 2, False)
    assert "×2+" in phase_breadcrumb(ps)


def test_full_log_mode_keeps_structured_fields():
    turn = {"level": "opencode_prompt", "prompt_kind": "turn", "msg": "full prompt body"}
    done = {"level": "supervisor_response", "digest": "⚖ DONE — 8/8", "msg": "raw verdict"}
    ps = phase_state([INITIAL, EDIT, OUTPUT, CONTINUE, turn, EDIT, OUTPUT, done])
    assert (ps.phase, ps.round) == ("done", 2)


def test_tool_phase_classifier():
    assert tool_phase("[edit] foo.py") == "code"
    assert tool_phase("[write] notes.md") == "code"
    assert tool_phase("[shell] apply_patch @' *** Begin Patch") == "code"
    assert tool_phase("[shell] @'\nimport io\n'@ | Set-Content a.py") == "code"
    assert tool_phase("[shell] cd x && pytest -q") == "test"
    assert tool_phase("[bash] npm test") == "test"
    assert tool_phase("[shell] python checks/test_pi_config.py") == "test"
    assert tool_phase("[shell] python -m pytest > out.txt") == "test"
    assert tool_phase("[shell] rg -n foo") == ""
    assert tool_phase("[read] x.py") == ""
    assert tool_phase("plain text") == ""


# ------------------------------------------------------ phase_breadcrumb --


def test_breadcrumb_markup_for_a_second_round():
    out = phase_breadcrumb(phase_state([INITIAL, EDIT, PYTEST, OUTPUT, CONTINUE, TURN]))
    assert out == (
        '<span class="phase-crumb">'
        '<span class="phase-done">plan</span>'
        '<span class="phase-arrow phase-arrow-lit">→</span>'
        '<span class="phase-loop">'
        '<span class="phase-current">code</span>'
        '<span class="phase-arrow phase-arrow-dim">→</span>'
        '<span class="phase-todo">test</span>'
        '<span class="phase-arrow phase-arrow-dim">→</span>'
        '<span class="phase-todo">review</span>'
        "</span>"
        '<span class="phase-round">×2</span>'
        '<span class="phase-arrow phase-arrow-dim">→</span>'
        '<span class="phase-todo">done</span>'
        "</span>"
    )
    assert "**" not in out
    assert "~~" not in out


def test_breadcrumb_skips_a_test_phase_the_round_never_ran():
    out = phase_breadcrumb(phase_state([INITIAL, EDIT, OUTPUT]))
    assert '<span class="phase-done">code</span>' in out
    assert '<span class="phase-skipped">test</span>' in out
    assert '<span class="phase-current">review</span>' in out
    assert out.count("phase-arrow-lit") == 3


def test_breadcrumb_tints_the_chip_once_the_job_ended():
    out = phase_breadcrumb(phase_state([INITIAL, EDIT, OUTPUT, DONE], "SUCCESS"))
    assert '<span class="phase-current phase-end-success">done</span>' in out
    assert "phase-skipped" not in out
    assert out.count("phase-arrow-lit") == 4
    out = phase_breadcrumb(phase_state([INITIAL, EDIT, PYTEST], "FAILED"))
    assert '<span class="phase-current phase-end-failed">test</span>' in out


def test_breadcrumb_before_any_activity_is_all_todo():
    out = phase_breadcrumb(phase_state([_ev("info", "Archiving previous workspace state...")]))
    assert "phase-current" not in out
    assert "phase-round" not in out
    assert out.count("phase-todo") == 5
    assert out.count("phase-arrow-dim") == 4


def test_preview_short_text_unchanged():
    text = "# Report\n\nAll **gates** passed.\n"
    assert markdown_preview(text, 2000) == text


def test_preview_cuts_at_paragraph_boundary():
    text = "intro\n\n" + "x" * 40 + "\n\ncut-should-land-here **open"
    out = markdown_preview(text, 60)
    assert out == "intro\n\n" + "x" * 40
    assert out.endswith("x" * 40)


def test_preview_drops_unbalanced_trailing_emphasis():
    text = "para one\n\nok **bo" + "y" * 200
    out = markdown_preview(text, 40)
    assert out.count("**") % 2 == 0
    assert not out.rstrip().endswith("**")


def test_preview_balanced_markers_kept():
    text = "a\n\n**done** " + "y" * 200
    out = markdown_preview(text, 100)
    assert "**done**" in out
    assert out.count("**") % 2 == 0


def test_preview_handles_all_pair_markers():
    text = "`tic` ~~strike~~ __under__ " + "y" * 100 + " ~~open"
    out = markdown_preview(text, 130)
    assert "open" not in out
    for mark in ("`", "~~", "__"):
        assert out.count(mark) % 2 == 0


def test_agent_thought_renders_under_the_agent_speech_toggle():
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    source = (
        repo / "services" / "webui" / "components" / "log_panel.py"
    ).read_text(encoding="utf-8")

    assert '"agent_thought"' in source
    assert 'hidden.add("agent_thought")' in source
    assert "💭" in source
