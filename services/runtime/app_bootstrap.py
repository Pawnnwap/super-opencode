from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

UPGRADE_SETTINGS_FILE = Path.home() / ".opencode_supervisor_settings.json"

# Default npm registry for CLI auto-upgrades: direct npmjs access is
# region-blocked for some users, so upgrades route through a mirror by
# default. Set "npm_registry": "" in the settings file to use npm's own
# default registry instead.
DEFAULT_NPM_REGISTRY = "https://registry.npmmirror.com"


def get_npm_registry(settings_file: Path = UPGRADE_SETTINGS_FILE) -> str:
    """Resolve the npm registry for upgrades ('' -> npm default registry)."""
    try:
        if settings_file.exists():
            cfg = json.loads(settings_file.read_text(encoding="utf-8"))
            value = cfg.get("npm_registry")
            if value is not None:
                return str(value).strip() or ""
    except Exception:  # noqa: BLE001, S110 - unreadable settings fall back to default
        pass
    return DEFAULT_NPM_REGISTRY


def _npm_install_cmd(package: str, registry: str) -> str:
    cmd = f"npm install -g {package}@latest"
    if registry:
        cmd += f" --registry {registry}"
    return cmd


def _installed_cli(locator_name: str) -> str:
    """Locate an engine CLI exactly the way the runners do ('' when absent)."""
    try:
        module = importlib.import_module(
            f"supervisor.runners.{locator_name}_support.locator",
        )
        return getattr(module, f"find_{locator_name}")()
    except FileNotFoundError:
        return ""
    except Exception:  # noqa: BLE001 - startup upgrade step must never block boot
        return ""


def _installed_codex() -> str:
    """Locate a working codex CLI exactly the way the runners do."""
    return _installed_cli("codex")


def should_skip_upgrade(settings_file: Path = UPGRADE_SETTINGS_FILE) -> bool:
    if os.environ.get("OPENCODE_SKIP_UPGRADE") == "1":
        return True
    try:
        if settings_file.exists():
            cfg = json.loads(settings_file.read_text(encoding="utf-8"))
            if cfg.get("skip_upgrade"):
                return True
    except Exception:  # noqa: BLE001, S110 - unreadable settings do not skip upgrades
        pass
    return False


def _auto_upgrade_npm_cli(
    package: str,
    label: str,
    settings_file: Path = UPGRADE_SETTINGS_FILE,
    registry: str | None = None,
) -> None:
    """`npm install -g <package>@latest` with the shared skip/registry/mirror logic.

    Shared by the opencode and pi CLI upgrades (plain npm packages, no config
    to protect); codex keeps its own routine because it guards ~/.codex.

    Idempotent like the codex path: a CLI that already resolves is left alone.
    Every UI restart used to run a global npm install; when the process was
    tree-killed mid install (2026-11-06) npm left the package unpacked but
    WITHOUT bin shims, and every queued run then failed with "not found". The
    install is a repair path, not a startup step.
    """
    if should_skip_upgrade(settings_file):
        print(
            f"[{label}-upgrade] Skipping upgrade: disabled via config/env var",
            file=sys.stderr,
        )
        return

    installed = _installed_cli(label)
    if installed:
        print(
            f"[{label}-upgrade] {label} already available at {installed}; "
            "skipping auto-upgrade.",
            file=sys.stderr,
        )
        return

    try:
        home_dir = str(Path.home())
        if registry is None:
            registry = get_npm_registry(settings_file)

        # npm everywhere (with registry mirror support); the old curl-based
        # installer cannot honor a mirror and is the access-restricted path.
        cmd = _npm_install_cmd(package, registry or "")
        print(f"[{label}-upgrade] Running: {cmd}", file=sys.stderr)

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=home_dir,
            shell=True,
        )
        stdout, stderr = proc.communicate(timeout=180)
        if stdout:
            print(f"[{label}-upgrade] stdout: {stdout.strip()}", file=sys.stderr)
        if stderr:
            print(f"[{label}-upgrade] stderr: {stderr.strip()}", file=sys.stderr)

        code_msg = (
            "successfully"
            if proc.returncode == 0
            else f"with code {proc.returncode}. Continuing startup."
        )
        print(
            f"[{label}-upgrade] Upgrade completed {code_msg}.", file=sys.stderr
        )
    except subprocess.TimeoutExpired:
        print(
            f"[{label}-upgrade] Upgrade timed out after 180 seconds. Continuing startup.",
            file=sys.stderr,
        )
    except FileNotFoundError:
        print(
            f"[{label}-upgrade] npm not found — install Node.js to enable auto-upgrade. Continuing startup.",
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001 - startup upgrade step must never block boot
        print(
            f"[{label}-upgrade] Unexpected error: {exc}. Continuing startup.",
            file=sys.stderr,
        )


def auto_upgrade_opencode(
    settings_file: Path = UPGRADE_SETTINGS_FILE,
    registry: str | None = None,
) -> None:
    _auto_upgrade_npm_cli("opencode-ai", "opencode", settings_file, registry)


def auto_upgrade_pi(
    settings_file: Path = UPGRADE_SETTINGS_FILE,
    registry: str | None = None,
) -> None:
    _auto_upgrade_npm_cli(
        "@earendil-works/pi-coding-agent", "pi", settings_file, registry
    )


def _is_root() -> bool:
    """True when already running with root privileges (sudo would be a no-op)."""
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid and geteuid() == 0)


