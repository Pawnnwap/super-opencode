from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from openai import (
    APIConnectionError,
    AuthenticationError,
    NotFoundError,
    OpenAI,
    PermissionDeniedError,
)

from supervisor.runners.codex_runner import CodexRunner, find_codex
from supervisor.runners.opencode_runner import (
    _SESSION_CAPTURE_LOCK,
    OpencodeRunner,
    find_opencode,
)
from supervisor.utils.text_utils import normalize_model_response


def run_with_timeout(fn, seconds: int = 30):
    result, error = [], []

    def worker():
        try:
            result.append(fn())
        except Exception as exc:
            error.append(exc)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=seconds)
    if thread.is_alive():
        raise TimeoutError(f"Timed out after {seconds}s")
    if error:
        raise error[0]
    return result[0]


def run_test_with_timeout(
    test_fn,
    test_name: str,
    timeout_seconds: int = 30,
) -> tuple[bool, str]:
    try:
        return run_with_timeout(test_fn, seconds=timeout_seconds)
    except TimeoutError:
        return False, f"{test_name} timed out."
    except Exception as exc:
        return False, f"{test_name} failed: {exc}"


# --------------------------------------------------------------------------- #
# Lowest-thinking-effort selection for connectivity probes.
#
# A connectivity probe only needs "is the endpoint alive and responding" —
# reasoning tokens buy nothing and cost seconds. Every probe therefore runs
# the model at the lowest thinking effort available, chosen automatically:
#
#   supervisor: suppress reasoning on the chat.completions call itself, trying
#     the common provider conventions until one is accepted (plain fallback).
#   codex: `-c model_reasoning_effort=<lowest accepted>` — ladder tried in
#     order, first configuration the model accepts wins (plain fallback).
#   opencode: `--variant <lowest>` — variant metadata from opencode's own
#     model listing, lowest reasoning effort first (plain fallback).
#
# Real (supervised) runs are untouched: these flags are only set by probes.


_REASONING_RANK = {
    "none": 0,
    "minimal": 1,
    "low": 2,
    "medium": 3,
    "high": 4,
    "xhigh": 5,
    "max": 6,
}
# Variants above this rank (medium+) would raise thinking effort relative to
# an unknown default, so they are never auto-selected for probes.
_LOW_EFFORT_MAX_RANK = 2

# Static effort ladders for engines/APIs that expose no per-model variant
# enumeration. Codex's `-c model_reasoning_effort` and OpenAI-compatible
# `reasoning_effort` both use these values; providers accept the subset they
# support. The UI offers them (plus "Default") for real runs; probes always
# auto-cascade from the bottom instead.
REASONING_EFFORT_CHOICES = ["none", "minimal", "low", "medium", "high", "xhigh"]


def _effort_rank(name: str) -> int:
    return _REASONING_RANK.get((name or "").strip().lower(), _LOW_EFFORT_MAX_RANK + 1)


def _parse_verbose_models(text: str) -> dict[str, dict]:
    """Parse `opencode models --verbose` output into {id: metadata}.

    Format: one `<provider>/<model>` header line followed by a pretty-printed
    JSON object per model. Unparseable blocks degrade to {} entries.
    """
    models: dict[str, dict] = {}
    lines = text.splitlines()
    i, n = 0, len(lines)
    pending_id = ""
    while i < n:
        line = lines[i]
        if line.startswith("{"):
            j = i
            while j < n and lines[j].rstrip() != "}":
                j += 1
            try:
                doc = json.loads("\n".join(lines[i : j + 1]))
            except json.JSONDecodeError:
                doc = {}
            if pending_id:
                models[pending_id.lower()] = doc if isinstance(doc, dict) else {}
            pending_id = ""
            i = j + 1
        elif line.strip() and not line.startswith(" "):
            pending_id = line.strip()
            i += 1
        else:
            i += 1
    return models


