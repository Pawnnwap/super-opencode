"""Capability-negotiating proxy between codex/supervisor and an OpenAI-compatible endpoint.

Bidirectional by construction:
  * Pass-through is the default. If the endpoint accepts what codex asks for,
    the request goes out unchanged (plus effort clamp), so a server that
    supports grammar custom tools / namespace tools / web_search keeps them.
  * Capabilities are discovered, not assumed. A cheap probe suite (a few tiny
    completions per (upstream, model)) records which tool shapes and which
    reasoning efforts the endpoint accepts; results are cached on disk
    (TTL, default 6 h) so restarts skip discovery.
  * Anything new is learned from the failure. On a 400 the message is parsed
    (REASON_RULES / SUPPORTED_RE), the narrowest downgrade is applied, the
    request is retried (<=4 attempts), and the restriction is cached for the key.

Handled shapes:
  * custom tool with format.type == "grammar" -> format {"type": "text"}
    (legacy freeform custom tool; the tool call itself is unchanged)
  * namespace (MCP group) tools               -> flattened top-level function
    tools, with a reversible name map so the response's tool calls are
    rewritten back to the namespaced form codex can route
  * web_search tools                          -> dropped
  * reasoning effort                          -> clamped to the nearest value
    the endpoint accepts (ladder none<minimal<low<medium<high<xhigh, ties down)

Run:  set SOP_UPSTREAM=http://HOST:PORT   (or pass the upstream as argv[2])
      python codex_compat_proxy.py [port] [upstream]     # listens on 127.0.0.1:8321
      GET  /healthz        -> profile + counters
      GET  <anything else> -> read-through to the upstream (e.g. /v1/models)
      POST /admin/reprobe  -> drop cached profile (re-probe on next request)
Point codex at http://127.0.0.1:<port>/v1 (codex_base_url) and the supervisor
at the same host (openai_base_url) so both engines share one negotiated profile.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from supervisor.security.redaction import redact_data, redact_text

ROOT = Path(__file__).resolve().parent
UPSTREAM = os.environ.get("SOP_UPSTREAM", "").strip().rstrip("/")
CACHE_FILE = Path(os.environ.get("SOP_CAP_CACHE", ROOT / "codex_capability_cache.json"))
LOG_FILE = Path(
    os.environ.get("SOP_CAP_LOG") or ROOT / "logs" / "codex_compat_proxy.log",
)
TTL_SECONDS = int(os.environ.get("SOP_CAP_TTL", str(6 * 3600)))
MAX_LOG_BYTES = 2_000_000
MAX_BODY_BYTES = int(os.environ.get("SOP_CAP_MAX_BODY", str(16 * 1024 * 1024)))
MAX_RESPONSE_BYTES = int(os.environ.get("SOP_CAP_MAX_RESPONSE", str(16 * 1024 * 1024)))
MAX_STREAM_BYTES = int(os.environ.get("SOP_CAP_MAX_STREAM", str(64 * 1024 * 1024)))
MAX_ERROR_BYTES = 64 * 1024
MAX_CONNECTIONS = int(os.environ.get("SOP_CAP_MAX_CONNECTIONS", "32"))
ADMIN_TOKEN = os.environ.get("SOP_CAP_ADMIN_TOKEN", "").strip()
# Effort clamp tie-break: "down" (default, cheaper) or "up".
EFFORT_TIE = os.environ.get("SOP_EFFORT_TIE", "down").strip().lower()

LADDER = ["none", "minimal", "low", "medium", "high", "xhigh"]

_lock = threading.RLock()
_profiles: dict[str, dict] = {}
_probing: set[str] = set()
_stats = {"requests": 0, "rewrites": 0, "learned": 0, "errors": 0}
_connections = threading.BoundedSemaphore(MAX_CONNECTIONS)
# NOTE: the flattened-name map is REQUEST-scoped (threaded server, several
# models flatten concurrently) — a module-global map once let one model's
# rewrite leak into another model's response-name restore.


def _blank_profile() -> dict:
    # None = unknown -> pass through untouched (optimistic, old-endpoint friendly).
    return {
        "grammar_custom_tools": None,
        "namespace_tools": None,
        "web_search_tool": None,
        "responses_efforts": None,
        "chat_efforts": None,
        "checked_at": 0.0,
    }


def _log(line: str) -> None:
    line = redact_text(line)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_LOG_BYTES:
            LOG_FILE.replace(LOG_FILE.with_suffix(".log.1"))
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {line}\n")
    except OSError:
        pass


def _load_cache() -> None:
    global _profiles
    base = _blank_profile()
    if not CACHE_FILE.exists():
        _profiles = {}
        return
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        _log(f"cache unreadable ({exc}); starting clean")
        _profiles = {}
        return
    out = {}
    for key, prof in (data.get("profiles") or {}).items():
        merged = dict(base)
        if isinstance(prof, dict):
            merged.update({k: v for k, v in prof.items() if k in base})
        out[key] = merged
    _profiles = out


def _save_cache() -> None:
    tmp = CACHE_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(
            redact_data({"upstream": UPSTREAM, "profiles": _profiles}),
            indent=2,
        ),
        encoding="utf-8",
    )
    tmp.replace(CACHE_FILE)


def _profile(model: str, auth: str = "") -> dict:
    key = f"{UPSTREAM}|{model}"
    with _lock:
        prof = _profiles.get(key)
        if (
            prof is None
            or (time.time() - float(prof.get("checked_at") or 0)) > TTL_SECONDS
        ):
            prof = _profiles.setdefault(key, _blank_profile())
            if key not in _probing:
                _probing.add(key)
                threading.Thread(
                    target=_probe, args=(key, model, auth), daemon=True
                ).start()
        return dict(prof)


def _validated_upstream() -> str:
    parsed = urlparse(UPSTREAM)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("SOP_UPSTREAM must be an absolute http(s) URL")
    return UPSTREAM


def _post_upstream(path: str, body: bytes, headers: dict, timeout: int = 120):
    req = urllib.request.Request(
        _validated_upstream() + path, data=body, headers=headers, method="POST"
    )
    # The upstream scheme and host are validated immediately above.
    return urllib.request.urlopen(req, timeout=timeout)  # nosec B310


def _try_shape(model: str, extra: dict, auth: str = "") -> tuple[int, str]:
    """One tiny completion; returns (http_status, first 300 chars of body/text)."""
    payload = {
        "model": model,
        "input": "say OK",
        "stream": False,
        "max_output_tokens": 16,
    }
    payload.update(extra)
    headers = {
        "Content-Type": "application/json",
        "Authorization": auth or "Bearer x",
    }
    try:
        resp = _post_upstream(
            "/v1/responses", json.dumps(payload).encode(), headers, 90
        )
        return resp.status, "ok"
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(301).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)[:300]


def _chat_effort_ok(model: str, effort: str, auth: str = "") -> bool:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "say OK"}],
        "max_tokens": 16,
        "reasoning_effort": effort,
    }
    headers = {"Content-Type": "application/json", "Authorization": auth or "Bearer x"}
    try:
        return (
            _post_upstream(
                "/v1/chat/completions", json.dumps(payload).encode(), headers, 60
            ).status
            == 200
        )
    except Exception:  # noqa: BLE001
        return False


def _probe(key: str, model: str, auth: str = "") -> None:
    """Discover capabilities with throwaway requests, then cache the profile.

    A baseline request with the exact same envelope is checked first: if the
    envelope itself is rejected (e.g. the server does not know
    `max_output_tokens`), every knob probe would come back "rejected" for the
    wrong reason, so probing is abandoned and the profile stays optimistic.
    """
    prof = _blank_profile()
    shapes = {
        "grammar_custom_tools": [
            {
                "type": "custom",
                "name": "apply_patch",
                "description": "p",
                "format": {
                    "type": "grammar",
                    "grammar": {"type": "lark", "definition": "start: /x+/"},
                },
            },
        ],
        "namespace_tools": [
            {
                "type": "namespace",
                "name": "mcp__probe",
                "description": "d",
                "tools": [
                    {
                        "type": "function",
                        "name": "probe_ping",
                        "description": "n",
                        "parameters": {"type": "object", "properties": {}},
                        "strict": False,
                    }
                ],
            },
        ],
        "web_search_tool": [{"type": "web_search", "external_web_access": False}],
    }
    try:
        baseline, detail = _try_shape(model, {}, auth)
        if baseline != 200:
            prof["checked_at"] = 0.0
            _log(
                f"probe aborted: baseline request rejected ({baseline} {detail[:160]})"
            )
            return
        for knob, tools in shapes.items():
            status, detail = _try_shape(model, {"tools": tools}, auth)
            prof[knob] = status == 200
            _log(
                f"probe {knob} -> {status}{'' if status == 200 else ' ' + detail[:140]}"
            )
        freeform, _ = _try_shape(
            model,
            {
                "tools": [
                    {
                        "type": "custom",
                        "name": "apply_patch",
                        "description": "p",
                        "format": {"type": "text"},
                    }
                ]
            },
            auth,
        )
        _log(f"probe freeform_custom -> {freeform}")
        responses_ok = [
            e
            for e in LADDER
            if _try_shape(model, {"reasoning": {"effort": e}}, auth)[0] == 200
        ]
        prof["responses_efforts"] = responses_ok
        prof["chat_efforts"] = [
            e for e in responses_ok if _chat_effort_ok(model, e, auth)
        ]
        _log(f"probe efforts -> responses={responses_ok} chat={prof['chat_efforts']}")
        prof["checked_at"] = time.time()
    finally:
        with _lock:
            _probing.discard(key)
            _profiles[key] = prof
            try:
                _save_cache()
            except OSError as exc:
                _log(f"cache save failed: {exc}")


def clamp_effort(effort: str, allowed: list[str] | None) -> str:
    """Nearest accepted rung; on a tie go down (cheaper) unless SOP_EFFORT_TIE=up."""
    if not allowed or not effort:
        return effort
    if effort in allowed:
        return effort
    rank = LADDER.index(effort) if effort in LADDER else len(LADDER) // 2
    sign = 1 if EFFORT_TIE != "up" else -1
    return min(
        allowed, key=lambda a: (abs(LADDER.index(a) - rank), sign * LADDER.index(a))
    )


def _flatten_namespace(
    ns: dict,
    notes: list[str],
    name_map: dict[str, tuple[str, str]],
) -> list[dict]:
    prefix = str(ns.get("name") or "mcp").replace(".", "_")
    out: list[dict] = []
    for item in ns.get("tools") or []:
        if not (isinstance(item, dict) and item.get("type") == "function"):
            continue
        flat = dict(item)
        original = str(flat.get("name") or "")
        name = (
            original if original.startswith(prefix + "__") else f"{prefix}__{original}"
        )
        flat["name"] = name[:64]
        name_map[flat["name"]] = (prefix, original)
        out.append(flat)
    notes.append(f"flatten:{prefix}({len(out)})")
    return out


def apply_profile(
    data: dict,
    prof: dict,
    kind: str,
) -> tuple[dict, list[str], dict[str, tuple[str, str]]]:
    """Downgrade only where the profile says the endpoint cannot take it.

    Returns ``(payload, notes, name_map)``; ``name_map`` holds this
    request's flattened-name mapping for the response-side restore.
    """
    notes: list[str] = []
    name_map: dict[str, tuple[str, str]] = {}
    tools = data.get("tools")
    if isinstance(tools, list):
        out: list = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            ttype = tool.get("type")
            if ttype == "custom" and prof.get("grammar_custom_tools") is False:
                fmt = tool.get("format")
                if isinstance(fmt, dict) and fmt.get("type") != "text":
                    tool = dict(tool)
                    tool["format"] = {"type": "text"}
                    notes.append(f"grammar->text:{tool.get('name')}")
            elif ttype == "namespace" and prof.get("namespace_tools") is False:
                out.extend(_flatten_namespace(tool, notes, name_map))
                continue
            elif ttype == "web_search" and prof.get("web_search_tool") is False:
                notes.append("drop:web_search")
                continue
            out.append(tool)
        data["tools"] = out

    if kind == "responses":
        allowed = prof.get("responses_efforts")
        reasoning = data.get("reasoning")
        if isinstance(reasoning, dict) and reasoning.get("effort") and allowed:
            new = clamp_effort(str(reasoning["effort"]), allowed)
            if new != reasoning["effort"]:
                data["reasoning"] = {**reasoning, "effort": new}
                notes.append(f"effort:{new}")
    else:
        allowed = prof.get("chat_efforts")
        if data.get("reasoning_effort") and allowed:
            new = clamp_effort(str(data.get("reasoning_effort")), allowed)
            if new != data.get("reasoning_effort"):
                data["reasoning_effort"] = new
                notes.append(f"effort:{new}")
    return data, notes, name_map


REASON_RULES = [
    (re.compile(r"grammar", re.IGNORECASE), "grammar_custom_tools"),
    (re.compile(r"namespace", re.IGNORECASE), "namespace_tools"),
    (re.compile(r"web_search", re.IGNORECASE), "web_search_tool"),
]
SUPPORTED_RE = re.compile(
    r"[Ss]upported (?:types|values|efforts) are ([^.]+)", re.IGNORECASE
)


def learn_from_400(text: str, prof: dict, kind: str) -> bool:
    """Restrictions explained by this 400; True when the profile narrowed."""
    changed = False
    low = text.lower()
    m = SUPPORTED_RE.search(text)
    if m and "reasoning" in low:
        allowed = [
            re.sub(r"\s*\(.*?\)", "", tok).strip().strip(".").strip("'\"").lower()
            for tok in re.split(r",| and ", m.group(1))
            if tok.strip()
        ]
        allowed = [a for a in allowed if a in LADDER]
        if allowed:
            field = "responses_efforts" if kind == "responses" else "chat_efforts"
            if prof.get(field) != allowed:
                prof[field] = allowed
                changed = True
                _log(f"learned {field}={allowed}")
    for pattern, field in REASON_RULES:
        if pattern.search(text) and prof.get(field) is not False:
            prof[field] = False
            changed = True
            _log(f"learned {field}=False from: {text[:160]}")
    return changed


def _restore_names(blob: bytes, name_map: dict[str, tuple[str, str]]) -> bytes:
    """Map flattened tool-call names back so codex can route them."""
    if not name_map:
        return blob
    try:
        data = json.loads(blob.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return blob
    state = {"changed": False}

    def fix(obj) -> None:
        if isinstance(obj, dict):
            name = obj.get("name")
            if isinstance(name, str) and name in name_map:
                ns, original = name_map[name]
                obj["name"] = original
                obj["namespace"] = ns
                state["changed"] = True
            for value in obj.values():
                fix(value)
        elif isinstance(obj, list):
            for value in obj:
                fix(value)

    fix(data)
    return json.dumps(data).encode() if state["changed"] else blob


def _restore_sse_names(frame: bytes, name_map: dict[str, tuple[str, str]]) -> bytes:
    if not name_map:
        return frame
    output = []
    for line in frame.splitlines(keepends=True):
        stripped = line.lstrip()
        if not stripped.startswith(b"data:"):
            output.append(line)
            continue
        prefix = line[: len(line) - len(stripped)]
        payload = stripped[5:].strip()
        restored = _restore_names(payload, name_map)
        if restored == payload:
            output.append(line)
            continue
        newline = (
            b"\r\n"
            if line.endswith(b"\r\n")
            else b"\n"
            if line.endswith(b"\n")
            else b""
        )
        output.append(prefix + b"data: " + restored + newline)
    return b"".join(output)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, headers: dict, payload: bytes) -> None:
        self.send_response(status)
        for k, v in headers.items():
            if k.lower() in (
                "transfer-encoding",
                "connection",
                "content-length",
                "content-encoding",
            ):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def do_GET(self):
        if self.path.startswith("/healthz"):
            with _lock:
                body = json.dumps(
                    redact_data(
                        {
                            "upstream": UPSTREAM,
                            "probing": sorted(_probing),
                            "stats": _stats,
                            "profiles": _profiles,
                        }
                    ),
                    indent=2,
                ).encode()
            self._send(200, {"Content-Type": "application/json"}, body)
            return
        # Read-through for everything else: the UI lists models with
        # `GET {codex_base_url}/models`, and codex_base_url points at this
        # proxy, so a 404 here made the model droplist permanently empty.
        # GETs carry no request body, so no capability rewriting applies.
        self._relay_get()

    def _relay_get(self) -> None:
        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower()
            not in ("host", "content-length", "connection", "accept-encoding")
        }
        try:
            req = urllib.request.Request(
                _validated_upstream() + self.path, headers=headers, method="GET"
            )
            # The upstream scheme and host are validated immediately above.
            resp = urllib.request.urlopen(req, timeout=60)  # nosec B310
        except urllib.error.HTTPError as exc:
            detail = exc.read(MAX_ERROR_BYTES)
            with _lock:
                _stats["errors"] += 1
            _log(f"upstream {exc.code} GET {self.path}: {detail[:220]!r}")
            self._send(
                exc.code,
                {"Content-Type": exc.headers.get("Content-Type", "application/json")},
                detail,
            )
            return
        except Exception as exc:  # noqa: BLE001
            with _lock:
                _stats["errors"] += 1
            _log(f"upstream error GET {self.path}: {exc}")
            self._send(
                502,
                {"Content-Type": "application/json"},
                json.dumps(
                    {"error": {"message": f"proxy upstream error: {exc}"}}
                ).encode(),
            )
            return
        self._relay_response(resp)

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY_BYTES:
            self._send(
                413,
                {"Content-Type": "application/json"},
                b'{"error":"request too large"}',
            )
            self.close_connection = True
            return
        raw = self.rfile.read(n)
        auth = self.headers.get("Authorization") or ""
        if self.path.startswith("/admin/reprobe"):
            expected = f"Bearer {ADMIN_TOKEN}" if ADMIN_TOKEN else ""
            if not expected or not hmac.compare_digest(auth, expected):
                self._send(
                    404, {"Content-Type": "application/json"}, b'{"error":"not found"}'
                )
                return
            with _lock:
                _profiles.clear()
            try:
                _save_cache()
            except OSError:
                pass
            self._send(200, {"Content-Type": "application/json"}, b'{"cleared":true}')
            return

        kind = "chat" if "chat/completions" in self.path else "responses"
        headers_base = {
            k: v
            for k, v in self.headers.items()
            if k.lower()
            not in ("host", "content-length", "connection", "accept-encoding")
        }

        def forward(body: bytes):
            headers = dict(headers_base)
            headers["Content-Length"] = str(len(body))
            return _post_upstream(self.path, body, headers, 3600)

        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            data = None
        if not isinstance(data, dict):
            self._relay(forward, raw)
            return

        model = str(data.get("model") or "")
        with _lock:
            _stats["requests"] += 1
        prof = _profile(model, auth)
        attempts = 0
        while True:
            attempts += 1
            payload, notes, name_map = apply_profile(
                json.loads(json.dumps(data)),
                prof,
                kind,
            )
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if notes:
                with _lock:
                    _stats["rewrites"] += 1
                _log(f"{self.path} model={model} notes={notes}")
            try:
                resp = forward(body)
            except urllib.error.HTTPError as exc:
                detail = exc.read(MAX_ERROR_BYTES)
                text = detail.decode("utf-8", "replace")
                narrowed = attempts <= 4 and learn_from_400(text, prof, kind)
                if exc.code == 400 and narrowed:
                    with _lock:
                        _stats["learned"] += 1
                        _profiles[f"{UPSTREAM}|{model}"] = prof
                        try:
                            _save_cache()
                        except OSError:
                            pass
                    data = payload
                    continue
                with _lock:
                    _stats["errors"] += 1
                _log(f"upstream {exc.code} {self.path} model={model}: {detail[:220]!r}")
                self._send(
                    exc.code,
                    {
                        "Content-Type": exc.headers.get(
                            "Content-Type", "application/json"
                        )
                    },
                    detail,
                )
                return
            except Exception as exc:  # noqa: BLE001
                with _lock:
                    _stats["errors"] += 1
                _log(f"upstream error {self.path}: {exc}")
                self._send(
                    502,
                    {"Content-Type": "application/json"},
                    json.dumps(
                        {"error": {"message": f"proxy upstream error: {exc}"}}
                    ).encode(),
                )
                return
            self._relay_response(resp, name_map)
            return

    def _relay(self, forward, raw: bytes) -> None:
        try:
            resp = forward(raw)
        except Exception as exc:  # noqa: BLE001
            self._send(
                502,
                {"Content-Type": "application/json"},
                json.dumps(
                    {"error": {"message": redact_text(str(exc))}},
                ).encode(),
            )
            return
        self._relay_response(resp)

    def _relay_response(self, resp, name_map: dict | None = None) -> None:
        ctype = resp.headers.get("Content-Type", "")
        if "text/event-stream" in ctype:
            self.send_response(resp.status)
            for k, v in resp.headers.items():
                if k.lower() in (
                    "transfer-encoding",
                    "connection",
                    "content-length",
                    "content-encoding",
                ):
                    continue
                self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            read1 = getattr(resp, "read1", None)
            if read1 is None:
                read1 = lambda size: resp.read(size)
            streamed = 0
            if not name_map:
                while True:
                    chunk = read1(8192)
                    if not chunk:
                        break
                    streamed += len(chunk)
                    if streamed > MAX_STREAM_BYTES:
                        _log("stream response limit exceeded")
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            else:
                pending = b""
                while True:
                    chunk = read1(8192)
                    if not chunk:
                        break
                    streamed += len(chunk)
                    if streamed > MAX_STREAM_BYTES:
                        _log("stream response limit exceeded")
                        break
                    pending += chunk
                    while pending:
                        boundaries = [
                            index
                            for index in (
                                pending.find(b"\n\n"),
                                pending.find(b"\r\n\r\n"),
                            )
                            if index >= 0
                        ]
                        if not boundaries:
                            break
                        index = min(boundaries)
                        delimiter = (
                            b"\r\n\r\n"
                            if pending.startswith(b"\r\n\r\n", index)
                            else b"\n\n"
                        )
                        frame = pending[: index + len(delimiter)]
                        pending = pending[index + len(delimiter) :]
                        self.wfile.write(_restore_sse_names(frame, name_map))
                        self.wfile.flush()
                if pending:
                    self.wfile.write(_restore_sse_names(pending, name_map))
                    self.wfile.flush()
            resp.close()
            return
        payload = resp.read(MAX_RESPONSE_BYTES + 1)
        resp.close()
        if len(payload) > MAX_RESPONSE_BYTES:
            self._send(
                502,
                {"Content-Type": "application/json"},
                b'{"error":"upstream response too large"}',
            )
            return
        if name_map:
            payload = _restore_names(payload, name_map)
        self._send(resp.status, {"Content-Type": ctype or "application/json"}, payload)

    def log_message(self, *args):
        pass


class _BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def process_request(self, request, client_address):
        if not _connections.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
                )
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            _connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            _connections.release()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8321
    if len(sys.argv) > 2:
        UPSTREAM = sys.argv[2].strip().rstrip("/")
    if not UPSTREAM:
        print(
            "no upstream: set SOP_UPSTREAM=http://HOST:PORT (scheme+host+port, "
            "no /v1) or pass it as the second argument",
            file=sys.stderr,
        )
        raise SystemExit(2)
    _validated_upstream()
    if port != 8321:
        # Keep a test instance's artifacts out of the production log/cache.
        LOG_FILE = LOG_FILE.with_name(f"{LOG_FILE.name}.{port}")
        CACHE_FILE = CACHE_FILE.with_name(f"{CACHE_FILE.name}.{port}")
    _load_cache()
    print(
        redact_text(
            f"capability proxy 127.0.0.1:{port} -> {UPSTREAM} (cache={CACHE_FILE})",
        ),
        flush=True,
    )
    _BoundedHTTPServer(("127.0.0.1", port), Handler).serve_forever()
