"""Tool-breadcrumb compaction checks (compact_tool_detail + both engines).

Windows shell tool calls used to fill the 200-char marker with wrapper noise
that repeats on every single call:

    [shell] "C:\\WINDOWS\\...\\powershell.exe" -Command
    '$env:PYTHONIOENCODING="utf-8"; & "C:/Users/<user>/.conda/.../python.exe" -c @" ...'

The compact form keeps the PAYLOAD (the loop detector keys on the marker, so
distinct calls must stay distinct) and drops the ever-repeating wrapper:
absolute exe paths become bare names, `$env:VAR="…"; ` preludes vanish.
"""

import json
import time

from supervisor.runners.codex_support.stream import classify_line as codex_classify
from supervisor.runners.command_common import compact_tool_detail
from supervisor.runners.opencode_support.stream import (
    classify_line as opencode_classify,
)

USER_SAMPLE = (
    '"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command '
    "'$env:PYTHONIOENCODING=\"utf-8\"; & \"C:/Users/<user>/.conda/envs/work/python.exe\" "
    '-c @" import sys,re sys.stdout.reconfigure(encoding=\'utf-8\'); '
    'import urllib.request; print(\'ok\')"'
)


def test_windows_powershell_wrapper_compacted():
    out = compact_tool_detail(USER_SAMPLE)
    assert out.startswith("python -c")
    for noise in (
        "WINDOWS", "conda", "PYTHONIOENCODING", "C:/", "C:\\", "&",
        "powershell", "-Command",
    ):
        assert noise not in out, noise
    # The actual payload survives.
    assert "sys.stdout.reconfigure" in out
    assert "urllib.request" in out


def test_powershell_command_wrapper_stripped_plain():
    assert compact_tool_detail('powershell -Command "Get-ChildItem -Force"') == "Get-ChildItem -Force"
    assert (
        compact_tool_detail("powershell -NoProfile -NonInteractive -Command 'rg -l pattern E:\\data'")
        == "rg -l pattern E:\\data"
    )
    # Call-operator leading the payload is dropped too.
    assert compact_tool_detail('powershell -Command "& python -c \'print(1)\'"') == "python -c 'print(1)'"


def test_unquoted_cmd_path_becomes_bare_name():
    assert compact_tool_detail("C:\\Windows\\System32\\cmd.exe /c echo hi") == "cmd /c echo hi"
    assert compact_tool_detail("& 'C:/tools/nodejs/node.exe' build.js") == "node build.js"


def test_plain_commands_untouched():
    for cmd in ("git status --short", "python -m pytest -q", "ls -la /tmp"):
        assert compact_tool_detail(cmd) == cmd


def test_env_prelude_stripped_alone():
    assert compact_tool_detail('$env:FOO="bar"; $env:BAZ=\'x\'; pytest -q') == "pytest -q"


def test_identical_semantics_stay_deduped():
    # Same command behind different env preludes (or spellings of the exe
    # path) collapse to ONE marker — they are the same command.
    a = compact_tool_detail('"C:\\A\\python.exe" -c "print(1)"')
    b = compact_tool_detail('"C:\\B\\python.exe" -c "print(1)"')
    c = compact_tool_detail('$env:PY="x"; & "C:\\B\\python.exe" -c "print(1)"')
    assert a == b == c
    # Different payloads stay distinct (loop-detector requirement).
    d = compact_tool_detail('"C:\\A\\python.exe" -c "print(2)"')
    assert d != a


def test_cap_respected():
    out = compact_tool_detail("python -c \"print('" + "x" * 500 + "')\"")
    assert len(out) <= 200