def _sudo_available_noninteractive() -> bool:
    """True when sudo can run without prompting (cached creds or NOPASSWD).

    Uses `sudo -n` so a missing/expired credential fails fast instead of
    blocking startup on a hidden password prompt.
    """
    try:
        proc = subprocess.run(
            ["sudo", "-n", "true"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        return proc.returncode == 0
    except Exception:  # noqa: BLE001 - startup upgrade step must never block boot
        return False


def _is_permission_error(stderr: str) -> bool:
    s = (stderr or "").lower()
    return any(tok in s for tok in ("eacces", "eperm", "permission denied"))


def _run_codex_upgrade(cmd: str, home_dir: str) -> tuple[int, str, str]:
    """Run one upgrade command, echo its output, return (code, stdout, stderr)."""
    print(f"[codex-upgrade] Running: {cmd}", file=sys.stderr)
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=home_dir,
        shell=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=180)
    except subprocess.TimeoutExpired:
        # Ownership gate: this is our own direct child, which is the one
        # provable case without a run ledger (see process_control). A process
        # object with no pid has no OS identity to prove, so only its own
        # kill hook may run.
        pid = getattr(proc, "pid", None)
        if pid is None:
            proc.kill()
        else:
            from supervisor.runtime.process_control import terminate_tree

            terminate_tree(None, int(pid), reason="codex upgrade timeout")
        proc.communicate()
        raise
    if stdout:
        print(f"[codex-upgrade] stdout: {stdout.strip()}", file=sys.stderr)
    if stderr:
        print(f"[codex-upgrade] stderr: {stderr.strip()}", file=sys.stderr)
    return proc.returncode, stdout or "", stderr or ""


def _restore_codex_config(config_path: Path, original: bytes, mode: int) -> None:
    """Restore an existing Codex config if an upgrade changed it."""
    try:
        if config_path.read_bytes() == original:
            return
    except OSError:
        pass

    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{config_path.name}.",
        dir=config_path.parent,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(original)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, config_path)
        print(
            f"[codex-upgrade] Restored user config changed during upgrade: {config_path}",
            file=sys.stderr,
        )
    finally:
        tmp_path.unlink(missing_ok=True)


