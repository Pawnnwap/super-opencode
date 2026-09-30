"""Web UI formatting helpers: the phase breadcrumb renders real markup.

Regression: the breadcrumb used to emit markdown (**review**, ~~plan~~) that
ProgressWidgets pushed into a plain-text ui.label, so the asterisks and
tildes showed literally on the Task Snapshot / live page.
"""

from services.webui.log_format import markdown_preview, phase_breadcrumb


def test_current_phase_is_bold_not_markdown():
    out = phase_breadcrumb("review", ["planning", "coding", "testing"])
    assert "<strong>review</strong>" in out
    assert "**" not in out
    assert "~~" not in out


def test_completed_phases_are_struck_through():
    out = phase_breadcrumb("review", ["planning", "coding", "testing"])
    for done in ("plan", "code", "test"):
        assert f"<s>{done}</s>" in out


def test_upcoming_phases_stay_plain():
    out = phase_breadcrumb("review", ["planning"])
    assert out == "<s>plan</s> → code → test → <strong>review</strong> → done"


def test_no_phase_and_no_history_renders_everything_plain():
    out = phase_breadcrumb("", None)
    assert out == "plan → code → test → review → done"


def test_unknown_phase_names_are_ignored():
    out = phase_breadcrumb("refactor", ["somewhere"])
    assert "refactor" not in out
    assert "somewhere" not in out
    assert out == "plan → code → test → review → done"


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
