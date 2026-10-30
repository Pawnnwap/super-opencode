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

from supervisor.runners.codex_support.stream import classify_line as codex_classify
from supervisor.runners.command_common import compact_tool_detail
from supervisor.runners.opencode_support.stream import classify_line as opencode_classify

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
