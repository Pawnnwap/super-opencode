from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from supervisor.runners.opencode_runner import find_opencode

UPGRADE_SETTINGS_FILE = Path.home() / ".opencode_supervisor_settings.json"


def should_skip_upgrade(settings_file: Path = UPGRADE_SETTINGS_FILE) -> bool:
    if os.environ.get("OPENCODE_SKIP_UPGRADE") == "1":
        return True
    try:
        if settings_file.exists():
            cfg = json.loads(settings_file.read_text(encoding="utf-8"))
            if cfg.get("skip_upgrade"):
                return True
    except Exception:
        pass
    return False


def auto_upgrade_opencode(settings_file: Path = UPGRADE_SETTINGS_FILE) -> None:
    if should_skip_upgrade(settings_file):
        print(
            "[opencode-upgrade] Skipping upgrade: disabled via config/env var",
            file=sys.stderr,
        )
        return

    try:
        home_dir = str(Path.home())

        if sys.platform == "win32":
            # Windows: keep using npm
            print(
                "[opencode-upgrade] Running: npm install -g opencode-ai@latest",
                file=sys.stderr,
            )
            cmd = "npm install -g opencode-ai@latest"
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=home_dir,
                shell=True,
            )
        else:
            # macOS / Linux: use the official curl install script
            print(
                "[opencode-upgrade] Running: curl -fsSL https://raw.githubusercontent.com/opencode-ai/opencode/main/install | bash",
                file=sys.stderr,
            )
            cmd = "curl -fsSL https://raw.githubusercontent.com/opencode-ai/opencode/main/install | bash"
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
            print(f"[opencode-upgrade] stdout: {stdout.strip()}", file=sys.stderr)
        if stderr:
            print(f"[opencode-upgrade] stderr: {stderr.strip()}", file=sys.stderr)

        code_msg = (
            "successfully"
            if proc.returncode == 0
            else f"with code {proc.returncode}. Continuing startup."
        )
        print(f"[opencode-upgrade] Upgrade completed {code_msg}.", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print(
            "[opencode-upgrade] Upgrade timed out after 180 seconds. Continuing startup.",
            file=sys.stderr,
        )
    except FileNotFoundError:
        print(
            "[opencode-upgrade] Required command not found — install Node.js (Windows) or curl (macOS/Linux) to enable auto-upgrade. Continuing startup.",
            file=sys.stderr,
        )
    except Exception as exc:
        print(
            f"[opencode-upgrade] Unexpected error: {exc}. Continuing startup.",
            file=sys.stderr,
        )


def auto_upgrade_dcp(settings_file: Path = UPGRADE_SETTINGS_FILE) -> None:
    if should_skip_upgrade(settings_file):
        print(
            "[dcp-upgrade] Skipping upgrade: disabled via config/env var",
            file=sys.stderr,
        )
        return

    home_dir = os.path.expanduser("~")

    try:
        opencode_exe = find_opencode("")
    except FileNotFoundError:
        print(
            "[dcp-upgrade] opencode executable not found — install via 'npm install -g opencode-ai' (Windows) or 'curl -fsSL https://raw.githubusercontent.com/opencode-ai/opencode/main/install | bash' (macOS/Linux) first. Continuing startup.",
            file=sys.stderr,
        )
        return

    try:
        if sys.platform == "win32":
            print(
                "[dcp-upgrade] Spawning detached background upgrade window...",
                file=sys.stderr,
            )
            cmd_string = (
                f'start "" cmd /c ""{opencode_exe}" plugin '
                '@tarquinen/opencode-dcp@latest --global"'
            )
            subprocess.Popen(
                cmd_string,
                shell=True,
                cwd=home_dir,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            print(
                "[dcp-upgrade] Launching detached background upgrade...",
                file=sys.stderr,
            )
            subprocess.Popen(
                [opencode_exe, "plugin", "@tarquinen/opencode-dcp@latest", "--global"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=home_dir,
                start_new_session=True,
            )

        print(
            "[dcp-upgrade] Upgrade launched. Streamlit startup continues.",
            file=sys.stderr,
        )
    except FileNotFoundError:
        print(
            f"[dcp-upgrade] Failed to launch '{opencode_exe}'. Continuing startup.",
            file=sys.stderr,
        )
    except Exception as exc:
        print(
            f"[dcp-upgrade] Unexpected error spawning background process: {exc}",
            file=sys.stderr,
        )
