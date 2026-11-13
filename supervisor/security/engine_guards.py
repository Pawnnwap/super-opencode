"""Translate the access policy into each backend's native enforcement.

Every backend gets the *same* rule set, expressed in the mechanism that
backend actually honours before a tool runs:

===========  ==============================================================
opencode     a workspace plugin (dual entrypoint: opencode 2.x ``setup`` and
             opencode 1.x ``server``) that checks every tool call's paths and
             delegates shell commands to :mod:`supervisor.security.guard_cli`;
             the plugin loads its snapshot through a workspace pointer into
             the read-only store (the shared opencode server does not inherit
             the runner's env), and the V1 ``permission`` map stays the
             file-tool back stop.
pi           a project extension loaded with ``-e``; ``tool_call`` returns
             ``{block: true}`` for policy violations.
codex        a ``PreToolUse`` hook that runs :mod:`supervisor.security.guard_cli`,
             merged into whichever representation the codex home already uses
             (``config.toml`` ``[[hooks]]`` tables or ``hooks.json`` - codex
             rejects both at one layer); the run also passes
             ``--dangerously-bypass-hook-trust`` so the hook is active in
             non-interactive execution.
===========  ==============================================================

The artifacts are derived state: re-running an install rewrites them, and
disabling the policy removes exactly the pieces this module owns. The user's
own config keys, hooks and extensions are preserved.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from supervisor.security.access_policy import AccessPolicy

logger = logging.getLogger(__name__)

# Policy artifacts live OUTSIDE the agent's writable cwd: a hard rule the
# agent can edit (or delete) is not a hard rule. The store is keyed by the
# workspace and by the policy content, so parallel replicas of the same
# workspace with different whitelists never share a file.
POLICY_STORE_DIR = Path.home() / ".opencode_supervisor" / "access"
POLICY_FILE_PREFIX = "policy-"
VIOLATION_LOG_PREFIX = "violations-"
GUARD_MARKER = "supervisor-access-guard"
CODEX_HOOKS_BEGIN = "# >>> supervisor-access-guard (managed by super-opencode; do not edit) >>>"
CODEX_HOOKS_END = "# <<< supervisor-access-guard <<<"

OPENCODE_CONFIG_REL = Path(".opencode") / "opencode.json"
OPENCODE_PLUGIN_REL = Path(".opencode") / "plugin" / "supervisor_access_guard.js"
# Workspace pointer to the store snapshot. The plugin runs inside opencode's
# long-lived server, which does not inherit the runner's environment, so the
# snapshot must be findable without env - but never READ from the writable
# workspace itself, hence a pointer validated against the store root.
OPENCODE_POINTER_REL = Path(".supervisor") / "access_policy.json"
PI_EXTENSION_REL = Path(".pi") / "extensions" / "supervisor-access-guard.ts"


@dataclass
class GuardInstallResult:
    """What an install produced and which CLI flags the runner must add."""

    engine: str
    enabled: bool
    artifacts: list[Path] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    removed: list[Path] = field(default_factory=list)
    # True when the engine's native gate could not be installed at all; the
    # caller must refuse to start rather than run without the policy.
    failed: bool = False

    @property
    def policy_path(self) -> Path | None:
        for artifact in self.artifacts:
            if artifact.name.startswith(POLICY_FILE_PREFIX):
                return artifact
        return None


def access_store_dir(workspace: Path) -> Path:
    key = hashlib.sha1(
        str(Path(workspace).resolve()).lower().encode("utf-8"),
    ).hexdigest()[:16]
    return POLICY_STORE_DIR / key


def _policy_digest(policy: AccessPolicy) -> str:
    payload = json.dumps(policy.snapshot(), sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]


def policy_path_for(workspace: Path, policy: AccessPolicy | None = None) -> Path:
    """External policy snapshot path (never inside the agent's workspace)."""
    directory = access_store_dir(workspace)
    if policy is None:
        return directory / f"{POLICY_FILE_PREFIX}current.json"
    return directory / f"{POLICY_FILE_PREFIX}{_policy_digest(policy)}.json"


def violation_log_for(workspace: Path, policy: AccessPolicy | None = None) -> Path:
    directory = access_store_dir(workspace)
    if policy is None:
        return directory / f"{VIOLATION_LOG_PREFIX}current.jsonl"
    return directory / f"{VIOLATION_LOG_PREFIX}{_policy_digest(policy)}.jsonl"


def integrity_path_for(workspace: Path) -> Path:
    """Where the installed-artifact hashes live (outside the workspace)."""
    return access_store_dir(workspace) / "guard_integrity.json"


def _artifact_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def record_guard_integrity(workspace: Path, artifacts: list[Path]) -> Path:
    """Snapshot the sha256 of every installed gate artifact.

    The gate artifacts (the opencode plugin, ``opencode.json``, the pi
    extension, the codex hook) live in agent-writable directories. Without a
    recorded hash, an agent that rewrites one of them disables the native gate
    while ``preflight`` still sees "file exists and carries the marker".
    """
    path = integrity_path_for(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    digests = {
        str(Path(item).resolve()): _artifact_digest(Path(item))
        for item in artifacts
        if Path(item).is_file()
    }
    path.write_text(
        json.dumps({"artifacts": digests}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def verify_guard_integrity(workspace: Path) -> list[str]:
    """Differences between the recorded artifact hashes and the files now.

    Returns one human-readable line per problem; empty means intact. A missing
    record is itself a problem: an unverifiable gate is not a verified gate.
    """
    path = integrity_path_for(workspace)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return [f"no integrity record at {path}"]
    recorded = data.get("artifacts") if isinstance(data, dict) else None
    if not isinstance(recorded, dict) or not recorded:
        return [f"integrity record at {path} lists no artifacts"]
    problems: list[str] = []
    for raw_path, expected in sorted(recorded.items()):
        artifact = Path(raw_path)
        if not artifact.is_file():
            problems.append(f"{artifact.name}: missing")
            continue
        actual = _artifact_digest(artifact)
        if actual != expected:
            problems.append(f"{artifact.name}: content changed since install")
    return problems


def install_access_guard(
    workspace: Path,
    policy: AccessPolicy,
    engine: str,
    *,
    codex_home: Path | None = None,
) -> GuardInstallResult:
    """Install the policy artifacts for *engine* (or remove them if disabled)."""
    workspace = Path(workspace)
    engine = (engine or "opencode").strip().lower()
    result = GuardInstallResult(engine=engine, enabled=policy.enabled)

    if not policy.enabled:
        result.removed = remove_access_guard(workspace, engine, codex_home=codex_home)
        return result

    snapshot_path = _write_policy_snapshot(workspace, policy)
    result.artifacts.append(snapshot_path)
    log_path = violation_log_for(workspace, policy)

    if engine == "opencode":
        _install_opencode(workspace, policy, result, snapshot_path)
    elif engine == "pi":
        _install_pi(workspace, policy, result)
    elif engine == "codex":
        _install_codex(workspace, policy, result, codex_home=codex_home)
    else:
        result.warnings.append(f"unknown engine {engine!r}: no native gate installed")

    # All engines read the snapshot through this env var; it also lets the
    # codex hook stay inert outside supervised runs.
    result.env["SOP_ACCESS_POLICY"] = str(snapshot_path)
    result.env["SOP_ACCESS_LOG"] = str(log_path)
    # The JS gates shell out to this exact interpreter for command analysis.
    result.env["SOP_GUARD_PYTHON"] = sys.executable
    # The codex hook runs in a fresh interpreter: make the supervisor package
    # importable even when the app was not installed editable.
    package_root = Path(__file__).resolve().parents[2]
    inherited = os.environ.get("PYTHONPATH", "")
    result.env["PYTHONPATH"] = (
        str(package_root) + (os.pathsep + inherited if inherited else "")
    )
    # Record what was installed so a later drill can prove the artifacts were
    # not rewritten by the agent (they live in writable directories).
    record_guard_integrity(workspace, list(result.artifacts))
    return result


# --------------------------------------------------------------------------- #
# Shared snapshot
# --------------------------------------------------------------------------- #


def _write_policy_snapshot(workspace: Path, policy: AccessPolicy) -> Path:
    path = policy_path_for(workspace, policy)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = policy.snapshot()
    payload["generatedBy"] = "supervisor.security.engine_guards"
    payload["workspaceDisplay"] = str(policy.workspace)
    payload["whitelistDisplay"] = [str(item) for item in policy.whitelist]
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# opencode
# --------------------------------------------------------------------------- #


def _install_opencode(
    workspace: Path,
    policy: AccessPolicy,
    result: GuardInstallResult,
    snapshot_path: Path,
) -> None:
    config_path = workspace / OPENCODE_CONFIG_REL
    try:
        _merge_opencode_config(config_path, policy)
        result.artifacts.append(config_path)
    except (OSError, ValueError) as exc:
        result.warnings.append(f"opencode permissions not installed: {exc}")
        result.failed = True

    plugin_path = workspace / OPENCODE_PLUGIN_REL
    try:
        plugin_path.parent.mkdir(parents=True, exist_ok=True)
        plugin_path.write_text(_render_opencode_plugin(), encoding="utf-8")
        result.artifacts.append(plugin_path)
    except OSError as exc:
        result.warnings.append(f"opencode plugin not installed: {exc}")
        result.failed = True

    pointer_path = workspace / OPENCODE_POINTER_REL
    try:
        pointer_path.parent.mkdir(parents=True, exist_ok=True)
        pointer_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "store": str(snapshot_path),
                    "workspace": str(workspace),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        result.artifacts.append(pointer_path)
    except OSError as exc:
        result.warnings.append(f"opencode policy pointer not installed: {exc}")
        result.failed = True


def _render_opencode_plugin() -> str:
    """Embed install-time absolutes into the plugin template.

    The shared opencode server does not inherit the runner's environment, so
    the analyzer interpreter and the importable package root are written into
    the plugin itself (JSON-escaped for JS); the env overrides still win.
    """
    package_root = Path(__file__).resolve().parents[2]
    return (
        _OPENCODE_PLUGIN_JS
        .replace("__SOP_GUARD_PYTHON__", json.dumps(str(sys.executable)))
        .replace("__SOP_REPO_ROOT__", json.dumps(str(package_root)))
    )


def opencode_permission_config(policy: AccessPolicy) -> dict[str, dict[str, str]]:
    """V1 ``permission`` pattern maps for the installed opencode 1.x CLI.

    V1 accepts a per-action map of whole-path globs with ``allow``/``deny``:

    * ``external_directory`` denies every path outside the project, then
      re-allows each whitelist directory - this is the read boundary;
    * ``edit`` denies the whitelist directories, which keeps them read-only
      even when the same path is readable;
    * ``read`` explicitly allows the whitelist (harmless given the default
      allow, but it documents intent and survives stricter user defaults).

    The V2 ``permissions`` array is deliberately NOT written: opencode 1.x
    treats that key as a configuration error ("V2 permissions are not
    supported by OpenCode V1"), which would invalidate the whole file.
    """
    external: dict[str, str] = {"*": "deny"}
    edit: dict[str, str] = {}
    read: dict[str, str] = {}
    for entry in policy.whitelist:
        base = _oc_path(entry)
        external[f"{base}/*"] = "allow"
        read[f"{base}/*"] = "allow"
        edit[f"{base}/*"] = "deny"
        edit[base] = "deny"
    permission: dict[str, dict[str, str]] = {"external_directory": external}
    if edit:
        permission["edit"] = edit
    if read:
        permission["read"] = read
    return permission


def _oc_path(path: Path) -> str:
    return str(path).replace("\\", "/")


def _merge_opencode_config(config_path: Path, policy: AccessPolicy) -> None:
    config: dict[str, Any] = {}
    if config_path.exists():
        raw = config_path.read_text(encoding="utf-8").strip()
        if raw:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError(f"{config_path.name} is not a JSON object")
            config = data

    # A V2 rules array left by an earlier build makes opencode 1.x reject the
    # whole file; strip only entries this module generated.
    previous = _read_generated_rules(config_path)
    existing_v2 = config.get("permissions")
    if isinstance(existing_v2, list):
        kept_v2 = [
            rule for rule in existing_v2 if rule not in (previous.get("v2") or [])
        ]
        if kept_v2:
            config["permissions"] = kept_v2
        else:
            config.pop("permissions", None)

    # V1 permission: keep whatever the operator chose for unrelated tools,
    # restore the tool grants a non-interactive run needs, then layer the path
    # patterns this module owns.
    permission = config.get("permission")
    if not isinstance(permission, dict):
        permission = {}
    for tool in ("read", "edit", "write", "bash"):
        permission.setdefault(tool, "allow")
    _strip_v1_patterns(permission, previous.get("v1") or {})
    generated = opencode_permission_config(policy)
    for action, patterns in generated.items():
        current = permission.get(action)
        if not isinstance(current, dict):
            current = {}
        current.update(patterns)
        permission[action] = current
    config["permission"] = permission
    config.setdefault("autoapprove", True)

    _write_json_with_backup(config_path, config)
    _remember_generated_rules(
        config_path,
        {
            "v1": {
                action: sorted(patterns)
                for action, patterns in generated.items()
            },
        },
    )


def _rules_marker_path(config_path: Path) -> Path:
    return config_path.parent / ".supervisor_generated_permissions.json"


def _read_generated_rules(config_path: Path) -> dict[str, Any]:
    marker = _rules_marker_path(config_path)
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _strip_v1_patterns(permission: dict[str, Any], previous: dict[str, Any]) -> None:
    for action, patterns in previous.items():
        current = permission.get(action)
        if not isinstance(current, dict):
            continue
        for pattern in patterns or []:
            value = current.get(pattern)
            if value in ("allow", "deny"):
                current.pop(pattern, None)


def _remember_generated_rules(config_path: Path, rules: dict[str, Any]) -> None:
    marker = _rules_marker_path(config_path)
    try:
        marker.write_text(json.dumps(rules, indent=2) + "\n", encoding="utf-8")
    except OSError:
        logger.debug("could not write opencode rules marker", exc_info=True)


def _write_json_with_backup(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak-access")
        if not backup.exists():
            shutil.copyfile(path, backup)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# pi
# --------------------------------------------------------------------------- #


def _install_pi(workspace: Path, policy: AccessPolicy, result: GuardInstallResult) -> None:
    extension = workspace / PI_EXTENSION_REL
    try:
        extension.parent.mkdir(parents=True, exist_ok=True)
        extension.write_text(_PI_EXTENSION_TS, encoding="utf-8")
        result.artifacts.append(extension)
    except OSError as exc:
        result.warnings.append(f"pi extension not installed: {exc}")
        result.failed = True
        return
    result.flags.extend(["-e", str(extension)])
    # pi's project-local extension discovery needs project trust; an explicit
    # -e path does not. --no-extensions still permits explicit paths, so the
    # existing hygiene flags stay untouched.


# --------------------------------------------------------------------------- #
# codex
# --------------------------------------------------------------------------- #


def _install_codex(
    workspace: Path,
    policy: AccessPolicy,
    result: GuardInstallResult,
    *,
    codex_home: Path | None = None,
) -> None:
    home = Path(codex_home) if codex_home else default_codex_home()
    config_path = home / "config.toml"
    hooks_path = home / "hooks.json"
    try:
        if _toml_has_hooks(config_path):
            # codex rejects two hook representations in one layer; join the
            # one this home already uses instead of creating a second.
            _merge_codex_toml(config_path, _codex_hook_command())
            result.artifacts.append(config_path)
            _migrate_hooks_json(hooks_path, result)
        else:
            _merge_codex_hooks(hooks_path, workspace)
            result.artifacts.append(hooks_path)
    except (OSError, ValueError) as exc:
        result.warnings.append(f"codex hook not installed: {exc}")
        result.failed = True
        return

    # Non-interactive `codex exec` skips hooks that were never trusted. The
    # supervisor installs this hook itself and vets it by construction, so the
    # per-invocation bypass is the correct trust decision here.
    result.flags.append("--dangerously-bypass-hook-trust")


def _toml_has_hooks(config_path: Path) -> bool:
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        import tomllib

        return bool(tomllib.loads(text).get("hooks"))
    except ImportError:
        return bool(re.search(r"^\s*\[\[hooks\.", text, re.MULTILINE))
    except ValueError:
        # Unparseable TOML: fall back to the surface shape so we still join
        # the representation instead of adding a conflicting second one.
        return bool(re.search(r"^\s*\[\[hooks\.", text, re.MULTILINE))


def _toml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _managed_toml_block(command: str) -> str:
    return (
        f"{CODEX_HOOKS_BEGIN}\n"
        "[[hooks.PreToolUse]]\n"
        "hooks = [\n"
        f'  {{ type = "command", command = {_toml_quote(command)}, timeout = 20, statusMessage = "{GUARD_MARKER}" }},\n'
        "]\n"
        f"{CODEX_HOOKS_END}\n"
    )


def _strip_managed_toml(text: str) -> str:
    pattern = re.compile(
        re.escape(CODEX_HOOKS_BEGIN) + r".*?" + re.escape(CODEX_HOOKS_END) + r"\n?",
        re.DOTALL,
    )
    return pattern.sub("", text)


def _merge_codex_toml(config_path: Path, command: str) -> None:
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    cleaned = _strip_managed_toml(text).rstrip("\n")
    if config_path.exists():
        backup = config_path.parent / (config_path.name + ".bak-supervisor")
        if not backup.exists():
            shutil.copyfile(config_path, backup)
    payload = (cleaned + "\n\n" if cleaned else "") + _managed_toml_block(command)
    tmp = config_path.parent / (config_path.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, config_path)


def _migrate_hooks_json(hooks_path: Path, result: GuardInstallResult) -> None:
    """Drop our hooks.json entry when the home uses TOML hooks.

    A hooks.json holding only our entry is supervisor-created and safe to
    delete; foreign entries stay, and the remaining dual representation is
    the operator's to resolve (warned, not silently rewritten).
    """
    if not hooks_path.exists():
        return
    try:
        data = json.loads(hooks_path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return
    entries = hooks.get("PreToolUse") or []
    foreign = [entry for entry in entries if not _is_our_hook_entry(entry)]
    other_events = [key for key, value in hooks.items() if key != "PreToolUse" and value]
    if foreign or other_events:
        _strip_codex_hook(hooks_path)
        result.warnings.append(
            f"{hooks_path} holds non-supervisor hooks alongside config.toml hooks; "
            "codex prefers a single representation per layer"
        )
        return
    backup = hooks_path.parent / (hooks_path.name + ".bak-supervisor")
    if not backup.exists():
        shutil.copyfile(hooks_path, backup)
    hooks_path.unlink()
    result.removed.append(hooks_path)


def default_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _codex_hook_command() -> str:
    args = "-m supervisor.security.guard_cli --policy-env SOP_ACCESS_POLICY --log-env SOP_ACCESS_LOG"
    exe = sys.executable
    if os.name != "nt":
        return f'"{exe}" {args}'

    # Windows hook commands are shell strings. An unquoted executable path is
    # executed by both cmd.exe and the default CLI shell, while a quoted path
    # needs a leading call operator that cmd.exe rejects. Prefer the short
    # (8.3) form when the interpreter path contains spaces, and keep the call
    # operator only as a last resort.
    if " " not in exe:
        return f"{exe} {args}"
    short = _windows_short_path(exe)
    if short and " " not in short:
        return f"{short} {args}"
    return f'& "{exe}" {args}'


def _windows_short_path(path: str) -> str:
    """Return the 8.3 form of *path* (empty when the volume has none)."""
    try:
        import ctypes

        get_short = ctypes.windll.kernel32.GetShortPathNameW
        needed = get_short(path, None, 0)
        if not needed:
            return ""
        buffer = ctypes.create_unicode_buffer(needed)
        if not get_short(path, buffer, needed):
            return ""
        return buffer.value
    except (AttributeError, OSError):
        return ""


def _is_our_hook_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    for handler in entry.get("hooks") or []:
        if isinstance(handler, dict) and (
            GUARD_MARKER in str(handler.get("command", ""))
            or "supervisor.security.guard_cli" in str(handler.get("command", ""))
        ):
            return True
    return False


def _merge_codex_hooks(hooks_path: Path, workspace: Path) -> None:
    data: dict[str, Any] = {}
    if hooks_path.exists():
        raw = hooks_path.read_text(encoding="utf-8").strip()
        if raw:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("hooks.json is not a JSON object")
            data = parsed

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}

    entries = hooks.get("PreToolUse")
    if not isinstance(entries, list):
        entries = []

    kept = [entry for entry in entries if not _is_our_hook_entry(entry)]
    kept.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": _codex_hook_command(),
                    "timeout": 20,
                    "statusMessage": GUARD_MARKER,
                },
            ],
        },
    )
    hooks["PreToolUse"] = kept
    data["hooks"] = hooks

    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    if hooks_path.exists():
        backup = hooks_path.with_suffix(".json.bak-supervisor")
        if not backup.exists():
            shutil.copyfile(hooks_path, backup)
    tmp = hooks_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, hooks_path)


