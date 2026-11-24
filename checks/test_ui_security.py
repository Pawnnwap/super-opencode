"""Local UI ingress must reject cross-origin and unauthenticated clients."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from services.webui.security import LocalUIAuth, LocalUIAuthMiddleware


def _scope(path="/", *, method="GET", headers=None, query=b""):
    items = list(headers or [])
    if not any(key == b"host" for key, _value in items):
        items.append((b"host", b"127.0.0.1:8501"))
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "root_path": "",
        "headers": items,
        "server": ("127.0.0.1", 8501),
        "client": ("127.0.0.1", 12345),
    }


async def _call(middleware, scope):
    sent = []

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message):
        sent.append(message)

    await middleware(scope, _receive, _send)
    return sent


def _header(messages, name):
    start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    if name == b":status":
        return str(start["status"])
    value = next(value for key, value in start["headers"] if key == name)
    return value.decode()


def test_token_and_storage_secret_are_random_and_persistent(tmp_path):
    auth = LocalUIAuth(tmp_path / "ui.token", tmp_path / "ui.storage_secret")
    token = auth.token()
    secret = auth.storage_secret()

    assert (tmp_path / "ui.token").is_file()
    assert (tmp_path / "ui.storage_secret").is_file()

    assert len(token) >= 32
    assert len(secret) >= 32
    assert token != "opencode-supervisor"
    again = LocalUIAuth(tmp_path / "ui.token", tmp_path / "ui.storage_secret")
    assert again.token() == token
    assert again.storage_secret() == secret


def test_concurrent_starters_share_one_token(tmp_path):
    tokens = []
    barrier = threading.Barrier(6)

    def _start():
        barrier.wait()
        auth = LocalUIAuth(tmp_path / "ui.token", tmp_path / "ui.storage_secret")
        tokens.append(auth.token())

    threads = [threading.Thread(target=_start) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert len(tokens) == 6
    assert len(set(tokens)) == 1
    assert (tmp_path / "ui.token").read_text(encoding="utf-8").strip() == tokens[0]


def test_healthz_is_public_but_ui_requires_a_token(tmp_path):
    auth = LocalUIAuth(tmp_path / "ui.token", tmp_path / "ui.storage_secret")

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = LocalUIAuthMiddleware(downstream, auth=auth)
    health = asyncio.run(_call(middleware, _scope("/healthz")))
    denied = asyncio.run(_call(middleware, _scope("/")))

    assert _header(health, b":status") == "204"
    assert _header(denied, b":status") == "401"


def test_bearer_and_cookie_tokens_are_accepted(tmp_path):
    auth = LocalUIAuth(tmp_path / "ui.token", tmp_path / "ui.storage_secret")

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = LocalUIAuthMiddleware(downstream, auth=auth)
    bearer = asyncio.run(
        _call(
            middleware,
            _scope(
                headers=[
                    (b"authorization", f"Bearer {auth.token()}".encode()),
                ]
            ),
        )
    )
    cookie = asyncio.run(
        _call(
            middleware,
            _scope(
                headers=[
                    (b"cookie", f"sop_ui_token={auth.token()}".encode()),
                ]
            ),
        )
    )

    assert _header(bearer, b":status") == "204"
    assert _header(cookie, b":status") == "204"


def test_token_query_bootstraps_a_strict_httponly_cookie(tmp_path):
    auth = LocalUIAuth(tmp_path / "ui.token", tmp_path / "ui.storage_secret")

    async def downstream(scope, receive, send):
        raise AssertionError("bootstrap must not reach the UI")

    middleware = LocalUIAuthMiddleware(downstream, auth=auth)
    messages = asyncio.run(
        _call(
            middleware,
            _scope("/wizard", query=f"token={auth.token()}".encode()),
        )
    )

    assert _header(messages, b":status") == "302"
    cookie = _header(messages, b"set-cookie")
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert auth.token() in cookie


def test_cross_origin_and_foreign_host_are_refused(tmp_path):
    auth = LocalUIAuth(tmp_path / "ui.token", tmp_path / "ui.storage_secret")

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = LocalUIAuthMiddleware(downstream, auth=auth)
    headers = [
        (b"authorization", f"Bearer {auth.token()}".encode()),
        (b"origin", b"https://evil.example"),
        (b"host", b"127.0.0.1:8501"),
    ]
    cross_origin = asyncio.run(
        _call(middleware, _scope(headers=headers, method="POST"))
    )
    foreign_host = asyncio.run(
        _call(
            middleware,
            _scope(
                headers=[
                    (b"authorization", f"Bearer {auth.token()}".encode()),
                    (b"host", b"evil.example"),
                ]
            ),
        )
    )

    assert _header(cross_origin, b":status") == "403"
    assert _header(foreign_host, b":status") == "403"


def test_same_origin_and_authenticated_websocket_are_allowed(tmp_path):
    auth = LocalUIAuth(tmp_path / "ui.token", tmp_path / "ui.storage_secret")
    reached = []

    async def downstream(scope, receive, send):
        reached.append(scope)

    middleware = LocalUIAuthMiddleware(downstream, auth=auth)
    scope = _scope(
        "/_nicegui_ws/socket.io/",
        headers=[
            (b"host", b"127.0.0.1:8501"),
            (b"origin", b"http://127.0.0.1:8501"),
            (b"cookie", f"sop_ui_token={auth.token()}".encode()),
        ],
    )
    scope["type"] = "websocket"
    asyncio.run(_call(middleware, scope))

    assert reached == [scope]


def test_unauthenticated_websocket_is_closed(tmp_path):
    auth = LocalUIAuth(tmp_path / "ui.token", tmp_path / "ui.storage_secret")
    sent = []

    async def downstream(scope, receive, send):
        raise AssertionError("unauthenticated socket reached downstream")

    async def _receive():
        return {"type": "websocket.connect"}

    async def _send(message):
        sent.append(message)

    scope = _scope(
        "/_nicegui_ws/socket.io/",
        headers=[
            (b"origin", b"http://127.0.0.1:8501"),
        ],
    )
    scope["type"] = "websocket"
    asyncio.run(LocalUIAuthMiddleware(downstream, auth=auth)(scope, _receive, _send))

    assert sent == [{"type": "websocket.close", "code": 1008}]


def test_cookie_post_without_origin_is_refused(tmp_path):
    auth = LocalUIAuth(tmp_path / "ui.token", tmp_path / "ui.storage_secret")

    async def downstream(scope, receive, send):
        raise AssertionError("CSRF request reached downstream")

    messages = asyncio.run(
        _call(
            LocalUIAuthMiddleware(downstream, auth=auth),
            _scope(
                "/jobs/run/open-folder",
                method="POST",
                headers=[
                    (b"cookie", f"sop_ui_token={auth.token()}".encode()),
                ],
            ),
        )
    )

    assert _header(messages, b":status") == "403"


def test_app_wires_random_secret_and_ingress_middleware():
    source = Path(__file__).resolve().parents[1] / "app.py"
    text = source.read_text(encoding="utf-8")

    assert "app.add_middleware(LocalUIAuthMiddleware, auth=_UI_AUTH)" in text
    assert "storage_secret=_UI_AUTH.storage_secret()" in text
    assert 'storage_secret="opencode-supervisor"' not in text
