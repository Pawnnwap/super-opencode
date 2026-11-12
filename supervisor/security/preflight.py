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
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

from supervisor.security.access_policy import AccessPolicy, is_within
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
        ws = Path(os.path.realpath(str(workspace)))
        found = _outside_fixture(ws, (ws, *tuple(policy.whitelist)))
        outside = found[0] if found else None
        report.checks.extend(_codex_hook_checks(workspace, install, outside))
    return report


def _outside_fixture(ws: Path, roots: tuple[Path, ...]) -> tuple[Path, int] | None:
    """Pick a drill secret outside *roots*, as close to the workspace as possible.

    Returns ``(secret_path, ancestor_depth)``; depth 0 marks a temp/home
    fallback (no relative escape available). ``None`` when every candidate
    is covered by the roots.
    """
    for depth, base in enumerate(ws.parents, start=1):
        secret = base / "cage-drill-outside" / "secret.txt"
        escape = base / "cage-drill-secret.txt"
        if not any(is_within(path, root) for path in (secret, escape) for root in roots):
            return secret, depth
    for base in (Path(tempfile.gettempdir()), Path.home()):
        secret = Path(os.path.realpath(str(base))) / "cage-drill-outside" / "secret.txt"
        if not any(is_within(secret, root) for root in roots):
            return secret, 0
    return None


def _without_whitelist(policy: AccessPolicy) -> AccessPolicy:
    """The cwd boundary alone, for probes whose target the whitelist covers."""
    if isinstance(policy, AccessPolicy):
        return replace(policy, whitelist=())
    return policy


def run_policy_drill(workspace: Path, policy: AccessPolicy) -> DrillReport:
    """Prove the policy engine agrees with the configured boundary.

    Fixtures must sit outside cwd AND the whitelist: a read-only reference
    tree may legitimately contain the workspace's parent, and a secret there
    is readable by design - not a cage leak.
    """
    ws = Path(os.path.realpath(str(workspace)))
    report = DrillReport()
    found = _outside_fixture(ws, (ws, *tuple(policy.whitelist)))
    sibling = Path(str(ws) + "-evil")

    def check(name: str, ok: bool, detail: str = "") -> None:
        report.checks.append(DrillCheck(name, ok, detail))

    check("cwd readable", policy.check_read(ws / "drill.txt").allowed)
    check("cwd writable", policy.check_write(ws / "drill.txt").allowed)

    sibling_probe = policy
    sibling_detail = str(sibling)
    if any(is_within(sibling, root) for root in policy.whitelist):
        sibling_probe = _without_whitelist(policy)
        sibling_detail += " (whitelist covers it; cwd boundary probed alone)"
    check(
        "sibling-prefix path denied",
        not sibling_probe.check_read(sibling / "x").allowed,
        sibling_detail,
    )

    if found is None:
        check(
            "outside drill root",
            False,
            "whitelist covers every candidate root; cannot prove the outside boundary",
        )
    else:
        outside, depth = found
        check("outside read denied", not policy.check_read(outside).allowed, str(outside))
        check("outside write denied", not policy.check_write(outside).allowed, str(outside))
        if depth:
            escape = "cat " + "../" * depth + "cage-drill-secret.txt"
            check("relative escape denied", bool(policy.scan_command(escape)), escape)
        else:
            escape = "cat ../../cage-drill-secret.txt"
            check(
                "relative escape denied",
                bool(_without_whitelist(policy).scan_command(escape)),
                "ancestors whitelisted; cwd boundary probed alone",
            )
        check(
            "absolute outside denied",
            bool(policy.scan_command(f'cat "{outside}"')),
            str(outside),
        )

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
        "inline interpreter denied",
        bool(policy.scan_command('python -c "print(1)"')),
    )
    check(
        "dynamic command denied",
        bool(policy.scan_command("echo $(whoami)")),
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
        hook_artifact = next(
            (item for item in install.artifacts if item.name in ("hooks.json", "config.toml")),
            None,
        )
        present = False
        if hook_artifact and hook_artifact.is_file():
            text = hook_artifact.read_text(encoding="utf-8", errors="ignore")
            if hook_artifact.name == "config.toml":
                present = (
                    "[[hooks.PreToolUse]]" in text
                    and "supervisor.security.guard_cli" in text
                )
            else:
                try:
                    data = json.loads(text)
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
                str(hook_artifact or "hook artifact not found"),
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


def _codex_hook_checks(
    workspace: Path,
    install: GuardInstallResult,
    outside: Path | None,
) -> list[DrillCheck]:
    """Execute the installed codex gate twice: deny outside, allow inside.

    *outside* is the policy drill's fixture (already proven outside every
    root); when none exists the policy drill carries the explicit failure.
    """
    snapshot = install.policy_path
    if snapshot is None:
        return [DrillCheck("codex gate drill", False, "policy snapshot missing")]

    env = {k: str(v) for k, v in (install.env or {}).items()}
    checks: list[DrillCheck] = []
    if outside is not None:
        denied = _run_hook(snapshot, env, workspace, f'cat "{outside}"')
        checks.append(
            DrillCheck(
                "codex gate denies outside read",
                '"deny"' in denied,
                _short(denied),
            )
        )
    allowed = _run_hook(snapshot, env, workspace, "cat src/drill.txt")
    checks.append(
        DrillCheck(
            "codex gate allows cwd read",
            allowed.strip() == "",
            _short(allowed) or "no decision",
        )
    )
    return checks


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