def auto_upgrade_codex(
    settings_file: Path = UPGRADE_SETTINGS_FILE,
    registry: str | None = None,
) -> None:
    if should_skip_upgrade(settings_file):
        print(
            "[codex-upgrade] Skipping upgrade: disabled via config/env var",
            file=sys.stderr,
        )
        return

    # Never reinstall a CLI that already resolves. Every UI restart used to
    # run a global npm install; when the watcher tree-killed the app mid
    # install (2026-11-06) npm left the package unpacked but WITHOUT bin
    # shims, and every queued run then failed with "codex not found". The
    # install is now a repair path: run it only when the runner locator
    # cannot find codex at all.
    installed = _installed_codex()
    if installed:
        print(
            f"[codex-upgrade] codex already available at {installed}; "
            "skipping auto-upgrade.",
            file=sys.stderr,
        )
        return

    config_path = Path.home() / ".codex" / "config.toml"
    try:
        original_config = config_path.read_bytes()
        original_mode = config_path.stat().st_mode & 0o7777
    except FileNotFoundError:
        original_config = None
        original_mode = 0
    except OSError as exc:
        print(
            f"[codex-upgrade] Cannot protect existing config {config_path}: {exc}. "
            "Skipping automatic Codex upgrade.",
            file=sys.stderr,
        )
        return

    try:
        home_dir = str(Path.home())

        # Codex CLI ships via npm (@openai/codex) on every platform, so a single
        # npm command covers Windows/macOS/Linux — no per-OS install script.
        if registry is None:
            registry = get_npm_registry(settings_file)
        base_cmd = _npm_install_cmd("@openai/codex", registry or "")
        code, _out, err = _run_codex_upgrade(base_cmd, home_dir)

        # A global npm install on macOS/Linux can hit EACCES when the npm prefix
        # is root-owned. Windows has no sudo, and running as root never needs it.
        # Retry with sudo only when it works non-interactively, so startup never
        # blocks on a password prompt — otherwise point the user at a fix.
        if code != 0 and sys.platform != "win32" and _is_permission_error(err):
            if _is_root():
                pass  # already root; sudo would not help
            elif _sudo_available_noninteractive():
                print(
                    "[codex-upgrade] Permission denied — retrying with sudo.",
                    file=sys.stderr,
                )
                code, _out, err = _run_codex_upgrade(
                    f"sudo -n {base_cmd}", home_dir
                )
            else:
                print(
                    "[codex-upgrade] Permission denied and sudo unavailable "
                    "non-interactively. Fix npm global perms or run manually: "
                    f"sudo {base_cmd}. Continuing startup.",
                    file=sys.stderr,
                )

        code_msg = (
            "successfully"
            if code == 0
            else f"with code {code}. Continuing startup."
        )
        print(f"[codex-upgrade] Upgrade completed {code_msg}", file=sys.stderr)
        # npm can report success (or be killed) with the package unpacked but
        # the bin shims missing; only the runner locator can prove the CLI
        # actually runs, so say so loudly when the repair did not take.
        repaired = _installed_codex()
        if repaired:
            print(f"[codex-upgrade] codex resolves at {repaired}", file=sys.stderr)
        else:
            print(
                "[codex-upgrade] ERROR: codex is still not resolvable after the "
                "install - queued runs cannot start. Re-run: "
                f"{base_cmd}",
                file=sys.stderr,
            )
    except subprocess.TimeoutExpired:
        print(
            "[codex-upgrade] Upgrade timed out after 180 seconds. Continuing startup.",
            file=sys.stderr,
        )
    except FileNotFoundError:
        print(
            "[codex-upgrade] npm not found — install Node.js to enable auto-upgrade. Continuing startup.",
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001 - startup upgrade step must never block boot
        print(
            f"[codex-upgrade] Unexpected error: {exc}. Continuing startup.",
            file=sys.stderr,
        )
    finally:
        if original_config is not None:
            try:
                _restore_codex_config(config_path, original_config, original_mode)
            except OSError as exc:
                print(
                    f"[codex-upgrade] Failed to restore user config {config_path}: {exc}",
                    file=sys.stderr,
                )

