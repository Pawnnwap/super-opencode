"""Authoritative opencode CLI facts, read from the CLI itself.

opencode 2.x validates every ``run`` flag against its resolved configuration
and hard-errors (exit 1, before any model call) on values it does not know::

    --model provider/model#variant   -> "Variant unavailable for <model>: <v>"
    --model <bare-name>              -> "Invalid model reference: <name>"
    --agent <unknown>                -> 'Agent not found: "<name>"'

Each of those kills a supervised turn before the model is asked, so the
supervisor judges an empty turn and the run stalls until an operator cancels
it (observed live on 2026-11-13: every opencode run died on
``Variant unavailable for local-deepseek/...: xhigh``).

The command builder therefore asks the installed CLI what it actually accepts
instead of trusting persisted settings:

  * ``opencode models`` — every ``provider/model`` id this install can run,
    built-in providers included (``opencode-go/…``, ``openrouter/…``).
  * ``opencode debug config`` — the resolved config (global + project merged),
    including each *configured* model's declared ``variants``.
  * ``opencode debug agents`` — the agent ids this build ships.

All three are local reads (~0.1 s, no network, no model call). Any probe
failure returns ``None`` ("unknown") and callers keep the configured value
rather than guessing; the runtime retry in ``opencode_support.process`` is the
safety net for whatever the catalog could not see.
"""

from __future__ import annotations

import json
import logging
import subprocess
from functools import lru_cache

logger = logging.getLogger(__name__)

# `models` / `debug …` are local reads; 15 s leaves headroom for a cold npm
# shim without ever looking like a hang.
_PROBE_TIMEOUT = 15.0


def cli_major(exe: str) -> int | None:
    """Major version of the installed CLI (delegates to the cached probe)."""
    from supervisor.runners.opencode_support.command_builder import _opencode_major

    return _opencode_major(exe)


def resolve_exe(exe: str) -> str:
    """Absolute path for *exe* (a bare name is not spawnable on Windows).

    ``CreateProcess`` does not apply PATHEXT, so ``opencode`` would resolve to
    the extension-less shell script instead of ``opencode.cmd``. The locator
    knows every install layout, so it is the single resolution point.
    """
    from supervisor.runners.opencode_support.locator import find_opencode

    try:
        return find_opencode(exe)
    except (FileNotFoundError, OSError):
        return exe


