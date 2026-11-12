"""A UI action that fails must SAY SO — never look like a dead button.

Incident (2026-11-12, remote operator): "the Launch button does nothing".
`batch_launch` raised `shutil.Error: [WinError 32]` (a MATLAB-held file), the
Run page caught only `ValueError`, so it escaped into NiceGUI's async task.
NiceGUI's default handler is `log.exception` -> stderr; the browser never sees
it, and `busy_buttons` re-enables the button in its `finally`. Result: a click
that silently did nothing.

The same trap existed at five more sites. These checks pin the contract
structurally (AST, not text): the risky call must sit inside a `try` whose
handler is broad enough to catch what it actually raises. A narrow
`except ValueError` around filesystem work is the bug, so it must fail here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]


def _handler(path: str, func_name: str) -> ast.AST:
    """The outermost FunctionDef/AsyncFunctionDef named *func_name*."""
    tree = ast.parse((_REPO / path).read_text(encoding="utf-8"))
    hits = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == func_name
    ]
    assert hits, f"{path}: no handler named {func_name}"
    # The outermost definition is the one under test (nested ones are helpers).
    return min(hits, key=lambda n: n.lineno)


def _catch_clauses(node: ast.AST) -> list[ast.ExceptHandler]:
    return [n for n in ast.walk(node) if isinstance(n, ast.ExceptHandler)]


def _is_broad(handler: ast.ExceptHandler) -> bool:
    """Bare `except:` or `except Exception` (an alias of it counts)."""
    if handler.type is None:
        return True
    names = []
    for sub in ast.walk(handler.type):
        if isinstance(sub, ast.Name):
            names.append(sub.id)
        elif isinstance(sub, ast.Attribute):
            names.append(sub.attr)
    return "Exception" in names or "BaseException" in names


def _guarded_calls(path: str, func_name: str) -> tuple[list[str], bool]:
    """Names called anywhere inside *func*, and whether a broad guard exists."""
    fn = _handler(path, func_name)
    calls = _names_called(fn)
    broad = any(_is_broad(h) for h in _catch_clauses(fn))
    return calls, broad


def _names_called(node: ast.AST) -> list[str]:
    """Every callee/operation name in *node*.

    Three shapes must all count, or the assertion silently goes vacuous:
    bare calls (``_persist()``), attribute calls
    (``self.manager.enqueue_job(...)``), and a callable passed as an argument
    (``nicegui_run.io_bound(test_agent_connectivity, ...)``).
    """
    names: list[str] = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            func = n.func
            if isinstance(func, ast.Name):
                names.append(func.id)
            elif isinstance(func, ast.Attribute):
                names.append(func.attr)
        elif isinstance(n, ast.Name):
            # Feed-through references (io_bound callbacks) and plain loads.
            names.append(n.id)
        elif isinstance(n, ast.Attribute):
            names.append(n.attr)
    return names


def _risky_calls_enclosed_by(path: str, func_name: str) -> set[str]:
    """Names whose calls sit inside a `try` with a broad handler.

    Only enclosure counts. A function can already contain a narrow inner
    `except Exception` around one LLM call while the FILESYSTEM write right
    after it stays unguarded — exactly the feasibility-panel and
    `_auto_generate` shapes. "The function mentions Exception somewhere" would
    pass those, so the assertion must be about position.
    """
    fn = _handler(path, func_name)
    enclosed: set[str] = set()
    for try_node in ast.walk(fn):
        if not isinstance(try_node, ast.Try):
            continue
        if not any(_is_broad(h) for h in try_node.handlers):
            continue
        for stmt in try_node.body:
            enclosed.update(_names_called(stmt))
    return enclosed


# --------------------------------------------------------------------------- #
# The dispatch/IO sites that could raise past a narrow guard                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path,func,risky",
    [
        # single-instance path: launches via launch_job -> enqueue_job, which
        # raises ValueError (workspace collision), OSError (store write),
        # KeyError (missing setting).
        ("services/webui/pages/run.py", "_launch", "_launch_run"),
        # batch path shares the same handler; keep both assertions honest.
        ("services/webui/pages/run.py", "_launch", "launch_study_batch"),
        # _persist writes the facts file -> OSError, after rows were redrawn.
        ("services/webui/components/feasibility_panel.py", "_generate", "_persist"),
        # apply_generation + _profile_sync write profile/ignore files.
        ("services/webui/pages/wizard.py", "_auto_generate", "apply_generation"),
        ("services/webui/pages/wizard.py", "_auto_generate", "_profile_sync"),
        # temp-dir mkdir / OpenAI() construction sit outside the probe guards.
        ("services/webui/pages/wizard.py", "_run", "test_agent_connectivity"),
        # config build + enqueue: OSError / KeyError beyond ValueError.
        ("services/webui/pages/harvest.py", "_launch", "enqueue_job"),
        ("services/webui/pages/harvest.py", "_launch", "build_supervisor_config"),
    ],
)
def test_handler_has_a_broad_guard(path, func, risky):
    calls, _broad = _guarded_calls(path, func)
    assert risky in calls, f"{path}:{func} never calls {risky} — test is stale"
    enclosed = _risky_calls_enclosed_by(path, func)
    assert risky in enclosed, (
        f"{path}:{func} runs {risky} OUTSIDE any `except Exception` — a failure "
        "there escapes to NiceGUI's stderr-only default handler and the button "
        "looks dead"
    )


def test_run_launch_guards_both_branches_not_just_the_batch():
    """The regression's exact shape: only the batch branch was guarded."""
    enclosed = _risky_calls_enclosed_by("services/webui/pages/run.py", "_launch")
    assert "launch_study_batch" in enclosed
    assert "_launch_run" in enclosed, (
        "the single-instance path must be guarded too — that is the branch the "
        "operator's Launch click actually took"
    )


