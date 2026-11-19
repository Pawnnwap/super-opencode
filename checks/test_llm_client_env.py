"""The streamed client must construct on this machine's environment.

openai 3.x builds its client on httpx2. Versions below 2.16 parse the
bracketed IPv6 entry in this machine's ``NO_PROXY`` (``[::1]``) as a port
``:1]`` and raise ``InvalidURL`` from the constructor, before any request is
sent. That broke every protocol refine while looking like a network problem.

These checks keep the floor honest without needing the network.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest


def test_httpx2_floor_is_declared():
    root = Path(__file__).resolve().parents[1]
    manifest = (root / "requirements.txt").read_text(encoding="utf-8")
    project = (root / "pyproject.toml").read_text(encoding="utf-8")

    assert re.search(r"^httpx2>=2\.16", manifest, re.MULTILINE), manifest
    assert "httpx2>=2.16" in project, project


def test_client_builds_with_bracketed_ipv6_no_proxy(monkeypatch):
    # Reproduce the environment that used to crash the constructor.
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,[::1],::1")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,[::1],::1")

    from supervisor.utils.llm_stream import make_stream_client

    client = make_stream_client(api_key="x", base_url="https://example.com/v2")

    assert str(client.base_url).startswith("https://example.com/v2")


def test_client_builds_with_a_timeout_override(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,[::1]")

    import httpx

    from supervisor.utils.llm_stream import make_stream_client

    client = make_stream_client(
        api_key="x", base_url="https://example.com/v2", timeout=httpx.Timeout(5.0),
    )

    assert client is not None


@pytest.mark.skipif(
    not os.environ.get("SOP_LIVE_LLM"),
    reason="live endpoint check; set SOP_LIVE_LLM=1 to run",
)
def test_live_endpoint_answers(monkeypatch):
    """Real round trip, opt-in only: CI stays offline."""
    import json

    from supervisor.utils.llm_stream import StreamedLLMHelper, stream_chat_text

    settings = json.loads(
        (Path.home() / ".opencode_supervisor_settings.json").read_text(encoding="utf-8-sig"),
    )

    class Probe(StreamedLLMHelper):
        pass

    llm = Probe(
        model=settings["supervisor_model"],
        api_key=settings.get("openai_key"),
        base_url=settings.get("base_url"),
    )
    out = stream_chat_text(
        llm._client,
        llm._model,
        [{"role": "user", "content": "Reply with exactly: PONG"}],
        temperature=0.0,
        field_name="live check",
    )

    assert "PONG" in out
