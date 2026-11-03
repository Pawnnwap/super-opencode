"""`fetch_pi_models` turns `pi --list-models` rows into "provider/model" ids.

pi's `--model` accepts exactly that spelling (live-verified with
"local-swift/ukisai/Swift-1.5-Qwen3.8-Flash-Next-NVFP4"), and the droplist
must not offer anything else.
"""

from types import SimpleNamespace

from services.config.agent_models import fetch_pi_models

_SAMPLE_TABLE = (
    "provider     model                                      context  max-out  thinking  images\n"
    "local-swift  ukisai/Swift-1.5-Qwen3.8-Flash-Next-NVFP4  262.1K   16.4K    yes       yes   \n"
    "spark-local  Spark-X2.5-4B                              16.4K    6.1K     no        no    \n"
)


def test_fetch_pi_models_parses_provider_and_model_columns(monkeypatch):
    import services.config.agent_models as mod

    monkeypatch.setattr(mod, "find_pi", lambda exe="": "pi.cmd")

    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=_SAMPLE_TABLE, stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    models = fetch_pi_models()

    assert models == [
        "local-swift/ukisai/Swift-1.5-Qwen3.8-Flash-Next-NVFP4",
        "spark-local/Spark-X2.5-4B",
    ]


def test_fetch_pi_models_swallows_cli_failure(monkeypatch):
    import services.config.agent_models as mod

    monkeypatch.setattr(mod, "find_pi", lambda exe="": "pi.cmd")

    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="Error: offline")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert fetch_pi_models() == []


def test_fetch_pi_models_swallows_missing_executable(monkeypatch):
    import services.config.agent_models as mod

    def boom(exe=""):
        raise FileNotFoundError("pi not found")

    monkeypatch.setattr(mod, "find_pi", boom)
    assert fetch_pi_models() == []
