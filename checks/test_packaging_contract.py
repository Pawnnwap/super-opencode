"""Dependency, package payload, and CI contracts stay deployable."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dependency_manifests_are_aligned():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()

    assert any(item.lower().startswith("psutil") for item in dependencies)
    assert any(item.lower().startswith("cryptography") for item in dependencies)
    assert any(item.lower().startswith("mcp") and "<2" in item for item in dependencies)
    assert "psutil" in requirements
    assert "cryptography" in requirements
    assert "mcp>=1.0.0,<2" in requirements
    assert "rich>=14.3.0" in requirements


def test_distribution_contains_the_whole_application():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    packages = project["tool"]["setuptools"]["packages"]["find"]["include"]
    modules = project["tool"]["setuptools"]["py-modules"]

    assert "supervisor*" in packages
    assert "services*" in packages
    assert "mcp_server*" in packages
    assert "app" in modules
    assert "codex_compat_proxy" in modules


def test_ci_runs_tests_lint_and_security_gates():
    workflow = ROOT / ".github" / "workflows" / "ci.yml"
    text = workflow.read_text(encoding="utf-8")

    assert "pytest" in text
    assert '"ruff", "check"' in text or "ruff check" in text
    assert "bandit" in text
    assert "python -m build" in text
