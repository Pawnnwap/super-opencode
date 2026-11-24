"""Runtime UI translation: English source string -> one Chinese line.

NiceGUI ships only framework-level translations (its own four reconnect
messages plus the Quasar language pack), and no community package covers an
application's own strings, so the console keeps its own table: every visible
string is written in English and passed through :func:`t` at RENDER time.

Two rules keep this honest:

- Lookup, not formatting: a string missing from the table falls through
  unchanged, which is exactly what logs, paths, job ids, model names and the
  job-state vocabulary need — they are English-only tokens and must never be
  machine-translated.
- Never translate at import time. Module caches (page lists, filter labels)
  must apply ``t`` when they render, because switching languages reloads the
  browser page but keeps this process alive.
"""

from __future__ import annotations

import contextlib

LANGS = ("en", "zh")
LANG_LABELS = {"en": "English", "zh": "中文"}

_current = "en"

_ZH: dict[str, str] = {
    # ── Shell / navigation ───────────────────────────────────────────────
    "Workspace": "工作区",
    "Protocol Wizard": "协议向导",
    "Live Run": "实时运行",
    "Harvester": "收割器",
    "run control": "运行控制",
    "Launch is locked until the connectivity probes pass — the board and "
    "history stay readable.": "连通性探测通过前启动已锁定 — 任务板与历史仍可查看。",
    "{n} queued": "{n} 个排队中",
    "(no workspace)": "(未设置工作区)",
    "No workspace set": "未设置工作区",
    "missing": "缺失",
    "judge · {name}": "裁判 · {name}",
    "✓ ready to launch": "✓ 可以启动",
    "tests pending": "测试未通过",
    "(no model)": "(未设置模型)",
    "(no judge)": "(未设置裁判)",
    "Preparing — CLI upgrades and MCP config…": "准备中 — CLI 升级与 MCP 配置…",
    "Startup tasks — {done}/{total} done": "启动任务 — {done}/{total} 已完成",
    "Interface language": "界面语言",
    "Toggle navigation": "切换导航栏",
    # ── Wizard: page + sections ─────────────────────────────────────────
    "Setup": "设置",
    "Write the task down, prove the models answer, bound what the agent may "
    "touch — then hand off. Steps 1–4 are done once and reused by every run.": "写下任务、验证模型可用、限定代理可触范围 — 然后交付。"
    "第 1–4 步只需完成一次，之后每次运行复用。",
    "Configuration": "配置",
    "Credentials, workspace and the execution engine": "凭据、工作区与执行引擎",
    "Credentials": "凭据",
    "API key": "API 密钥",
    "API base URL (optional)": "API 基础 URL（可选）",
    "Supervisor (judge) model": "主管（裁判）模型",
    "Workspace path": "工作区路径",
    "Runtime": "运行时",
    "Execution engine": "执行引擎",
    "Default (model default)": "默认（模型默认值）",
    "Thinking/reasoning effort for real runs. Choose Default unless the "
    "model documents these values.": "真实运行时的思考/推理强度。除非模型文档说明了这些取值，请选默认。",
    "Agent model ({engine})": "代理模型（{engine}）",
    "(custom)": "（自定义）",
    "Refresh models": "刷新模型",
    "Fetch models": "获取模型",
    "Re-read the engine's model list.": "重新读取引擎的模型列表。",
    "Read the engine's model list.": "读取引擎的模型列表。",
    "Advanced settings": "高级设置",
    "Not needed for a normal run — models, arbiter, limits": "常规运行无需改动 — 模型、裁决器、限额",
    "Models & reasoning": "模型与推理",
    "Agent backup model": "代理备用模型",
    "Supervisor backup model": "主管备用模型",
    "Codex reasoning effort": "Codex 推理强度",
    "Pi thinking level": "Pi 思考等级",
    "Thinking variant": "思考变体",
    "Supervisor thinking effort": "主管思考强度",
    "Codex base URL (responses API)": "Codex 基础 URL（responses API）",
    "Codex API key": "Codex API 密钥",
    "Decision arbiter (jev)": "决策裁决器（jev）",
    "Jev decision arbiter": "Jev 决策裁决器",
    "Ask the structured-decision service to arbitrate textual judgment "
    "calls: terminal / stand-down wording, MET-claim contradictions, "
    "stagnation, protocol flags, DONE proposals. Falls back to the text "
    "heuristics when the service is unavailable.": "让结构化决策服务仲裁文本类判定：终止/收工措辞、MET 声明矛盾、停滞、"
    "协议标记、DONE 提议。服务不可用时回退到文本启发式。",
    "Jev decision URL": "Jev 决策 URL",
    "POST target of the noul/choice decision API, e.g. "
    "http://DECISION_HOST/systemone/api/v1/systemone.": "noul/choice 决策 API 的 POST 目标，例如 "
    "http://DECISION_HOST/systemone/api/v1/systemone。",
    "Jev API key (optional)": "Jev API 密钥（可选）",
    "Jev timeout (s)": "Jev 超时（秒）",
    "Jev confidence threshold": "Jev 置信度阈值",
    "Jev choice threshold": "Jev 多选阈值",
    "Minimum probability for a multi-way jev ruling (DONE, stagnation, "
    "protocol review, unblock plan) to take effect.": "多分支 jev 裁决（DONE、停滞、协议复核、解阻计划）生效所需的最低概率。",
    "Run behavior": "运行行为",
    "Occam Razor": "奥卡姆剃刀",
    "Supervisor skills": "主管技能",
    "Full job logs (debug)": "完整任务日志（调试）",
    "Off: one key line per event, full bodies kept in the job's sidecar "
    "(errors always inline). On: verbatim events.": "关：每个事件一行摘要，完整内容存入任务附属文件（错误始终内联）。"
    "开：逐字事件。",
    "Limits": "限额",
    "Max retries": "最大重试次数",
    "Context threshold %": "上下文阈值 %",
    "Max tokens": "最大 tokens",
    "Turn timeout (min)": "回合超时（分钟）",
    "Wall clock per turn before the agent engine is killed. Default 120 "
    "min; raise it for long compute turns (1440 = 24 h). Enforced as "
    "config.timeout * 60 seconds.": "每回合杀掉代理引擎前的墙钟时限。默认 120 分钟；长计算回合可调大"
    "（1440 = 24 小时）。按 config.timeout * 60 秒执行。",
    "Max parallel jobs": "最大并行任务数",
    "Stored jobs (log retention)": "存储任务数（日志保留）",
    "Maintenance": "维护",
    "npm registry for CLI upgrades": "CLI 升级用的 npm 源",
    "Empty = the default registry.": "留空 = 使用默认源。",
    "Supervisor skills extra dir (optional)": "主管技能额外目录（可选）",
    "Beyond the built-in bank and the workspace's "
    ".opencode/supervisor_skills directory.": "除内置技能库与工作区的 .opencode/supervisor_skills 目录之外。",
    "Clean artifacts": "清理产物",
    "Delete build artifacts and caches in the configured workspace.": "删除已配置工作区中的构建产物与缓存。",
    "Supervisor skill bank — loaded only when the judge asks for one.": "主管技能库 — 仅在裁判请求时加载。",
    "No skills discovered.": "未发现技能。",
    "Fetching {engine} model list…": "正在获取 {engine} 模型列表…",
    "Fetched {n} {engine} models — reloading page.": "已获取 {n} 个 {engine} 模型 — 正在刷新页面。",
    "No models returned for {engine} (codex needs a base URL with a "
    "/models endpoint; pi needs a working `pi --list-models`).": "{engine} 未返回任何模型（codex 需要带 /models 端点的 base URL；"
    "pi 需要可用的 `pi --list-models`）。",
    "Cleaned artifacts in {workspace}": "已清理 {workspace} 中的产物",
    # ── Wizard: connectivity ────────────────────────────────────────────
    "Connectivity Tests": "连通性测试",
    "Two probes: the agent engine and the judge model. Both must pass "
    "before Live Run unlocks.": "两个探测：代理引擎与裁判模型。两者都通过后实时运行才会解锁。",
    "Set workspace path before running tests.": "运行测试前请先设置工作区路径。",
    "Testing… (agent up to ~60s)": "测试中…（代理最长约 60 秒）",
    "Connectivity test failed: {detail}": "连通性测试失败：{detail}",
    "{mark} {name}: {message}": "{mark} {name}：{message}",
    "✅ Both connectivity tests passed — Live Run unlocked.": "✅ 两项连通性测试均通过 — 实时运行已解锁。",
    "Run Tests": "运行测试",
    "Test {engine}": "测试 {engine}",
    "Test Supervisor": "测试主管",
    "both probes passed": "两项探测均通过",
    "probes not passed yet": "探测尚未通过",
    "Both connectivity tests passed.": "两项连通性测试均已通过。",
    # ── Wizard: protocol editor ─────────────────────────────────────────
    "Protocol": "协议",
    "What the task is: context, objectives, boundaries": "任务是什么：背景、目标、边界",
    "Set a valid workspace path in Configuration — the protocol is read "
    "from and written to that folder.": "请先在「配置」中设置有效的工作区路径 — 协议从该文件夹读写。",
    "Could not load {name}: {error}": "无法加载 {name}：{error}",
    "Loaded {name} from {workspace}.": "已从 {workspace} 加载 {name}。",
    "No protocol yet — fill the three sections, refine with AI, then "
    "Accept & Save. Access & Protection unlocks on save.": "还没有协议 — 填写三个部分，用 AI 完善，然后「接受并保存」。"
    "保存后访问与保护即解锁。",
    "INPUT (context)": "INPUT（背景）",
    "TARGET (objectives)": "TARGET（目标）",
    "RESTRICTIONS (boundaries)": "RESTRICTIONS（边界）",
    "Restored an unsaved protocol draft from before the last page reload "
    "— Accept & Save to keep it.": "已恢复上次页面刷新前的未保存协议草稿 — 接受并保存以保留。",
    "overall": "总评",
    "no issues": "无问题",
    "{n} issue(s)": "{n} 个问题",
    "Refine with AI": "用 AI 完善",
    "Cancel": "取消",
    "Accept & Save": "接受并保存",
    "Fill INPUT, TARGET and RESTRICTIONS first.": "请先填写 INPUT、TARGET 和 RESTRICTIONS。",
    "Protocol invalid: {error}": "协议无效：{error}",
    "TARGET not ready: {issues}": "TARGET 未就绪：{issues}",
    "{name} saved to {workspace} — Access & Protection below is now "
    "unlocked.": "{name} 已保存到 {workspace} — 下方「访问与保护」现已解锁。",
    "Refining protocol with AI… (the model reasons at length before "
    "answering)": "正在用 AI 完善协议…（模型会先长时间推理再作答）",
    "Refining — {phase}… {reasoning} reasoning chars · {answer} answer "
    "chars · {elapsed}s": "完善中 — {phase}… {reasoning} 字符推理 · {answer} 字符回答 · {elapsed} 秒",
    "reasoning": "推理中",
    "Gate check failed — auto-fix round {round}/{total}: {issues}": "关卡校验未通过 — 自动修复第 {round}/{total} 轮：{issues}",
    "Refine cancelled.": "完善已取消。",
    "Refine failed: {error}": "完善失败：{error}",
    "Refined — drafting feasibility gates…": "已完成完善 — 正在草拟可行性关卡…",
    "Refined — set a workspace to save gates.": "已完成完善 — 请设置工作区以保存关卡。",
    "Refined — {n} feasibility gate(s) auto-saved. Review, then Accept & "
    "Save.": "已完成完善 — 已自动保存 {n} 个可行性关卡。请审阅后「接受并保存」。",
    "Refined — no gates could be drafted from the TARGET.": "已完成完善 — 未能从 TARGET 草拟出关卡。",
    "Refined — gate draft failed: {error} (draft them in the gates panel "
    "below).": "已完成完善 — 关卡草拟失败：{error}（可在下方关卡面板手工草拟）。",
    "Cancelling refine…": "正在取消完善…",
    # ── Wizard: access & protection ─────────────────────────────────────
    "Access & Protection": "访问与保护",
    "Read-only paths, protected files, ignore list": "只读路径、受保护文件、忽略列表",
    "What the agent may touch: read-only reference directories, files it "
    "must never modify, and the ignore list. Saved to "
    "access_protection.json in the workspace; the config files protect "
    "themselves automatically.": "代理可触范围：只读参考目录、绝不可修改的文件、忽略列表。保存在工作区的 "
    "access_protection.json；配置文件会自动保护自身。",
    "Set a valid workspace path in Configuration first.": "请先在「配置」中设置有效的工作区路径。",
    "Starting…": "开始…",
    "{phase} — {reasoning} reasoning chars · {answer} answer chars · "
    "{elapsed}": "{phase} — {reasoning} 字符推理 · {answer} 字符回答 · {elapsed}",
    "waiting for the model": "等待模型响应",
    "writing answer": "撰写回答",
    "1/2 suggesting read-only directories (LLM) — waiting for the model": "1/2 正在建议只读目录（LLM）— 等待模型响应",
    "2/2 generating ignore patterns (LLM) — waiting for the model": "2/2 正在生成忽略规则（LLM）— 等待模型响应",
    "{n} suggestion(s) rejected by validation": "{n} 条建议未通过校验",
    "whitelist suggestion failed: {detail}": "白名单建议失败：{detail}",
    "ignore suggestion failed: {detail}": "忽略规则建议失败：{detail}",
    "1/2 FAILED after {elapsed}": "1/2 失败（耗时 {elapsed}）",
    "2/2 FAILED after {elapsed}": "2/2 失败（耗时 {elapsed}）",
    "Auto-generate failed while saving: {detail}": "自动生成在保存时失败：{detail}",
    "Auto-generated: +{n} read-only dir(s); ignore {state}; config files "
    "protected.": "已自动生成：新增 {n} 个只读目录；忽略规则{state}；配置文件已受保护。",
    "updated": "已更新",
    "unchanged": "未变化",
    "Auto-generate access & protection (LLM)": "自动生成访问与保护（LLM）",
    "One LLM pass: suggests read-only reference directories from the saved "
    "protocol, generates .opencodeignore patterns, validates every entry, "
    "and saves both into the workspace.": "一次 LLM 处理：根据已保存的协议建议只读参考目录，生成 .opencodeignore "
    "规则，校验每个条目，并将两者保存到工作区。",
    "Protected files": "受保护文件",
    "Files the agent may not modify or delete. Saved in "
    "access_protection.json; its own config files are locked.": "代理不可修改或删除的文件。保存在 access_protection.json；"
    "自身的配置文件已锁定。",
    "Set a valid workspace path first.": "请先设置有效的工作区路径。",
    "Add protected files (workspace scan)": "添加受保护文件（扫描工作区）",
    "No protected files.": "没有受保护文件。",
    "config · auto": "配置 · 自动",
    "Stop protecting this file": "取消保护此文件",
    "Path access whitelist (hard rule)": "路径访问白名单（硬规则）",
    "The project cwd is read/write. Whitelisted directories are READ-ONLY. "
    "Everything else is denied for every backend (opencode, codex, pi), "
    "regardless of the agent's own rules. Shell commands are parsed; "
    "dynamic constructs (substitutions, unknown variables, encoded "
    "commands) are denied outright.": "项目当前目录可读写。白名单目录为只读。其余路径对所有后端"
    "（opencode、codex、pi）一律拒绝，不受代理自身规则影响。Shell 命令会被"
    "解析；动态构造（替换、未知变量、编码命令）直接拒绝。",
    "Enforce the hard path policy": "强制执行硬路径策略",
    "Strict: also deny interpreter execution (python/node/perl scripts)": "严格模式：同时拒绝解释器执行（python/node/perl 脚本）",
    "Add a read-only reference directory (absolute path)": "添加只读参考目录（绝对路径）",
    "Add": "添加",
    "Enter an absolute directory path.": "请输入绝对目录路径。",
    "Rejected: {error}": "已拒绝：{error}",
    "Added read-only: {path}": "已添加只读：{path}",
    "No whitelisted directories.": "没有白名单目录。",
    "read-only": "只读",
    "Remove from the whitelist": "从白名单移除",
    "Policy drill passed — ": "策略演练通过 — ",
    "Policy drill FAILED — ": "策略演练失败 — ",
    ".opencodeignore": ".opencodeignore",
    "Glob patterns the agent must leave alone. Auto-saved on every edit.": "代理必须避开的 Glob 规则。每次编辑自动保存。",
    ".opencodeignore content": ".opencodeignore 内容",
    "auto-saved": "已自动保存",
    "⚠ No saved protocol yet — write INPUT / TARGET / RESTRICTIONS in the "
    "Protocol section above, then click “Accept & Save”. The "
    "auto-generator reads the protocol from disk, so an unsaved draft is "
    "not visible to it.": "⚠ 还没有已保存的协议 — 请在上方「协议」区填写 INPUT / TARGET / "
    "RESTRICTIONS，然后点击“接受并保存”。自动生成器从磁盘读取协议，"
    "未保存的草稿对它不可见。",
    "⚠ {name} is empty — fill in the Protocol section above and save it "
    "before auto-generating access & protection.": "⚠ {name} 为空 — 请先在上方「协议」区填写并保存，再自动生成访问与保护。",
    # ── Wizard: hand-off / stepper ──────────────────────────────────────
    "Configure": "配置",
    "Test": "测试",
    "Protect": "保护",
    "Launch": "启动",
    "Next": "下一步",
    "Setup complete — hand off to Live Run at the bottom.": "设置完成 — 请在页面底部交付到实时运行。",
    "{done}/{total} steps done": "已完成 {done}/{total} 步",
    "Hand-off": "交付",
    "{n} step(s) left before this page can be left": "还需 {n} 步才能离开本页",
    "Every setup step and probe is complete": "所有设置步骤与探测均已完成",
    "Locked until every setup step is complete:": "锁定，直到所有设置步骤完成：",
    "Setup complete — the run page is unlocked and reads this workspace "
    "and protocol.": "设置完成 — 运行页已解锁，将读取此工作区与协议。",
    "Ready for Live Run!": "前往实时运行！",
    "unlocks automatically": "自动解锁",
    "Set a valid workspace path in Configuration.": "请先在「配置」中设置有效的工作区路径。",
    "no usable workspace path": "没有可用的工作区路径",
    "no {name} in {workspace}": "{workspace} 中没有 {name}",
    "connectivity probes not passed": "连通性探测未通过",
    "access & protection not configured": "访问与保护未配置",
    "Connectivity Tests: run them above — both must pass.": "连通性测试：请在上方运行 — 两项都必须通过。",
    "Protocol: fill the sections and click “Accept & Save”.": "协议：填写各部分并点击“接受并保存”。",
    "Access & Protection: auto-generate it, or edit it by hand.": "访问与保护：自动生成，或手工编辑。",
    # ── Run page ────────────────────────────────────────────────────────
    "Execute": "执行",
    "Launch against the saved protocol, watch the queue, open any task "
    "for its live stream.": "按已保存的协议启动，观察队列，打开任意任务查看实时流。",
    "Open Protocol Wizard": "打开协议向导",
    "{name} ready in {workspace} — this launch reads it.": "{name} 已在 {workspace} 就绪 — 本次启动将读取它。",
    "No protocol in this workspace — Live Run would have nothing to "
    "execute.": "此工作区没有协议 — 实时运行没有可执行的内容。",
    "Start a task": "启动任务",
    "Protocol: {name} · one job per launch": "协议：{name} · 每次启动一个任务",
    "Launch is locked: {missing}. Browsing and opening finished tasks "
    "below is unaffected.": "启动已锁定：{missing}。仍可浏览和打开下方已完成的任务。",
    "requirements are not met": "前置条件未满足",
    "Plan mode rounds": "计划模式轮数",
    "Show the supervisor a skill catalog it may load on demand "
    "(LOAD_SKILL); judge-context only.": "向主管提供可按需加载的技能目录（LOAD_SKILL）；仅用于裁判上下文。",
    "Parallel instances": "并行实例",
    ">1 clones this workspace N times (one isolated study per copy) and "
    "launches them as one batch.": ">1 时把此工作区克隆 N 份（每份独立研究）并作为一批启动。",
    "Copy workspace (artifacts excluded)": "复制工作区（不含产物）",
    "protocol.md only": "仅 protocol.md",
    "Replica init": "副本初始化",
    "How each parallel instance's workspace is seeded.": "每个并行实例的工作区如何初始化。",
    "{n} instance(s)": "{n} 个实例",
    "plan rounds {n}": "计划轮数 {n}",
    "occam {state}": "奥卡姆 {state}",
    "skills {state}": "技能 {state}",
    "init: {mode}": "初始化：{mode}",
    "on": "开",
    "off": "关",
    "Batch launched: {n} instance(s)": "已启动批次：{n} 个实例",
    "Launch failed: {detail}": "启动失败：{detail}",
    "disabled: {missing}": "不可用：{missing}",
    "Task Board": "任务板",
    "live · 1.5 s refresh": "实时 · 1.5 秒刷新",
    "Task is running in the background.": "任务正在后台运行。",
    "Run job {id} not found.": "运行任务 {id} 未找到。",
    # ── Harvester page ──────────────────────────────────────────────────
    "Distil": "提炼",
    "Collect finished runs into one clean folder: a subfolder per run, a "
    "root index and a batch summary — then hand the folder to a reader.": "把已结束的运行归集到一个干净的文件夹：每个运行一个子文件夹，"
    "根目录索引与批次摘要 — 然后交给读者。",
    "Completed (SUCCESS + BLOCKED)": "已完成（SUCCESS + BLOCKED）",
    "All finished (incl. failed)": "全部结束（含失败）",
    "Select finished runs": "选择已结束的运行",
    "Type to auto-select matching runs (id, workspace, batch or final "
    "report), or tick them by hand.": "输入以自动勾选匹配的运行（id、工作区、批次或最终报告），"
    "也可手动勾选。",
    "Match": "匹配",
    "State": "状态",
    "Select shown": "全选可见",
    "Clear": "清空",
    "{shown} shown · {selected} selected · {total} finished run(s)": "显示 {shown} · 已选 {selected} · 共 {total} 个已结束运行",
    "Harvest selected ({n})": "收割所选（{n}）",
    "Harvest selected": "收割所选",
    "No matching runs.": "没有匹配的运行。",
    "No finished runs in the store yet — run something first.": "存储中还没有已结束的运行 — 请先运行任务。",
    "Harvest output": "收割输出",
    "Distilled by {engine} · {model}": "由 {engine} · {model} 提炼",
    "Harvest location": "收割位置",
    "A fresh harvest_<timestamp>/ folder is created inside this location. "
    "Each run gets its own subfolder with report.md, run.json and "
    "harvest.md; the root gets INDEX.md and a batch HARVEST.md.": "在此位置下新建 harvest_<timestamp>/ 文件夹。每个运行有独立子文件夹"
    "（report.md、run.json、harvest.md）；根目录生成 INDEX.md 与"
    "批次 HARVEST.md。",
    "Harvest prompt · custom": "收割提示词 · 自定义",
    "Harvest prompt · default": "收割提示词 · 默认",
    "Applied to each run": "应用于每个运行",
    "Select at least one finished run.": "请至少选择一个已结束的运行。",
    "Set a harvest location.": "请设置收割位置。",
    "Cannot create harvest folder: {error}": "无法创建收割文件夹：{error}",
    "Harvest launch failed: {detail}": "收割启动失败：{detail}",
    "Harvest launched: {id} → {path}": "收割已启动：{id} → {path}",
    "Harvest Board": "收割板",
    "finished harvest jobs": "已结束的收割任务",
    "Harvest is running in the background.": "收割正在后台运行。",
    "Harvest job {id} not found.": "收割任务 {id} 未找到。",
    "⚠ no workspace": "⚠ 无工作区",
    "batch {id}": "批次 {id}",
    "report {n}c": "报告 {n} 字符",
    # ── Board ───────────────────────────────────────────────────────────
    "History": "历史",
    "History ({n})": "历史（{n}）",
    "Search & filter": "搜索与筛选",
    "Search & filter · active": "搜索与筛选 · 已启用",
    "All states": "全部状态",
    "Active queue": "活动队列",
    "All workspaces": "全部工作区",
    "Search": "搜索",
    "Auto-refresh": "自动刷新",
    "task(s)": "个任务",
    "active · {run} run / {queued} queued": "活动 · {run} 运行 / {queued} 排队",
    "success": "成功",
    "blocked": "受阻",
    "need attention": "需要关注",
    "No tasks match these filters": "没有匹配这些筛选条件的任务",
    "Widen the state filter, clear the workspace filter or the search "
    "box — History keeps every task the store still holds.": "放宽状态筛选，或清空工作区筛选/搜索框 — 历史保留存储中的所有任务。",
    "No tasks yet": "还没有任务",
    "Launches appear here the moment they are queued: an active card "
    "while the job runs, then a History card with its report.": "任务一入队就会出现在这里：运行时显示活动卡片，结束后进入历史并附报告。",
    "Active Queue": "活动队列",
    "stops take effect between turns": "停止在回合之间生效",
    "Study batch {id}": "研究批次 {id}",
    "{n} replica(s)": "{n} 个副本",
    "{running} running / {queued} queued": "{running} 运行中 / {queued} 排队",
    "{n} running": "{n} 运行中",
    "Cancel batch": "取消批次",
    "Cancelled {n} job(s) of batch {id}": "已取消批次 {id} 的 {n} 个任务",
    "Harvested into {folder}": "已收割到 {folder}",
    "Stop this run": "停止本次运行",
    "Open the live screen": "打开实时界面",
    "Remove this record": "删除此记录",
    "Delete this job record": "删除此任务记录",
    "Open this workspace in the file manager": "在文件管理器中打开此工作区",
    "Open in the file manager": "在文件管理器中打开",
    "View": "查看",
    "Show more": "显示更多",
    "Show {n} more": "再显示 {n} 个",
    "Showing {shown} of {total} finished task(s)": "已显示 {shown}/{total} 个已结束任务",
    " — all shown": " — 已全部显示",
    "Stop requested: {id}": "已请求停止：{id}",
    "Stop failed: cancellation was not persisted": "停止失败：取消请求未能持久化",
    # ── Live screen ─────────────────────────────────────────────────────
    "Progress": "进度",
    "Latest": "最新",
    "newest events, one line each": "最新事件，每行一条",
    "Full log": "完整日志",
    "Details": "详情",
    "Run job": "运行任务",
    "Harvest job": "收割任务",
    "Stop": "停止",
    "Back to board": "返回任务板",
    "Reload this page": "刷新此页面",
    "Other active tasks": "其他活动任务",
    "Started": "开始时间",
    "Engine": "引擎",
    "Model": "模型",
    "Runs": "运行数",
    "Folder": "文件夹",
    "Agent model": "代理模型",
    "Judge model": "裁判模型",
    "(not set)": "(未设置)",
    "{n} selected": "已选 {n} 个",
    "Failure · {state}": "失败 · {state}",
    "No error event was recorded — open the full log below.": "没有记录到错误事件 — 请打开下方完整日志。",
    "Copy error": "复制错误",
    "Open folder": "打开文件夹",
    "Launch again": "再次启动",
    "Report": "报告",
    "Harvest Summary": "收割摘要",
    "{n} chars": "{n} 字符",
    "Download markdown": "下载 Markdown",
    "Job {id} not found.": "任务 {id} 未找到。",
    "It may have been removed by retention. The board lists every task "
    "still in the store.": "可能已被存储保留策略清理。任务板会列出存储中的全部任务。",
    "Job {id} finished: {state}": "任务 {id} 已结束：{state}",
    "Job {id} state is unreadable ({n} consecutive failed reads) — use "
    "Refresh": "任务 {id} 状态无法读取（连续 {n} 次读取失败）— 请刷新",
    " (auto-refreshing)": "（自动刷新中）",
    # ── Log panel ───────────────────────────────────────────────────────
    "Search logs…": "搜索日志…",
    "Agent speech": "代理发言",
    "Tool use": "工具调用",
    "The agent's plain-text prose and reasoning summaries (💭 thoughts). "
    "Tool I/O stays out; tool markers have their own toggle.": "代理的纯文本叙述与推理摘要（💭 思考）。工具 I/O 不在此列；"
    "工具标记有独立开关。",
    "Shell / edit / mcp tool markers from the agent.": "代理的 Shell / 编辑 / MCP 工具标记。",
    "event #": "事件 #",
    "Fetch one headline event's full body from the job sidecar.": "从任务附属文件取某条摘要事件的完整内容。",
    "Download the full log as text.": "以文本下载完整日志。",
    "{err} err · {warn} warn · {tool} tool": "{err} 错误 · {warn} 警告 · {tool} 工具",
    "Enter an event # from a headline line, e.g. 12": "请输入摘要行中的事件编号，例如 12",
    "No sidecar record for event #{ref}": "事件 #{ref} 没有附属记录",
    "Event #{ref} · {level} · {chars} chars · {ts}": "事件 #{ref} · {level} · {chars} 字符 · {ts}",
    "Close": "关闭",
    # ── Progress widgets ────────────────────────────────────────────────
    "🟢 Background process active": "🟢 后台进程运行中",
    " · 💓 {age}s ago": " · 💓 {age} 秒前",
    " · 🧭 {n}": " · 🧭 {n}",
    "ended · {state}": "已结束 · {state}",
    "{current} / {maximum} tokens": "{current} / {maximum} tokens",
    # ── How-now panel ───────────────────────────────────────────────────
    "How now": "现状如何",
    "Wake one fresh agent turn to sweep every live session and report "
    "which runs need you. Keeps running if you leave.": "唤醒一个全新的代理回合，巡检所有活动会话并报告哪些运行需要你。"
    "离开页面也会继续运行。",
    "Run sweep": "执行巡检",
    "Start a sweep (one at a time).": "开始一次巡检（同时只能有一个）。",
    "A how-now sweep is already running.": "已有一次巡检正在进行。",
    "Sweeping live sessions… {elapsed}s, {n} step(s)": "正在巡检活动会话… {elapsed} 秒，{n} 步",
    "sweep · {time}": "巡检 · {time}",
    " · {n}s": " · {n} 秒",
    "{n}s": "{n} 秒",
    "Sweep failed: {error}": "巡检失败：{error}",
    # ── Feasibility gates ───────────────────────────────────────────────
    "Feasibility gates": "可行性关卡",
    "Feasibility gates — no gates recorded": "可行性关卡 — 未记录关卡",
    "open — untested": "未测试",
    "passing — passed before": "通过 — 以前通过过",
    "blocked — never passed": "受阻 — 从未通过",
    "One row per acceptance criterion: what DONE means, gate by gate. "
    "Every change is saved to feasibility_facts.json automatically; any "
    "blocked gate marks the goal unreachable before the run starts.": "每条验收标准一行：逐关说明 DONE 的含义。所有修改自动保存到 "
    "feasibility_facts.json；任何受阻关卡都会在运行开始前把目标标记为"
    "不可达。",
    "Feasibility gates · {icon} {n} gate(s)": "可行性关卡 · {icon} {n} 个",
    " · {n} blocked": " · {n} 个受阻",
    "protocol draft — {n} numbered TARGET item(s)": "协议草稿 — {n} 个编号 TARGET 条目",
    "protocol draft (no numbered TARGET items detected)": "协议草稿（未检测到编号 TARGET 条目）",
    "protocol.md — {n} numbered TARGET item(s)": "protocol.md — {n} 个编号 TARGET 条目",
    "protocol.md (no numbered TARGET items detected)": "protocol.md（未检测到编号 TARGET 条目）",
    "no protocol.md in this workspace yet": "此工作区还没有 protocol.md",
    "no prior-attempt artifacts": "无既往尝试产物",
    "In {workspace}: {gate}; prior-attempt evidence: {artifacts}.": "在 {workspace}：{gate}；既往尝试证据：{artifacts}。",
    "⚠ Protocol changed since these gates were saved: {drift} — "
    "regenerate or fix the refs.": "⚠ 协议在这些关卡保存后已变更：{drift} — 请重新生成或修正引用。",
    "⚠ {name} failed {n} consecutive run(s) — mark it blocked?": "⚠ {name} 连续 {n} 次运行失败 — 标记为受阻？",
    "Mark blocked": "标记受阻",
    "Dismiss": "忽略",
    "auto-saved (no gates — file cleared)": "已自动保存（无关卡 — 文件已清空）",
    "auto-saved · {n} gate(s) · {time}": "已自动保存 · {n} 个关卡 · {time}",
    "No gates yet — add one manually or draft them from the protocol.": "还没有关卡 — 手动添加，或从协议自动生成。",
    "gate name": "关卡名",
    "pass condition": "通过条件",
    "Machine-checked gate (deterministic; the judge is never asked): "
    "{check}": "机器校验关卡（确定性；不会询问裁判）：{check}",
    "Remove this gate": "删除此关卡",
    "Add gate": "添加关卡",
    "Overall notes (optional)": "总体备注（可选）",
    "Cancelling draft…": "正在取消草稿…",
    "No protocol yet — refine one above; nothing to derive gates from.": "还没有协议 — 请先在上方完善；无从生成关卡。",
    "Thinking… {n} reasoning chars ({elapsed}s) — Cancel if this drags": "思考中… {n} 字符推理（{elapsed} 秒）— 太久可取消",
    "Writing… {n} chars ({elapsed}s)": "写入中… {n} 字符（{elapsed} 秒）",
    "Drafting gates from the protocol (LLM)…": "正在从协议草拟关卡（LLM）…",
    "Draft cancelled.": "草稿已取消。",
    "Draft failed: {error}": "草稿失败：{error}",
    "Draft contained no gates — nothing filled.": "草稿中没有关卡 — 未做任何填充。",
    "Drafted {n} gate(s) ({blocked} blocked) — auto-saved; edit freely.": "已草拟 {n} 个关卡（{blocked} 个受阻）— 已自动保存；可随意编辑。",
    "Draft gates from protocol (LLM)": "从协议草拟关卡（LLM）",
    "The supervisor model transcribes one gate per TARGET item, with "
    "statuses only where prior-attempt artifacts evidence them; it never "
    "invents results.": "主管模型为每个 TARGET 条目转录一个关卡，仅在有既往尝试产物佐证时"
    "给出状态；绝不虚构结果。",
    # ── Shared plumbing ─────────────────────────────────────────────────
    "Nothing is running.": "没有正在运行的任务。",
    "Cancelling…": "正在取消…",
    "Action failed: {detail}": "操作失败：{detail}",
    "{label} — fix it in the Protocol Wizard before launching.": "{label} — 请先在协议向导中修复再启动。",
    "Task launched: {id}": "任务已启动：{id}",
    "This page needs a workspace before it can do anything.": "本页需要先设置工作区才能工作。",
    "No usable workspace": "没有可用的工作区",
    "The saved path does not exist — deleted, moved, or on a drive that "
    "is not connected.": "已保存的路径不存在 — 已被删除、移动，或所在驱动器未连接。",
    "No workspace path is set yet.": "尚未设置工作区路径。",
    " Point Configuration at an existing folder: the protocol and every "
    "run live there.": " 请在「配置」中指向一个已存在的文件夹：协议与所有运行都在那里。",
    "(empty)": "(空)",
    "The record may have been removed by retention, or the id in the "
    "address bar belongs to another job type.": "该记录可能已被存储保留策略清理，或地址栏中的 id 属于另一类任务。",
}


