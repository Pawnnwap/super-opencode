"""The four boundary bypasses from the LLM-trust audit (F-01 ~ F-04).

Each test drives the real gate, not a mock of it:

* F-01 — the MCP codebase tools must not read outside the run's workspace;
* F-02 — ``web_fetch`` must not reach loopback / private / metadata addresses;
* F-03 — the agent-writable facts file must not become a command channel;
* F-04 — irreversible git operations must be refused.
"""

from __future__ import annotations

import json

import pytest

from supervisor.protocols.feasibility_gate import Gate
from supervisor.protocols.gate_rubric import evaluate_rubric
from supervisor.security.access_policy import build_policy
from supervisor.security.shell_scan import analyze


def _policy(tmp_path):
    return build_policy(tmp_path, [])


# --------------------------------------------------------------------------- #
# F-01: MCP codebase_path containment
# --------------------------------------------------------------------------- #


def test_codebase_path_outside_workspace_is_refused(tmp_path, monkeypatch):
    from mcp_server.codehelp_support.paths import resolve_codebase_path

    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("SOP_WORKSPACE", str(workspace))

    resolved, error = resolve_codebase_path("..")
    assert resolved is None
    assert "outside the workspace" in error


def test_codebase_path_inside_workspace_is_allowed(tmp_path, monkeypatch):
    from mcp_server.codehelp_support.paths import resolve_codebase_path

    workspace = tmp_path / "ws"
    (workspace / "sub").mkdir(parents=True)
    monkeypatch.setenv("SOP_WORKSPACE", str(workspace))

    resolved, error = resolve_codebase_path("sub")
    assert error == ""
    assert resolved == (workspace / "sub").resolve()


def test_codebase_path_falls_back_to_cwd_without_a_run(tmp_path, monkeypatch):
    from mcp_server.codehelp_support.paths import resolve_codebase_path

    monkeypatch.delenv("SOP_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)
    resolved, error = resolve_codebase_path(".")
    assert error == ""
    assert resolved == tmp_path.resolve()


def test_docstring_search_reports_outside_workspace(tmp_path, monkeypatch):
    from mcp_server.codehelp_support.docstrings import _codehelp_search_docstrings

    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("SOP_WORKSPACE", str(workspace))

    result = _codehelp_search_docstrings("anything", codebase_path=str(tmp_path))
    assert result["error_type"] == "outside_workspace"
    assert "found" not in result


def test_dependency_analysis_reports_outside_workspace(tmp_path, monkeypatch):
    from mcp_server.codehelp_support.dependencies import _codehelp_analyze_dependencies

    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("SOP_WORKSPACE", str(workspace))

    result = _codehelp_analyze_dependencies(codebase_path=str(tmp_path))
    assert result["status"] == "error"
    assert result["error_type"] == "outside_workspace"


def test_gate_checks_codebase_path(tmp_path):
    """The native gate must treat codebase_path as a path, not ignore it."""
    from supervisor.security.guard_cli import violations_for_event

    policy = _policy(tmp_path)
    event = {
        "tool_name": "codehelp_search_docstrings",
        "tool_input": {"codebase_path": "C:\\Windows"},
    }
    assert violations_for_event(policy, event)


# --------------------------------------------------------------------------- #
# F-02: web_fetch SSRF
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8501/healthz",
        "http://localhost/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "http://10.0.0.5/",
        "http://service.internal/",
        "http://printer.local/",
        "http://0.0.0.0/",
    ],
)
def test_private_and_metadata_urls_are_refused(url):
    from mcp_server.codehelp_support.docs import is_public_http_url

    assert is_public_http_url(url, require_https=False) is False


@pytest.mark.parametrize(
    "url",
    ["https://example.com/docs", "http://example.com/", "https://pypi.org/project/x"],
)
def test_public_urls_are_allowed(url):
    from mcp_server.codehelp_support.docs import is_public_http_url

    assert is_public_http_url(url, require_https=False) is True


def test_docs_url_still_requires_https():
    from mcp_server.codehelp_support.docs import is_public_http_url

    assert is_public_http_url("http://example.com/", require_https=True) is False
    assert is_public_http_url("https://example.com/", require_https=True) is True


def test_web_fetch_rejects_a_loopback_url(monkeypatch):
    """The tool itself must refuse, not just the helper."""
    import asyncio

    from mcp_server import web_tools

    called: list[str] = []
    monkeypatch.setattr(
        web_tools, "_http_get_text",
        lambda url, **_kw: called.append(url) or "body",
    )
    with pytest.raises(ValueError, match="public http"):
        asyncio.run(
            web_tools.call_tool("web_fetch", {"url": "http://127.0.0.1:8501/healthz"}),
        )
    assert called == [], "the request was issued despite the SSRF check"


def test_web_fetch_passes_the_validator_to_redirects(monkeypatch):
    """A public URL must still be re-checked after a redirect."""
    import asyncio

    from mcp_server import web_tools

    seen: dict = {}

    def fake_get(url, **kwargs):
        seen["url"] = url
        seen["validate_url"] = kwargs.get("validate_url")
        return "body"

    monkeypatch.setattr(web_tools, "_http_get_text", fake_get)
    asyncio.run(web_tools.call_tool("web_fetch", {"url": "https://example.com/x"}))
    validator = seen["validate_url"]
    assert validator is not None
    assert validator("https://example.com/next") is True
    assert validator("http://127.0.0.1/") is False


