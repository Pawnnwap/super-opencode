"""Tests for the supervisor skill bank: discovery, bounded loading, and the
LOAD_SKILL verdict marker (catalog-only auto rendering, chain-free loads)."""

from __future__ import annotations

from types import SimpleNamespace

from supervisor.core.llm_support.models import (
    parse_skill_requests,
    parse_verdict_structure,
)
from supervisor.core.skill_bank import (
    SkillBank,
    builtin_bank_dir,
    strip_skill_markers,
)


def _write_skill(
    directory,
    name: str,
    description: str = "",
    body: str = "guidance body\n",
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    text = ""
    if description:
        text += f"---\ndescription: {description}\n---\n"
    (directory / f"{name}.md").write_text(text + body, encoding="utf-8")


# ---------------------------------------------------------------------------- #
# Discovery + parsing                                                          #
# ---------------------------------------------------------------------------- #


def test_front_matter_parse_name_and_description(tmp_path):
    _write_skill(
        tmp_path, "my-skill", description="Does a thing", body="Body text.\n",
    )

    bank = SkillBank(workspace_dir=tmp_path)

    skill = bank.skills()[0]
    assert skill.name == "my-skill"
    assert skill.description == "Does a thing"
    assert skill.body.startswith("Body text.")


def test_description_falls_back_to_first_paragraph(tmp_path):
    _write_skill(tmp_path, "fallback", body="First paragraph here.\n\nSecond.\n")

    bank = SkillBank(workspace_dir=tmp_path)

    assert bank.skills()[0].description == "First paragraph here."


def test_invalid_names_are_ignored(tmp_path):
    _write_skill(tmp_path, "Bad Name!")
    _write_skill(tmp_path, "valid-name")

    bank = SkillBank(workspace_dir=tmp_path)

    assert bank.names() == ["valid-name"]


def test_workspace_shadows_builtin_on_clash(tmp_path):
    _write_skill(tmp_path, "shared", description="workspace wins")
    builtin = tmp_path / "builtin"
    _write_skill(builtin, "shared", description="builtin loses")
    _write_skill(builtin, "only-builtin")

    bank = SkillBank(builtin_dir=builtin, workspace_dir=tmp_path)

    by_name = {skill.name: skill for skill in bank.skills()}
    assert by_name["shared"].description == "workspace wins"
    assert by_name["shared"].source == "workspace"
    assert "only-builtin" in by_name


def test_body_is_truncated_to_budget(tmp_path):
    _write_skill(tmp_path, "long", body="x" * 3000 + "\n")

    bank = SkillBank(workspace_dir=tmp_path, body_budget=1000)

    assert len(bank.skills()[0].body) == 1000 + len("\n…(skill body truncated)")


def test_builtin_bank_ships_seed_skills():
    bank = SkillBank(builtin_dir=builtin_bank_dir())

    assert bank.has_skills()
    assert "test-gated-done" in bank.names()


# ---------------------------------------------------------------------------- #
# Catalog rendering                                                            #
# ---------------------------------------------------------------------------- #


def test_empty_bank_renders_no_catalog(tmp_path):
    bank = SkillBank(workspace_dir=tmp_path / "missing")

    assert bank.catalog_block() == ""
    assert bank.context_block() == ""


def test_catalog_lists_names_and_load_instruction(tmp_path):
    _write_skill(tmp_path, "alpha", description="First skill")
    _write_skill(tmp_path, "beta", description="Second skill")

    catalog = SkillBank(workspace_dir=tmp_path).catalog_block()

    assert "- alpha: First skill" in catalog
    assert "- beta: Second skill" in catalog
    assert "LOAD_SKILL: <name>" in catalog
    # Selectivity is explicit: the judge is told not to load everything.
    assert "do not" in catalog


def test_catalog_is_bounded_by_entry_cap(tmp_path):
    for i in range(20):
        _write_skill(tmp_path, f"skill-{i:02d}", description="d")

    catalog = SkillBank(workspace_dir=tmp_path).catalog_block()

    assert "(+8 more not listed)" in catalog


# ---------------------------------------------------------------------------- #
# Loading: caps, dedup, LRU eviction, pending queue                            #
# ---------------------------------------------------------------------------- #


def test_load_known_unknown_and_duplicate(tmp_path):
    _write_skill(tmp_path, "known", description="d")
    bank = SkillBank(workspace_dir=tmp_path)

    first = bank.load(["known", "nope", "known"])
    second = bank.load(["known"])

    assert [skill.name for skill in first.loaded] == ["known"]
    assert any("nope" in note for note in first.notes)
    assert not second.loaded
    assert any("already loaded" in note for note in second.notes)


def test_load_is_capped_per_turn_and_rest_is_deferred(tmp_path):
    for name in ("a", "b", "c"):
        _write_skill(tmp_path, name, description="d")
    bank = SkillBank(workspace_dir=tmp_path, max_loads_per_turn=2)

    result = bank.load(["a", "b", "c"])

    assert sorted(skill.name for skill in result.loaded) == ["a", "b"]
    assert any("deferred" in note for note in result.notes)
    # Deferred requests are fulfilled on the next turn, not lost.
    bank.load(bank.drain_pending())
    assert bank.loaded_names() == ["a", "b", "c"]


def test_resident_set_evicts_lru_over_count_cap(tmp_path):
    for name in ("a", "b", "c"):
        _write_skill(tmp_path, name, description="d", body="x" * 50 + "\n")
    bank = SkillBank(workspace_dir=tmp_path, max_resident=2, max_loads_per_turn=3)

    bank.load(["a", "b"])
    bank.load(["a"])  # touch a -> b becomes LRU
    bank.load(["c"])

    assert sorted(bank.loaded_names()) == ["a", "c"]


def test_resident_set_evicts_lru_over_char_budget(tmp_path):
    for name in ("big-a", "big-b", "big-c"):
        _write_skill(tmp_path, name, description="d", body="x" * 450 + "\n")
    bank = SkillBank(
        workspace_dir=tmp_path,
        body_budget=600,
        max_total_chars=1000,
        max_loads_per_turn=3,
    )

    bank.load(["big-a", "big-b", "big-c"])

    assert sorted(bank.loaded_names()) == ["big-b", "big-c"]


def test_resident_block_contains_loaded_bodies(tmp_path):
    _write_skill(tmp_path, "alpha", description="First", body="Alpha body.\n")
    bank = SkillBank(workspace_dir=tmp_path)

    bank.load(["alpha"])
    block = bank.context_block()

    assert "[skill: alpha] First" in block
    assert "Alpha body." in block


def test_pending_queue_dedups_and_skips_resident(tmp_path):
    _write_skill(tmp_path, "alpha", description="d")
    bank = SkillBank(workspace_dir=tmp_path)

    bank.load(["alpha"])
    bank.queue_pending(["alpha", "alpha", "beta"])
    bank.drain_pending()
    bank.queue_pending(["beta"])

    assert bank.drain_pending() == ["beta"]


# ---------------------------------------------------------------------------- #
# LOAD_SKILL marker parsing + feedback stripping                               #
# ---------------------------------------------------------------------------- #


def test_parse_skill_requests_extracts_lowercased_unique_names():
    reply = (
        "Some prose.\n"
        "LOAD_SKILL: Test-Gated-Done\n"
        "load_skill: restart-hygiene\n"
        "LOAD_SKILL: test-gated-done extra-token\n"
        "LOAD_SKILL:\n"
    )

    assert parse_skill_requests(reply) == ["test-gated-done", "restart-hygiene"]


def test_skill_requests_do_not_interfere_with_verdict_parsing():
    reply = (
        "CRITERIA:\n"
        "- [MET] target one — file exists\n"
        "- [UNMET] target two — not done\n"
        "NEXT_ACTION: keep going\n"
        "DONE: no\n"
        "NEED_EVIDENCE: test_report\n"
        "LOAD_SKILL: scope-creep-guard\n"
    )

    done, criteria, next_action, evidence = parse_verdict_structure(reply)

    assert done is False
    assert len(criteria) == 2
    assert next_action == "keep going"
    assert evidence == ["test_report"]
    assert parse_skill_requests(reply) == ["scope-creep-guard"]


def test_strip_skill_markers_removes_only_load_skill_lines():
    text = "Feedback line 1.\nLOAD_SKILL: scope-creep-guard\nNEED_EVIDENCE: read_file\nFeedback line 2.\n"

    stripped = strip_skill_markers(text)

    assert "LOAD_SKILL" not in stripped
    assert "NEED_EVIDENCE: read_file" in stripped
    assert "Feedback line 1." in stripped and "Feedback line 2." in stripped


# ---------------------------------------------------------------------------- #
# Loop wiring (BaseLoop helpers, no LLM, no runner)                            #
# ---------------------------------------------------------------------------- #


class _FakeSupervisor:
    def __init__(self):
        self.providers: list[tuple[str, object]] = []

    def register_context_provider(self, name, provider):
        self.providers.append((name, provider))


class _FakeGuard:
    def sanitize_message(self, text):
        return text, []


def _loop_with_bank(tmp_path, *, enable: bool = True):
    from supervisor.core.loop_base import BaseLoop

    loop = BaseLoop(
        SimpleNamespace(
            workspace=tmp_path,
            enable_supervisor_skills=enable,
            supervisor_skills_dir="",
        ),
    )
    loop.supervisor = _FakeSupervisor()
    loop.guard = _FakeGuard()
    loop._setup_skill_bank()
    return loop


def test_setup_registers_catalog_provider(tmp_path):
    loop = _loop_with_bank(tmp_path)

    names = [name for name, _ in loop.supervisor.providers]
    assert "skills" in names
    block = loop._skill_bank.context_block()
    assert "Supervisor Skills (bank)" in block


def test_setup_without_skills_or_disabled_is_inert(tmp_path, monkeypatch):
    # Point the built-in bank at a missing dir so discovery finds nothing.
    monkeypatch.setattr(
        "supervisor.core.skill_bank.builtin_bank_dir",
        lambda: tmp_path / "no-builtin",
    )
    loop = _loop_with_bank(tmp_path / "empty", enable=True)
    disabled = _loop_with_bank(tmp_path, enable=False)

    assert loop._skill_bank is None
    assert disabled._skill_bank is None


def test_resolve_skill_requests_loads_and_renders(tmp_path):
    bank_dir = tmp_path / ".opencode" / "supervisor_skills"
    _write_skill(bank_dir, "alpha", description="First", body="Alpha body.\n")
    loop = _loop_with_bank(tmp_path)
    verdict = SimpleNamespace(skill_requests=["alpha"])

    loaded = loop._resolve_skill_requests(verdict)

    assert loaded == ["alpha"]
    assert "Alpha body." in loop._skill_bank.resident_block()
    events = list(loop._drain_skill_events())
    assert events == [{"level": "info", "msg": "Supervisor loaded skill(s): alpha"}]
    assert list(loop._drain_skill_events()) == []  # drained


def test_rejected_and_deferred_requests_are_recorded(tmp_path):
    loop = _loop_with_bank(tmp_path)
    verdict = SimpleNamespace(skill_requests=["nope", "a", "b", "c"])

    loaded = loop._resolve_skill_requests(verdict)

    assert loaded == []  # bank is empty: everything rejected
    events = list(loop._drain_skill_events())
    assert any(
        event["level"] == "warn" and "nope" in event["msg"] for event in events
    )


def test_fulfilled_pending_skills_are_recorded(tmp_path):
    bank_dir = tmp_path / ".opencode" / "supervisor_skills"
    _write_skill(bank_dir, "alpha", description="First")
    loop = _loop_with_bank(tmp_path)
    loop._queue_deferred_skill_requests(SimpleNamespace(skill_requests=["alpha"]))

    loop._fulfill_pending_skills()
    events = list(loop._drain_skill_events())

    assert loop._skill_bank.loaded_names() == ["alpha"]
    assert any("alpha" in event["msg"] for event in events)


def _generator_return(generator):
    """Drain a generator and return its StopIteration value (the return)."""
    try:
        while True:
            next(generator)
    except StopIteration as stop:
        return stop.value


def test_sanitize_feedback_strips_skill_markers(tmp_path):
    loop = _loop_with_bank(tmp_path)

    safe_msg = _generator_return(
        loop._sanitize_feedback("Do X next.\nLOAD_SKILL: alpha\nDONE: no"),
    )

    assert "LOAD_SKILL" not in safe_msg
    assert "Do X next." in safe_msg


def test_deferred_requests_wait_for_next_turn(tmp_path):
    bank_dir = tmp_path / ".opencode" / "supervisor_skills"
    _write_skill(bank_dir, "alpha", description="d")
    loop = _loop_with_bank(tmp_path)
    rejudge_verdict = SimpleNamespace(skill_requests=["alpha"])

    loop._queue_deferred_skill_requests(rejudge_verdict)
    assert loop._skill_bank.loaded_names() == []

    loop._fulfill_pending_skills()
    assert loop._skill_bank.loaded_names() == ["alpha"]


# ---------------------------------------------------------------------------- #
# Real loop _get_verdict flow (init bypassed; no LLM, no runner)               #
# ---------------------------------------------------------------------------- #


_STEP_PROGRESS = SimpleNamespace(
    current_step=1,
    total_steps_estimate=5,
    phase=SimpleNamespace(name="unknown"),  # enum-like: .name is read
    completed_phases=[],
)


class _ScriptedSupervisor:
    """Returns scripted verdicts; records the prompts it was given."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def judge_with_step_context(self, output, step_context):
        self.prompts.append(output)
        return self.replies.pop(0)

    def judge(self, output):
        self.prompts.append(output)
        return self.replies.pop(0)


def _verdict(skills=(), evidence=()):
    return SimpleNamespace(
        skill_requests=list(skills),
        evidence_requests=list(evidence),
    )


def test_supervisor_loop_get_verdict_loads_once_and_queues_chains(tmp_path):
    from supervisor.core.loop import SupervisorLoop

    bank_dir = tmp_path / ".opencode" / "supervisor_skills"
    _write_skill(bank_dir, "alpha", description="First", body="Alpha body.\n")
    bank = SkillBank(workspace_dir=bank_dir)
    supervisor = _ScriptedSupervisor(
        [_verdict(skills=["alpha"]), _verdict(skills=["beta"])],
    )
    loop = object.__new__(SupervisorLoop)
    loop._judge_harness = None
    loop._skill_bank = bank
    loop._skill_events = []
    loop.supervisor = supervisor

    verdict = loop._get_verdict("agent output", _STEP_PROGRESS)

    # Exactly one re-judge; the chained "beta" request waits for next turn.
    assert len(supervisor.prompts) == 2
    assert "SUPERVISOR SKILLS LOADED" in supervisor.prompts[1]
    assert "agent output" in supervisor.prompts[1]
    assert bank.loaded_names() == ["alpha"]
    assert bank.drain_pending() == ["beta"]
    assert verdict.skill_requests == ["beta"]
    # The load action is recorded as an event for the stream, like other
    # supervisor actions.
    assert loop._skill_events == [
        ("info", "Supervisor loaded skill(s): alpha"),
    ]


def test_supervisor_loop_get_verdict_combines_skills_and_evidence(tmp_path):
    from supervisor.core.loop import SupervisorLoop

    bank_dir = tmp_path / ".opencode" / "supervisor_skills"
    _write_skill(bank_dir, "alpha", description="First", body="Alpha body.\n")
    bank = SkillBank(workspace_dir=bank_dir)
    harness_calls = []

    class _Harness:
        def fulfill(self, requests):
            harness_calls.append(list(requests))
            return "test report body"

    supervisor = _ScriptedSupervisor(
        [_verdict(skills=["alpha"], evidence=["test_report"]), _verdict()],
    )
    loop = object.__new__(SupervisorLoop)
    loop._judge_harness = _Harness()
    loop._skill_bank = bank
    loop._skill_events = []
    loop.supervisor = supervisor

    loop._get_verdict("agent output", _STEP_PROGRESS)

    assert harness_calls == [["test_report"]]
    assert len(supervisor.prompts) == 2  # single combined re-judge
    assert "HARNESS EVIDENCE" in supervisor.prompts[1]
    assert "test report body" in supervisor.prompts[1]
    assert "SUPERVISOR SKILLS LOADED" in supervisor.prompts[1]
    assert bank.loaded_names() == ["alpha"]


def test_evolution_loop_get_verdict_loads_and_queues_chains(tmp_path):
    from supervisor.core.self_evolution_loop import SelfEvolutionLoop

    bank_dir = tmp_path / ".opencode" / "supervisor_skills"
    _write_skill(bank_dir, "alpha", description="First", body="Alpha body.\n")
    bank = SkillBank(workspace_dir=bank_dir)
    supervisor = _ScriptedSupervisor(
        [_verdict(skills=["alpha"]), _verdict()],
    )
    loop = object.__new__(SelfEvolutionLoop)
    loop._skill_bank = bank
    loop._skill_events = []
    loop.supervisor = supervisor

    loop._get_verdict("agent output", _STEP_PROGRESS)

    assert len(supervisor.prompts) == 2
    assert "SUPERVISOR SKILLS LOADED" in supervisor.prompts[1]
    assert bank.loaded_names() == ["alpha"]
