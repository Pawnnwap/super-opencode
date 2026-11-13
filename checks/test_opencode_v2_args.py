"""opencode 2.x argument compatibility: stale settings must not kill a run.

opencode 2.x validates every ``run`` argument against its resolved
configuration and exits 1 BEFORE any model call when a value is not accepted::

    --model provider/model#variant   -> "Variant unavailable for <model>: <v>"
    --model <bare-name>              -> "Invalid model reference: <name>"
    --agent <unknown>                -> 'Agent not found: "<name>"'

On 2026-11-13 every opencode run died on the first of those (a persisted
``xhigh`` for a model that declares no variants), so the supervisor judged an
empty turn, the gates stayed unmet, and the run stalled until an operator
cancelled it. These tests pin the three layers that now prevent it:

  1. ``catalog`` reads what the installed CLI actually accepts.
  2. ``build_cmd`` replaces/drops values the CLI would reject.
  3. ``process.run_prompt`` retries once without the rejected argument, which
     covers models the catalog cannot see (built-in providers).
"""

import json
from types import SimpleNamespace

import pytest

from supervisor.runners.opencode_support import catalog
from supervisor.runners.opencode_support import command_builder as oc_builder
from supervisor.runners.opencode_support import process as oc_process
from supervisor.runners.opencode_support.command_builder import build_cmd
from supervisor.runners.opencode_support.result import RunResult

# --------------------------------------------------------------------------- #
# catalog
# --------------------------------------------------------------------------- #

_CONFIG_DOC = [
    {
        "type": "document",
        "path": "C:/Users/x/.config/opencode/opencode.json",
        "info": {
            "providers": {
                "local-ds": {
                    "models": {
                        "swift-1.5": {"modelID": "local-ds/swift-1.5"},
                        "swift-2": {
                            "modelID": "local-ds/swift-2",
                            "variants": [
                                {"id": "low", "settings": {"reasoningEffort": "low"}},
                                {
                                    "id": "xhigh",
                                    "settings": {"reasoningEffort": "xhigh"},
                                },
                            ],
                        },
                    },
                },
            },
        },
    },
    {
        "type": "document",
        "path": "C:/ws/.opencode/opencode.json",
        "info": {
            "providers": {
                "local-ds": {"models": {"swift-3": {"modelID": "local-ds/swift-3"}}},
            },
        },
    },
]

_AGENTS_DOC = [{"id": "build"}, {"id": "plan"}, {"id": "explore"}]

_MODELS_TEXT = "local-ds/swift-1.5\nlocal-ds/swift-2\nopencode-go/glm-5.3\n"


