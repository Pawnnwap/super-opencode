"""stdin/stdout gate used by the codex ``PreToolUse`` hook.

The hook process receives the event JSON on stdin (Claude-Code-compatible
shape) and must answer with a JSON decision. Everything is decided by the
policy snapshot written at run start - the gate never trusts the agent's own
permission settings.

Usage (installed by :mod:`supervisor.security.engine_guards`)::

    python -m supervisor.security.guard_cli --policy <workspace>/.supervisor/access_policy.json

Exit status is always 0 for a *decision* (deny is expressed in JSON). A
non-zero exit only happens when the event or policy cannot be read at all;
in that case the gate fails closed by printing a deny decision too.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from supervisor.security.access_policy import AccessPolicy, PolicyViolation

logger = logging.getLogger(__name__)

_EVENT_NAME = "PreToolUse"


def _read_stdin_json() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_policy(path: Path) -> AccessPolicy:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    roots = data.get("roots") or []
    workspace = ""
    whitelist: list[str] = []
    for entry in roots:
        if not isinstance(entry, dict):
            continue
        root = str(entry.get("path") or "")
        if not root:
            continue
        if entry.get("mode") == "rw":
            workspace = root
        else:
            whitelist.append(root)
    if not workspace:
        workspace = str(data.get("workspace") or "")
    if not workspace:
        raise ValueError("policy snapshot has no writable workspace")
    return AccessPolicy(
        workspace=Path(workspace),
        whitelist=tuple(Path(item) for item in whitelist),
        enabled=bool(data.get("enabled", True)),
        strict_dynamic=bool(data.get("strictDynamic", False)),
    )


def _string_paths(value: Any) -> list[str]:
    """Collect path-looking strings from a tool input, recursively."""
    found: list[str] = []
    if isinstance(value, str):
        text = value.strip()
        if _looks_like_path(text):
            found.append(text)
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(_string_paths(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_string_paths(item))
    return found


def _looks_like_path(text: str) -> bool:
    if not text or "\n" in text or len(text) > 4096:
        return False
    return (
        (len(text) > 2 and text[1] == ":" and text[0].isalpha())
        or text.startswith("\\\\")
        or text.startswith("~/")
        or text.startswith("%USERPROFILE%")
    )


def violations_for_event(policy: AccessPolicy, event: dict[str, Any]) -> list[PolicyViolation]:
    tool = str(event.get("tool_name") or event.get("tool") or "").strip().lower()
    tool_input = event.get("tool_input") or event.get("input") or {}
    cwd = event.get("cwd") or str(policy.workspace)

    if not isinstance(tool_input, dict):
        tool_input = {"value": tool_input}

    # Any tool carrying a command string is a shell escape: scan the text
    # rather than treating the whole string as one path. This also covers
    # engine-specific tool names the mapping below does not know about.
    generic_command = tool_input.get("command") or tool_input.get("cmd")
    if isinstance(generic_command, str) and generic_command.strip():
        return policy.scan_command(generic_command, cwd=cwd)

    if tool in {
        "shell",
        "bash",
        "exec",
        "exec_command",
        "shell_command",
        "local_shell",
        "run_command",
    }:
        command = str(
            tool_input.get("command")
            or tool_input.get("cmd")
            or tool_input.get("command_line")
            or "",
        )
        return policy.scan_command(command, cwd=cwd)

    write_tools = {
        "write",
        "write_file",
        "edit",
        "edit_file",
        "multi_edit",
        "multiedit",
        "apply_patch",
        "patch",
        "create_file",
        "delete_file",
        "move_file",
        "rename_file",
    }
    access = "write" if tool in write_tools else "read"
    violations: list[PolicyViolation] = []

    # `apply_patch` carries the whole patch in `input`; its file headers are
    # the authoritative paths (`*** Update File: C:\...`).
    patch_text = str(tool_input.get("input") or tool_input.get("patch") or "")
    if tool in {"apply_patch", "patch"} and patch_text:
        for line in patch_text.splitlines():
            if line.startswith("*** ") and " File: " in line:
                candidate = line.split(" File: ", 1)[1].strip()
                if candidate:
                    decision = policy.check(candidate, access)
                    if not decision.allowed:
                        violations.append(
                            PolicyViolation(candidate, access, decision.reason)
                        )
        if violations:
            return violations

    candidates: list[str] = []
    for key in ("path", "file_path", "filePath", "filepath", "target", "destination", "src", "dst"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
    if not candidates:
        candidates = _string_paths(tool_input)

    for candidate in candidates:
        decision = policy.check(candidate, access)
        if not decision.allowed:
            violations.append(
                PolicyViolation(candidate, access, decision.reason)
            )
    # Unknown tools with no path-like input are left alone.
    return violations


def decision_payload(violations: list[PolicyViolation], policy: AccessPolicy) -> dict[str, Any]:
    if not violations:
        return {}
    reason = "; ".join(item.headline() for item in violations[:4])
    if len(violations) > 4:
        reason += f"; (+{len(violations) - 4} more)"
    reason = (
        f"Blocked by the supervisor access policy. {reason}. "
        f"Writable root: {policy.workspace}. "
        "Whitelisted directories are read-only and everything else is denied."
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": _EVENT_NAME,
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
        "systemMessage": reason,
    }


def log_violations(
    log_path: Path | None,
    event: dict[str, Any],
    violations: list[PolicyViolation],
) -> None:
    if log_path is None or not violations:
        return
    record = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "tool": str(event.get("tool_name") or event.get("tool") or ""),
        "cwd": event.get("cwd", ""),
        "violations": [item.as_dict() for item in violations],
    }
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        logger.debug("could not append access violation log", exc_info=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Supervisor access-policy gate")
    parser.add_argument("--policy", default="", help="Policy snapshot JSON")
    parser.add_argument("--log", default="", help="Violation JSONL path")
    parser.add_argument(
        "--policy-env",
        default="",
        help="Env var holding the policy path (inert when unset: outside a run)",
    )
    parser.add_argument(
        "--log-env",
        default="",
        help="Env var holding the violation log path",
    )
    parser.add_argument(
        "--check-command",
        action="store_true",
        help="Analyze {command,cwd} from stdin and print an allow/deny JSON",
    )
    args = parser.parse_args(argv)

    def _deny_check(reason: str) -> None:
        print(json.dumps({"allow": False, "reason": reason, "violations": []}))

    policy_ref = args.policy
    if args.policy_env:
        policy_ref = os.environ.get(args.policy_env, "")
        if not policy_ref:
            # The codex hook is installed globally and stays inert outside a
            # supervised run; the command analyzer is only ever called by an
            # active gate, so a missing policy there fails closed.
            if args.check_command:
                _deny_check("access policy snapshot missing (failing closed)")
            return 0
    if not policy_ref:
        if args.check_command:
            _deny_check("access policy path is required")
            return 0
        parser.error("either --policy or --policy-env is required")

    event = _read_stdin_json()
    try:
        policy = load_policy(Path(policy_ref))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        if args.check_command:
            _deny_check(f"access policy unavailable: {exc}")
            return 0
        payload = {
            "hookSpecificOutput": {
                "hookEventName": _EVENT_NAME,
                "permissionDecision": "deny",
                "permissionDecisionReason": f"access policy unavailable: {exc}",
            },
            "systemMessage": f"Supervisor access policy unavailable: {exc}",
        }
        print(json.dumps(payload))
        return 0

    if args.check_command:
        command = str(event.get("command") or "")
        cwd = str(event.get("cwd") or policy.workspace)
        violations = policy.scan_command(command, cwd)
        reason = "; ".join(item.headline() for item in violations)
        print(
            json.dumps(
                {
                    "allow": not violations,
                    "reason": reason,
                    "violations": [item.as_dict() for item in violations],
                },
            ),
        )
        event["tool_name"] = event.get("tool_name") or "shell"
        log_path = Path(args.log) if args.log else None
        if args.log_env:
            log_ref = os.environ.get(args.log_env, "")
            log_path = Path(log_ref) if log_ref else None
        log_violations(log_path, event, violations)
        return 0

    violations = violations_for_event(policy, event)
    log_ref = args.log
    if args.log_env:
        log_ref = os.environ.get(args.log_env, "")
    log_path = Path(log_ref) if log_ref else None
    log_violations(log_path, event, violations)
    payload = decision_payload(violations, policy)
    if payload:
        print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
