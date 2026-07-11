from __future__ import annotations

import re
import ipaddress
from urllib.parse import urlparse
from typing import Any

from .http import _http_get_text_with_meta
from .packages import _codehelp_search_package_version


def _text_excerpt(text: str, max_chars: int) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars] + ("..." if len(text) > max_chars else "")


def _safe_docs_url(url: str) -> bool:
    """Allow only public HTTPS documentation URLs declared by a registry."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    try:
        return not ipaddress.ip_address(parsed.hostname).is_private
    except ValueError:
        return parsed.hostname.lower() != "localhost"


def _codehelp_fetch_official_docs(
    package_name: str,
    ecosystem: str = "auto",
    query: str = "",
    max_chars: int = 4_000,
) -> dict[str, Any]:
    registry = _codehelp_search_package_version(package_name, ecosystem)
    if registry.get("status") != "ok":
        return registry
    docs_url = registry.get("docs_url") or registry.get("homepage")
    if not isinstance(docs_url, str) or not _safe_docs_url(docs_url):
        return {
            "status": "error",
            "error_type": "unsafe_or_missing_docs_url",
            "error": "Registry metadata does not provide a public HTTPS documentation URL.",
            "package": package_name,
            "provenance": registry.get("provenance", []),
        }
    max_chars = max(200, min(max_chars, 8_000))
    try:
        text, document_provenance = _http_get_text_with_meta(docs_url, validate_url=_safe_docs_url)
    except Exception as exc:
        return {
            "status": "error",
            "error_type": "fetch_failed",
            "error": str(exc),
            "package": package_name,
            "docs_url": docs_url,
            "provenance": registry.get("provenance", []),
        }
    return {
        "status": "ok",
        "package": registry["package"],
        "ecosystem": registry["ecosystem"],
        "docs_url": docs_url,
        "query": query,
        "content": _text_excerpt(text, max_chars),
        "authority": "URL declared by the package registry; verify project ownership before relying on it.",
        "provenance": [*registry.get("provenance", []), {"source": "registry_declared_docs", **document_provenance}],
    }
