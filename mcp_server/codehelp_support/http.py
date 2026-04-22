from __future__ import annotations

import gzip
import json
import urllib.request
from typing import Any

_DEFAULT_TIMEOUT = 10
_UA = "codehelp-mcp/1.0 (super-opencode; +https://github.com/Pawnnwap/super-opencode)"


def _http_get(url: str, *, timeout: int = _DEFAULT_TIMEOUT) -> dict[str, Any] | list[Any]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": _UA, "Accept-Encoding": "gzip"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        if response.info().get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))
