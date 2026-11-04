"""The judge's file picker must see what moved, not just what exists.

Before this, the candidate listing was `<path> (<size> bytes)` in alphabetical
order — no timestamp, no delta — so a deliverable rewritten on the judged turn
was indistinguishable from a stale artifact, and verification could only be
aimed blindly. The listing now leads with changed files and carries
`<delta>` and `<age>` per row.
"""
import os
import time

from supervisor.analyzers.codebase_analyzer import snapshot_codebase
from supervisor.core.llm_support.context import SupervisorContextManager, _age_label
from supervisor.workspace.ignore_patterns import IgnoreMatcher


def _manager(tmp_path):
    return SupervisorContextManager(
        workspace=tmp_path,
        max_tokens=320_000,
        max_protected_files_for_suggestions=5,
        protocol_target="target",
        model="m",
        client=None,
        ignore_matcher=IgnoreMatcher(tmp_path),
        log_prompt=lambda *a, **k: None,
        skip_dirs=set(),
        skip_dir_prefixes=(),
        generated_markdown_files=set(),
    )


class _CapturingClient:
    """Records the selection prompt; answers with the first listed path."""

    def __init__(self):
        self.prompt = ""
        outer = self

        class _Completions:
            @staticmethod
            def create(**kwargs):
                outer.prompt = kwargs["messages"][0]["content"]

                class _Msg:
                    content = '["a.py"]'

                class _Choice:
                    message = _Msg()

                class _Resp:
                    choices = [_Choice()]

                return _Resp()

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


# ── snapshot capability ─────────────────────────────────────────────────────


def test_snapshot_records_mtime(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    snap = snapshot_codebase(tmp_path)
    assert snap.files[0].mtime > 0


def test_recency_order_is_newest_first(tmp_path):
    old = tmp_path / "old.py"
    new = tmp_path / "new.py"
    old.write_text("a\n", encoding="utf-8")
    new.write_text("b\n", encoding="utf-8")
    past = time.time() - 7200
    os.utime(old, (past, past))
    snap = snapshot_codebase(tmp_path)
    assert snap.recency_order()[0].rel_path == "new.py"


def test_deltas_report_added_modified_removed(tmp_path):
    (tmp_path / "mod.py").write_text("a\nb\nc\n", encoding="utf-8")
    (tmp_path / "gone.py").write_text("bye\n", encoding="utf-8")
    before = snapshot_codebase(tmp_path)

    (tmp_path / "mod.py").write_text("a\nB\nc\nd\n", encoding="utf-8")
    (tmp_path / "fresh.py").write_text("x\ny\n", encoding="utf-8")
    (tmp_path / "gone.py").unlink()
    after = snapshot_codebase(tmp_path)

    deltas = before.deltas(after)
    assert deltas["fresh.py"].status == "added"
    assert deltas["gone.py"].status == "removed"
    modified = deltas["mod.py"]
    assert modified.status == "modified"
    assert modified.added >= 1 and modified.removed >= 1


def test_unchanged_files_have_no_delta(tmp_path):
    (tmp_path / "same.py").write_text("a\n", encoding="utf-8")
    before = snapshot_codebase(tmp_path)
    assert before.deltas(snapshot_codebase(tmp_path)) == {}


def test_delta_rendering_is_compact():
    from supervisor.analyzers.codebase_analyzer import FileDelta

    assert FileDelta("a", "added", added=3).render() == "+new"
    assert FileDelta("a", "removed", removed=4).render() == "-deleted"
    assert FileDelta("a", "modified", added=12, removed=3).render() == "~+12/-3"
    assert FileDelta("a", "modified", 1, 1, approx=True).render() == "~+1/-1~"


# ── the selection listing ──────────────────────────────────────────────────


def test_listing_marks_changes_and_puts_them_first(tmp_path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    (tmp_path / "z_stale.py").write_text("y\n", encoding="utf-8")
    past = time.time() - 7200
    os.utime(tmp_path / "z_stale.py", (past, past))

    mgr = _manager(tmp_path)
    client = _CapturingClient()
    mgr._client = client
    mgr.set_change_providers(
        deltas=lambda: {"a.py": "~+4/-1"},
        mtimes=lambda: {"a.py": time.time(), "z_stale.py": past},
    )
    mgr.llm_select_files({"a.py": 2, "z_stale.py": 2}, 2)

    listing = client.prompt[client.prompt.find("## Candidate"):]
    lines = [ln for ln in listing.splitlines() if ".py (" in ln]
    assert lines[0].startswith("a.py"), lines
    assert "~+4/-1" in lines[0]
    assert "just edited" in lines[0]
    # The stale file stays visible but carries no delta and an age.
    assert "z_stale.py" in lines[1]
    assert "+4" not in lines[1]
    assert "2h ago" in lines[1]


def test_listing_bounds_the_delta_vocabulary(tmp_path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    mgr = _manager(tmp_path)
    client = _CapturingClient()
    mgr._client = client
    mgr.set_change_providers(deltas=lambda: {}, mtimes=lambda: {"a.py": time.time()})
    mgr.llm_select_files({"a.py": 2}, 1)
    prompt = client.prompt
    for token in ("+new", "~+12/-3", "-deleted"):
        assert token in prompt


def test_fallback_picks_changed_then_newest(tmp_path):
    """When selection fails, the fallback must not fall back to alphabetical."""
    for name in ("aaa.py", "bbb.py", "zzz.py"):
        (tmp_path / name).write_text("x\n", encoding="utf-8")
    now = time.time()
    mgr = _manager(tmp_path)  # client=None -> selection raises -> fallback
    mgr.set_change_providers(
        deltas=lambda: {"zzz.py": "~+9/-0"},
        mtimes=lambda: {"aaa.py": now, "bbb.py": now - 5000, "zzz.py": now - 9999},
    )
    chosen = mgr.llm_select_files({"aaa.py": 2, "bbb.py": 2, "zzz.py": 2}, 2)
    assert chosen[0] == "zzz.py", "the changed file must lead the fallback"


def test_listing_degrades_without_providers(tmp_path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    mgr = _manager(tmp_path)
    client = _CapturingClient()
    mgr._client = client
    mgr.llm_select_files({"a.py": 2}, 1)
    # Recency still works from a direct stat; no delta is fabricated.
    assert "just edited" in client.prompt


def test_age_labels_are_human_scaled():
    now = 1_000_000.0
    assert _age_label(now - 10, now) == "just edited"
    assert _age_label(now - 300, now) == "5m ago"
    assert _age_label(now - 10_800, now) == "3h ago"
    assert _age_label(now - 172_800, now) == "2d ago"
    assert _age_label(0.0, now) == ""