def _variant_entries(doc: dict) -> list[tuple[str, int]]:
    """(variant key, effort rank) pairs for one model, cheapest first.

    Rank comes from the variant's reasoningEffort metadata when present,
    else from the key name; unrecognized keys sort last (by name).
    """
    entries: list[tuple[str, int]] = []
    for key, meta in (doc.get("variants") or {}).items():
        effort = ""
        if isinstance(meta, dict):
            effort = str(meta.get("reasoningEffort") or "")
        entries.append((str(key), _effort_rank(effort or key)))
    entries.sort(key=lambda kv: (kv[1], kv[0]))
    return entries


@functools.lru_cache(maxsize=32)
def opencode_variant_choices(exe: str, model: str) -> tuple[str, ...]:
    """All thinking variants for `provider/model`, cheapest first (may be ()).

    Read from opencode's local model cache (`opencode models <provider>
    --verbose`, no network refresh) — the authoritative per-model variant
    names, so the UI droplist always matches what `--variant` accepts. Any
    listing/parse problem returns () and the caller falls back to Default.
    """
    model = (model or "").strip()
    if not model or "/" not in model:
        return ()
    provider = model.split("/", 1)[0]
    try:
        proc = subprocess.run(
            [exe, "models", provider, "--verbose"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        if proc.returncode != 0:
            return ()
        doc = _parse_verbose_models(proc.stdout).get(model.lower())
        if not doc:
            return ()
        return tuple(key for key, _rank in _variant_entries(doc))
    except Exception:  # noqa: BLE001 — metadata lookup must never break the probe
        return ()


@functools.lru_cache(maxsize=8)
def lowest_opencode_variant(exe: str, model: str) -> str:
    """Cheapest low-effort variant for `provider/model`, or '' for default.

    Only none/minimal/low-ranked variants qualify — a lone "thinking"-style
    variant would raise effort, not lower it.
    """
    for key in opencode_variant_choices(exe, model):
        if _effort_rank(key) <= _LOW_EFFORT_MAX_RANK:
            return key
    return ""


def _ephemeral_workspace(prefix: str) -> Path:
    """Fresh throwaway workspace dir — every probe runs in its own session.

    Codex/opencode both key their session rollouts to the process cwd, so a
    unique cwd guarantees a probe never lands in (or resumes) another cwd's
    session — crucial for parallel jobs whose `resume --last` fallback must
    never grab a smoke-test session. The dir is removed by the caller.
    """
    base = Path(os.environ.get("TEMP", os.environ.get("TMPDIR", "/tmp")))
    workspace = base / f"{prefix}_probe_{uuid.uuid4().hex[:8]}"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def test_opencode_connectivity(
    opencode_executable: str,
    opencode_model: str | None,
    opencode_model_backup: str | None,
    timeout: int = 30,
    enable_headroom: bool = True,
    headroom_executable: str = "",
    headroom_allow_custom_provider: bool = False,
) -> tuple[bool, str]:
    workspace = _ephemeral_workspace("opencode")
    try:
        exe = find_opencode(opencode_executable or "")
    except FileNotFoundError as exc:
        shutil.rmtree(workspace, ignore_errors=True)
        return False, str(exc)

    variant = lowest_opencode_variant(exe, opencode_model or "")

    def _probe(attempt_variant: str, seconds: int) -> tuple[bool, str]:
        runner = OpencodeRunner(
            workspace=workspace,
            opencode_model=opencode_model,
            opencode_executable=exe,
            opencode_model_backup=opencode_model_backup,
            timeout=timeout,
            enable_headroom=enable_headroom,
            headroom_executable=headroom_executable,
            headroom_allow_custom_provider=headroom_allow_custom_provider,
        )
        runner.opencode_variant = attempt_variant

        def _inner():
            runner._alive = True
            # Hold the session-capture lock while the probe runs. The probe
            # creates a throwaway opencode session in a temp workspace, and
            # concurrently a real task's start() uses a before/after
            # session-list diff to isolate *its* session ID. Letting the probe
            # fire inside that diff window would make the diff ambiguous and
            # force the task into a --continue fallback. Holding the lock
            # serialises the two paths.
            with _SESSION_CAPTURE_LOCK:
                for _ in runner._run_prompt("hi"):
                    pass
            _output, timed_out = runner.read_output(timeout=25)
            if timed_out:
                return False, "opencode timed out reading output."
            if runner._last_result and runner._last_result.ok:
                note = f" (variant: {attempt_variant})" if attempt_variant else ""
                return True, f"opencode responded successfully{note}."
            diag = runner.last_diagnostic() if runner._last_result else "(no result)"
            return False, f"opencode returned an error.\n{diag}"

        try:
            return run_with_timeout(_inner, seconds=seconds)
        except TimeoutError:
            return False, "opencode test timed out."
        except Exception as exc:  # noqa: BLE001
            return False, f"opencode test failed: {exc}"
        finally:
            try:
                runner.stop()
            except Exception:  # noqa: BLE001
                pass

    deadline = time.monotonic() + timeout
    result = _probe(variant, max(3, int(deadline - time.monotonic())))
    if not result[0] and variant:
        # The variant came from opencode's own listing, but the provider may
        # still reject it — retry once on the model default with whatever
        # budget remains before reporting failure.
        retry = _probe("", max(3, int(deadline - time.monotonic())))
        if retry[0]:
            return True, (
                "opencode responded successfully (variant rejected; default used)."
            )
    shutil.rmtree(workspace, ignore_errors=True)
    return result


def test_codex_connectivity(
    codex_executable: str,
    codex_model: str | None,
    codex_model_backup: str | None,
    timeout: int = 30,
    base_url: str = "",
    api_key: str = "",
) -> tuple[bool, str]:
    workspace = _ephemeral_workspace("codex")
    try:
        exe = find_codex(codex_executable or "")
    except FileNotFoundError as exc:
        shutil.rmtree(workspace, ignore_errors=True)
        return False, str(exc)

    # Full session-store isolation via CODEX_HOME when the probe is
    # self-contained (external provider + key supplied via `-c`/env, so no
    # ~/.codex auth or config is needed). Without an external endpoint codex
    # must keep the user's real CODEX_HOME for auth — there the unique cwd
    # above still separates the probe's session from every other cwd's.
    extra_env: dict[str, str] = {}
    if (base_url or "").strip() and (api_key or "").strip():
        isolated_home = workspace / "codex_home"
        isolated_home.mkdir(exist_ok=True)
        extra_env["CODEX_HOME"] = str(isolated_home)

    # Lowest accepted reasoning effort: codex rejects unsupported
    # `-c model_reasoning_effort` values immediately, so walk the ladder from
    # the bottom — first configuration the model accepts wins; the untouched
    # config default is the last resort.
    efforts = ("none", "minimal", "")

    def _probe(effort: str, seconds: int) -> tuple[bool, str]:
        runner = CodexRunner(
            workspace=workspace,
            opencode_model=codex_model,
            opencode_executable=exe,
            opencode_model_backup=codex_model_backup,
            timeout=timeout,
            codex_base_url=base_url,
            codex_api_key=api_key,
        )
        runner.codex_reasoning_effort = effort
        runner.codex_extra_env = extra_env

        def _inner():
            runner._alive = True
            # codex needs no session-list capture lock (it uses resume/--last),
            # so the probe is a single self-contained `codex exec` call (the
            # prompt travels over stdin like every supervised run).
            for _ in runner._run_prompt("hi"):
                pass
            _output, timed_out = runner.read_output(timeout=25)
            if timed_out:
                return False, "codex timed out reading output."
            if runner._last_result and runner._last_result.ok:
                note = f" (reasoning effort: {effort})" if effort else ""
                return True, f"codex responded successfully{note}."
            diag = runner.last_diagnostic() if runner._last_result else "(no result)"
            return False, f"codex returned an error.\n{diag}"

        try:
            return run_with_timeout(_inner, seconds=seconds)
        except TimeoutError:
            return False, "codex test timed out."
        except Exception as exc:  # noqa: BLE001
            return False, f"codex test failed: {exc}"
        finally:
            try:
                runner.stop()
            except Exception:  # noqa: BLE001
                pass

    deadline = time.monotonic() + timeout
    result = (False, "codex test did not run.")
    for effort in efforts:
        remaining = int(deadline - time.monotonic())
        if remaining < 3:
            break
        result = _probe(effort, remaining)
        if result[0]:
            break
    shutil.rmtree(workspace, ignore_errors=True)
    return result


def test_agent_connectivity(
    engine: str,
    executable: str,
    model: str | None,
    model_backup: str | None,
    timeout: int = 30,
    base_url: str = "",
    api_key: str = "",
    enable_headroom: bool = True,
    headroom_executable: str = "",
    headroom_allow_custom_provider: bool = False,
) -> tuple[bool, str]:
    """Dispatch the execution-agent connectivity probe by engine.

    ``base_url`` / ``api_key`` configure codex's external OpenAI-compatible
    (Responses API) endpoint; they are ignored for opencode, which manages its
    own provider config.
    """
    if (engine or "opencode").strip().lower() == "codex":
        return test_codex_connectivity(
            executable, model, model_backup, timeout, base_url, api_key
        )
    return test_opencode_connectivity(
        executable,
        model,
        model_backup,
        timeout,
        enable_headroom,
        headroom_executable,
        headroom_allow_custom_provider,
    )


# Reasoning-suppression conventions passed via extra_body, cheapest first.
# The bare call is the last resort so a provider that rejects every known
# convention still gets probed exactly as before.
_REASONING_OFF_BODIES: tuple[tuple[dict, str], ...] = (
    ({"reasoning_effort": "none"}, "reasoning: none"),
    ({"reasoning_effort": "minimal"}, "reasoning: minimal"),
    ({"chat_template_kwargs": {"thinking": False}}, "thinking off (chat_template_kwargs)"),
    ({"thinking": {"type": "disabled"}}, "thinking disabled"),
    ({"enable_thinking": False}, "enable_thinking=false"),
    ({}, "provider default"),
)


def test_supervisor_connectivity(
    api_key: str,
    model: str,
    base_url: str | None = None,
    timeout: float = 25.0,
) -> tuple[bool, str]:
    if not api_key:
        return False, "API key is not set."

    client = OpenAI(api_key=api_key, base_url=base_url or None, timeout=timeout)
    deadline = time.monotonic() + timeout
    last_msg = "Supervisor test failed."

    for extra, label in _REASONING_OFF_BODIES:
        remaining = int(deadline - time.monotonic())
        if remaining < 3:
            break

        def _call(_extra=extra):
            kwargs = {"extra_body": _extra} if _extra else {}
            return client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "hi"}],
                **kwargs,
            )

        try:
            resp = run_with_timeout(_call, seconds=remaining)
            text = normalize_model_response(
                resp.choices[0].message.content,
                "supervisor connectivity test response",
            )
            if text.strip():
                return True, f"Supervisor responded [{label}]: {text.strip()[:120]}"
            last_msg = "Supervisor returned an empty response."
        except (
            AuthenticationError,
            APIConnectionError,
            NotFoundError,
            PermissionDeniedError,
        ) as exc:
            # Auth / endpoint / model problems no reasoning setting can fix —
            # fail immediately instead of hammering the endpoint five more
            # times.
            return False, f"Supervisor test failed: {exc}"
        except TimeoutError:
            return False, "Supervisor test timed out."
        except Exception as exc:  # noqa: BLE001
            # Most likely this endpoint rejected the reasoning parameter —
            # fall through to the next convention (ultimately the plain call).
            last_msg = f"Supervisor test failed: {exc}"
    return False, last_msg
