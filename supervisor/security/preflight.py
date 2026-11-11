"""Preflight drills for the access policy ("canary" checks).

A policy that is configured but not actually enforced is worse than no policy:
it looks safe. Borrowing the drill idea from the local ``cage`` project - a
read-only canary and escape attempts that must fail - this module proves the
boundary before the first agent turn:

* the Python policy answers correctly for cwd / whitelist / outside paths and
  for shell commands (including ``..`` escapes);
* the installed engine gate exists and, for codex, actually returns a deny
  decision when handed an out-of-policy ``PreToolUse`` event.

A failed drill aborts the run instead of silently degrading to "no policy".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from supervisor.security.access_policy import AccessPolicy
from supervisor.security.engine_guards import GUARD_MARKER, GuardInstallResult


@dataclass(frozen=True)
class DrillCheck:
    name: str
    ok: bool
    detail: str = ""

    def line(self) -> str:
        mark = "PASS" if self.ok else "FAIL"
        return f"[{mark}] {self.name}" + (f": {self.detail}" if self.detail else "")


@dataclass
class DrillReport:
    checks: list[DrillCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(check.ok for check in self.checks)

    @property
    def passed(self) -> int:
        return sum(1 for check in self.checks if check.ok)

    def summary(self) -> str:
        return f"{self.passed}/{len(self.checks)} checks passed"

    def failures(self) -> list[DrillCheck]:
        return [check for check in self.checks if not check.ok]

    def fail_text(self) -> str:
        return "; ".join(
            f"{check.name} ({check.detail})" for check in self.failures()
        ) or "no checks ran"


def run_drill(
    engine: str,
    workspace: Path,
    policy: AccessPolicy,
    install: GuardInstallResult | None = None,
) -> DrillReport:
    """Run every applicable drill for *engine*; the caller aborts on failure."""
    report = run_policy_drill(workspace, policy)
    report.checks.extend(_static_gate_checks(engine, workspace, install))
    if engine == "codex" and install is not None and install.policy_path:
        report.checks.extend(_codex_hook_checks(workspace, install))
    return report


def run_policy_drill(workspace: Path, policy: AccessPolicy) -> DrillReport:
    """Prove the policy engine agrees with the documented boundary."""
    ws = Path(workspace)
    report = DrillReport()
    outside = ws.parent / "cage-drill-outside" / "secret.txt"
    sibling = Path(str(ws) + "-evil")

    def check(name: str, ok: bool, detail: str = "") -> None:
        report.checks.append(DrillCheck(name, ok, detail))

    check("cwd readable", policy.check_read(ws / "drill.txt").allowed)
    check("cwd writable", policy.check_write(ws / "drill.txt").allowed)
    check(
        "sibling-prefix path denied",
        not policy.check_read(sibling / "x").allowed,
        str(sibling),
    )
    check("outside read denied", not policy.check_read(outside).allowed, str(outside))
    check("outside write denied", not policy.check_write(outside).allowed, str(outside))

    if policy.whitelist:
        entry = policy.whitelist[0]
        check("whitelist readable", policy.check_read(entry / "drill.txt").allowed, str(entry))
        check(
            "whitelist write denied",
            not policy.check_write(entry / "drill.txt").allowed,
            str(entry),
        )
    else:
        check("whitelist empty is explicit", True, "no read-only roots configured")

    check("inside shell command allowed", not policy.scan_command("cat src/drill.txt"))
    check(
        "relative escape denied",
        bool(policy.scan_command("cat ../../cage-drill-secret.txt")),
    )
    check(
        "absolute outside denied",
        bool(policy.scan_command(f'cat "{outside}"')),
    )
    return report


def _static_gate_checks(
    engine: str,
    workspace: Path,
    install: GuardInstallResult | None,
) -> list[DrillCheck]:
    checks: list[DrillCheck] = []
    if install is None:
        return [DrillCheck("gate install result", False, "no install result")]

    snapshot = install.policy_path
    checks.append(
        DrillCheck(
            "policy snapshot on disk",
            bool(snapshot and snapshot.is_file() and snapshot.parent != Path(workspace)),
            str(snapshot or "missing"),
        ),
    )
    if snapshot and snapshot.is_file():
        try:
            payload = json.loads(snapshot.read_text(encoding="utf-8"))
            roots = payload.get("roots") or []
            checks.append(
                DrillCheck(
                    "policy snapshot parses",
                    bool(payload.get("version") == 1 and roots),
                    f"{len(roots)} root(s)",
                ),
            )
        except (OSError, json.JSONDecodeError) as exc:
            checks.append(DrillCheck("policy snapshot parses", False, str(exc)))

    if engine == "opencode":
        plugin = workspace / ".opencode" / "plugin" / "supervisor_access_guard.js"
        checks.append(
            DrillCheck(
                "opencode plugin installed",
                plugin.is_file() and "SupervisorAccessGuard" in plugin.read_text(
                    encoding="utf-8", errors="ignore",
                ),
                str(plugin),
            ),
        )
        config = workspace / ".opencode" / "opencode.json"
        deny = {}
        try:
            deny = (
                json.loads(config.read_text(encoding="utf-8"))
                .get("permission", {})
                .get("external_directory", {})
            )
        except (OSError, json.JSONDecodeError):
            pass
        checks.append(
            DrillCheck(
                "opencode external_directory denied",
                deny.get("*") == "deny",
                json.dumps(deny)[:120],
            ),
        )
        checks.append(
            DrillCheck(
                "opencode rejects V2 permissions key",
                "permissions" not in _read_json(config),
                "V1 config must not contain 'permissions'",
            ),
        )
    elif engine == "pi":
        extension = workspace / ".pi" / "extensions" / "supervisor-access-guard.ts"
        checks.append(
            DrillCheck(
                "pi extension installed",
                extension.is_file() and GUARD_MARKER in extension.read_text(
                    encoding="utf-8", errors="ignore",
                ),
                str(extension),
            ),
        )
        checks.append(
            DrillCheck(
                "pi extension passed via -e",
                bool(install.flags) and install.flags[0] == "-e",
                " ".join(install.flags[:2]),
            ),
        )
    elif engine == "codex":
        hooks_value = install.artifacts[-1] if install.artifacts else None
        hooks_path = Path(hooks_value) if hooks_value and str(hooks_value).endswith(".json") else None
        present = False
        if hooks_path and hooks_path.is_file():
            try:
                data = json.loads(hooks_path.read_text(encoding="utf-8"))
                for entry in data.get("hooks", {}).get("PreToolUse", []):
                    for handler in entry.get("hooks", []):
                        if "supervisor.security.guard_cli" in str(handler.get("command", "")):
                            present = True
            except (OSError, json.JSONDecodeError):
                present = False
        checks.append(
            DrillCheck(
                "codex PreToolUse hook registered",
                present,
                str(hooks_path or "hooks.json not found"),
            ),
        )
        checks.append(
            DrillCheck(
                "codex hook trust bypass flag",
                "--dangerously-bypass-hook-trust" in install.flags,
            ),
        )
    return checks


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _codex_hook_checks(workspace: Path, install: GuardInstallResult) -> list[DrillCheck]:
    """Execute the installed codex gate twice: deny outside, allow inside."""
    snapshot = install.policy_path
    if snapshot is None:
        return [DrillCheck("codex gate drill", False, "policy snapshot missing")]

    env = {k: str(v) for k, v in (install.env or {}).items()}
    outside = Path(workspace).parent / "cage-drill-outside" / "secret.txt"

    denied = _run_hook(snapshot, env, workspace, f'cat "{outside}"')
    allowed = _run_hook(snapshot, env, workspace, "cat src/drill.txt")
    return [
        DrillCheck(
            "codex gate denies outside read",
            '"deny"' in denied,
            _short(denied),
        ),
        DrillCheck(
            "codex gate allows cwd read",
            allowed.strip() == "",
            _short(allowed) or "no decision",
        ),
    ]


def _run_hook(
    snapshot: Path,
    env: dict[str, str],
    workspace: Path,
    command: str,
) -> str:
    event = json.dumps(
        {
            "tool_name": "shell",
            "tool_input": {"command": command},
            "cwd": str(workspace),
        },
    )
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "supervisor.security.guard_cli",
                "--policy",
                str(snapshot),
            ],
            input=event,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(workspace),
            env={**os.environ, **env},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"gate execution failed: {exc}"
    return proc.stdout.strip()


def _short(text: str, limit: int = 120) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


__all__ = [
    "DrillCheck",
    "DrillReport",
    "run_drill",
    "run_policy_drill",
]
