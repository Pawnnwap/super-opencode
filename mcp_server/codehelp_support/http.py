from __future__ import annotations

import gzip
import http.client
import io
import ipaddress
import json
import os
import socket
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import Any

_DEFAULT_TIMEOUT = 10
_UA = "codehelp-mcp/1.0 (super-opencode; +https://github.com/Pawnnwap/super-opencode)"
_CACHE_TTL_S = 15 * 60
_MAX_COMPRESSED_BYTES = 1_000_000
_MAX_DECOMPRESSED_BYTES = 4_000_000
_MAX_CACHE_BYTES = int(os.environ.get("SOP_HTTP_CACHE_BYTES", str(32 * 1024 * 1024)))
_MAX_CACHE_ENTRIES = int(os.environ.get("SOP_HTTP_CACHE_ENTRIES", "128"))
_CACHE: OrderedDict[str, tuple[float, Any, str, int]] = OrderedDict()
_CACHE_BYTES = 0
_CACHE_LOCK = threading.Lock()


def _cache_get(key: str, now: float):
    global _CACHE_BYTES
    with _CACHE_LOCK:
        item = _CACHE.get(key)
        if item is None:
            return None
        if item[0] <= now:
            _CACHE.pop(key, None)
            _CACHE_BYTES -= item[3]
            return None
        _CACHE.move_to_end(key)
        return item[1], item[2]


def _cache_put(key: str, value: Any, expires_at: float, fetched_at: str) -> None:
    global _CACHE_BYTES
    size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    if size > _MAX_CACHE_BYTES:
        return
    with _CACHE_LOCK:
        old = _CACHE.pop(key, None)
        if old is not None:
            _CACHE_BYTES -= old[3]
        _CACHE[key] = (expires_at, value, fetched_at, size)
        _CACHE_BYTES += size
        while len(_CACHE) > _MAX_CACHE_ENTRIES or _CACHE_BYTES > _MAX_CACHE_BYTES:
            _old_key, (_expired, _value, _stamp, old_size) = _CACHE.popitem(last=False)
            _CACHE_BYTES -= old_size


def is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def resolve_public_addresses(
    host: str,
    port: int,
    resolver: Callable[[str, int], list[str]] | None = None,
) -> list[str]:
    if resolver is None:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = [str(info[4][0]) for info in infos]
    else:
        addresses = list(resolver(host, port))
    if not addresses or any(not is_public_address(address) for address in addresses):
        raise ValueError("hostname must resolve only to public addresses")
    return list(dict.fromkeys(addresses))


def _connect_public(
    host: str,
    port: int,
    timeout: int,
    source_address,
    resolver: Callable[[str, int], list[str]] | None,
):
    last_error: OSError | None = None
    for address in resolve_public_addresses(host, port, resolver):
        try:
            return socket.create_connection(
                (address, port),
                timeout,
                source_address,
            )
        except OSError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise OSError("no public address available")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, **kwargs):
        self._resolver = kwargs.pop("resolver", None)
        super().__init__(host, **kwargs)

    def connect(self):
        self.sock = _connect_public(
            self.host,
            self.port,
            self.timeout,
            self.source_address,
            self._resolver,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, **kwargs):
        self._resolver = kwargs.pop("resolver", None)
        super().__init__(host, **kwargs)

    def connect(self):
        raw = _connect_public(
            self.host,
            self.port,
            self.timeout,
            self.source_address,
            self._resolver,
        )
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, resolver=None):
        super().__init__()
        self._resolver = resolver

    def http_open(self, req):
        return self.do_open(
            partial(_PinnedHTTPConnection, resolver=self._resolver),
            req,
        )


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, resolver=None):
        super().__init__()
        self._resolver = resolver

    def https_open(self, req):
        return self.do_open(
            partial(_PinnedHTTPSConnection, resolver=self._resolver),
            req,
        )


