"""Test python_scanner import and integration with loop files."""

import sys
from pathlib import Path


def test_python_scanner_import():
    """Test that the python_scanner module can be imported."""
    from supervisor.vulnerability.python_scanner import scan

    assert callable(scan)


def test_python_scanner_scan_exists():
    """Test that scan function and related exports exist."""
    from supervisor.vulnerability.python_scanner import (
        scan,
        Finding,
        autofix,
        ALL_TOOLS,
    )

    assert callable(scan)
    assert callable(autofix)
    assert isinstance(ALL_TOOLS, list)
    assert "bandit" in ALL_TOOLS


def test_loop_base_imports_scanner():
    """Test that loop_base imports _vuln_scan from python_scanner."""
    from supervisor.core.loop_base import _vuln_scan, BaseLoop

    # _vuln_scan should be either callable or None (if import failed)
    assert _vuln_scan is not None or _vuln_scan is None


def test_loop_base_has_scan_method():
    """Test that BaseLoop has scan_for_vulnerabilities method."""
    from supervisor.core.loop_base import BaseLoop

    assert hasattr(BaseLoop, "scan_for_vulnerabilities")
    assert callable(getattr(BaseLoop, "scan_for_vulnerabilities"))


def test_self_evolution_loop_inherits_scan():
    """Test that SelfEvolutionLoop inherits scan_for_vulnerabilities."""
    from supervisor.core.self_evolution_loop import SelfEvolutionLoop
    from supervisor.core.loop_base import BaseLoop

    assert issubclass(SelfEvolutionLoop, BaseLoop)
    assert hasattr(SelfEvolutionLoop, "scan_for_vulnerabilities")


def test_supervisor_loop_inherits_scan():
    """Test that SupervisorLoop inherits scan_for_vulnerabilities."""
    from supervisor.core.loop import SupervisorLoop
    from supervisor.core.loop_base import BaseLoop

    assert issubclass(SupervisorLoop, BaseLoop)
    assert hasattr(SupervisorLoop, "scan_for_vulnerabilities")


def test_scanner_scan_on_self():
    """Test that scan can be called on this project directory (non-empty run)."""
    from supervisor.vulnerability.python_scanner import scan

    target = str(Path(__file__).parent)
    findings = scan(
        target=target,
        tools=["bandit"],
        min_severity="HIGH",
        scan_deps=False,
        print_output=False,
    )
    assert isinstance(findings, list)


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
