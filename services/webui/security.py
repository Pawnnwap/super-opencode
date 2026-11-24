"""Authentication and browser-origin policy for the loopback NiceGUI app."""

from __future__ import annotations

import hmac
import os
import secrets
import time
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_COOKIE_NAME = "sop_ui_token"
_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _load_or_create(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(32)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return value
    except FileExistsError:
        pass
    for _ in range(50):
        try:
            existing = path.read_text(encoding="utf-8").strip()
            if len(existing) >= 32:
                return existing
        except OSError:
            pass
        time.sleep(0.01)
    raise RuntimeError(f"invalid UI secret file: {path}")


def _headers(scope) -> dict[bytes, bytes]:
    return {key.lower(): value for key, value in scope.get("headers", [])}


def _host_parts(value: str, default_port: int) -> tuple[str, int] | None:
    try:
        parsed = urlparse(f"//{value}")
        host = (parsed.hostname or "").lower()
        port = parsed.port or default_port
    except ValueError:
        return None
    return host, port


class LocalUIAuth:
    def __init__(self, token_path: Path, storage_secret_path: Path):
        self.token_path = Path(token_path)
        self.storage_secret_path = Path(storage_secret_path)
        self._token: str | None = None
        self._storage_secret: str | None = None
        self._token = _load_or_create(self.token_path)
        self._storage_secret = _load_or_create(self.storage_secret_path)

    def token(self) -> str:
        if self._token is None:
            self._token = _load_or_create(self.token_path)
        return self._token

    def storage_secret(self) -> str:
        if self._storage_secret is None:
            self._storage_secret = _load_or_create(self.storage_secret_path)
        return self._storage_secret

    def valid(self, value: str | None) -> bool:
        return bool(value) and hmac.compare_digest(value, self.token())


class LocalUIAuthMiddleware:
    def __init__(self, app, *, auth: LocalUIAuth):
        self.app = app
        self.auth = auth

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers = _headers(scope)
        default_port = int(scope.get("server", ("127.0.0.1", 80))[1] or 80)
        host = _host_parts(headers.get(b"host", b"").decode("latin-1"), default_port)
        if host is None or host[0] not in _ALLOWED_HOSTS:
            await self._deny(scope, send, 403, "forbidden host")
            return
        origin = headers.get(b"origin")
        if origin:
            parsed = urlparse(origin.decode("latin-1"))
            try:
                origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
            except ValueError:
                await self._deny(scope, send, 403, "forbidden origin")
                return
            if (
                parsed.scheme != "http"
                or (parsed.hostname or "").lower() not in _ALLOWED_HOSTS
                or origin_port != host[1]
            ):
                await self._deny(scope, send, 403, "forbidden origin")
                return
        elif scope["type"] == "websocket":
            await self._deny(scope, send, 403, "missing origin")
            return

        method = scope.get("method", "GET").upper()
        if (
            scope["type"] == "http"
            and scope.get("path") == "/healthz"
            and method in {"GET", "HEAD"}
        ):
            await self.app(scope, receive, send)
            return

        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        supplied = query.get("token", [None])[0]
        if scope["type"] == "http" and method == "GET" and self.auth.valid(supplied):
            cookie = (
                f"{_COOKIE_NAME}={self.auth.token()}; Path=/; HttpOnly; SameSite=Strict"
            )
            await self._respond(
                send,
                302,
                [
                    (b"location", scope.get("path", "/").encode()),
                    (b"set-cookie", cookie.encode()),
                ],
            )
            return

        authorization = headers.get(b"authorization", b"").decode("latin-1")
        bearer = (
            authorization[7:] if authorization.lower().startswith("bearer ") else None
        )
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get(b"cookie", b"").decode("latin-1"))
        except CookieError:
            cookie = SimpleCookie()
        cookie_token = (
            cookie.get(_COOKIE_NAME).value if _COOKIE_NAME in cookie else None
        )
        if not (self.auth.valid(bearer) or self.auth.valid(cookie_token)):
            await self._deny(scope, send, 401, "authentication required")
            return
        if (
            scope["type"] == "http"
            and method in {"POST", "PUT", "PATCH", "DELETE"}
            and not origin
        ):
            await self._deny(scope, send, 403, "origin required")
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _deny(scope, send, status: int, message: str) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await LocalUIAuthMiddleware._respond(
            send,
            status,
            [(b"content-type", b"text/plain")],
            message.encode(),
        )

    @staticmethod
    async def _respond(
        send, status: int, headers: list[tuple[bytes, bytes]], body: bytes = b""
    ) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": headers,
            }
        )
        await send({"type": "http.response.body", "body": body})
