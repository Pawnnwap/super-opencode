"""DNS and connection policy for agent-supplied HTTP URLs."""

from __future__ import annotations

import socket

import pytest

from mcp_server.codehelp_support.docs import is_public_http_url


def _answers(*addresses: str):
    return [
        (
            socket.AF_INET6 if ":" in address else socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            (address, 443),
        )
        for address in addresses
    ]


@pytest.mark.parametrize(
    "addresses",
    [
        ("127.0.0.1",),
        ("10.0.0.8",),
        ("169.254.169.254",),
        ("::1",),
        ("93.184.216.34", "192.168.1.2"),
    ],
)
def test_any_non_public_dns_answer_refuses_the_url(monkeypatch, addresses):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: _answers(*addresses))

    assert is_public_http_url("https://docs.example.test/guide") is False


def test_dns_failure_refuses_the_url(monkeypatch):
    def _fail(*_args, **_kwargs):
        raise socket.gaierror("dns unavailable")

    monkeypatch.setattr(socket, "getaddrinfo", _fail)

    assert is_public_http_url("https://docs.example.test/guide") is False


def test_all_public_dns_answers_allow_the_url(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_k: _answers(
            "93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"
        ),
    )

    assert is_public_http_url("https://docs.example.test/guide") is True


def test_http_connection_pins_a_validated_address(monkeypatch):
    from mcp_server.codehelp_support.http import _PinnedHTTPConnection

    connected: list[tuple] = []
    fake_socket = object()

    def _connect(address, *args, **kwargs):
        connected.append(address)
        return fake_socket

    monkeypatch.setattr(socket, "create_connection", _connect)
    connection = _PinnedHTTPConnection(
        "docs.example.test",
        port=443,
        timeout=5,
        resolver=lambda _host, _port: ["93.184.216.34"],
    )

    connection.connect()

    assert connected == [("93.184.216.34", 443)]
    assert connection.sock is fake_socket


def test_http_connection_refuses_mixed_dns_without_connecting(monkeypatch):
    from mcp_server.codehelp_support.http import _PinnedHTTPConnection

    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not connect")),
    )
    connection = _PinnedHTTPConnection(
        "docs.example.test",
        resolver=lambda _host, _port: ["93.184.216.34", "127.0.0.1"],
    )

    with pytest.raises(ValueError, match="public addresses"):
        connection.connect()


def test_https_connection_preserves_sni_hostname(monkeypatch):
    from mcp_server.codehelp_support.http import _PinnedHTTPSConnection

    raw_socket = object()
    tls_socket = object()
    seen: list[str] = []

    class _Context:
        def wrap_socket(self, sock, *, server_hostname):
            assert sock is raw_socket
            seen.append(server_hostname)
            return tls_socket

    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: raw_socket)
    connection = _PinnedHTTPSConnection(
        "docs.example.test",
        port=443,
        timeout=5,
        context=_Context(),
        resolver=lambda _host, _port: ["93.184.216.34"],
    )

    connection.connect()

    assert seen == ["docs.example.test"]
    assert connection.sock is tls_socket


def test_http_cache_is_byte_bounded(monkeypatch):
    from mcp_server.codehelp_support import http as http_mod

    monkeypatch.setattr(http_mod, "_MAX_CACHE_BYTES", 100)
    monkeypatch.setattr(http_mod, "_MAX_CACHE_ENTRIES", 2)
    http_mod._CACHE.clear()
    http_mod._CACHE_BYTES = 0

    http_mod._cache_put("a", "x" * 80, 9999999999.0, "now")
    http_mod._cache_put("b", "y" * 80, 9999999999.0, "now")

    assert "a" not in http_mod._CACHE
    assert "b" in http_mod._CACHE
    assert http_mod._CACHE_BYTES <= 100
