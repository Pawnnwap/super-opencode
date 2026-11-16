"""Connectivity-probe results that survive a restart.

The two probe flags used to live in process memory only. Every restart of the
local console therefore re-locked Launch behind up to 90 seconds of probing,
while the wizard promised that "steps 1-4 are done once and reused by every
run". The flags are still runtime-first (a fresh pass wins immediately), but a
recorded pass is now honoured as long as the thing that was tested has not
changed.

Storage is a small JSON file next to the job store: settings.json is the
operator's configuration, this is a *measurement* of it, and mixing the two
would make "reset settings" also silently forget a passing probe.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

PROBE_FILE = Path(".job_store") / "probes.json"

# A probe is a snapshot of "this engine answered with this model and this key".
# Networks and quotas change, so the record expires; 7 days keeps the daily
# restart painless without pretending the result is permanent.
TTL_SECONDS = 7 * 24 * 3600

# Fields that define *what was tested*. If any changes, the record is void.
_FINGERPRINT_KEYS = (
    "engine",
    "opencode_model",
    "supervisor_model",
    "base_url",
    "opencode_executable",
)


def _secret_digest(value: object) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def fingerprint(values: dict) -> str:
    """Stable digest of everything the probes actually depended on."""
    parts = [str(values.get(key) or "") for key in _FINGERPRINT_KEYS]
    parts.append(_secret_digest(values.get("openai_key")))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def record(passed: bool, values: dict, *, path: Path | None = None) -> None:
    """Persist the outcome of the latest probe run (best effort)."""
    target = path or PROBE_FILE
    payload = {
        "passed": bool(passed),
        "at": time.time(),
        "fingerprint": fingerprint(values),
        "engine": str(values.get("engine") or ""),
        "model": str(values.get("opencode_model") or ""),
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    except OSError:
        pass


_cache: dict = {"key": None, "data": {}}


def load(path: Path | None = None) -> dict:
    target = path or PROBE_FILE
    try:
        stat = target.stat()
    except OSError:
        return {}
    # Cached by (path, mtime, size): `tests_ok()` is consulted on every page
    # render, and this is a disk read on the event loop.
    key = (str(target), stat.st_mtime_ns, stat.st_size)
    custom = path is not None
    if not custom and _cache["key"] == key:
        return _cache["data"]
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if not custom:
        _cache["key"] = key
        _cache["data"] = data
    return data


def fresh(values: dict, *, path: Path | None = None) -> bool:
    """True when a *passing* record matches the current configuration."""
    data = load(path)
    if not data or not data.get("passed"):
        return False
    if data.get("fingerprint") != fingerprint(values):
        return False
    at = data.get("at")
    if not isinstance(at, (int, float)):
        return False
    return (time.time() - at) < TTL_SECONDS


def status(values: dict, *, path: Path | None = None) -> dict:
    """``{state: fresh|stale|missing, at, engine}`` for the header chip."""
    data = load(path)
    if not data:
        return {"state": "missing", "at": 0, "engine": ""}
    state = "fresh" if fresh(values, path=path) else "stale"
    return {
        "state": state,
        "at": float(data.get("at") or 0),
        "engine": str(data.get("engine") or ""),
    }


__all__ = ["PROBE_FILE", "TTL_SECONDS", "fingerprint", "fresh", "load", "record", "status"]