# --------------------------------------------------------------------------- #
# F-03: the facts file must not be a command channel
# --------------------------------------------------------------------------- #


def _gate(cmd: str) -> Gate:
    return Gate(name="tests", check={"kind": "command", "cmd": cmd})


def test_gate_command_is_refused_by_shell_policy(tmp_path, monkeypatch):
    """An agent-written check.cmd must not execute arbitrary code."""
    import supervisor.protocols.gate_rubric as rubric

    def boom(*_args, **_kwargs):
        raise AssertionError("subprocess.run must not be reached")

    monkeypatch.setattr(rubric.subprocess, "run", boom)
    evaluation = rubric._evaluate_command(
        _gate('python -c "open(\'pwned\',\'w\')"'),
        {"kind": "command", "cmd": 'python -c "open(\'pwned\',\'w\')"'},
        tmp_path,
        10.0,
    )
    assert evaluation.outcome == "ERROR"
    assert "rejected by shell policy" in evaluation.fact


def test_gate_command_still_runs_when_policy_allows(tmp_path):
    """A verifiable command (a script file inside the workspace) still runs."""
    script = tmp_path / "check.py"
    script.write_text("raise SystemExit(0)\n", encoding="utf-8")
    evaluation = _evaluate_command_for(tmp_path, "python check.py")
    assert evaluation.outcome == "PASS", evaluation.fact
    assert "rejected by shell policy" not in evaluation.fact


def _evaluate_command_for(tmp_path, cmd: str):
    import supervisor.protocols.gate_rubric as rubric

    return rubric._evaluate_command(
        _gate(cmd), {"kind": "command", "cmd": cmd}, tmp_path, 30.0,
    )


def test_metric_source_cannot_escape_the_workspace(tmp_path):
    gate = Gate(
        name="coverage",
        check={
            "kind": "metric",
            "metric": "coverage",
            "op": ">=",
            "value": 0.8,
            "source": "../../secret.json:coverage",
        },
    )
    evaluation = evaluate_rubric([gate], tmp_path)["coverage"]
    assert evaluation.outcome == "ERROR"
    assert "escapes workspace" in evaluation.fact


def test_metric_source_inside_the_workspace_still_works(tmp_path):
    (tmp_path / "metrics.json").write_text('{"coverage": 0.9}', encoding="utf-8")
    gate = Gate(
        name="coverage",
        check={
            "kind": "metric",
            "metric": "coverage",
            "op": ">=",
            "value": 0.8,
            "source": "metrics.json:coverage",
        },
    )
    evaluation = evaluate_rubric([gate], tmp_path)["coverage"]
    assert evaluation.outcome == "PASS"


def test_agent_cannot_write_the_facts_file(tmp_path):
    from supervisor.security.guard_cli import violations_for_event

    policy = _policy(tmp_path)
    event = {
        "tool_name": "write",
        "tool_input": {"file_path": str(tmp_path / "feasibility_facts.json")},
    }
    violations = violations_for_event(policy, event)
    assert violations, "an agent write to the facts file was allowed"
    assert "control file" in violations[0].reason


def test_agent_cannot_write_the_access_profile(tmp_path):
    from supervisor.security.guard_cli import violations_for_event

    policy = _policy(tmp_path)
    event = {
        "tool_name": "edit",
        "tool_input": {"path": str(tmp_path / "access_protection.json")},
    }
    assert violations_for_event(policy, event)


def test_agent_may_still_write_ordinary_workspace_files(tmp_path):
    from supervisor.security.guard_cli import violations_for_event

    policy = _policy(tmp_path)
    event = {
        "tool_name": "write",
        "tool_input": {"file_path": str(tmp_path / "notes.md")},
    }
    assert violations_for_event(policy, event) == []


def test_protected_check_cli_round_trip(tmp_path, monkeypatch, capsys):
    """The JS plugin reaches the same rule through this subcommand."""
    import io
    import sys

    from supervisor.security import guard_cli

    monkeypatch.setattr(
        sys, "stdin",
        io.StringIO(json.dumps({
            "path": str(tmp_path / "feasibility_facts.json"),
            "workspace": str(tmp_path),
        })),
    )
    assert guard_cli.main(["--check-protected"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["protected"] is True


# --------------------------------------------------------------------------- #
# F-04: irreversible git operations
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "command",
    [
        "git reset --hard HEAD~1",
        "git reset --hard",
        "git clean -fdx",
        "git clean -fd",
        "git clean -f",
        "git checkout -- .",
        "git checkout .",
        "git restore .",
        "git restore --source HEAD~1 file.py",
        "git stash drop",
        "git stash clear",
        "git branch -D feature",
        "git push --force origin main",
        "git push -f origin main",
    ],
)
def test_irreversible_git_operations_are_denied(tmp_path, command):
    assert analyze(_policy(tmp_path), command, cwd=tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "git clean -n",
        "git clean --dry-run",
        "git status",
        "git diff",
        "git log --oneline -5",
        "git commit -m x",
        "git checkout feature-branch",
        "git restore --staged file.py",
        "git push origin main",
    ],
)
def test_safe_git_operations_are_allowed(tmp_path, command):
    assert analyze(_policy(tmp_path), command, cwd=tmp_path) == []
