"""Streaming proxy responses must flush promptly and restore MCP tool names."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _proxy():
    path = Path(__file__).resolve().parents[1] / "codex_compat_proxy.py"
    spec = importlib.util.spec_from_file_location("codex_compat_proxy_sse_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response:
    status = 200

    def __init__(self, chunks, content_type="text/event-stream"):
        self.headers = {"Content-Type": content_type}
        self._chunks = list(chunks)
        self.read_calls = 0
        self.read1_calls = 0
        self.closed = False

    def read1(self, _size):
        self.read1_calls += 1
        return self._chunks.pop(0) if self._chunks else b""

    def read(self, _size=None):
        self.read_calls += 1
        raise AssertionError("SSE must not use buffered read()")

    def close(self):
        self.closed = True


class _Writer:
    def __init__(self):
        self.chunks = []
        self.flushes = 0

    def write(self, data):
        self.chunks.append(data)

    def flush(self):
        self.flushes += 1


class _Handler:
    def __init__(self):
        self.wfile = _Writer()
        self.close_connection = False
        self.status = None
        self.headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers.append((key, value))

    def end_headers(self):
        pass


def test_sse_uses_incremental_reads_and_restores_split_names():
    proxy = _proxy()
    response = _Response(
        [
            b'data: {"type":"function_call","name":"alpha__shared"}\n',
            b"\n",
            b"data: [DONE]\n\n",
        ]
    )
    handler = _Handler()

    proxy.Handler._relay_response(
        handler,
        response,
        {"alpha__shared": ("alpha", "shared")},
    )

    output = b"".join(handler.wfile.chunks)
    assert response.read_calls == 0
    assert response.read1_calls == 4
    assert handler.wfile.flushes >= 2
    assert b'"name": "shared"' in output
    assert b'"namespace": "alpha"' in output
    assert b"alpha__shared" not in output
    assert b"data: [DONE]" in output
    assert response.closed


def test_sse_restores_multiple_events_in_one_chunk():
    proxy = _proxy()
    response = _Response(
        [
            b'data: {"name":"alpha__one"}\n\ndata: {"name":"alpha__two"}\n\n',
        ]
    )
    handler = _Handler()

    proxy.Handler._relay_response(
        handler,
        response,
        {
            "alpha__one": ("alpha", "one"),
            "alpha__two": ("alpha", "two"),
        },
    )

    output = b"".join(handler.wfile.chunks)
    assert output.count(b'"namespace": "alpha"') == 2
    assert b'"name": "one"' in output
    assert b'"name": "two"' in output


def test_sse_without_name_map_streams_raw_chunks():
    proxy = _proxy()
    response = _Response([b'data: {"text":"hi"}\n\n', b"data: [DONE]\n\n"])
    handler = _Handler()

    proxy.Handler._relay_response(handler, response, None)

    assert b"".join(handler.wfile.chunks) == b'data: {"text":"hi"}\n\ndata: [DONE]\n\n'
    assert response.read_calls == 0
    assert handler.wfile.flushes == 2
