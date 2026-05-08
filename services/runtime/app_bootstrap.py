from __future__ import annotations

import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

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


def _detect_artifact_suffix() -> tuple[str, str]:
    """Return (os_tag, arch_tag) matching the opencode release naming convention."""
    os_tag = sys.platform  # fallback
    if sys.platform == "linux":
        os_tag = "linux"
    elif sys.platform == "darwin":
        os_tag = "mac"

    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        arch_tag = "arm64"
    elif machine in ("x86_64", "amd64"):
        arch_tag = "x86_64"
    else:
        arch_tag = machine

    return os_tag, arch_tag


def _download_and_install_opencode() -> None:
    """Pure-Python: download the latest opencode release tarball and install to ~/.opencode/bin."""
    os_tag, arch_tag = _detect_artifact_suffix()
    filename = f"opencode-{os_tag}-{arch_tag}.tar.gz"
    url = f"https://github.com/opencode-ai/opencode/releases/latest/download/{filename}"
    install_dir = Path.home() / ".opencode" / "bin"
    install_dir.mkdir(parents=True, exist_ok=True)

    print(
        f"[opencode-upgrade] Downloading {url}",
        file=sys.stderr,
    )

    req = Request(url, headers={"User-Agent": "opencode-supervisor/1.0"})
    with urlopen(req, timeout=120) as resp:
        data = resp.read()

    print(
        f"[opencode-upgrade] Downloaded {len(data)} bytes, extracting...",
        file=sys.stderr,
    )

    extracted = False
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for member in tf.getmembers():
            # Look for the opencode binary (may be at root or inside a subdirectory)
            basename = Path(member.name).name
            if member.isfile() and basename == "opencode":
                # Extract to a buffer then write to install_dir
                member_file = tf.extractfile(member)
                if member_file is None:
                    continue
                dest = install_dir / "opencode"
                dest.write_bytes(member_file.read())
                dest.chmod(0o755)
                extracted = True
                print(
                    f"[opencode-upgrade] Installed opencode to {dest}",
                    file=sys.stderr,
                )
                break

    if not extracted:
        # Fallback: extract everything and look for opencode binary
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            tf.extractall(install_dir)
        # Search for opencode binary in the extracted tree
        for candidate in install_dir.rglob("opencode"):
            if candidate.is_file():
                candidate.chmod(0o755)
                # Move to top-level
                final = install_dir / "opencode"
                if candidate != final:
                    shutil.move(str(candidate), str(final))
                extracted = True
                print(
                    f"[opencode-upgrade] Installed opencode to {final}",
                    file=sys.stderr,
                )
                break

    if not extracted:
        print(
            "[opencode-upgrade] Could not find 'opencode' binary in the downloaded archive. Continuing startup.",
            file=sys.stderr,
        )


def auto_upgrade_opencode(settings_file: Path = UPGRADE_SETTINGS_FILE) -> None:
    if should_skip_upgrade(settings_file):
        print(
            "[opencode-upgrade] Skipping upgrade: disabled via config/env var",
            file=sys.stderr,
        )
        return

    try:
        if sys.platform == "win32":
            # Windows: keep using npm
            home_dir = str(Path.home())
            print(
                "[opencode-upgrade] Running: npm install -g opencode-ai@latest",
                file=sys.stderr,
            )
            proc = subprocess.Popen(
                "npm install -g opencode-ai@latest",
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
        else:
            # macOS / Linux: pure-Python download & install
            _download_and_install_opencode()

    except subprocess.TimeoutExpired:
        print(
            "[opencode-upgrade] Upgrade timed out after 180 seconds. Continuing startup.",
            file=sys.stderr,
        )
    except (URLError, OSError) as exc:
        print(
            f"[opencode-upgrade] Network/download error: {exc}. Continuing startup.",
            file=sys.stderr,
        )
    except FileNotFoundError:
        print(
            "[opencode-upgrade] Required command not found — install Node.js (Windows) to enable auto-upgrade. Continuing startup.",
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
