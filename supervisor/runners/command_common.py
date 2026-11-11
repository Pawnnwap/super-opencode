from __future__ import annotations

import logging
import os
import re
from collections.abc import Sequence
from pathlib import Path

from supervisor.utils.text_utils import coerce_str

logger = logging.getLogger(__name__)

# Export flag announcing that several runs are in flight at once. Set by the
# job manager and read by every command builder whose resume fallback
# (`resume --last`, `--continue`) could otherwise adopt another replica's
# session. It is an environment contract across the UI/daemon/child boundary,
# so both sides must spell it identically.
PARALLEL_RUNS_ENV = "SOP_PARALLEL_RUNS"


def parallel_runs_active() -> bool:
    """True when more than one supervised run shares the session store."""
    return os.environ.get(PARALLEL_RUNS_ENV) == "1"


# Provider-unreachable signatures on agent engine output lines. Matched only
# against raw/error stream lines while the turn has produced nothing yet, so
# an agent *writing about* connection errors can never trip the abort.
_CONNECT_PHRASES = (
    "unable to connect",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "connection refused",
    "econnrefused",
    "connection reset",
    "econnreset",
    "socket hang up",
    "fetch failed",
    "connection error",
    "network error",
    "etimed out",
    "connect timeout",
)


def provider_connection_error(text: str) -> str | None:
    """Return the matched signature when *text* says the provider is down."""
    lowered = str(text).lower()
    for phrase in _CONNECT_PHRASES:
        if phrase in lowered:
            return phrase
    for code in ("502", "503", "504"):
        idx = lowered.find(code)
        if idx != -1 and any(
            word in lowered for word in ("error", "status", "api", "gateway",
                                          "unavailable", "response")
        ):
            return f"http {code}"
    return None


def validate_message(message: str, context: str, engine: str) -> str | None:
    """Return cleaned message or None when empty after coercion.

    *engine* ("opencode"/"codex") only labels the warning so logs make clear
    which backend received the empty message.
    """
    message = coerce_str(message, context)
    if not message:
        logger.warning(
            "Empty message provided to %s (%s). Returning None to trigger graceful handling.",
            engine,
            context,
        )
        return None
    return message


def safe_command_summary(command: Sequence[str]) -> str:
    """Describe an agent invocation without logging prompt, URL, or overrides."""
    visible = [str(part) for part in command[:2] if str(part)]
    return " ".join(visible) + " [arguments redacted]"


def resolve_model_arg(
    model: str | None,
    self_model: str | None,
    dot_model_file: Path,
    engine: str,
) -> str:
    """Resolve one engine's model string for a single command (shared helper).

    Precedence: explicit per-call argument > the runner's configured model >
    the repo-root ``.<engine>_model`` pin file (a debugging escape hatch —
    drop a model string in the file and every run uses it when nothing is
    configured).
    """
    resolved = coerce_str(model, f"model arg ({engine} build_cmd)") or coerce_str(
        self_model, f"{engine}_model ({engine} build_cmd)"
    )
    if not resolved and dot_model_file.exists():
        resolved = dot_model_file.read_text(encoding="utf-8").strip()
        logger.debug("Model resolved from %s file: %r", dot_model_file.name, resolved)
    return resolved


# Quoted or bare absolute paths to common shells/interpreters, optionally
# preceded by the PowerShell call operator. On Windows every agent shell tool
# call repeats the full exe path (C:\\WINDOWS\\...\\powershell.exe, the conda
# python, ...) — pure noise in a one-line tool breadcrumb.
#
# LINEAR-TIME INVARIANT: a segment may not contain a separator. The previous
# `(?:[^\s"']*?[\\/])*` let every separator be consumed either by the lazy
# tail or by the trailing class, so a long slash run (a URL path, a base64
# blob) that never reaches an interpreter name backtracked exponentially —
# 26 URL segments ran >8s, and on 2026-11-10 03:00 one such line held the GIL
# inside re.sub for 11+ hours and froze every job in the worker. Do not
# reintroduce an ambiguous inner quantifier here.
#
# The lookbehind keeps `https://…` from matching at the scheme's `s:`.
_EXE_PATH_RE = re.compile(
    r'(?:&\s+)?["\']?'
    r'(?<![A-Za-z0-9])[A-Za-z]:[\\/](?:[^\s"\'\\/]*[\\/])*'
    r'(?P<name>powershell|pwsh|cmd|pythonw?|bash|sh|node|npm|npx|uvx?|conda)'
    r'(?:\.exe|\.cmd|\.bat|\.ps1)?'
    r'["\']?',
    re.IGNORECASE,
)
# Leading `$env:NAME="value"; ` segments that the agents' Windows shell
# wrappers repeat on every single call (PYTHONIOENCODING and friends) — both
# at the very start and right after `-Command`.
_ENV_PRELUDE_RE = re.compile(r'^\$env:[A-Za-z_]\w*\s*=\s*(?:"[^"]*"|\'[^\']*\'|\S+);\s*')
_ENV_AFTER_COMMAND_RE = re.compile(
    r"(-Command\s+['\"]?)(?:\$env:[A-Za-z_]\w*\s*=\s*(?:\"[^\"]*\"|'[^']*'|\S+);\s*)+",
    re.IGNORECASE,
)
# Flags the wrappers always pass and no human needs in a breadcrumb.
_NOISE_FLAGS_RE = re.compile(r'\s+(?:-NoProfile|-NonInteractive)\b', re.IGNORECASE)
# The whole `powershell -Command` wrapper: the payload IS the command — the
# shell name and flag repeat on every Windows call and add nothing.
_SHELL_WRAPPER_RE = re.compile(
    r'^(?:powershell|pwsh)\s+'
    r'(?:-NoProfile\s+|-NonInteractive\s+|-ExecutionPolicy\s+\S+\s+|-Sta\s+|-MTA\s+)*'
    r'-Command\s*["\']?',
    re.IGNORECASE,
)