def current() -> str:
    """The active UI language code."""
    return _current


def t(text: str) -> str:
    """Translate one English UI string; unknown strings pass through."""
    if _current == "en":
        return text
    return _ZH.get(text, text)


def tr(text: str, **fmt) -> str:
    """Translate a ``{placeholder}`` template, then fill it.

    Messages built from runtime values (a path, a model name) are written as
    one English template so the table holds the whole sentence instead of a
    translated prefix glued to English data.
    """
    out = t(text)
    try:
        return out.format(**fmt)
    except (KeyError, IndexError, ValueError):
        return out


# Sentences that end in machine-generated detail (a drill's failed check, an
# exception's message). Only the fixed prefix is translated; the detail stays
# verbatim because it names code-level facts, not UI copy.
_PREFIX_ZH = {
    "Access & Protection: policy drill failed — ": "访问与保护：策略演练失败 — ",
    "Access & Protection: ": "访问与保护：",
    "Connectivity Tests: ": "连通性测试：",
    "Protocol: ": "协议：",
}


def ts(text: str) -> str:
    """Translate a sentence that may carry untranslatable detail.

    Exact table hit first; otherwise a known English prefix is swapped and the
    remainder is kept verbatim.
    """
    if _current == "en":
        return text
    exact = _ZH.get(text)
    if exact is not None:
        return exact
    for prefix, zh_prefix in _PREFIX_ZH.items():
        if text.startswith(prefix):
            return zh_prefix + text[len(prefix) :]
    return text


def set_lang(lang: str) -> str:
    """Switch the process-wide language ('' / unknown -> English)."""
    global _current
    _current = lang if lang in LANGS else "en"
    # Keep NiceGUI's own framework messages and the Quasar locale pack in
    # step: the next page load reads core.app.config.language. No app context
    # (a bare unit test) is harmless — the page functions set it again.
    with contextlib.suppress(Exception):
        from nicegui import core

        core.app.config.language = "zh-CN" if _current == "zh" else "en-US"
    return _current


def install(lang: str | None) -> str:
    """Apply the persisted preference at startup."""
    return set_lang(str(lang or ""))


def html_lang() -> str:
    """Value for the document's lang attribute."""
    return "zh-CN" if _current == "zh" else "en"


__all__ = [
    "LANGS",
    "LANG_LABELS",
    "current",
    "html_lang",
    "install",
    "set_lang",
    "t",
    "tr",
    "ts",
]
