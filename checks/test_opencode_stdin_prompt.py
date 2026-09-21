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
import time
from types import SimpleNamespace

from supervisor.runners.opencode_support import process as opencode_process
from supervisor.runners.opencode_support.command_builder import build_cmd

# ~260k chars: far past both Windows limits, routine size for a turn prompt
# that carries roll-up / summary content.
LONG_PROMPT = "Write DESIGN.md.\n" + "detail line with some substance\n" * 20000


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
        enable_headroom=False,
        headroom_executable="",
        headroom_allow_custom_provider=False,
        timeout=30,
        workspace=str(tmp_path),
        _use_continue=False,
        _session_id=None,
        _headroom_lease=None,
        _headroom_status="",
        _process=None,
        _last_result=None,
        _chars_exchanged=0,
        _last_tokens_total=0,
    )

    orig_popen = opencode_process.subprocess.Popen
    opencode_process.subprocess.Popen = fake_popen
    try:
        list(
            opencode_process.run_prompt(
                runner, LONG_PROMPT, find_opencode_fn=lambda exe: exe
            )
        )
    finally:
        opencode_process.subprocess.Popen = orig_popen

    assert captured["kwargs"]["stdin"] == opencode_process.subprocess.PIPE
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
