"""UI translation table: switchable, complete, and blind to English-only data.

The console's language switch is a lookup table keyed on the English source
string, so three things must never drift: the table must actually be used at
the call sites, it must not machine-translate tokens that only exist in
English (log lines, paths, job ids, state names), and switching must be
reversible without leaving process state behind.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from services.webui import i18n

_WEBUI = Path(__file__).resolve().parents[1] / "services" / "webui"


@pytest.fixture(autouse=True)
def restore_language():
    """Every test starts and ends in English (the process-wide default)."""
    i18n.set_lang("en")
    yield
    i18n.set_lang("en")


# ── Switching ────────────────────────────────────────────────────────────────


def test_chinese_translates_a_known_string_and_english_passes_it_through():
    assert i18n.t("Workspace") == "Workspace"
    i18n.set_lang("zh")
    assert i18n.t("Workspace") == "工作区"
    i18n.set_lang("en")
    assert i18n.t("Workspace") == "Workspace"


def test_unknown_strings_are_returned_verbatim():
    """Log lines, paths and ids pass through untouched — they are English-only
    data, not UI copy."""
    for raw in (
        "Traceback (most recent call last): File x.py",
        r"D:\runs\study_0001",
        "run_01234567",
        "PENDING",
    ):
        i18n.set_lang("zh")
        assert i18n.t(raw) == raw


def test_tr_fills_placeholders_after_translation():
    i18n.set_lang("zh")
    assert i18n.tr("History ({n})", n=7) == "历史（7）"


def test_tr_leaves_a_malformed_template_intact():
    assert i18n.tr("{missing_placeholder}", other=1) == "{missing_placeholder}"


def test_ts_translates_the_prefix_and_keeps_the_detail_verbatim():
    i18n.set_lang("zh")
    text = "Access & Protection: policy drill failed — cwd readable: denied"
    out = i18n.ts(text)
    assert out.startswith("访问与保护：策略演练失败 — ")
    assert out.endswith("cwd readable: denied")


def test_only_supported_codes_are_accepted():
    assert i18n.set_lang("klingon") == "en"
    assert i18n.current() == "en"
    assert i18n.set_lang("zh") == "zh"
    assert i18n.current() == "zh"


def test_html_lang_follows_the_active_language():
    assert i18n.html_lang() == "en"
    i18n.set_lang("zh")
    assert i18n.html_lang() == "zh-CN"


# ── Table integrity ──────────────────────────────────────────────────────────


def _table() -> dict[str, str]:
    source = (_WEBUI / "i18n.py").read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if isinstance(node, ast.AnnAssign) and getattr(
            node.target, "id", "",
        ) == "_ZH":
            return {
                key.value: value.value
                for key, value in zip(node.value.keys, node.value.values)
            }
    raise AssertionError("_ZH table not found in i18n.py")


def test_no_duplicate_source_keys():
    source = (_WEBUI / "i18n.py").read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if isinstance(node, ast.AnnAssign) and getattr(
            node.target, "id", "",
        ) == "_ZH":
            keys = [key.value for key in node.value.keys]
            assert len(keys) == len(set(keys)), "duplicate key in _ZH"
            return
    raise AssertionError("_ZH table not found")


def test_every_call_site_literal_is_in_the_table():
    """A literal passed to i18n.t/tr/ts that is missing from the table would
    silently render in English — the exact bug this table exists to catch."""
    table = _table()
    missing: list[str] = []
    for path in _WEBUI.rglob("*.py"):
        if path.name == "i18n.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "i18n"
                and node.func.attr in {"t", "tr", "ts"}
            ):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    literal = arg.value
                elif isinstance(arg, ast.JoinedStr):
                    literal = "".join(
                        part.value for part in arg.values
                        if isinstance(part, ast.Constant)
                    )
                else:
                    continue
                if literal and literal not in table:
                    missing.append(f"{path.name}: {literal[:60]}")
    assert not missing, "untranslated call-site literals:\n" + "\n".join(missing)


def test_no_translation_is_empty_or_english_only_noise():
    # Keys whose "translation" is deliberately the English token: product and
    # file names are not prose. Their presence is only to make lookup explicit.
    identity_ok = {".opencodeignore", "{current} / {maximum} tokens"}
    for key, value in _table().items():
        assert value.strip(), f"empty translation for {key!r}"
        if key in identity_ok:
            continue
        # Templates made only of placeholders, separators and emoji carry no
        # prose of their own; every other line must contain Chinese text.
        prose = key
        for placeholder in ("{mark}", "{name}", "{message}", "{n}", "{id}",
                            "{state}", "{phase}", "{error}", "{detail}",
                            "{workspace}", "{path}", "{time}", "{elapsed}",
                            "{round}", "{total}", "{issues}", "{check}",
                            "{drift}", "{artifacts}", "{gate}", "{folder}",
                            "{running}", "{queued}", "{engine}", "{model}",
                            "{mode}", "{current}", "{maximum}", "{age}",
                            "{run}", "{shown}", "{selected}", "{ref}", "{level}",
                            "{chars}", "{ts}", "{err}", "{warn}", "{tool}",
                            "{done}", "{left}", "{kind}", "{label}", "{mark}"):
            prose = prose.replace(placeholder, "")
        if any(ch.isalpha() for ch in prose):
            assert any("\u4e00" <= ch <= "\u9fff" for ch in value), key


# ── Tokens that must stay English ────────────────────────────────────────────


def test_job_state_vocabulary_is_never_translated():
    """The board, pills and filters key on these exact words."""
    from services.jobs.job_states import STATE_CONTRACT

    table = _table()
    i18n.set_lang("zh")
    for state in STATE_CONTRACT:
        assert state not in table
        assert i18n.t(state) == state


def test_engine_names_and_file_names_stay_english():
    table = _table()
    for token in ("opencode", "codex", "pi", "protocol.md"):
        assert token not in table
