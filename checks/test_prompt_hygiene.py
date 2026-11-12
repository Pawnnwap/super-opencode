"""Prompt size discipline: a judge prompt must not cost more than it informs.

Measured on the two 2026-11-03 studies (52.7 MB and 36.4 MB of judge prompts):

* the system message was 91% inline file BODIES (47,562 of 52,061 chars) and is
  re-sent on every call — 14.2 MB of duplicate payload for one run;
* suggestion calls carried a 503 KB user message because protected-file reads
  had no size cap (median 147 KB over 216 calls, 86% of that run's prompts);
* the rotated ``supervisor_prompts.log.1`` backup (up to 64 MB) was re-read and
  injected into every judge prompt because only ``*.log`` was skipped;
* ``--- Step Context ---`` appeared twice in every suggestion prompt.
"""
import pathlib

from supervisor.analyzers.codebase_analyzer import (
    CodebaseSnapshot,
    FileSnapshot,
    snapshot_codebase,
)
from supervisor.core.llm_support import context as ctx_mod
from supervisor.core.llm_support.context import SupervisorContextManager
from supervisor.core.llm_support.judgement import generate_suggestions


def _snap(rel: str, body: str = "x = 1\n") -> FileSnapshot:
    return FileSnapshot(rel_path=rel, content=body, truncated=False, sha256="0", skeleton=None)


def _context(workspace, **overrides):
    kwargs = {
        "workspace": workspace,
        "max_tokens": 320_000,
        "max_protected_files_for_suggestions": 5,
        "protocol_target": "TARGET",
        "model": "test",
        "client": None,
        "ignore_matcher": None,
        "log_prompt": lambda *a, **k: None,
        "skip_dirs": {".git"},
        "skip_dir_prefixes": (".",),
        "generated_markdown_files": {"summary.md"},
    }
    kwargs.update(overrides)
    return SupervisorContextManager(**kwargs)


# ── the system prompt carries structure, not bodies ─────────────────────────


def test_skinned_digest_is_much_smaller_than_full_bodies():
    body = "def f():\n" + "    x = 1\n" * 2_000
    files = [_snap(f"mod_{i}.py", body) for i in range(15)]
    snap = CodebaseSnapshot(root=pathlib.Path("ws"), files=files)
    full = snap.digest_for_prompt(max_files=15)
    skim = snap.skimmed_digest_for_prompt(max_files=15)
    assert len(skim) < len(full) * 0.5


def test_skimmed_digest_still_names_every_file_and_keeps_signatures(tmp_path):
    body = "class RoundLotTicketConcentration(BaseIndicator):\n    pass\n"
    (tmp_path / "indicator.py").write_text(body, encoding="utf-8")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "core.py").write_text("x = 1\n", encoding="utf-8")
    snap = snapshot_codebase(tmp_path)
    skim = snap.skimmed_digest_for_prompt(max_files=15)
    assert "indicator.py" in skim
    assert "core.py" in skim
    assert "RoundLotTicketConcentration" in skim


# ── protected-file injection is bounded ─────────────────────────────────────


def test_protected_file_bodies_are_capped(tmp_path):
    big = tmp_path / ".opencode" / "target_state.json"
    big.parent.mkdir(parents=True)
    big.write_text("z" * 200_000, encoding="utf-8")
    protected = _context(tmp_path).read_protected_files()
    assert all(
        len(text) <= ctx_mod._MAX_PROTECTED_FILE_CHARS + 200
        for text in protected.values()
    )


def test_protected_injection_stops_at_the_total_budget(tmp_path):
    d = tmp_path / ".opencode"
    d.mkdir(parents=True)
    for i in range(10):
        (d / f"blob_{i}.json").write_text("y" * 20_000, encoding="utf-8")
    protected = _context(tmp_path).read_protected_files()
    assert sum(len(v) for v in protected.values()) <= (
        ctx_mod._MAX_PROTECTED_TOTAL_CHARS + ctx_mod._MAX_PROTECTED_FILE_CHARS
    )


def test_rotated_prompt_log_backup_is_not_injected(tmp_path):
    d = tmp_path / ".opencode"
    d.mkdir(parents=True)
    (d / "supervisor_prompts.log").write_text("live log", encoding="utf-8")
    (d / "supervisor_prompts.log.1").write_text("rotated " * 10_000, encoding="utf-8")
    (d / "target_state.json").write_text("{}", encoding="utf-8")
    protected = _context(tmp_path).read_protected_files()
    assert not any(name.endswith(".log.1") for name in protected)
    assert not any(name.endswith(".log") for name in protected)
    assert any(name.endswith("target_state.json") for name in protected)


def test_suggestion_files_are_capped_per_file_and_in_total(tmp_path):
    for i in range(5):
        (tmp_path / f"doc_{i}.md").write_text("w" * 100_000, encoding="utf-8")
    mgr = _context(tmp_path)
    mgr.llm_select_files = lambda meta, top_k, **kw: sorted(meta)[:5]
    picked, _paths = mgr.read_protected_files_for_suggestions()
    assert picked
    assert all(len(v) <= ctx_mod._MAX_SUGGESTION_FILE_CHARS + 200 for v in picked.values())
    assert sum(len(v) for v in picked.values()) <= (
        ctx_mod._MAX_SUGGESTION_TOTAL_CHARS + ctx_mod._MAX_SUGGESTION_FILE_CHARS
    )


# ── the doubled step-context marker ─────────────────────────────────────────


class _FakeSupervisor:
    def __init__(self):
        self.calls = []

    def _read_protected_files_for_suggestions(self):
        return {"a.py": "x = 1\n"}, ["a.py"]

    def _build_experience_context(self):
        return ""

    def _get_evaluation_context(self):
        return "", ""

    def _chat(self, msg, **kwargs):
        self.calls.append((msg, kwargs))

        class _V:
            raw = "No suggestions at this time."

        return _V()


def test_suggestion_prompt_has_one_step_context_header():
    from supervisor.core.llm_support.models import StepContext

    sup = _FakeSupervisor()
    step = StepContext(current_step=3, total_steps_estimate=5, phase="coding",
                       completed_phases=["planning"])
    generate_suggestions(sup, "agent output", step_context=step)
    msg, _kwargs = sup.calls[0]
    assert msg.count("--- Step Context ---") == 1
    assert "Current step: 3/5" in msg


def test_suggestion_prompt_omits_step_context_when_absent():
    sup = _FakeSupervisor()
    generate_suggestions(sup, "agent output")
    msg, _kwargs = sup.calls[0]
    assert "--- Step Context ---" not in msg
    assert "--- opencode output ---" in msg


def test_suggestion_call_does_not_replay_judge_history():
    """Advisory calls must not carry the judge's conversation (62% of payload)."""
    sup = _FakeSupervisor()
    generate_suggestions(sup, "agent output")
    _msg, kwargs = sup.calls[0]
    assert kwargs.get("omit_history") is True
    assert kwargs.get("is_intermediate") is True
