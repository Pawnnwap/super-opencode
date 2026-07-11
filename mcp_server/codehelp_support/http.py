from __future__ import annotations

import gzip
import io
import json
import threading
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any, Callable

_DEFAULT_TIMEOUT = 10
_UA = "codehelp-mcp/1.0 (super-opencode; +https://github.com/Pawnnwap/super-opencode)"
_CACHE_TTL_S = 15 * 60
_MAX_COMPRESSED_BYTES = 1_000_000
_MAX_DECOMPRESSED_BYTES = 4_000_000
_CACHE: dict[str, tuple[float, Any, str]] = {}
_CACHE_LOCK = threading.Lock()


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


def _http_get(url: str, *, timeout: int = _DEFAULT_TIMEOUT) -> dict[str, Any] | list[Any]:
    data, _ = _http_get_with_meta(url, timeout=timeout)
    return data


def _http_get_with_meta(
    url: str,
    *,
    timeout: int = _DEFAULT_TIMEOUT,
) -> tuple[dict[str, Any] | list[Any], dict[str, Any]]:
    data, metadata = _cached_request(url, "json", timeout=timeout)
    if not isinstance(data, (dict, list)):
        raise ValueError(f"Expected JSON object or list from {url}")
    return data, metadata


def _http_get_text(
    url: str,
    *,
    timeout: int = _DEFAULT_TIMEOUT,
) -> str:
    text, _ = _http_get_text_with_meta(url, timeout=timeout)
    return text


def _http_get_text_with_meta(
    url: str,
    *,
    timeout: int = _DEFAULT_TIMEOUT,
    validate_url: Callable[[str], bool] | None = None,
) -> tuple[str, dict[str, Any]]:
    data, metadata = _cached_request(url, "text", timeout=timeout, validate_url=validate_url)
    if not isinstance(data, str):
        raise ValueError(f"Expected text response from {url}")
    return data, metadata


def _cached_request(
    url: str,
    response_kind: str,
    *,
    timeout: int,
    validate_url: Callable[[str], bool] | None = None,
) -> tuple[Any, dict[str, Any]]:
    cache_key = f"{response_kind}:{url}"
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
    if cached and cached[0] > now:
        return cached[1], {
            "url": url,
            "fetched_at": cached[2],
            "cached": True,
        }

    request = urllib.request.Request(
        url,
        headers={"User-Agent": _UA, "Accept-Encoding": "gzip"},
    )
    if validate_url:
        response_opener = urllib.request.build_opener(_ValidatedRedirectHandler(validate_url)).open
    else:
        response_opener = urllib.request.urlopen
    with response_opener(request, timeout=timeout) as response:
        raw = response.read(_MAX_COMPRESSED_BYTES + 1)
        if len(raw) > _MAX_COMPRESSED_BYTES:
            raise ValueError(f"Response exceeds {_MAX_COMPRESSED_BYTES} byte limit: {url}")
        if response.info().get("Content-Encoding") == "gzip":
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
                raw = compressed.read(_MAX_DECOMPRESSED_BYTES + 1)
            if len(raw) > _MAX_DECOMPRESSED_BYTES:
                raise ValueError(f"Decompressed response exceeds {_MAX_DECOMPRESSED_BYTES} byte limit: {url}")
        charset = response.headers.get_content_charset() or "utf-8"

    if response_kind == "json":
        value: Any = json.loads(raw.decode(charset))
    elif response_kind == "text":
        value = raw.decode(charset, errors="replace")
    else:
        raise ValueError(f"Unsupported response kind: {response_kind}")

    fetched_at = datetime.now(UTC).isoformat()
    with _CACHE_LOCK:
        _CACHE[cache_key] = (time.monotonic() + _CACHE_TTL_S, value, fetched_at)
    return value, {"url": response.geturl(), "fetched_at": fetched_at, "cached": False}
