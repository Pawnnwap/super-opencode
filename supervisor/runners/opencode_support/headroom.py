"""Safe, process-local Headroom support for OpenCode runs.

``headroom wrap opencode`` rewrites the user's global OpenCode configuration.
The supervisor instead starts (or reuses) a local proxy and supplies routing
only to its child OpenCode process through ``OPENCODE_CONFIG_CONTENT``. This
keeps user configuration and other OpenCode sessions untouched.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from supervisor.runners.locator_common import find_executable
from supervisor.utils.text_utils import coerce_str

logger = logging.getLogger(__name__)

DEFAULT_HEADROOM_PORT = 8787
_NATIVE_PROVIDERS = frozenset({"anthropic", "openai"})
_REGISTRY_LOCK = threading.RLock()


@dataclass(frozen=True)
class HeadroomPlan:
    """Decision to route one OpenCode command through Headroom or not."""

    enabled: bool
    reason: str
    executable: str = ""
    port: int = DEFAULT_HEADROOM_PORT


@dataclass(frozen=True)
class HeadroomProxyLease:
    """A runner's reference to a managed or externally-owned proxy."""

    port: int
    managed: bool


@dataclass
class _ManagedProxy:
    process: subprocess.Popen
    references: int = 1


_MANAGED_PROXIES: dict[int, _ManagedProxy] = {}


def find_headroom(explicit: str = "") -> str:
    """Locate Headroom without requiring its scripts directory on PATH."""
    windows_dirs = [
        Path(sys.executable).resolve().parent,
        Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Scripts",
        Path.home() / "AppData" / "Roaming" / "Python" / "Scripts",
    ]
    python_root = Path.home() / "AppData" / "Local" / "Programs" / "Python"
    if python_root.is_dir():
        windows_dirs.extend(path / "Scripts" for path in python_root.glob("Python*"))

    return find_executable(
        "headroom",
        names=["headroom.exe", "headroom.cmd", "headroom.bat", "headroom"],
        windows_extra_dirs=windows_dirs,
        unix_extra_dirs=[Path.home() / ".local" / "bin", Path("/usr/local/bin")],
        not_found_message="Headroom is not installed or not discoverable.",
        explicit=explicit,
    )


def resolve_headroom_plan(
    *,
    enabled: bool,
    model: str | None,
    executable: str = "",
    port: int = DEFAULT_HEADROOM_PORT,
    allow_custom_provider: bool = False,
    find_headroom_fn=find_headroom,
) -> HeadroomPlan:
    """Return a safe routing decision without starting or mutating anything."""
    if not enabled:
        return HeadroomPlan(False, "disabled", port=port)
    if not 1 <= port <= 65535:
        return HeadroomPlan(False, f"invalid proxy port {port}", port=port)

    resolved_model = coerce_str(model, "headroom model")
    provider, separator, model_id = resolved_model.partition("/")
    provider = provider.strip().lower()
    if not separator or not model_id.strip():
        return HeadroomPlan(
            False,
            "model must use provider/model form before Headroom can route safely",
            port=port,
        )
    if provider not in _NATIVE_PROVIDERS and not allow_custom_provider:
        return HeadroomPlan(
            False,
            f"custom provider '{provider}' not routed: installed Headroom lacks transport plugin",
            port=port,
        )

    try:
        resolved_executable = find_headroom_fn(executable)
    except FileNotFoundError:
        return HeadroomPlan(False, "Headroom executable not found", port=port)

    if provider in _NATIVE_PROVIDERS:
        reason = f"native {provider} provider"
    else:
        reason = f"custom provider '{provider}' explicitly allowed"
    return HeadroomPlan(True, reason, resolved_executable, port)


