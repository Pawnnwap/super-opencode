"""Capability proxy administration and resource use are bounded."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _proxy():
    path = Path(__file__).resolve().parents[1] / "codex_compat_proxy.py"
    spec = importlib.util.spec_from_file_location(
        "codex_compat_proxy_limits_test", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Writer:
    def __init__(self):
        self.data = b""

    def write(self, data):
        self.data += data

    def flush(self):
        pass


class _Handler:
    def __init__(self, path, headers):
        self.path = path
        self.headers = headers
        self.wfile = _Writer()
        self.close_connection = False
        self.status = None
        self.response_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass

    def _send(self, status, headers, payload):
        self.status = status
        self.response_headers.update(headers)
        self.wfile.write(payload)


def test_oversized_post_is_refused_before_read(monkeypatch):
    proxy = _proxy()
    monkeypatch.setattr(proxy, "MAX_BODY_BYTES", 10, raising=False)
    handler = _Handler("/v1/responses", {"Content-Length": "11"})

    class _Read:
        def read(self, _size):
            raise AssertionError("oversized body was read")

    handler.rfile = _Read()
    proxy.Handler.do_POST(handler)

    assert handler.status == 413
    assert handler.close_connection is True


def test_admin_reprobe_requires_configured_token(monkeypatch):
    proxy = _proxy()
    monkeypatch.setattr(proxy, "ADMIN_TOKEN", "", raising=False)
    monkeypatch.setattr(proxy, "_save_cache", lambda: None)
    handler = _Handler("/admin/reprobe", {})

    class _Read:
        def read(self, _size):
            return b""

    handler.rfile = _Read()
    proxy.Handler.do_POST(handler)

    assert handler.status == 404


def test_probe_auth_is_request_scoped(monkeypatch):
    proxy = _proxy()
    seen = {}

    class _Response:
        status = 200

    def _post(path, body, headers, timeout):
        seen["headers"] = headers
        return _Response()

    monkeypatch.setattr(proxy, "_post_upstream", _post)

    assert proxy._try_shape("model", {}, auth="Bearer request-secret")[0] == 200
    assert seen["headers"]["Authorization"] == "Bearer request-secret"


def test_server_and_nonstream_response_are_bounded():
    source = (Path(__file__).resolve().parents[1] / "codex_compat_proxy.py").read_text(
        encoding="utf-8",
    )
    assert "_BoundedHTTPServer" in source
    assert "MAX_RESPONSE_BYTES" in source
    assert "resp.read(MAX_RESPONSE_BYTES + 1)" in source
