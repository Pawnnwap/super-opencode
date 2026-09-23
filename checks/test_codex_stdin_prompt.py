"""Regression checks: the codex prompt travels over stdin, never argv.

Windows caps command lines at 8191 chars when codex runs through a ``.cmd``
shim (cmd.exe /c) and at 32767 via CreateProcess even without a shell. A
prompt past the limit fails before codex even starts ("The command line is
too long", exit 1) — which the supervisor's judge reads as "agent produced
only a shell failure, no work product", turn after turn.
"""

import io
import time
from types import SimpleNamespace

from supervisor.runners.codex_support import process as codex_process
from supervisor.runners.codex_support.command_builder import build_cmd

# ~260k chars: far past both Windows limits, routine size for a turn prompt
# that carries roll-up / summary content.
LONG_PROMPT = "Write DESIGN.md.\n" + "detail line with some substance\n" * 20000


def test_build_cmd_never_puts_prompt_on_command_line():
    cmd = build_cmd(
        exe="codex.cmd",
        prompt=LONG_PROMPT,
        agent="",
        codex_model=None,
        use_continue=False,
        session_id=None,
        use_shell=True,
    )

    assert cmd[-2:] == ["--", "-"]
    assert LONG_PROMPT.strip() not in cmd
    assert sum(len(part) for part in cmd) < 8191, (
        "command line must stay under the cmd.exe limit regardless of prompt size"
    )


def test_build_cmd_resume_uses_stdin_marker():
    cmd = build_cmd(
        exe="codex",
        prompt=LONG_PROMPT,
        agent="",
        codex_model=None,
        use_continue=True,
        session_id="abc123",
        use_shell=False,
    )

    assert cmd[1:3] == ["exec", "--json"]
    assert "resume" in cmd
    assert "abc123" in cmd
    assert cmd[-2:] == ["--", "-"]


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
        self.stdout = io.StringIO("codex says done\n")
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
        opencode_executable="codex.cmd",
        opencode_model="test-model",
        opencode_model_backup=None,
        agent="",
        codex_base_url="",
        codex_api_key="",
        codex_context_window=0,
        codex_reasoning_effort="",
        codex_extra_env={},
        timeout=30,
        workspace=str(tmp_path),
        _use_continue=False,
        _session_id=None,
        _process=None,
        _last_result=None,
        _chars_exchanged=0,
        _last_tokens_total=0,
    )

    orig_popen = codex_process.subprocess.Popen
    codex_process.subprocess.Popen = fake_popen
    try:
        list(codex_process.run_prompt(runner, LONG_PROMPT, find_codex_fn=lambda exe: exe))
    finally:
        codex_process.subprocess.Popen = orig_popen

    assert captured["kwargs"]["stdin"] == codex_process.subprocess.PIPE
    assert captured["cmd"][-2:] == ["--", "-"]
    assert runner._last_result.ok
    assert "codex says done" in runner._last_result.stdout

    # The stdin writer runs in a daemon thread; close() is its last action.
    deadline = time.time() + 5
    while not runner._process.stdin.closed and time.time() < deadline:
        time.sleep(0.01)
    assert runner._process.stdin.closed
    # run_prompt coerces the prompt (trailing whitespace stripped) before use.
    # The writer appends a line terminator: codex blocks ~10-20s on stdin EOF
    # without one, so the received bytes may (correctly) end in "\n".
    assert [c.rstrip("\n") for c in runner._process.stdin.chunks] == [
        LONG_PROMPT.strip()
    ]


def test_build_cmd_appends_extra_config_flags_only_when_given():
    base_kwargs = dict(
        exe="codex.cmd",
        prompt="hi",
        agent="",
        codex_model=None,
        use_continue=False,
        session_id=None,
        use_shell=True,
    )

    plain = build_cmd(**base_kwargs)
    flagged = build_cmd(**base_kwargs, extra_config_flags=["-c", "features.plugins=false"])

    assert "features.plugins=false" not in plain
    assert "-c" in flagged and "features.plugins=false" in flagged
    assert flagged[-2:] == ["--", "-"]