def build_headroom_environment(
    base_environment: Mapping[str, str],
    *,
    executable: str,
    port: int,
    workspace: Path,
) -> dict[str, str]:
    """Build child-only OpenCode routing configuration for a live proxy."""
    proxy_url = f"http://127.0.0.1:{port}"
    config = {
        "provider": {
            "anthropic": {"options": {"baseURL": f"{proxy_url}/v1"}},
            "openai": {"options": {"baseURL": f"{proxy_url}/v1"}},
        },
        "mcp": {
            "headroom": {
                "type": "local",
                "command": [executable, "mcp", "serve", "--proxy-url", proxy_url],
                "enabled": True,
            },
        },
    }
    environment = dict(base_environment)
    environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(config, separators=(",", ":"))
    environment["HEADROOM_PROXY_URL"] = proxy_url
    environment.setdefault("HEADROOM_PROJECT", workspace.name)
    return environment


def is_headroom_proxy_healthy(port: int, *, timeout: float = 0.5) -> bool:
    """Check identity as well as reachability; another local service is unsafe."""
    try:
        with urlopen(f"http://127.0.0.1:{port}/livez", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("service") == "headroom-proxy"
        and payload.get("status") == "healthy"
    )


def _port_is_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def _select_port(preferred_port: int) -> int:
    if _port_is_available(preferred_port):
        return preferred_port
    for candidate in range(preferred_port + 1, min(preferred_port + 101, 65536)):
        if _port_is_available(candidate):
            return candidate
    raise RuntimeError(
        f"Headroom proxy port {preferred_port} is occupied and no fallback port is available",
    )


def _terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def _wait_for_proxy(process: subprocess.Popen, port: int, startup_timeout: float) -> None:
    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        if is_headroom_proxy_healthy(port):
            return
        returncode = process.poll()
        if returncode is not None:
            raise RuntimeError(f"Headroom proxy exited during startup (exit {returncode})")
        time.sleep(0.2)
    raise RuntimeError(f"Headroom proxy did not become healthy on port {port}")


def acquire_headroom_proxy(
    executable: str,
    *,
    preferred_port: int = DEFAULT_HEADROOM_PORT,
    startup_timeout: float = 20.0,
) -> HeadroomProxyLease:
    """Start one shared proxy when needed, then return a reference-counted lease."""
    with _REGISTRY_LOCK:
        current = _MANAGED_PROXIES.get(preferred_port)
        if current and current.process.poll() is None and is_headroom_proxy_healthy(preferred_port):
            current.references += 1
            return HeadroomProxyLease(preferred_port, managed=True)
        if current:
            _MANAGED_PROXIES.pop(preferred_port, None)
            _terminate_process(current.process)

        # A preferred port can be occupied by another unrelated service. In
        # that case the first supervisor runner selects a fallback port; later
        # runners should share that proxy instead of each creating another one.
        for port, managed in tuple(_MANAGED_PROXIES.items()):
            if managed.process.poll() is None and is_headroom_proxy_healthy(port):
                managed.references += 1
                return HeadroomProxyLease(port, managed=True)
            _MANAGED_PROXIES.pop(port, None)
            _terminate_process(managed.process)

        if is_headroom_proxy_healthy(preferred_port):
            return HeadroomProxyLease(preferred_port, managed=False)

        port = _select_port(preferred_port)
        process = subprocess.Popen(
            [executable, "proxy", "--port", str(port)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        try:
            _wait_for_proxy(process, port, startup_timeout)
        except Exception:
            _terminate_process(process)
            raise
        _MANAGED_PROXIES[port] = _ManagedProxy(process=process)
        logger.info("Started Headroom proxy on port %d", port)
        return HeadroomProxyLease(port, managed=True)


def release_headroom_proxy(lease: HeadroomProxyLease | None) -> None:
    """Release a managed proxy; never stop a proxy that predates this runner."""
    if lease is None or not lease.managed:
        return
    with _REGISTRY_LOCK:
        current = _MANAGED_PROXIES.get(lease.port)
        if current is None:
            return
        current.references -= 1
        if current.references > 0:
            return
        _MANAGED_PROXIES.pop(lease.port, None)
        try:
            _terminate_process(current.process)
        except OSError as exc:
            logger.warning("Could not stop Headroom proxy on port %d: %s", lease.port, exc)
