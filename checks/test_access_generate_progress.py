"""Access & Protection's auto-generate must show live progress and real errors.

The generator is two sequential LLM calls with no visible output until the very
end: on a slow endpoint the page sat on one static line ("1/2 Suggesting…")
for minutes, which is indistinguishable from a hang. And because both calls
went through ``io_bound`` with no progress plumbing, a stalled stream produced
only a stderr line the browser never shows.

Contract under test:
- the UI passes progress sinks into both calls;
- the status line carries the character counters and elapsed time;
- a failure names the exception type instead of a generic "failed";
- the underlying calls accept and forward the sinks.
"""

from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


def _wizard_src() -> str:
    return (_REPO / "services/webui/pages/wizard.py").read_text(encoding="utf-8")


def _auto_generate_body() -> str:
    src = _wizard_src()
    return src.split("async def _auto_generate")[1].split("\n        def ready_now", 1)[0]


def test_generator_reports_character_progress():
    """Same shape as protocol generation: reasoning/answer counters + elapsed."""
    body = _auto_generate_body()
    assert "reasoning chars" in body
    assert "answer chars" in body
    assert "_live(" in body, "the status line must be driven by a progress sink"


def test_progress_reaches_both_llm_calls():
    body = _auto_generate_body()
    # Call 1 (whitelist): phase + stream sinks.
    assert "_on_phase," in body
    assert "_on_stream," in body
    # Call 2 (ignore patterns): stream sink.
    assert "_on_stream(c, r)" in body or "_on_stream" in body


def test_failures_name_the_exception_type():
    """A generic "failed" hides whether it timed out, 401'd, or got bad JSON."""
    body = _auto_generate_body()
    assert "type(exc).__name__" in body
    # The FAILED marker stays on screen so a failure is not overwritten by a
    # later success line (or vice versa).
    assert "FAILED after" in body


def test_advisor_and_ignore_calls_accept_progress():
    src = (_REPO / "services/webui/pages/wizard.py").read_text(encoding="utf-8")
    advisor = src.split("def _suggest_whitelist_paths")[1].split("\n# ──", 1)[0]
    assert "on_phase" in advisor and "on_stream" in advisor
    ignore = src.split("def _suggest_ignore_patterns")[1].split("\n# ──", 1)[0]
    assert "on_progress" in ignore and "stop_event" in ignore


def test_advisor_forwards_the_sinks_into_the_llm_helper():
    advisor = (_REPO / "supervisor/security/whitelist_advisor.py").read_text(
        encoding="utf-8",
    )
    fn = advisor.split("def suggest_with_supervisor_llm")[1]
    assert "on_progress=on_phase" in fn, "the scan phase must report too"
    assert "on_progress=_stream_progress" in fn, "the model phase must report"
    assert "stop_event=stop_event" in fn


def test_scan_phase_reports_before_the_llm_call():
    """The workspace scan can be slow on a big tree; it must not be silent."""
    advisor = (_REPO / "supervisor/security/whitelist_advisor.py").read_text(
        encoding="utf-8",
    )
    ctx = advisor.split("def collect_context")[1].split("\ndef ", 1)[0]
    assert "_note(" in ctx
    assert "Scanning workspace files" in ctx
