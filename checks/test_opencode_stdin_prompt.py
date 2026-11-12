"""Regression checks: the opencode prompt travels over stdin, never argv.

Windows caps command lines at 8191 chars when opencode runs through its npm
``.cmd`` shim (cmd.exe /c) and at 32767 via CreateProcess even without a
shell. A prompt past the limit fails before opencode even starts ("The
command line is too long", exit 1) — which the supervisor's judge reads as
"agent produced only a shell failure, no work product", turn after turn.

``opencode run`` reads the message from stdin when no message argument is
given (verified against opencode 1.18: empty stdin errors with "You must
provide a message or a command").
"""

import io
import subprocess
import time
from types import SimpleNamespace

import pytest

from supervisor.runners.opencode_support import command_builder as oc_command_builder
from supervisor.runners.opencode_support import process as opencode_process
from supervisor.runners.opencode_support.command_builder import (
    _opencode_major as _opencode_major_impl,
)
from supervisor.runners.opencode_support.command_builder import build_cmd
from supervisor.runners import process_common

# ~260k chars: far past both Windows limits, routine size for a turn prompt
# that carries roll-up / summary content.
LONG_PROMPT = "Write DESIGN.md.\n" + "detail line with some substance\n" * 20000


@pytest.fixture(autouse=True)
def _pin_opencode_major_legacy(monkeypatch):
    """Default every test here to the legacy (1.x) command shape.

    build_cmd probes the installed CLI's major version (a real subprocess);
    pinning keeps these unit tests hermetic. Version-specific tests pin 2 or
    None themselves.
    """
    monkeypatch.setattr(oc_command_builder, "_opencode_major", lambda exe: 1)


def test_build_cmd_never_puts_prompt_on_command_line():
    cmd = build_cmd(
        exe="opencode.cmd",
        prompt=LONG_PROMPT,
        agent="build",
        opencode_model=None,
        use_continue=False,
        session_id=None,
        use_shell=True,
        dir_=".",
    )

    assert "opencode" in cmd[0] and cmd[1] == "run"
    assert LONG_PROMPT.strip() not in cmd
    assert "--" not in cmd  # no trailing prompt marker, no message argument
    assert sum(len(part) for part in cmd) < 8191, (
        "command line must stay under the cmd.exe limit regardless of prompt size"
    )


def test_build_cmd_resume_keeps_session_flags_without_message():
    cmd = build_cmd(
        exe="opencode",
        prompt=LONG_PROMPT,
        agent="",
        opencode_model=None,
        use_continue=True,
        session_id="ses_abc123",
        use_shell=False,
        dir_=".",
    )

    assert cmd[1] == "run"
    assert "--session" in cmd
    assert "ses_abc123" in cmd
    assert cmd[-2:] != ["--", LONG_PROMPT]


def _build_kwargs(**overrides):
    """Keyword bundle for build_cmd calls; overrides win."""
    kwargs = dict(
        exe="opencode",
        prompt=LONG_PROMPT,
        agent="build",
        opencode_model="local-ds/swift-1.5",
        use_continue=False,
        session_id=None,
    )
    kwargs.update(overrides)
    return kwargs


def test_build_cmd_v2_drops_removed_flags_and_folds_variant(monkeypatch):
    # Verified live on opencode v2.2.7: --dir and --pure hard-error with a
    # help dump (exit 1) and --variant no longer exists - the model string
    # carries the variant instead (`--model provider/model#variant` parses).
    monkeypatch.setattr(oc_command_builder, "_opencode_major", lambda exe: 2)
    cmd = build_cmd(
        **_build_kwargs(use_pure=True, dir_="C:/tmp/ws", variant="none"),
    )

    joined = " ".join(cmd)
    assert "--dir" not in joined
    assert "--pure" not in joined
    assert "--variant" not in joined
    assert cmd[cmd.index("--model") + 1] == "local-ds/swift-1.5#none"
    assert "--format" in cmd and cmd[cmd.index("--format") + 1] == "json"