# --------------------------------------------------------------------------- #
# Removal
# --------------------------------------------------------------------------- #


def remove_access_guard(
    workspace: Path,
    engine: str,
    *,
    codex_home: Path | None = None,
) -> list[Path]:
    """Remove the artifacts this module owns; leave everything else alone."""
    workspace = Path(workspace)
    engine = (engine or "").strip().lower()
    removed: list[Path] = []

    if engine == "opencode":
        config_path = workspace / OPENCODE_CONFIG_REL
        try:
            if config_path.exists():
                _strip_opencode_rules(config_path)
        except (OSError, ValueError):
            logger.debug("could not strip opencode permissions", exc_info=True)
        pointer = workspace / OPENCODE_POINTER_REL
        try:
            if pointer.is_file():
                data = json.loads(pointer.read_text(encoding="utf-8"))
                # Only our pointer shape (a redirect, not a full snapshot) is
                # ours to remove; a user's own snapshot file stays.
                if isinstance(data, dict) and data.get("store") and "roots" not in data:
                    pointer.unlink()
                    removed.append(pointer)
        except (OSError, ValueError, json.JSONDecodeError):
            logger.debug("could not remove opencode policy pointer", exc_info=True)

    for snapshot in access_store_dir(workspace).glob(f"{POLICY_FILE_PREFIX}*.json"):
        try:
            snapshot.unlink()
            removed.append(snapshot)
        except OSError:
            logger.debug("could not remove policy snapshot %s", snapshot, exc_info=True)
    marker = (workspace / OPENCODE_CONFIG_REL).parent / ".supervisor_generated_permissions.json"
    if marker.exists():
        marker.unlink()
        removed.append(marker)

    plugin = workspace / OPENCODE_PLUGIN_REL
    if engine == "opencode" and _is_our_text(plugin, "supervisor_access_guard"):
        plugin.unlink()
        removed.append(plugin)

    extension = workspace / PI_EXTENSION_REL
    if engine == "pi" and _is_our_text(extension, GUARD_MARKER):
        extension.unlink()
        removed.append(extension)

    if engine == "codex":
        home = Path(codex_home) if codex_home else default_codex_home()
        config_path = home / "config.toml"
        try:
            if config_path.exists():
                text = config_path.read_text(encoding="utf-8")
                cleaned = _strip_managed_toml(text)
                if cleaned != text:
                    cleaned = cleaned.rstrip("\n")
                    tmp = config_path.parent / (config_path.name + ".tmp")
                    tmp.write_text(cleaned + "\n" if cleaned else "", encoding="utf-8")
                    os.replace(tmp, config_path)
                    removed.append(config_path)
        except (OSError, ValueError):
            logger.debug("could not strip codex TOML hook", exc_info=True)
        hooks_path = home / "hooks.json"
        try:
            if hooks_path.exists():
                _strip_codex_hook(hooks_path)
        except (OSError, ValueError):
            logger.debug("could not strip codex hook", exc_info=True)

    return removed


