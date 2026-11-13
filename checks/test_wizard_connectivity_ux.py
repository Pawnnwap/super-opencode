"""Wizard + nav UX: connectivity tests visible, live runs viewable unlocked.

- The connectivity-test buttons used to sit in a folded expansion three
  sections below the LLM configuration they prove; first-run users had to
  find and open it. They now render open, directly after the Configuration
  section.
- The run nav lock (and the run page's lock message) gated VIEWING
  behind the connectivity tests; with a live or queued job present the pages
  must stay open - that is where the run is watched and stopped.
"""
from pathlib import Path

from services.webui.layout import _has_active_jobs

_REPO = Path(__file__).resolve().parents[1]


def _read(*parts: str) -> str:
    return (_REPO.joinpath(*parts)).read_text(encoding="utf-8")


def test_log_panel_has_speech_and_tool_toggles_and_no_noise_switch():
    """The log display offers explicit Agent-speech / Tool-use toggles; the
    noise switch is gone and noise levels are never rendered."""
    panel = _read("services", "webui", "components", "log_panel.py")
    assert 'ui.switch(\n                "Agent speech"' in panel
    assert 'ui.switch(\n                "Tool use"' in panel
    assert "Show noise" not in panel
    assert "verbose_switch" not in panel
    # Noise is unconditionally in the hidden set; both toggles gate their
    # category.
    assert "hidden = set(DEFAULT_HIDDEN_LEVELS)" in panel
    assert 'hidden.add("tool")' in panel
    assert 'hidden.add("opencode_output")' in panel


def test_connectivity_section_is_flat_not_an_expansion():
    src = _read("services", "webui", "pages", "wizard.py")
    body = src.split("def _connectivity_section")[1].split("\ndef ", 1)[0]
    # Plain heading + buttons on the page; no collapsible wrapper.
    assert 'ui.label("Connectivity Tests")' in body
    assert "_section(" not in body


def test_connectivity_section_comes_right_after_llm_configuration():
    src = _read("services", "webui", "pages", "wizard.py")
    render_body = src.split("def render() -> None:")[1]
    order = [
        line.strip().removeprefix("_").removesuffix("()")
        for line in render_body.splitlines()
        if line.strip().startswith("_") and line.strip().endswith("()")
    ]
    assert order.index("config_section") < order.index("connectivity_section")
    # The LLM/model config lives inside the Configuration section.
    assert order.index("connectivity_section") == order.index("config_section") + 1


def test_protocol_comes_before_access_and_protection():
    """The auto-generator derives the whitelist FROM the protocol.

    Access & Protection's "Auto-generate" run reads the workspace protocol to
    propose read-only reference directories, so it must sit below the protocol
    editor - otherwise the operator is asked to generate from a document that
    does not exist yet, and an unsaved draft is invisible to it.
    """
    src = _read("services", "webui", "pages", "wizard.py")
    render_body = src.split("def render() -> None:")[1]
    order = [
        line.strip().removeprefix("_").removesuffix("()")
        for line in render_body.splitlines()
        if line.strip().startswith("_") and line.strip().endswith("()")
    ]
    # Advanced -> Protocol -> Access & Protection: the rarely-touched knobs
    # stay third-from-last, then the setup flow in dependency order.
    assert order.index("advanced_section") < order.index("protocol_section"), (
        "Advanced settings come before the protocol they configure"
    )
    assert order.index("protocol_section") < order.index("protection_section"), (
        "Access & Protection must follow the protocol editor it generates from"
    )
    assert order[-1] == "protection_section"


def test_run_page_lock_yields_to_queued_jobs():
    # PENDING counts as live: a queued batch must be viewable unlocked. The
    # rule now lives in one shared helper instead of a per-page tuple.
    src = _read("services", "webui", "pages", "run.py")
    assert "active_jobs(get_job_manager())" in src.split("def _has_running_job")[1].split(
        "\n\ndef ", 1,
    )[0]
    states = _read("services", "jobs", "job_states.py")
    assert "ACTIVE_JOB_STATES" in states
    assert '"PENDING"' in states and '"RUNNING"' in states


def _job(jid: str, state: str, jtype: str = "run") -> dict:
    return {"job_id": jid, "state": state, "type": jtype}


class _FakeStore:
    def __init__(self, jobs):
        self._jobs = jobs

    def list_jobs(self):
        return list(self._jobs)

    def get_job_state(self, job_id):
        return self._jobs.get(job_id)


class _FakeManager:
    def __init__(self, jobs):
        self.store = _FakeStore(jobs)


def test_nav_lock_helper_sees_live_and_queued_but_not_history():
    live = _FakeManager({
        "a": _job("a", "RUNNING"),
        "b": _job("b", "PENDING"),
    })
    assert _has_active_jobs(live)

    finished = _FakeManager({
        "c": _job("c", "SUCCESS"),
        "d": _job("d", "FAILED"),
        "e": _job("e", "CANCELLED"),
        "f": _job("f", "BLOCKED"),
        "g": _job("g", "ABANDONED"),
    })
    assert not _has_active_jobs(finished)
