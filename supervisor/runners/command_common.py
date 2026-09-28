from __future__ import annotations

import logging
from collections.abc import Sequence

from supervisor.utils.text_utils import coerce_str

logger = logging.getLogger(__name__)

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


def fresh_session_prompt(prompt: str) -> str:
    """Return the task prompt unchanged for the first turn of a session.

    Inlining brevity/style rules here was removed empirically: several free
    models (nemotron ultra, laguna) fixate on a trailing rules block and reply
    "mode active — awaiting command" / "please provide the protocol" instead
    of executing the task. The bare task prompt works everywhere; style
    guidance already lives in the supervisor-driven protocol when needed.
    """
    return prompt


def safe_command_summary(command: Sequence[str]) -> str:
    """Describe an agent invocation without logging prompt, URL, or overrides."""
    visible = [str(part) for part in command[:2] if str(part)]
    return " ".join(visible) + " [arguments redacted]"


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