def _is_our_text(path: Path, marker: str) -> bool:
    try:
        return path.is_file() and marker in path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def _strip_opencode_rules(config_path: Path) -> None:
    data = json.loads(config_path.read_text(encoding="utf-8") or "{}")
    if not isinstance(data, dict):
        return
    previous = _read_generated_rules(config_path)
    permission = data.get("permission")
    if isinstance(permission, dict):
        _strip_v1_patterns(permission, previous.get("v1") or {})
    rules = data.get("permissions")
    if isinstance(rules, list):
        data["permissions"] = [
            rule for rule in rules if rule not in (previous.get("v2") or [])
        ]
    _write_json_with_backup(config_path, data)


def _strip_codex_hook(hooks_path: Path) -> None:
    data = json.loads(hooks_path.read_text(encoding="utf-8") or "{}")
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return
    entries = hooks.get("PreToolUse")
    if not isinstance(entries, list):
        return
    hooks["PreToolUse"] = [entry for entry in entries if not _is_our_hook_entry(entry)]
    hooks_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #

_OPENCODE_PLUGIN_JS = r"""
/**
 * supervisor-access-guard (supervisor_access_guard.js) - installed by
 * opencode-supervisor.
 *
 * Hard path gate for every opencode tool call. File paths are checked here;
 * shell commands are delegated to the supervisor's Python analyzer
 * (`supervisor.security.guard_cli`) so all engines share one parser and one
 * rule set.
 *
 * A missing policy, a failed analyzer or a timeout denies the call: the gate
 * fails closed. Dual entrypoint, no SDK import (loads without node_modules):
 * opencode 2.x default-exports the Plugin.define-shaped object and runs
 * `setup()`; opencode 1.x (>= 1.18.29) calls `server()` for the classic
 * hooks object.
 *
 * The policy snapshot lives in the supervisor's read-only store
 * (~/.opencode_supervisor/access), never in the agent-writable workspace:
 * the workspace only holds a pointer file (`.supervisor/access_policy.json`
 * -> store path). A pointer is honored only when it resolves inside the
 * store AND the snapshot owns this workspace (a rw root equal to this
 * directory), so a workspace-writable file can never inject, redirect or
 * borrow a policy. The V1 `permission` config map stays the file-tool back
 * stop: opencode 2.2.x still honors `permission.external_directory`.
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";

// Install-time absolutes: the shared background service does not inherit the
// runner's environment, so the interpreter and importable package root are
// embedded when the plugin is written (env overrides still win).
const DEFAULT_GUARD_PYTHON = __SOP_GUARD_PYTHON__;
const DEFAULT_REPO_ROOT = __SOP_REPO_ROOT__;

const POINTER_REL = path.join(".supervisor", "access_policy.json");

function realResolve(target) {
  let current = path.resolve(target);
  const tail = [];
  for (let i = 0; i < 64; i += 1) {
    if (fs.existsSync(current)) break;
    const parent = path.dirname(current);
    if (parent === current) return path.resolve(target);
    tail.unshift(path.basename(current));
    current = parent;
  }
  try {
    current = fs.realpathSync.native(current);
  } catch (_) {
    /* keep the lexical path */
  }
  return tail.length ? path.join(current, ...tail) : current;
}

const IS_WIN = process.platform === "win32";

function norm(p) {
  if (!IS_WIN) return p.replace(/\/+$/, "") || "/";
  return p.replace(/[\\/]+/g, "\\").replace(/\\$/, "").toLowerCase();
}

function within(child, root) {
  const c = norm(child);
  const r = norm(root);
  return c === r || c.startsWith(r + (IS_WIN ? "\\" : "/"));
}

function storeRoot() {
  return realResolve(path.join(os.homedir(), ".opencode_supervisor", "access"));
}

function miss(reason) {
  return {
    missing: true,
    missingReason: reason,
    snapshot: null,
    policyPath: "",
    logPath: "",
  };
}

function resolvePolicy(directory) {
  const root = storeRoot();
  const candidates = [];
  const envPath = process.env.SOP_ACCESS_POLICY || "";
  if (envPath) candidates.push({ file: envPath, trustedFull: true });
  for (const base of [process.cwd(), directory]) {
    if (base) candidates.push({ file: path.join(String(base), POINTER_REL), trustedFull: false });
  }
  for (const candidate of candidates) {
    let raw;
    try {
      raw = JSON.parse(fs.readFileSync(candidate.file, "utf8"));
    } catch (_) {
      continue;
    }
    if (!raw || typeof raw !== "object") continue;
    let snapshotFile = candidate.file;
    if (!candidate.trustedFull) {
      // Workspace files are agent-writable: only a POINTER into the store is
      // honored, never a snapshot sitting in the workspace itself.
      if (typeof raw.store !== "string" || !raw.store || raw.roots) continue;
      snapshotFile = realResolve(raw.store);
      if (!within(snapshotFile, root)) {
        return miss("policy pointer points outside the supervisor store: " + raw.store);
      }
      try {
        raw = JSON.parse(fs.readFileSync(snapshotFile, "utf8"));
      } catch (err) {
        return miss("policy snapshot unreadable: " + String(err).slice(0, 80));
      }
      if (!raw || typeof raw !== "object" || !Array.isArray(raw.roots)) {
        return miss("policy snapshot malformed: " + snapshotFile);
      }
    } else if (!Array.isArray(raw.roots)) {
      continue;
    }
    // The snapshot must own this directory (matching rw root), so project A
    // cannot borrow project B's wider boundary through its own pointer.
    const here = norm(realResolve(directory || process.cwd()));
    const owns = (raw.roots || []).some(
      (entry) =>
        entry && entry.mode === "rw" && norm(String(entry.path || "")) === here,
    );
    if (!owns) {
      return miss("policy snapshot does not match this workspace: " + snapshotFile);
    }
    const logFile = path.join(
      path.dirname(snapshotFile),
      path.basename(snapshotFile)
        .replace(/^policy-/, "violations-")
        .replace(/\.json$/, ".jsonl"),
    );
    return {
      missing: false,
      missingReason: "",
      snapshot: raw,
      policyPath: snapshotFile,
      logPath: logFile,
    };
  }
  return miss("no policy pointer in workspace (failing closed)");
}

function check(resolved, rawPath, access, base) {
  if (resolved.missing) {
    return resolved.missingReason || "access policy snapshot missing (failing closed)";
  }
  const policy = resolved.snapshot;
  if (!policy) return "access policy not loaded";
  if (policy.enabled === false) return null;
  let text = String(rawPath || "").trim();
  if (!text) return "empty path";
  if (text.startsWith("\\\\") || text.startsWith("\\\\.\\")) return "UNC or device paths are not allowed";
  if (/^[A-Za-z]:[^\\/]/.test(text)) return "drive-relative paths are ambiguous";
  text = text
    .replace(/^~/, process.env.USERPROFILE || process.env.HOME || "~")
    .replace(/%USERPROFILE%/gi, process.env.USERPROFILE || "")
    .replace(/\$\{?HOME\}?/gi, process.env.HOME || process.env.USERPROFILE || "");
  const target = path.isAbsolute(text) ? text : path.resolve(base || process.cwd(), text);
  let resolvedPath;
  try {
    resolvedPath = realResolve(target);
  } catch (err) {
    return "unresolvable path: " + String(err);
  }
  for (const root of policy.roots || []) {
    if (!within(resolvedPath, root.path)) continue;
    if (root.mode === "rw") return null;
    return access === "write" ? "whitelist directories are read-only" : null;
  }
  return "outside workspace and whitelist";
}

/**
 * Delegate a shell command to the single authoritative parser. The analyzer
 * rejects dynamic constructs (substitutions, unknown variables, encoded
 * commands, wrappers it cannot unwrap), so an unparseable command is denied
 * rather than guessed at.
 */
function scanCommand(command, base, resolved) {
  if (!command) return null;
  if (resolved.missing) {
    return resolved.missingReason || "access policy snapshot missing (failing closed)";
  }
  const python = process.env.SOP_GUARD_PYTHON || DEFAULT_GUARD_PYTHON;
  const childEnv = { ...process.env };
  if (!childEnv.PYTHONPATH || !childEnv.PYTHONPATH.includes(DEFAULT_REPO_ROOT)) {
    childEnv.PYTHONPATH = DEFAULT_REPO_ROOT
      + (childEnv.PYTHONPATH ? path.delimiter + childEnv.PYTHONPATH : "");
  }
  try {
    const output = execFileSync(
      python,
      [
        "-m",
        "supervisor.security.guard_cli",
        "--policy",
        resolved.policyPath,
        "--log",
        resolved.logPath,
        "--check-command",
      ],
      {
        input: JSON.stringify({ command: String(command), cwd: base || process.cwd() }),
        encoding: "utf8",
        timeout: 8000,
        env: childEnv,
        windowsHide: true,
      },
    );
    const result = JSON.parse(output);
    return result.allow ? null : result.reason || "denied by the access policy";
  } catch (err) {
    return "shell analyzer unavailable (failing closed): " + String(err).slice(0, 160);
  }
}

function toolPaths(args) {
  const paths = [];
  if (!args || typeof args !== "object") return paths;
  for (const key of ["filePath", "file_path", "filepath", "path", "target", "destination", "codebase_path"]) {
    if (typeof args[key] === "string") paths.push(args[key]);
  }
  if (Array.isArray(args.paths)) paths.push(...args.paths.filter((p) => typeof p === "string"));
  return paths;
}

/**
 * Run control files (feasibility_facts.json, access_protection.json, ...) sit
 * in the writable workspace root, so the path policy alone lets an agent
 * rewrite them. The facts file's check.cmd is executed by the harness, which
 * makes such a write a propose→execute channel. Delegate to the Python gate so
 * both native gates share one rule instead of re-implementing it here.
 */
function protectedReason(candidate, base) {
  const python = process.env.SOP_GUARD_PYTHON || DEFAULT_GUARD_PYTHON;
  const childEnv = { ...process.env };
  if (!childEnv.PYTHONPATH || !childEnv.PYTHONPATH.includes(DEFAULT_REPO_ROOT)) {
    childEnv.PYTHONPATH = DEFAULT_REPO_ROOT
      + (childEnv.PYTHONPATH ? path.delimiter + childEnv.PYTHONPATH : "");
  }
  try {
    const output = execFileSync(
      python,
      ["-m", "supervisor.security.guard_cli", "--check-protected"],
      {
        input: JSON.stringify({ path: String(candidate), workspace: String(base || "") }),
        encoding: "utf8",
        timeout: 8000,
        env: childEnv,
        windowsHide: true,
      },
    );
    const result = JSON.parse(output);
    return result.protected ? (result.reason || "run control file") : null;
  } catch (err) {
    return "protected-file check unavailable (failing closed): " + String(err).slice(0, 160);
  }
}

function gateToolCall(tool, args, resolved, base) {
  if (tool === "bash" || tool === "shell" || tool === "execute") {
    const reason = scanCommand(String(args.command || args.cmd || ""), base, resolved);
    if (reason) {
      throw new Error("Blocked by supervisor access policy (shell): " + reason);
    }
    return;
  }

  const access = ["write", "edit", "patch", "multiedit", "multi_edit"].includes(tool)
    ? "write"
    : "read";
  for (const candidate of toolPaths(args)) {
    const reason = check(resolved, candidate, access, base);
    if (reason) {
      throw new Error(
        "Blocked by supervisor access policy: " + candidate + " (" + reason + ")",
      );
    }
    if (access === "write") {
      const guarded = protectedReason(candidate, base);
      if (guarded) {
        throw new Error(
          "Blocked by supervisor access policy: " + candidate + " (" + guarded + ")",
        );
      }
    }
  }
  if (tool === "patch" && typeof args.patchText === "string") {
    for (const line of args.patchText.split("\n")) {
      const match = line.match(/^\*\*\* .* File: (.+)$/);
      if (!match) continue;
      const reason = check(resolved, match[1].trim(), "write", base);
      if (reason) {
        throw new Error(
          "Blocked by supervisor access policy: " + match[1].trim() + " (" + reason + ")",
        );
      }
    }
  }
}

export default {
  id: "supervisor.access-guard",
  async setup(ctx) {
    const directory = ctx?.location?.directory || ctx?.location?.project?.path || process.cwd();
    const resolved = resolvePolicy(directory);
    const base = directory || process.cwd();
    await ctx.tool.hook("execute.before", (event) => {
      gateToolCall(String(event?.tool || "").toLowerCase(), event?.input || {}, resolved, base);
    });
  },
  async server(context) {
    const directory = context?.directory || context?.worktree || process.cwd();
    const resolved = resolvePolicy(directory);
    const base = directory || process.cwd();
    return {
      "tool.execute.before": async (input, output) => {
        gateToolCall(String(input?.tool || "").toLowerCase(), output?.args || {}, resolved, base);
      },
    };
  },
};
"""

