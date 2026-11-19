"""Real page renders in both languages (NiceGUI's own simulation harness).

The unit tests pin the table; this one pins what an operator actually sees:
the default is English, Chinese renders after the switch, and English-only
tokens (state pills, ids, paths) survive into the Chinese page unchanged.

Deliberately NOT the running console on :8501 — a job may be live there, and
this simulation spins its own in-process app with the real page functions.
"""

from __future__ import annotations

import asyncio

import pytest
from nicegui.testing.user_simulation import user_simulation

from services.webui import i18n, layout
from services.webui.jobs import get_job_manager
from services.webui.pages import run


@pytest.fixture(autouse=True)
def english_by_default(monkeypatch, tmp_path):
    """Isolated settings file and a clean language on both ends of a test."""
    from services.config import settings

    monkeypatch.setattr(settings, "_SETTINGS_FILE", tmp_path / "settings.json")
    i18n.set_lang("en")
    yield
    i18n.set_lang("en")


def _page() -> None:
    layout.apply_theme()
    layout.render_shell("run", get_job_manager())
    run.render()


def test_english_is_the_default_page():
    async def scenario() -> None:
        async with user_simulation(_page) as user:
            await user.open("/")
            await user.should_see("Live Run")
            await user.should_see("Workspace")
            await user.should_not_see("实时运行")

    asyncio.run(scenario())


def test_chinese_renders_after_the_language_switch():
    i18n.set_lang("zh")

    async def scenario() -> None:
        async with user_simulation(_page) as user:
            await user.open("/")
            await user.should_see("实时运行")
            await user.should_see("工作区")
            await user.should_see("任务板")

    asyncio.run(scenario())


def test_filter_option_labels_follow_the_language():
    """Select labels are translated while their VALUES stay the English
    sentinels the filter logic compares against."""
    i18n.set_lang("zh")

    async def scenario() -> None:
        async with user_simulation(_page) as user:
            await user.open("/")
            await user.should_see("全部状态")

    asyncio.run(scenario())


def test_unknown_text_and_ids_are_not_translated():
    """English-only data passes through the Chinese table untouched."""
    i18n.set_lang("zh")
    assert i18n.t("run_01234567") == "run_01234567"
    assert i18n.t("D:\\runs\\study_0001") == "D:\\runs\\study_0001"
    assert i18n.t("SUCCESS") == "SUCCESS"
