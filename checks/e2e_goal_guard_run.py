"""E2E smoke test: full SupervisorLoop with free models (opencode + LLM judge)."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, r"E:\pyprojects\super-opencode")

from supervisor.core.loop import SupervisorLoop
from supervisor.core.loop_base import LoopState
from supervisor.utils.config import SupervisorConfig

SETTINGS = json.load(open(r"C:\Users\Vladi\.opencode_supervisor_settings.json", encoding="utf-8"))
WS = Path(sys.argv[1] if len(sys.argv) > 1 else r"E:\e2e_goal_ws")

OR_KEY = json.load(open(r"C:\Users\Vladi\.local\share\opencode\auth.json", encoding="utf-8"))["openrouter"]["key"]

config = SupervisorConfig(
    protocol_path=WS / "protocol.md",
    workspace=WS,
    supervisor_model="nvidia/nemotron-3.5-lightning:free",
    supervisor_model_backup="poolside/laguna-s-2.1:free",
    openai_api_key=OR_KEY,
    openai_base_url="https://openrouter.ai/api/v1",
    opencode_model=sys.argv[2] if len(sys.argv) > 2 else "openrouter/poolside/laguna-s-2.1:free",
    opencode_model_backup="openrouter/nvidia/nemotron-3.5-lightning:free",
    opencode_pure=True,
    plan_mode_rounds=0,
    enable_python_scanner=False,
    enable_occam_razor=False,
    max_retries=1,
    timeout=120,
    max_tokens=64000,
    log_level="WARNING",
)

loop = SupervisorLoop(config)
levels: dict[str, int] = {}
t0 = time.monotonic()
for ev in loop.run_streaming():
    levels[ev["level"]] = levels.get(ev["level"], 0) + 1
    msg = " ".join(str(ev["msg"]).split())
    if ev["level"] in ("supervisor_response", "opencode_prompt"):
        msg = msg[:220]
    print(f"[{ev['level']:>18}] {msg[:400]}", flush=True)

print("\n=== RESULT ===")
print("state:", loop._state.name, f"({time.monotonic() - t0:.0f}s)")
print("events:", dict(sorted(levels.items())))
greet = WS / "greet.py"
tests = WS / "tests" / "test_greet.py"
print("greet.py exists:", greet.exists(), "| tests exist:", tests.exists())
audit = WS / ".opencode" / "goal_audit.jsonl"
if audit.exists():
    print("--- goal_audit.jsonl ---")
    for line in audit.read_text(encoding="utf-8").strip().splitlines():
        print(" ", line[:220])
ts = WS / ".opencode" / "target_state.json"
if ts.exists():
    st = json.loads(ts.read_text(encoding="utf-8"))
    print("--- target_state ---")
    print("  status:", st["status"], "| iteration:", st["iteration"],
          "| lessons:", st["lessons"], "| criteria:", len(st["success_criteria"]))
print("=== TASK_STATE tail ===")
tstate = WS / "TASK_STATE.md"
if tstate.exists():
    print("\n".join(tstate.read_text(encoding="utf-8").splitlines()[-8:]))
sys.exit(0 if loop._state == LoopState.ENDED_SUCCESS else 1)