_PI_EXTENSION_TS = r"""/**
 * supervisor-access-guard - installed by opencode-supervisor.
 *
 * pi loads this with an explicit `-e <path>`. File paths are checked in
 * process; shell commands are delegated to the supervisor's Python analyzer so
 * every engine shares one parser. Any analyzer failure denies the call.
 */
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";

type Policy = {
  enabled?: boolean;
  missing?: boolean;
  roots?: Array<{ path: string; mode: "rw" | "ro" }>;
};

let cached: Policy | null = null;
let cachedPath = "";

function loadPolicy(cwd: string): Policy {
  const candidates = [
    process.env.SOP_ACCESS_POLICY,
    path.join(cwd || process.cwd(), ".supervisor", "access_policy.json"),
  ].filter(Boolean) as string[];
  for (const candidate of candidates) {
    if (cached && cachedPath === candidate) return cached;
    try {
      cached = JSON.parse(fs.readFileSync(candidate, "utf8")) as Policy;
      cachedPath = candidate;
      return cached;
    } catch (_) {
      /* next */
    }
  }
  return { enabled: true, roots: [], missing: true };
}

function realResolve(target: string): string {
  let current = path.resolve(target);
  const tail: string[] = [];
  for (let i = 0; i < 64; i += 1) {
    if (fs.existsSync(current)) break;
    const parent = path.dirname(current);
    if (parent === current) return path.resolve(target);
    tail.unshift(path.basename(current));
    current = parent;
  }
  try {
    current = fs.realpathSync.native(current);
  } catch (_) {
    /* keep lexical */
  }
  return tail.length ? path.join(current, ...tail) : current;
}

const IS_WIN = process.platform === "win32";

function norm(p: string): string {
  if (!IS_WIN) return p.replace(/\/+$/, "") || "/";
  return p.replace(/[\\/]+/g, "\\").replace(/\\$/, "").toLowerCase();
}

function within(child: string, root: string): boolean {
  const c = norm(child);
  const r = norm(root);
  return c === r || c.startsWith(r + (IS_WIN ? "\\" : "/"));
}

function check(
  policy: Policy,
  rawPath: string,
  access: "read" | "write",
  base: string,
): string | null {
  if (policy.enabled === false) return null;
  if (policy.missing) return "access policy snapshot missing (failing closed)";
  let text = String(rawPath || "").trim();
  if (!text) return "empty path";
  if (text.startsWith("\\\\") || text.startsWith("\\\\.\\")) return "UNC or device paths are not allowed";
  if (/^[A-Za-z]:[^\\/]/.test(text)) return "drive-relative paths are ambiguous";
  text = text
    .replace(/^~/, process.env.USERPROFILE || process.env.HOME || "~")
    .replace(/%USERPROFILE%/gi, process.env.USERPROFILE || "")
    .replace(/\$\{?HOME\}?/gi, process.env.HOME || process.env.USERPROFILE || "");
  const target = path.isAbsolute(text) ? text : path.resolve(base || process.cwd(), text);
  let resolved: string;
  try {
    resolved = realResolve(target);
  } catch (err) {
    return "unresolvable path: " + String(err);
  }
  for (const root of policy.roots || []) {
    if (!within(resolved, root.path)) continue;
    if (root.mode === "rw") return null;
    return access === "write" ? "whitelist directories are read-only" : null;
  }
  return "outside workspace and whitelist";
}

function scanCommand(command: string, base: string): string | null {
  if (!command) return null;
  if (!process.env.SOP_ACCESS_POLICY) {
    return "access policy snapshot missing (failing closed)";
  }
  const python = process.env.SOP_GUARD_PYTHON || "python";
  try {
    const output = execFileSync(
      python,
      [
        "-m",
        "supervisor.security.guard_cli",
        "--policy-env",
        "SOP_ACCESS_POLICY",
        "--log-env",
        "SOP_ACCESS_LOG",
        "--check-command",
      ],
      {
        input: JSON.stringify({ command, cwd: base || process.cwd() }),
        encoding: "utf8",
        timeout: 8000,
        env: process.env,
        windowsHide: true,
      },
    );
    const result = JSON.parse(output);
    return result.allow ? null : result.reason || "denied by the access policy";
  } catch (err) {
    return "shell analyzer unavailable (failing closed): " + String(err).slice(0, 160);
  }
}

function blockReason(policy: Policy, event: any, cwd: string): string | null {
  const tool = String(event?.toolName || "").toLowerCase();
  const input = event?.input || {};
  if (tool === "bash" || tool === "shell" || tool === "execute") {
    return scanCommand(String(input.command || input.cmd || ""), cwd);
  }
  const access: "read" | "write" = ["write", "edit", "patch", "multiedit"].includes(tool)
    ? "write"
    : "read";
  const candidates: string[] = [];
  for (const key of ["path", "filePath", "file_path", "filepath", "target", "destination"]) {
    if (typeof input[key] === "string") candidates.push(input[key]);
  }
  if (Array.isArray(input.paths)) {
    candidates.push(...input.paths.filter((p: unknown) => typeof p === "string"));
  }
  for (const candidate of candidates) {
    const reason = check(policy, candidate, access, cwd);
    if (reason) return candidate + ": " + reason;
  }
  if (tool === "patch" && typeof input.patchText === "string") {
    for (const line of input.patchText.split("\n")) {
      const match = line.match(/^\*\*\* .* File: (.+)$/);
      if (!match) continue;
      const reason = check(policy, match[1].trim(), "write", cwd);
      if (reason) return match[1].trim() + ": " + reason;
    }
  }
  return null;
}

export default function (pi: any) {
  pi.on("tool_call", async (event: any, ctx: any) => {
    const cwd = ctx?.cwd || process.cwd();
    const policy = loadPolicy(cwd);
    const reason = blockReason(policy, event, cwd);
    if (reason) {
      return { block: true, reason: "Blocked by supervisor access policy: " + reason };
    }
    return undefined;
  });
}
"""


__all__ = [
    "GUARD_MARKER",
    "POLICY_FILE_PREFIX",
    "VIOLATION_LOG_PREFIX",
    "GuardInstallResult",
    "access_store_dir",
    "default_codex_home",
    "install_access_guard",
    "opencode_permission_config",
    "policy_path_for",
    "remove_access_guard",
    "violation_log_for",
]