@pytest.fixture(autouse=True)
def _hermetic_catalog(monkeypatch):
    """Answer every CLI probe from fixtures; no test shells out."""
    catalog.clear_cache()

    def fake_run(cmd, **kwargs):
        args = list(cmd[1:])
        if args == ["debug", "config"]:
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(_CONFIG_DOC), stderr=""
            )
        if args == ["debug", "agents"]:
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(_AGENTS_DOC), stderr=""
            )
        if args == ["models"]:
            return SimpleNamespace(returncode=0, stdout=_MODELS_TEXT, stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected probe")

    monkeypatch.setattr(catalog.subprocess, "run", fake_run)
    monkeypatch.setattr(catalog, "resolve_exe", lambda exe: exe)
    yield
    catalog.clear_cache()


def test_declared_variants_distinguishes_none_from_unknown():
    # Configured model that declares no variants: () — offering one would make
    # opencode exit 1 with "Variant unavailable".
    assert catalog.declared_variants("opencode", "local-ds/swift-1.5") == ()
    # Configured model WITH variants: the declared ids.
    assert catalog.declared_variants("opencode", "local-ds/swift-2") == ("low", "xhigh")
    # Model absent from the resolved config (built-in provider): unknown, so
    # the configured value is kept and the runtime retry covers a rejection.
    assert catalog.declared_variants("opencode", "opencode-go/glm-5.3") is None
    # A ref that already carries a variant is validated on its base model.
    assert catalog.declared_variants("opencode", "local-ds/swift-2#low") == (
        "low",
        "xhigh",
    )


def test_declared_variants_merges_project_config_over_global():
    # swift-3 exists only in the project config; the merge must see it.
    assert catalog.declared_variants("opencode", "local-ds/swift-3") == ()


def test_declared_agents_lists_this_build():
    assert catalog.declared_agents("opencode") == frozenset(
        {"build", "plan", "explore"}
    )


def test_resolve_model_qualifies_a_unique_bare_name():
    resolved, note = catalog.resolve_model("opencode", "swift-1.5")
    assert resolved == "local-ds/swift-1.5"
    assert "qualified" in note


def test_resolve_model_drops_an_unknown_bare_name():
    resolved, note = catalog.resolve_model("opencode", "does-not-exist")
    assert resolved is None
    assert "omitting --model" in note


def test_resolve_model_keeps_a_qualified_ref_untouched():
    assert catalog.resolve_model("opencode", "opencode-go/glm-5.3") == (
        "opencode-go/glm-5.3",
        "",
    )


def test_catalog_probe_failure_is_unknown_not_empty(monkeypatch):
    """A broken probe must never look like "this model has no variants"."""
    catalog.clear_cache()
    monkeypatch.setattr(
        catalog.subprocess,
        "run",
        lambda cmd, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    assert catalog.declared_variants("opencode", "local-ds/swift-1.5") is None
    assert catalog.declared_agents("opencode") is None
    assert catalog.known_models("opencode") is None


# --------------------------------------------------------------------------- #
# build_cmd
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _pin_v2(monkeypatch):
    monkeypatch.setattr(oc_builder, "_opencode_major", lambda exe: 2)


def _build(**overrides):
    kwargs = {
        "exe": "opencode",
        "prompt": "do the thing",
        "agent": "build",
        "opencode_model": "local-ds/swift-1.5",
        "use_continue": False,
        "session_id": None,
    }
    kwargs.update(overrides)
    return build_cmd(**kwargs)


def test_v2_replaces_an_agent_this_build_does_not_ship():
    # 1.x's "coder" is gone in 2.x; passing it exits 1 before the model call.
    cmd = _build(agent="coder")
    assert cmd[cmd.index("--agent") + 1] == "build"


def test_v2_keeps_a_shipped_agent():
    cmd = _build(agent="plan")
    assert cmd[cmd.index("--agent") + 1] == "plan"


def test_v2_qualifies_a_bare_model_name():
    cmd = _build(opencode_model="swift-1.5")
    assert cmd[cmd.index("--model") + 1] == "local-ds/swift-1.5"


def test_v2_drops_a_model_the_cli_does_not_list():
    cmd = _build(opencode_model="ghost/model-9")
    assert "--model" not in cmd


def test_v2_drops_a_variant_the_model_does_not_declare():
    # The exact 2026-11-13 failure: xhigh on a model with no variants.
    cmd = _build(opencode_model="local-ds/swift-1.5", variant="xhigh")
    assert cmd[cmd.index("--model") + 1] == "local-ds/swift-1.5"
    assert "--variant" not in cmd


def test_v2_keeps_a_declared_variant_in_the_model_ref():
    cmd = _build(opencode_model="local-ds/swift-2", variant="low")
    assert cmd[cmd.index("--model") + 1] == "local-ds/swift-2#low"


def test_v2_drops_an_undeclared_variant_embedded_in_the_model_ref():
    # swift-2 declares low/xhigh only; a persisted `#max` must not survive.
    cmd = _build(opencode_model="local-ds/swift-2#max")
    assert cmd[cmd.index("--model") + 1] == "local-ds/swift-2"


def test_v2_auto_approves_permissions():
    # Without --auto a non-interactive run auto-REJECTS every `ask` rule, so a
    # user's global permission entry silently disables a tool for the run.
    assert "--auto" in _build()


def test_v2_keeps_the_removed_flags_out():
    cmd = _build(use_pure=True, dir_="C:/ws", variant="low")
    joined = " ".join(cmd)
    assert "--dir" not in joined
    assert "--pure" not in joined
    assert "--variant" not in joined


def test_legacy_major_keeps_the_1x_shape(monkeypatch):
    monkeypatch.setattr(oc_builder, "_opencode_major", lambda exe: 1)
    cmd = _build(
        agent="coder", opencode_model="swift-1.5", variant="xhigh", dir_="C:/ws"
    )
    # 1.x has no catalog validation and no --auto; the flags stay as before.
    assert cmd[cmd.index("--agent") + 1] == "coder"
    assert cmd[cmd.index("--model") + 1] == "swift-1.5"
    assert cmd[cmd.index("--variant") + 1] == "xhigh"
    assert cmd[cmd.index("--dir") + 1] == "C:/ws"
    assert "--auto" not in cmd


# --------------------------------------------------------------------------- #
# runtime retry
# --------------------------------------------------------------------------- #


class _FakeRunner:
    """Runner surface ``run_prompt`` touches, with a scripted attempt list."""

    def __init__(self, results):
        self._results = list(results)
        self.attempts = []
        self.opencode_model = "local-ds/swift-1.5"
        self.opencode_model_backup = "local-ds/swift-1.5"
        self.opencode_variant = "xhigh"
        self.agent = "build"
        self.opencode_pure = False
        self.workspace = "C:/ws"
        self._use_continue = False
        self._session_id = None
        self._last_result = None

    def _next(self):
        self._last_result = self._results.pop(0)
        return self._last_result


def _drive(monkeypatch, runner):
    """Run ``run_prompt`` with the shared driver stubbed to one attempt each."""
    calls = []

    def fake_common(runner_, prompt, **kwargs):
        calls.append(
            {
                "variant": runner_.opencode_variant,
                "model": runner_.opencode_model,
                "agent": runner_.agent,
            }
        )
        runner_._next()
        return iter(())

    monkeypatch.setattr(oc_process, "run_prompt_common", fake_common)
    events = list(oc_process.run_prompt(runner, "hi", find_opencode_fn=lambda exe: exe))
    return calls, events


def test_variant_rejection_retries_once_without_the_variant(monkeypatch):
    runner = _FakeRunner(
        [
            RunResult(
                stdout="Variant unavailable for local-ds/swift-1.5: xhigh",
                returncode=1,
            ),
            RunResult(stdout="done", returncode=0),
        ],
    )
    calls, events = _drive(monkeypatch, runner)

    assert [c["variant"] for c in calls] == ["xhigh", ""]
    assert runner._last_result.ok
    assert any("rejected the variant" in e["msg"] for e in events)
    # The configured value is restored for the next turn.
    assert runner.opencode_variant == "xhigh"


def test_model_rejection_retries_once_without_the_model(monkeypatch):
    runner = _FakeRunner(
        [
            RunResult(stdout="Invalid model reference: swift-1.5", returncode=1),
            RunResult(stdout="done", returncode=0),
        ],
    )
    calls, _events = _drive(monkeypatch, runner)

    assert [c["model"] for c in calls] == ["local-ds/swift-1.5", None]
    assert runner.opencode_model == "local-ds/swift-1.5"
    assert runner.opencode_model_backup == "local-ds/swift-1.5"


def test_agent_rejection_retries_once_with_build(monkeypatch):
    runner = _FakeRunner(
        [
            RunResult(stdout='Agent not found: "coder"', returncode=1),
            RunResult(stdout="done", returncode=0),
        ],
    )
    runner.agent = "coder"
    calls, _events = _drive(monkeypatch, runner)

    assert [c["agent"] for c in calls] == ["coder", "build"]
    assert runner.agent == "coder"


def test_a_healthy_turn_is_never_retried(monkeypatch):
    runner = _FakeRunner([RunResult(stdout="done", returncode=0)])
    calls, events = _drive(monkeypatch, runner)

    assert len(calls) == 1
    assert not any("rejected" in e["msg"] for e in events)


def test_an_unrelated_failure_is_never_retried(monkeypatch):
    runner = _FakeRunner([RunResult(stdout="something else broke", returncode=1)])
    calls, _events = _drive(monkeypatch, runner)

    assert len(calls) == 1


def test_a_cancelled_turn_is_never_retried(monkeypatch):
    runner = _FakeRunner(
        [RunResult(stdout="Variant unavailable: xhigh", returncode=-1, cancelled=True)],
    )
    calls, _events = _drive(monkeypatch, runner)

    assert len(calls) == 1
