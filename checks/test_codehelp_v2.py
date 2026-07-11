from __future__ import annotations

import json

from mcp_server.codehelp_support.dependencies import _codehelp_analyze_dependencies
from mcp_server.codehelp_support.docs import _codehelp_fetch_official_docs, _safe_docs_url
from mcp_server.codehelp_support.packages import _pypi_version


def test_analyze_dependency_reads_python_and_node_manifests(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        """[project]
dependencies = ["requests>=2.0", "rich==13.7.0"]

[project.optional-dependencies]
dev = ["pytest>=8"]
""",
        encoding="utf-8",
    )
    (tmp_path / "requirements-dev.txt").write_text("numpy~=1.26\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps(
            {"dependencies": {"react": "^19.0.0"}, "devDependencies": {"typescript": "~5.0.0"}},
        ),
        encoding="utf-8",
    )
    (tmp_path / "package-lock.json").write_text(
        json.dumps({"packages": {"node_modules/react": {"version": "19.0.1"}}}),
        encoding="utf-8",
    )

    result = _codehelp_analyze_dependencies(str(tmp_path))

    assert result["status"] == "ok"
    found = {(item["ecosystem"], item["name"]): item for item in result["dependencies"]}
    assert found[("pypi", "requests")]["compatibility_risk"] == "bounded range"
    assert found[("pypi", "rich")]["compatibility_risk"] == "pinned"
    assert found[("pypi", "pytest")]["groups"] == ["optional:dev"]
    assert found[("npm", "react")]["locked_version"] == "19.0.1"
    assert found[("npm", "typescript")]["compatibility_risk"] == "bounded range"
    assert {item["source"] for item in result["provenance"]} == {
        "pyproject.toml",
        "requirements-dev.txt",
        "package.json",
        "package-lock.json",
    }


def test_registry_result_has_docs_and_provenance(monkeypatch):
    monkeypatch.setattr(
        "mcp_server.codehelp_support.packages._http_get_with_meta",
        lambda _url: (
            {
                "info": {
                    "version": "1.2.3",
                    "summary": "Example",
                    "project_urls": {"Documentation": "https://docs.example.test"},
                },
                "releases": {"1.2.3": [{"upload_time": "2026-01-02T00:00:00"}]},
            },
            {"url": "https://pypi.org/pypi/example/json", "cached": False, "fetched_at": "now"},
        ),
    )

    result = _pypi_version("example")

    assert result["status"] == "ok"
    assert result["docs_url"] == "https://docs.example.test"
    assert result["provenance"][0]["source"] == "pypi_registry"
    assert result["provenance"][0]["cached"] is False


def test_fetch_official_docs_strips_markup_and_returns_provenance(monkeypatch):
    monkeypatch.setattr(
        "mcp_server.codehelp_support.docs._codehelp_search_package_version",
        lambda *_args: {
            "status": "ok",
            "package": "example",
            "ecosystem": "PyPI",
            "docs_url": "https://docs.example.test/guide",
            "provenance": [{"source": "pypi_registry"}],
        },
    )
    monkeypatch.setattr(
        "mcp_server.codehelp_support.docs._http_get_text_with_meta",
        lambda url, **_kwargs: (
            "<html><style>hide</style><script>bad()</script><body>Useful API text</body></html>",
            {"url": url, "cached": True},
        ),
    )

    result = _codehelp_fetch_official_docs("example", query="API", max_chars=200)

    assert result["status"] == "ok"
    assert result["content"] == "Useful API text"
    assert result["provenance"][-1]["cached"] is True
    assert "verify project ownership" in result["authority"]


def test_docs_urls_require_public_https():
    assert _safe_docs_url("https://docs.example.test")
    assert not _safe_docs_url("http://docs.example.test")
    assert not _safe_docs_url("https://127.0.0.1/docs")
    assert not _safe_docs_url("https://localhost/docs")