def test_no_bare_narrow_guard_around_launch_work():
    """`except ValueError` alone must not cover launch/dispatch work."""
    for path, func in [
        ("services/webui/pages/run.py", "_launch"),
        ("services/webui/pages/harvest.py", "_launch"),
    ]:
        fn = _handler(path, func)
        for try_node in ast.walk(fn):
            if not isinstance(try_node, ast.Try):
                continue
            caught = {
                n.id
                for h in try_node.handlers
                if h.type is not None
                for n in ast.walk(h.type)
                if isinstance(n, ast.Name)
            }
            if not caught or "Exception" in caught:
                continue
            body_names = {
                n.func.id
                for n in ast.walk(try_node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
            for risky in ("enqueue_job", "launch_study_batch", "_launch_run",
                          "build_supervisor_config"):
                assert risky not in body_names, (
                    f"{path}:{func} guards {risky} with only {caught}"
                )


# --------------------------------------------------------------------------- #
# The app-level net                                                           #
# --------------------------------------------------------------------------- #


def test_app_registers_the_last_resort_net():
    src = (_REPO / "app.py").read_text(encoding="utf-8")
    assert "error_net.install()" in src, (
        "app.py must arm the net so an unguarded handler still tells the operator"
    )


def test_the_net_notifies_the_operator(monkeypatch):
    """Behavioral: the net must produce a VISIBLE red notice, not just a log."""
    from nicegui import ui

    from services.webui import error_net

    seen: list[dict] = []
    monkeypatch.setattr(ui, "notify", lambda msg, **kw: seen.append({"msg": msg, **kw}))

    error_net.report_unhandled(OSError("[WinError 32] file in use"))

    assert seen, "the net showed the operator nothing"
    assert "WinError 32" in seen[0]["msg"]
    assert seen[0]["type"] == "negative"
    assert "OSError" in seen[0]["msg"], "the exception type must be nameable"


def test_the_net_never_masks_the_original_error(monkeypatch):
    """A failing notifier (e.g. no client context) must not raise out of it."""
    from nicegui import ui

    from services.webui import error_net

    def boom(*_a, **_k):
        raise RuntimeError("no client context")

    monkeypatch.setattr(ui, "notify", boom)
    error_net.report_unhandled(OSError("original"))  # must not raise


def test_message_is_bounded():
    """A pathological message must not flood the browser toast."""
    from services.webui import error_net

    msg = error_net.message_for(OSError("x" * 10_000))
    assert len(msg) < 400
