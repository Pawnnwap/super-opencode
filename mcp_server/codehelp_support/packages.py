from __future__ import annotations

import urllib.error
import urllib.parse
from typing import Any

from .http import _http_get_with_meta

_PYPI_URL = "https://pypi.org/pypi/{package}/json"
_NPM_URL = "https://registry.npmjs.org/{package}/latest"


def _pypi_version(package: str) -> dict[str, Any]:
    url = _PYPI_URL.format(package=urllib.parse.quote(package, safe=""))
    data, provenance = _http_get_with_meta(url)
    info = data.get("info", {})
    releases = data.get("releases", {})
    latest = info.get("version", "unknown")
    project_urls = info.get("project_urls") or {}
    documentation_url = next(
        (
            value
            for key, value in project_urls.items()
            if key.lower() in {"documentation", "docs", "api reference"}
            and isinstance(value, str)
        ),
        "",
    )

    published_date = "unknown"
    if latest in releases:
        files = releases[latest]
        if files:
            published_date = files[0].get("upload_time", "unknown")
            if published_date != "unknown":
                published_date = published_date[:10]

    return {
        "status": "ok",
        "ecosystem": "PyPI",
        "package": package,
        "latest_version": latest,
        "published_date": published_date,
        "summary": info.get("summary", ""),
        "homepage": info.get("home_page") or info.get("project_url") or f"https://pypi.org/project/{package}/",
        "docs_url": info.get("docs_url") or documentation_url,
        "project_urls": project_urls,
        "requires_python": info.get("requires_python") or "",
        "source": _PYPI_URL.format(package=package),
        "provenance": [{"source": "pypi_registry", **provenance}],
    }


def _npm_version(package: str) -> dict[str, Any]:
    url = _NPM_URL.format(package=urllib.parse.quote(package, safe="@/"))
    data, provenance = _http_get_with_meta(url)
    return {
        "status": "ok",
        "ecosystem": "npm",
        "package": package,
        "latest_version": data.get("version", "unknown"),
        "published_date": "see npm",
        "summary": data.get("description", ""),
        "homepage": data.get("homepage", f"https://www.npmjs.com/package/{package}"),
        "docs_url": data.get("homepage", ""),
        "project_urls": {"homepage": data.get("homepage", "")},
        "source": f"https://registry.npmjs.org/{package}/latest",
        "provenance": [{"source": "npm_registry", **provenance}],
    }


def _codehelp_search_package_version(package_name: str, ecosystem: str = "auto") -> dict[str, Any]:
    package_name = package_name.strip()
    if not package_name:
        return {"error": "package_name must not be empty"}

    ecosystem = ecosystem.lower()
    errors: list[str] = []

    if ecosystem in ("auto", "pypi", "python"):
        try:
            return _pypi_version(package_name)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                errors.append(f"PyPI: package '{package_name}' not found (404)")
            else:
                errors.append(f"PyPI HTTP {exc.code}: {exc.reason}")
        except Exception as exc:
            errors.append(f"PyPI: {exc}")

    if ecosystem in ("auto", "npm", "node", "javascript", "js"):
        try:
            return _npm_version(package_name)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                errors.append(f"npm: package '{package_name}' not found (404)")
            else:
                errors.append(f"npm HTTP {exc.code}: {exc.reason}")
        except Exception as exc:
            errors.append(f"npm: {exc}")

    return {
        "status": "error",
        "error": "Package not found in any registry",
        "error_type": "not_found",
        "package": package_name,
        "tried": errors,
        "provenance": [],
    }