# Cap how much of a tool's intent we keep in its ``[tool] …`` marker. The
# marker is a breadcrumb of *what the agent did*, not a transcript, so a short
# slice is plenty and keeps history lean. Defined here so both engine stream
# parsers share one number.
TOOL_DETAIL_LIMIT = 200

# The marker keeps at most ``limit`` chars, so scanning more than a few KiB of
# the raw detail buys nothing. The bound also caps memory and any downstream
# regex work on absurd single-line tool payloads (multi-MB diffs/outputs).
_TOOL_DETAIL_INPUT_LIMIT = 8192


def compact_tool_detail(detail: str, limit: int = TOOL_DETAIL_LIMIT) -> str:
    """Compact a tool-call detail string for the ``[tool] …`` breadcrumb.

    Collapses whitespace, replaces absolute paths to well-known shells and
    interpreters with the bare executable name (``"C:\\...\\powershell.exe"``
    → ``powershell``, ``& "C:/.../python.exe"`` → ``python``), drops the
    repeating ``$env:VAR="…"; `` prelude and -NoProfile/-NonInteractive flags,
    and unwraps a leading ``powershell -Command "…"`` so only the executed
    payload remains. The payload itself is preserved — the loop detector keys
    on the marker, so distinct calls must stay distinct; only the
    ever-repeating wrapper noise is removed. Capped at *limit* chars.
    """
    text = " ".join((detail or "")[:_TOOL_DETAIL_INPUT_LIMIT].split())
    if not text:
        return ""
    text = _EXE_PATH_RE.sub(lambda m: m.group("name"), text)
    while True:
        stripped = _ENV_PRELUDE_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    text = _ENV_AFTER_COMMAND_RE.sub(r"\1", text)
    text = _NOISE_FLAGS_RE.sub("", text)
    wrapper = _SHELL_WRAPPER_RE.match(text)
    if wrapper:
        text = text[wrapper.end():].lstrip()
        # The wrapper consumed the payload's OPENING quote — drop its
        # matching closing quote (when it survives the 200-char cap it
        # would just read as a stray character).
        opening = wrapper.group(0)[-1]
        if opening in ('"', "'") and text.endswith(opening):
            text = text[:-1]
    text = text.removeprefix("& ")
    return text[:limit]


def feed_stdin_prompt(proc, prompt: str) -> None:
    """Write *prompt* to *proc*'s stdin and close it (best-effort).

    Designed to run in a daemon thread (callers launch the agent with the
    prompt passed over stdin, never argv — Windows caps command lines at
    8191 chars via cmd.exe shims and 32767 via CreateProcess). The thread
    matters because prompts can exceed the OS pipe buffer while the CLI is
    already streaming events to stdout: an inline write would deadlock
    exactly like an undrained stdout pipe. A child that exits early (bad
    flag, auth error) breaks the pipe; the write swallows that and normal
    stream consumption reports the real stderr/exit code.
    """
    try:
        # Codex (and opencode) block on a line terminator before starting:
        # stdin EOF without a trailing newline stalls the agent ~10-20s.
        write = prompt if prompt.endswith("\n") else prompt + "\n"
        proc.stdin.write(write)
        proc.stdin.flush()
    except Exception:
        pass
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