def _run_json(exe: str, args: list[str], cwd: str) -> object | None:
    """Run one CLI introspection command and parse its JSON stdout."""
    try:
        proc = subprocess.run(
            [resolve_exe(exe), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PROBE_TIMEOUT,
            cwd=cwd or None,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("opencode %s probe failed for %r", args, exe, exc_info=True)
        return None
    if proc.returncode != 0:
        logger.debug("opencode %s exited %d", args, proc.returncode)
        return None
    try:
        return json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        logger.debug("opencode %s output is not JSON", args)
        return None


@lru_cache(maxsize=32)
def _available_models(exe: str, cwd: str) -> frozenset[str] | None:
    """Every ``provider/model`` id the install can run, or None if unknown.

    ``opencode models`` prints one id per line and covers built-in providers
    too, which ``debug config`` does not list (it only shows configured ones).
    """
    try:
        proc = subprocess.run(
            [resolve_exe(exe), "models"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PROBE_TIMEOUT,
            cwd=cwd or None,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("opencode models probe failed for %r", exe, exc_info=True)
        return None
    if proc.returncode != 0:
        return None
    ids = {
        line.strip()
        for line in proc.stdout.splitlines()
        if "/" in line and not line.startswith(" ")
    }
    return frozenset(ids) or None


@lru_cache(maxsize=32)
def _providers(exe: str, cwd: str) -> dict | None:
    """Merged ``providers`` map from the resolved config, or None if unknown.

    ``debug config`` prints one entry per config source (global first, then
    project); later sources win, and a project entry only carries the
    providers/models it defines, so the merge is per provider and per model.
    """
    doc = _run_json(exe, ["debug", "config"], cwd)
    if not isinstance(doc, list):
        return None
    providers: dict[str, dict] = {}
    for entry in doc:
        info = entry.get("info") if isinstance(entry, dict) else None
        if not isinstance(info, dict):
            continue
        for provider_id, data in (info.get("providers") or {}).items():
            if not isinstance(data, dict):
                continue
            current = providers.setdefault(str(provider_id), {})
            for key, value in data.items():
                if key == "models" and isinstance(value, dict):
                    current.setdefault("models", {}).update(value)
                else:
                    current[key] = value
    return providers or None


@lru_cache(maxsize=32)
def _agents(exe: str, cwd: str) -> frozenset[str] | None:
    doc = _run_json(exe, ["debug", "agents"], cwd)
    if not isinstance(doc, list):
        return None
    ids = {
        entry["id"]
        for entry in doc
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]
    }
    return frozenset(ids) or None


def declared_agents(exe: str, cwd: str = "") -> frozenset[str] | None:
    """Agent ids the installed CLI ships, or None when it cannot be asked."""
    return _agents(exe, str(cwd or ""))


def declared_variants(
    exe: str,
    model: str,
    cwd: str = "",
) -> tuple[str, ...] | None:
    """Thinking variants the model declares, or None when that is unknown.

    ``()`` means "the model is configured and declares no variants" — the
    distinction matters: offering a variant to such a model makes opencode 2.x
    exit 1 with "Variant unavailable" before the model is ever called.

    A model absent from the resolved config (a built-in provider model) returns
    None: its variants are not enumerable here, so the configured value is kept
    and the runtime retry covers a rejection.
    """
    model = (model or "").strip().split("#", 1)[0]
    if "/" not in model:
        return None
    provider_id, _, model_id = model.partition("/")
    providers = _providers(exe, str(cwd or ""))
    if providers is None:
        return None
    data = providers.get(provider_id)
    if not isinstance(data, dict):
        return None
    model_data = (data.get("models") or {}).get(model_id)
    if not isinstance(model_data, dict):
        return None
    variants = model_data.get("variants")
    if not isinstance(variants, list):
        return ()
    ids: list[str] = []
    for entry in variants:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]:
            ids.append(entry["id"])
        elif isinstance(entry, str) and entry:
            ids.append(entry)
    return tuple(ids)


def known_models(exe: str, cwd: str = "") -> frozenset[str] | None:
    """Every ``provider/model`` id this install can run, or None if unknown."""
    return _available_models(exe, str(cwd or ""))


def resolve_model(exe: str, model: str, cwd: str = "") -> tuple[str | None, str]:
    """Qualify a bare model name; ``(None, note)`` means "omit --model".

    opencode 2.x requires ``provider/model`` and exits 1 on a bare name
    ("Invalid model reference"). A bare name that matches exactly one
    available model is qualified; an ambiguous or unknown one is dropped so
    the CLI's own configured default applies instead of a guaranteed failure.
    A qualified ref the CLI does not list is dropped for the same reason.
    """
    model = (model or "").strip()
    if not model:
        return model, ""
    known = known_models(exe, str(cwd or ""))
    if not known:
        return model, ""
    if "/" in model:
        if model in known:
            return model, ""
        return None, (
            f"model {model!r} is not in the CLI's model list; "
            "omitting --model so the CLI default applies"
        )
    matches = sorted(ref for ref in known if ref.split("/", 1)[1] == model)
    if len(matches) == 1:
        return matches[0], f"bare model {model!r} qualified to {matches[0]!r}"
    if len(matches) > 1:
        return None, (
            f"bare model {model!r} is ambiguous ({', '.join(matches)}); "
            "omitting --model so the CLI default applies"
        )
    return None, (
        f"model {model!r} is not in the CLI's model list; "
        "omitting --model so the CLI default applies"
    )


def clear_cache() -> None:
    """Drop every cached probe (tests and config reloads)."""
    _available_models.cache_clear()
    _providers.cache_clear()
    _agents.cache_clear()


__all__ = [
    "clear_cache",
    "cli_major",
    "declared_agents",
    "declared_variants",
    "known_models",
    "resolve_exe",
    "resolve_model",
]
