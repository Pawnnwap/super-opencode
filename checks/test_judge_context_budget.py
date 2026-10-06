"""Regression tests for the judge-context budget fixes.

Background (observed on the 2026-10-03 academic_study runs): every judge
turn estimated ~220k tokens against a 96k ceiling. Four compounding
defects, fixed together:

1. ``digest_for_prompt`` rendered the FULL workspace file tree (5,720
   files ≈ 80k tokens) into the judge system prompt — unbounded, and it
   grows as the run generates artifacts.
2. A hardcoded 128k "model limit" silently clamped the explicitly
   configured 320k budget.
3. With system > available, ``fit_request_to_budget`` floored the
   evidence budget at max_tokens//8 and amputated the agent's output
   with no judge-visible marker — the judge ruled on ~16% of the
   evidence without knowing it was partial.
4. ``supervisor_prompts.log`` grew unbounded (543 MB over 3 days).
"""

from pathlib import Path
from types import SimpleNamespace

from supervisor.analyzers.codebase_analyzer import (
    CodebaseSnapshot,
    FileSnapshot,
    _MAX_DIGEST_CHARS,
    _MAX_TREE_ENTRIES,
)
from supervisor.core.llm_support.chat import fit_request_to_budget
from supervisor.core.llm_support.history import (
    _MAX_PROMPT_LOG_BYTES,
    log_prompt,
)
from supervisor.core.llm_support.models import resolve_max_tokens


def _snap(rel_path: str, content: str = "x = 1\n") -> FileSnapshot:
    return FileSnapshot(
        rel_path=rel_path, content=content, truncated=False, sha256="0" * 64,
    )


# ── Fix 1: bounded digest ────────────────────────────────────────────────────


def test_digest_tree_is_bounded_for_many_files():
    files = [_snap(f"dir_{i // 50}/file_{i}.py") for i in range(5_720)]
    snap = CodebaseSnapshot(root=Path("ws"), files=files)
    digest = snap.digest_for_prompt(max_files=15)
    assert len(digest) <= _MAX_DIGEST_CHARS
    assert "(5720 files total)" in digest
    assert "more files not listed" in digest


def test_digest_char_budget_drops_file_sections_not_tree():
    big = "y = " + "0123456789" * 1_000 + "\n"  # ~10k chars per file
    files = [_snap(f"f{i}.py", big) for i in range(20)]
    snap = CodebaseSnapshot(root=Path("ws"), files=files)
    digest = snap.digest_for_prompt(max_files=20)
    assert len(digest) <= _MAX_DIGEST_CHARS
    # The tree section survives; the overflow is in file bodies.
    assert "### File tree" in digest
    assert "f0.py" in digest


def test_small_workspace_digest_unchanged():
    files = [_snap("a.py", "a = 1\n"), _snap("b.py", "b = 2\n")]
    snap = CodebaseSnapshot(root=Path("ws"), files=files)
    digest = snap.digest_for_prompt(max_files=15)
    assert "a = 1" in digest and "b = 2" in digest
    assert "more files not listed" not in digest


def test_tree_without_cap_still_renders_everything():
    files = [_snap(f"f{i}.py") for i in range(300)]
    snap = CodebaseSnapshot(root=Path("ws"), files=files)
    assert snap.tree().count("└─") == 300
    assert snap.tree(max_entries=_MAX_TREE_ENTRIES).count("└─") == _MAX_TREE_ENTRIES


def test_skimmed_digest_also_bounded():
    files = [_snap(f"dir_{i // 50}/file_{i}.py") for i in range(5_720)]
    snap = CodebaseSnapshot(root=Path("ws"), files=files)
    digest = snap.skimmed_digest_for_prompt(max_files=30)
    assert len(digest) <= _MAX_DIGEST_CHARS


# ── Fix 2: configured budget wins over the 128k fallback ─────────────────────


def test_resolve_max_tokens_honors_larger_config():
    assert resolve_max_tokens(320_000, 128_000) == 320_000


def test_resolve_max_tokens_keeps_smaller_config():
    assert resolve_max_tokens(20_000, 128_000) == 20_000


def test_resolve_max_tokens_falls_back_when_unset():
    assert resolve_max_tokens(0, 128_000) == 128_000


def test_resolve_max_tokens_caps_absurd_values():
    assert resolve_max_tokens(10_000_000_000, 128_000) <= 10_000_000


# ── Fix 3: truncation receipt + evidence floor ───────────────────────────────


def _fake_supervisor(max_tokens: int, system: str, history=None):
    return SimpleNamespace(
        _max_tokens=max_tokens,
        _system=system,
        _history=history or [],
    )


def test_truncated_evidence_carries_visible_receipt():
    sup = _fake_supervisor(128_000, "small system")
    huge_evidence = "evidence line\n" * 60_000  # ~900k chars ≈ 200k+ tokens
    out = fit_request_to_budget(sup, huge_evidence)
    assert "[SYSTEM NOTE — PARTIAL EVIDENCE:" in out
    assert "UNSEEN" in out
    assert len(out) < len(huge_evidence)


def test_untruncated_evidence_has_no_receipt():
    sup = _fake_supervisor(128_000, "small system")
    out = fit_request_to_budget(sup, "all good")
    assert out == "all good"


def test_huge_system_leaves_evidence_a_quarter_of_available():
    # System prompt larger than the whole available budget: the evidence
    # floor must be available//4 (~24k tokens), not max_tokens//8 (16k).
    big_system = "s" * 800_000  # ~200k tokens
    sup = _fake_supervisor(128_000, big_system)
    evidence = "e" * 400_000  # ~100k tokens
    out = fit_request_to_budget(sup, evidence)
    assert "[SYSTEM NOTE — PARTIAL EVIDENCE:" in out
    # ~24k tokens of head-preserved excerpt ≈ at least 40k chars.
    assert len(out.split("[SYSTEM NOTE")[0]) > 40_000


# ── Fix 4: prompt log rotation ───────────────────────────────────────────────


def test_prompt_log_rotates_at_cap(tmp_path):
    sup = SimpleNamespace(_workspace=tmp_path)
    log_file = tmp_path / ".opencode" / "supervisor_prompts.log"

    big_msg = [{"role": "system", "content": "z" * 200}]
    # Grow past the cap with small writes would be slow; fake it by writing
    # a large first entry then one more.
    log_prompt(sup, "Supervisor Chat", [{"role": "system", "content": "z" * (_MAX_PROMPT_LOG_BYTES + 1)}])
    assert log_file.exists()
    assert log_file.stat().st_size > _MAX_PROMPT_LOG_BYTES

    log_prompt(sup, "Supervisor Chat", big_msg)
    backup = tmp_path / ".opencode" / "supervisor_prompts.log.1"
    assert backup.exists(), "oversized log must rotate to .1"
    assert backup.stat().st_size > _MAX_PROMPT_LOG_BYTES
    assert log_file.stat().st_size <= _MAX_PROMPT_LOG_BYTES + 5_000
