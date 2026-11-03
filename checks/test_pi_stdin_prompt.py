"""Regression checks for the pi runner's command shape and stdin prompt.

- The prompt travels over stdin, never argv (Windows caps command lines at
  8191 chars via the npm .cmd shim's cmd.exe, 32767 via CreateProcess).
- Continuation pins the captured session id (`--session <id>`); the bare
  `--continue` fallback is suppressed under SOP_PARALLEL_RUNS, mirroring
  codex's `resume --last` guard.
- agent_reasoning maps onto pi's `--thinking` ladder; unknown values are
  omitted (model default) instead of failing CLI parsing.
- The stream's `session` event pins the runner's session id.
"""

import io
import time
from types import SimpleNamespace

from supervisor.runners import process_common
from supervisor.runners.pi_support import process as pi_process
from supervisor.runners.pi_support.command_builder import (
    SESSIONS_DIR_NAME,
    build_cmd,
    thinking_flags,
)

# ~260k chars: far past both Windows limits, routine size for a turn prompt
# that carries roll-up / summary content.
LONG_PROMPT = "Write DESIGN.md.\n" + "detail line with some substance\n" * 20000

_BASE_KWARGS = {
    "exe": "pi.cmd",
    "prompt": LONG_PROMPT,
    "agent": "",
    "pi_model": None,
    "sessions_dir": "ws/.pi-sessions",
    "use_shell": True,
}


def test_build_cmd_never_puts_prompt_on_command_line():
    cmd = build_cmd(**_BASE_KWARGS, use_continue=False, session_id=None)

    assert cmd[1:3] == ["-p", "--mode"]
    assert "--mode" in cmd and "json" in cmd
    assert LONG_PROMPT.strip() not in cmd
    assert sum(len(part) for part in cmd) < 8191, (
        "command line must stay under the cmd.exe limit regardless of prompt size"
    )


def test_build_cmd_scopes_sessions_to_workspace_and_enables_hygiene_flags():
    cmd = build_cmd(**_BASE_KWARGS, use_continue=False, session_id=None)

    idx = cmd.index("--session-dir")
    assert cmd[idx + 1] == "ws/.pi-sessions"
    assert "--no-extensions" in cmd
    assert "--no-mcp" in cmd
    assert "--no-skills" in cmd


def test_build_cmd_resume_pins_captured_session_id():
    cmd = build_cmd(**_BASE_KWARGS, use_continue=True, session_id="01a19a67-114f")

    assert "--session" in cmd
    assert "01a19a67-114f" in cmd


def test_build_cmd_continue_fallback_suppressed_under_parallel_runs(monkeypatch):
    monkeypatch.setenv("SOP_PARALLEL_RUNS", "1")
    parallel = build_cmd(**_BASE_KWARGS, use_continue=True, session_id=None)
    assert "--continue" not in parallel

    monkeypatch.delenv("SOP_PARALLEL_RUNS")
    serial = build_cmd(**_BASE_KWARGS, use_continue=True, session_id=None)
    assert "--continue" in serial


def test_thinking_flags_map_onto_pi_ladder():
    assert thinking_flags("none") == ["--thinking", "off"]
    assert thinking_flags("off") == ["--thinking", "off"]
    assert thinking_flags("xhigh") == ["--thinking", "xhigh"]
    assert thinking_flags("") == []
    # Unknown values must be omitted, not passed through: pi rejects unknown
    # levels at argument parsing, killing the whole run.
    assert thinking_flags("ultrathink") == []


def test_build_cmd_model_forwarded():
    cmd = build_cmd(
        **_BASE_KWARGS,
        use_continue=False,
        session_id=None,
        model="local-swift/ukisai/Swift-1.5-Qwen3.8-Flash-Next-NVFP4",
    )
    assert "--model" in cmd
    assert "local-swift/ukisai/Swift-1.5-Qwen3.8-Flash-Next-NVFP4" in cmd


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
        # A minimal but realistic pi stream: session id, a tool call, prose.
        self.stdout = io.StringIO(
            '{"type":"session","version":3,"id":"01a19a67-114f-76df"}\n'
            '{"type":"message_update","assistantMessageEvent":{"type":"toolcall_end",'
            '"toolCall":{"type":"toolCall","name":"write",'
            '"arguments":{"path":"DESIGN.md","content":"v1"}}}}\n'
            '{"type":"message_end","message":{"role":"system","content":"",'
            '"sections":{"preamble":"SYSTEM PROMPT NOISE"}}}\n'
            '{"type":"message_end","message":{"role":"assistant","content":'
            '[{"type":"text","text":"pi says done"}]}}\n'
            '{"type":"turn_end","message":{"role":"assistant","content":[],'
            '"usage":{"input":900,"output":100,"cacheRead":0,"cacheWrite":0,'
            '"totalTokens":1000}}}\n'
        )
        self.stderr = io.StringIO()
        self.returncode = 0
        self.pid = 4242

    def wait(self, timeout=None):
        return 0


def _make_runner(tmp_path):
    return SimpleNamespace(
        opencode_executable="pi.cmd",
        opencode_model="local-swift/test-model",
        opencode_model_backup=None,
        agent="",
        opencode_variant="",
        pi_sessions_dir=str(tmp_path / SESSIONS_DIR_NAME),
        timeout=30,
        workspace=str(tmp_path),
        _use_continue=False,
        _session_id=None,
        _process=None,
        _last_result=None,
        _chars_exchanged=0,
        _last_tokens_total=0,
    )


def test_run_prompt_feeds_prompt_via_stdin_and_captures_session(tmp_path):
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc(cmd, **kwargs)

    runner = _make_runner(tmp_path)

    orig_popen = process_common.subprocess.Popen
    process_common.subprocess.Popen = fake_popen
    try:
        list(pi_process.run_prompt(runner, LONG_PROMPT, find_pi_fn=lambda exe: exe))
    finally:
        process_common.subprocess.Popen = orig_popen

    assert captured["kwargs"]["stdin"] == process_common.subprocess.PIPE
    assert runner._last_result.ok
    # Only assistant prose survives; the system prompt echo must not leak
    # into the supervisor's context.
    assert "pi says done" in runner._last_result.stdout
    assert "SYSTEM PROMPT NOISE" not in runner._last_result.stdout
    # Session id captured from the stream's `session` event.
    assert runner._session_id == "01a19a67-114f-76df"
    # Real context tokens from turn_end usage.
    assert runner._last_tokens_total == 1000

    # The stdin writer runs in a daemon thread; close() is its last action.
    deadline = time.time() + 5
    while not runner._process.stdin.closed and time.time() < deadline:
        time.sleep(0.01)
    assert runner._process.stdin.closed
    # run_prompt coerces the prompt (trailing whitespace stripped) before
    # use. The writer appends a line terminator (agents block on stdin EOF
    # without one), so received bytes may — correctly — end in "\n".
    assert [c.rstrip("\n") for c in runner._process.stdin.chunks] == [
        LONG_PROMPT.strip()
    ]