def test_build_cmd_v2_keeps_already_varianted_model(monkeypatch):
    # A model ref that already carries `#variant` must not get a second one.
    monkeypatch.setattr(oc_command_builder, "_opencode_major", lambda exe: 2)
    cmd = build_cmd(
        **_build_kwargs(opencode_model="local-ds/swift-1.5#low", variant="none"),
    )

    assert cmd[cmd.index("--model") + 1] == "local-ds/swift-1.5#low"


def test_build_cmd_legacy_major_or_unknown_probe_keeps_flags(monkeypatch):
    # 1.x (or a failed probe -> None) keeps the legacy command unchanged.
    for pinned in (1, None):
        monkeypatch.setattr(
            oc_command_builder,
            "_opencode_major",
            lambda exe, _pinned=pinned: _pinned,
        )
        cmd = build_cmd(
            **_build_kwargs(use_pure=True, dir_="C:/tmp/ws", variant="none"),
        )

        assert "--pure" in cmd
        assert cmd[cmd.index("--dir") + 1] == "C:/tmp/ws"
        assert cmd[cmd.index("--variant") + 1] == "none"


def test_opencode_major_probe_parses_version_output(monkeypatch):
    # The pristine (uncached) function: _opencode_major_impl is the
    # lru_cache wrapper captured at import, .__wrapped__ the real body -
    # neither is affected by the autouse fixture's monkeypatch.
    real = _opencode_major_impl.__wrapped__

    cases = [
        ("opencode v2.2.7\n", "", 2),
        ("", "opencode/2.2.7", 2),
        ("opencode 1.18.29", "", 1),
        ("something odd", "", None),
        ("", "", None),
    ]
    for stdout, stderr, expected in cases:
        monkeypatch.setattr(
            oc_command_builder.subprocess,
            "run",
            lambda cmd, _out=stdout, _err=stderr, **kwargs: SimpleNamespace(
                stdout=_out,
                stderr=_err,
            ),
        )
        assert real("opencode") == expected


def test_opencode_major_probe_failures_return_none(monkeypatch):
    real = _opencode_major_impl.__wrapped__

    def not_found(cmd, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(oc_command_builder.subprocess, "run", not_found)
    assert real("opencode") is None

    def too_slow(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 15)

    monkeypatch.setattr(oc_command_builder.subprocess, "run", too_slow)
    assert real("opencode") is None


class _FakeStdin:
    def __init__(self):
        self.chunks = []
        self.closed = False

    def write(self, s):
        self.chunks.append(s)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _FakeProc:
    def __init__(self, cmd, **kwargs):
        self._cmd = cmd
        self._kwargs = kwargs
        self.stdin = _FakeStdin()
        self.stdout = io.StringIO('{"type":"step.finish"}\n')
        self.stderr = io.StringIO()
        self.returncode = 0
        self.pid = 4242

    def wait(self, timeout=None):
        return 0


def test_run_prompt_feeds_prompt_via_stdin(tmp_path):
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc(cmd, **kwargs)

    runner = SimpleNamespace(
        opencode_executable="opencode.cmd",
        opencode_model="test-model",
        opencode_model_backup=None,
        agent="build",
        opencode_pure=False,
        opencode_variant="",
        timeout=30,
        workspace=str(tmp_path),
        _use_continue=False,
        _session_id=None,
        _process=None,
        _last_result=None,
        _chars_exchanged=0,
        _last_tokens_total=0,
    )

    orig_popen = process_common.subprocess.Popen
    process_common.subprocess.Popen = fake_popen
    try:
        list(
            opencode_process.run_prompt(
                runner, LONG_PROMPT, find_opencode_fn=lambda exe: exe
            )
        )
    finally:
        process_common.subprocess.Popen = orig_popen

    assert captured["kwargs"]["stdin"] == process_common.subprocess.PIPE
    assert runner._last_result.ok

    # The stdin writer runs in a daemon thread; close() is its last action.
    deadline = time.time() + 5
    while not runner._process.stdin.closed and time.time() < deadline:
        time.sleep(0.01)
    assert runner._process.stdin.closed
    # run_prompt coerces the prompt (trailing whitespace stripped) before use.
    # The writer appends a line terminator (agents block on stdin EOF without
    # one), so received bytes may — correctly — end in "\n".
    assert [c.rstrip("\n") for c in runner._process.stdin.chunks] == [
        LONG_PROMPT.strip()
    ]