def test_opencode_step_finish_tokens_22x_sum():
    # opencode 2.2.x dropped "tokens.total" (captured live on v2.2.7); the
    # context total is input+output+reasoning. Without the fallback the
    # supervisor's context tracking read 0 for every step on 2.x.
    line = json.dumps(
        {
            "type": "step_finish",
            "part": {
                "type": "step-finish",
                "reason": "tool-calls",
                "cost": 0,
                "tokens": {
                    "input": 16030,
                    "output": 51,
                    "reasoning": 291,
                    "cache": {"read": 0, "write": 0},
                },
            },
        },
    )
    event = opencode_classify(line)
    assert event.kind == "tokens"
    assert event.tokens_total == 16030 + 51 + 291


def test_opencode_step_finish_tokens_explicit_total_wins():
    # Older shapes that still carry "total" keep working.
    line = json.dumps(
        {
            "type": "step_finish",
            "part": {"type": "step-finish", "tokens": {"total": 16531, "input": 16030}},
        },
    )
    assert opencode_classify(line).tokens_total == 16531


def test_opencode_step_finish_tokens_garbage_is_zero():
    for part in (
        {"type": "step-finish", "tokens": {"cache": {"read": 0}}},
        {"type": "step-finish"},
        {},
    ):
        assert opencode_classify(json.dumps({"type": "step_finish", "part": part})).tokens_total == 0


def test_long_url_and_path_runs_stay_linear():
    # Regression: the old `(?:[^\s"']*?[\\/])*` backtracked exponentially on
    # separator runs that never reach an interpreter name. On 2026-11-10 a
    # long URL in a shell command held the GIL for 11+ hours inside this
    # function and froze every job in the worker. 26 segments already took
    # >8s; these must be instant.
    for detail in (
        "https://api.example.com/" + "/".join(f"v{i}" for i in range(40)),
        "C:/" + "/".join(f"seg{i}" for i in range(300)) + "/file.txt",
        "C:\\" + "\\".join(f"seg{i}" for i in range(300)) + "\\file.txt",
    ):
        started = time.perf_counter()
        compact_tool_detail(detail)
        elapsed = time.perf_counter() - started
        assert elapsed < 2.0, (len(detail), elapsed)


def test_urls_are_not_mistaken_for_exe_paths():
    # The scheme's `s:/` matches a drive-letter prefix; it must not be
    # rewritten (and must not trigger the path scan at all).
    for url in (
        "https://api.example.com/v1/tools/python",
        "curl -s https://example.org/~user/bin/node -o out.bin",
    ):
        assert compact_tool_detail(url) == url, compact_tool_detail(url)


def test_oversized_detail_keeps_the_same_prefix():
    detail = "git log --oneline " + "x" * 100_000
    out = compact_tool_detail(detail)
    assert out.startswith("git log --oneline xxx")
    assert len(out) == 200


def test_codex_engine_emits_compact_marker():
    item = {
        "type": "item.completed",
        "item": {"type": "command_execution", "command": USER_SAMPLE},
    }
    event = codex_classify(json.dumps(item))
    assert event is not None and event.kind == "tool"
    assert event.marker.startswith("[shell] python -c")
    assert "conda" not in event.marker and "powershell" not in event.marker


def test_opencode_engine_emits_compact_marker():
    part = {
        "type": "tool",
        "tool": "bash",
        "state": {"input": {"command": USER_SAMPLE}},
    }
    event = opencode_classify(json.dumps({"type": "tool_use", "part": part}))
    assert event is not None and event.kind == "tool"
    assert event.marker.startswith("[bash] python -c")
    assert "conda" not in event.marker and "powershell" not in event.marker


def test_codex_error_item_keeps_its_message():
    # Session-init problems (stale model metadata, MCP init failures) arrive
    # as an `error` ITEM; the marker must carry the message, not render an
    # unexplained bare `[error]`.
    event = codex_classify(json.dumps({
        "type": "item.completed",
        "item": {
            "type": "error",
            "message": "Model metadata for `deepseek-ai/X` not found. "
                       "Defaulting to fallback metadata.",
        },
    }))
    assert event is not None and event.kind == "tool"
    assert event.marker.startswith("[error] Model metadata for")
    assert "fallback metadata" in event.marker