class _ValidatedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects outside caller-provided URL policy."""

    def __init__(self, validator: Callable[[str], bool]) -> None:
        super().__init__()
        self._validator = validator

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        absolute_url = urllib.parse.urljoin(req.full_url, newurl)
        if not self._validator(absolute_url):
            raise ValueError(f"Redirect target rejected by URL policy: {absolute_url}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _http_get(
    url: str, *, timeout: int = _DEFAULT_TIMEOUT
) -> dict[str, Any] | list[Any]:
    data, _ = _http_get_with_meta(url, timeout=timeout)
    return data


def _http_get_with_meta(
    url: str,
    *,
    timeout: int = _DEFAULT_TIMEOUT,
    resolver: Callable[[str, int], list[str]] | None = None,
) -> tuple[dict[str, Any] | list[Any], dict[str, Any]]:
    data, metadata = _cached_request(url, "json", timeout=timeout, resolver=resolver)
    if not isinstance(data, (dict, list)):
        raise ValueError(f"Expected JSON object or list from {url}")  # noqa: TRY004 - payload shape, not argument type
    return data, metadata


def _http_get_text(
    url: str,
    *,
    timeout: int = _DEFAULT_TIMEOUT,
    validate_url: Callable[[str], bool] | None = None,
    resolver: Callable[[str, int], list[str]] | None = None,
) -> str:
    text, _ = _http_get_text_with_meta(
        url,
        timeout=timeout,
        validate_url=validate_url,
        resolver=resolver,
    )
    return text


def _http_get_text_with_meta(
    url: str,
    *,
    timeout: int = _DEFAULT_TIMEOUT,
    validate_url: Callable[[str], bool] | None = None,
    resolver: Callable[[str, int], list[str]] | None = None,
) -> tuple[str, dict[str, Any]]:
    data, metadata = _cached_request(
        url,
        "text",
        timeout=timeout,
        validate_url=validate_url,
        resolver=resolver,
    )
    if not isinstance(data, str):
        raise ValueError(f"Expected text response from {url}")  # noqa: TRY004 - payload shape, not argument type
    return data, metadata


def _cached_request(
    url: str,
    response_kind: str,
    *,
    timeout: int,
    validate_url: Callable[[str], bool] | None = None,
    resolver: Callable[[str, int], list[str]] | None = None,
) -> tuple[Any, dict[str, Any]]:
    cache_key = f"{response_kind}:{url}"
    now = time.monotonic()
    cached = _cache_get(cache_key, now)
    if cached is not None:
        value, fetched_at = cached
        return value, {
            "url": url,
            "fetched_at": fetched_at,
            "cached": True,
        }

    request = urllib.request.Request(
        url,
        headers={"User-Agent": _UA, "Accept-Encoding": "gzip"},
    )
    handlers = [
        _PinnedHTTPHandler(resolver),
        _PinnedHTTPSHandler(resolver),
        urllib.request.ProxyHandler({}),
    ]
    if validate_url:
        handlers.append(_ValidatedRedirectHandler(validate_url))
    response_opener = urllib.request.build_opener(*handlers).open
    with response_opener(request, timeout=timeout) as response:
        raw = response.read(_MAX_COMPRESSED_BYTES + 1)
        if len(raw) > _MAX_COMPRESSED_BYTES:
            raise ValueError(
                f"Response exceeds {_MAX_COMPRESSED_BYTES} byte limit: {url}"
            )
        if response.info().get("Content-Encoding") == "gzip":
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
                raw = compressed.read(_MAX_DECOMPRESSED_BYTES + 1)
            if len(raw) > _MAX_DECOMPRESSED_BYTES:
                raise ValueError(
                    f"Decompressed response exceeds {_MAX_DECOMPRESSED_BYTES} byte limit: {url}"
                )
        charset = response.headers.get_content_charset() or "utf-8"

    if response_kind == "json":
        value: Any = json.loads(raw.decode(charset))
    elif response_kind == "text":
        value = raw.decode(charset, errors="replace")
    else:
        raise ValueError(f"Unsupported response kind: {response_kind}")

    fetched_at = datetime.now(UTC).isoformat()
    _cache_put(cache_key, value, time.monotonic() + _CACHE_TTL_S, fetched_at)
    return value, {"url": response.geturl(), "fetched_at": fetched_at, "cached": False}
