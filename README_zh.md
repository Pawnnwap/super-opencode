# opencode-supervisor

> **一个自主编码代理 —— 它按你的协议监督并评判每一次迭代。**

---

[English](./README.md) | 中文

一个由 NiceGUI 驱动的监督式编码系统。`SupervisorLoop` 负责协议引导的任务执行，
通过 LLM 评判器（`LLMSupervisor`）对每次迭代进行协议对齐评估。文件修改
使用各执行后端原生的读取、编辑、补丁和写入工具。

核心能力：

- **监督循环** — `SupervisorLoop` 针对一个工作区运行一份协议，每一轮都由评判器评估
- **三执行后端** — OpenCode（默认）、Codex 或 pi，通过 `config.engine` 选择；监督循环与后端无关
- **证据锚定评判器** — `LLMSupervisor` 在每一步评估代理输出是否符合协议目标，
  并通过 `JudgeHarness` 证据注册表锚定到确定性的工作区事实
- **目标守卫** — 证据门控完成："all targets met" 只是一个提议，须对照确定性证据校验
- **循环与停滞检测** — 回合内工具循环检测，加上跨回合的工作区指纹停滞提示阶梯
- **原生文件编辑** — OpenCode、Codex 和 pi 使用各自维护的读取、编辑、补丁和写入工具
- **Web UI** — NiceGUI 仪表盘：协议向导、实时运行、结果收割三个页面，带持久化的后台任务看板和整页实时任务屏幕
- **计划模式** — 可配置的执行前规划轮数，使用只读 opencode 分析
- **奥卡姆剃刀阶段** — 可选的成功后冗余精简阶段，只在归档副本上运行，绝不触碰实时工作区
- **任务状态日志** — 只追加的 `TASK_STATE.md` 进度日志，在上下文重置后重新注入
- **要点式作业日志** — 每条事件只落盘一行关键行；大块内容（提示词 / 输出 / 评审结论 / 建议）全文写入每任务侧车文件 `.job_store/<job>.full`，error 事件永远全文内联
- **同协议并行研究批次** — 在运行页把一份协议并行开 N 个隔离实例（整仓克隆或仅协议两种播种方式）；看板按批次分组、一键取消，并发上限可配置
- **增量工作区归档** — `.archive/` 快照为 git 对象库（每快照一个无父提交）：未变更文件按哈希共享，保留策略能真正释放空间；git 不可用时自动回退全量复制
- **上下文管理** — 基于 token 的监控，带分级警告和自动压缩

---

## 硬性路径策略（白名单 / 只读引用目录）

每次受监督运行都会执行一条确定性的文件系统边界，覆盖各后端自身的权限默认值：

| 范围 | 读 | 写 |
|------|----|----|
| 项目 cwd（运行工作区） | 允许 | 允许 |
| 白名单目录 | 允许 | **禁止**（始终只读） |
| 其他所有路径 | 禁止 | 禁止 |

在 Protocol Wizard 的 **Path Access Whitelist** 面板配置：

- 添加绝对路径的引用目录（测试夹具、语料、相邻仓库）；
- 可选禁止内联解释器代码（`python -c`、`node -e`、`bash -c`）；
- **AI suggest whitelist** 会根据协议内容推荐目录。推荐结果会在本地重新校验：
  盘符根目录、系统/程序目录、用户目录根、UNC 共享、不存在的路径以及工作区内部路径一律拒绝。

强制由各后端原生执行，而不是提示词请求：

- **opencode**：V1 `permission` 路径规则（`external_directory` 拒绝，逐条允许读、拒绝写），
  外加项目插件拦截权限扫描器无法归类的 shell 写入；不写 V2 `permissions` 键，
  因为 opencode 1.x 会因此判定整个配置文件无效；
- **pi**：通过显式 `-e` 加载 `tool_call` 扩展，在工具执行前返回 `{ block: true }`；
- **codex**：在 `$CODEX_HOME/hooks.json` 注册 `PreToolUse` 钩子，调用 supervisor 的策略闸门。

策略快照存放在工作区之外（`~/.opencode_supervisor/access/`），通过
`SOP_ACCESS_POLICY` 传给后端，agent 无法通过修改 cwd 内文件放宽自己的策略。
被拦截的调用会写入 JSONL 审计日志，并以 `ACCESS POLICY BLOCKED ...` 告警出现在任务日志中。

shell 命令由三端共享的保守分析器解析：支持 POSIX、cmd.exe、PowerShell 的引号与
转义空格、已知环境变量、重定向目标，并递归展开包装命令（`cmd /c`、
`-Command`/`-c`/`-lc`）。**无法静态确定的一律拒绝**：命令替换、反引号、未知变量、
延迟展开、编码命令、脚本块、heredoc、控制流、`find -exec`、危险程序
（`wsl`、`ssh`、`certutil`、`mshta` 等）以及内联解释器代码。未知命令的路径参数
按“写”处理，防止自定义 `--out <路径>` 写入只读白名单。分析器失败或超时，
同样拒绝该次调用。

首轮对话前会执行一次**运行前演练**：用 cwd、白名单、外部路径与 `..` 命令逃逸
实际验证策略引擎；对 codex 还会真实调用已安装的钩子并检查其返回 deny。
闸门若不能拒绝，直接中止运行而不是静默降级。UI 中的 **Run policy drill**
可随时手动执行同一套演练。

这是一层确定性的**规则闸门**，不是内核沙箱：它约束 agent 使用的工具与命令，
不宣称能抵御任意原生代码。若需要容器级边界（无网络、只读根、可丢弃文件系统），
可像 `cage` 项目那样把 agent 放进 Docker/WSL 或虚拟机，本策略作为内层防线。

仅在明确需要恢复旧的无限制行为时，才在 `~/.opencode_supervisor_settings.json`
中设置 `access_policy_enabled: false`。

---

## 前置要求

- Python 3.11 或更高版本
- OpenAI 或任何兼容提供商（如 NVIDIA NIM、Ollama）的 API 密钥
- 已安装 opencode CLI（参见下方安装说明）；若选择 Codex 执行后端，则需安装 Codex CLI；
  若选择 pi 执行后端，则需安装 pi CLI（`npm install -g @earendil-works/pi-coding-agent`）

---

## 安装 opencode

### 安装 Node.js / npm

opencode 以 npm 包形式分发。如果尚未安装 Node.js，请从 [Node.js 官方网站](https://nodejs.org/)
下载并安装 LTS 版本。安装后即可在所有受支持平台（Windows、macOS、Linux）上使用 `npm` 命令。

验证安装：

```bash
node --version
npm --version
```

### 安装 opencode

**Windows（npm）：**

```bash
npm install -g opencode-ai
```

**macOS / Linux（curl）：**

```bash
curl -fsSL https://opencode.ai/install | bash
```

`opencode` 可执行文件将被放置到 PATH 中：

- **Windows：** `%AppData%\npm\opencode.cmd`（通常为 `C:\Users\<你>\AppData\Roaming\npm\opencode.cmd`）
- **macOS / Linux：** `~/.opencode/bin/opencode`

运行 `where opencode`（Windows）或 `which opencode`（macOS/Linux）可查看实际路径。
如需覆盖自动检测，请在 `~/.opencode_supervisor_settings.json` 中设置
`"opencode_executable"`；留空则由 supervisor 自动定位 CLI。

### 升级 opencode

**Windows：**

```bash
npm install -g opencode-ai@latest
```

**macOS / Linux（curl）：**

```bash
curl -fsSL https://opencode.ai/install | bash
```

Web 应用在启动时也会自动运行这些命令（可通过 `OPENCODE_SKIP_UPGRADE=1` 环境变量
或 `~/.opencode_supervisor_settings.json` 中的 `skip_upgrade: true` 关闭）。

---

## Supervisor API 配置

Supervisor 接受任何 OpenAI 兼容的 API 端点。以下是预配置的免费选项：

| 提供商 | Base URL | 模型 | 说明 |
|--------|----------|------|------|
| **Google AI Studio** | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemma-4-31b-it` | 每天约 1.5K 次免费请求 |
| **Nvidia NIM** | `https://integrate.api.nvidia.com/v1` | `nvidia/nemotron-3-super-120b-a12b` | 提供免费套餐 |
| **iflow** *(已弃用)* | `https://apis.iflow.cn/v1` | `iflow/qwen3-coder-plus` | 服务将于 2026 年 4 月 17 日后停止 |

> **隐私警告：** 免费 API 服务可能会将您的输入数据用于模型训练。处理敏感或专有信息时请谨慎。如果隐私是您的关注点，请考虑切换到付费套餐。

---

## 设置

### 1. 创建虚拟环境

**Windows:**
```bash
python -m venv venv
venv\Scripts\activate
```

**macOS/Linux:**
```bash
python -m venv venv
source venv/bin/activate
```

### 2. 安装依赖

```bash
pip install -e . --force-reinstall
```

> **注意：** 需要 `pip install -e .`，以便 `supervisor` 包可以从任何位置导入 —— 包括当应用从不同工作目录启动 `app.py` 时。

或者，您可以直接从 `requirements.txt` 安装：

```bash
pip install -r requirements.txt
```

核心依赖定义在 `pyproject.toml` 中：`openai`、`nicegui`、`tiktoken`、`pytest`、
`rich`、`psutil` 和 `mcp`。`requirements.txt` 额外固定了一套静态分析工具链
（Bandit、Pylint、pyflakes、Semgrep、pip-audit、Ruff、Vulture、deadcode、pyscn）以及
自动修复辅助工具（autoflake、isort、autopep8、pyupgrade），供手动使用。

> **MCP 服务器：** `mcp` 包是 Codehelp MCP 服务器（`mcp_server/codehelp.py`）的必需依赖，为 OpenCode 和 Codex 提供代码辅助工具。

---

## 运行应用程序

```powershell
.\start_suprun.ps1                # 代理 (127.0.0.1:8321) + 应用 (127.0.0.1:8501)
.\start_suprun.ps1 -Upstream http://127.0.0.1:8111   # 模型端点（默认值）
.\start_suprun.ps1 -Restart       # 重启应用；-ProxyOnly / -AppOnly 只启动其中一部分
```

`start_suprun.ps1` 是幂等的：已在监听的组件保持运行；若代理的实时上游与
`-Upstream` 不一致会自动重启代理，保证参数生效。等效的手动方式：
`python app.py`（应用在 `http://127.0.0.1:8501` 提供服务）。

---

## Supervisor 的作用

监督系统在一个受控的反馈循环中运行代理：

1. **协议驱动执行** — `protocol.md` 文件定义 INPUT、TARGET 和 RESTRICTIONS 来指导代理的行为
2. **实时监控** — 跟踪上下文窗口使用情况，并在需要时自动触发压缩
3. **工作区安全** — 系统阻止对工作区外路径的引用，以防止意外的修改
4. **工作区归档** — 每次运行前和每次迭代后，工作区状态都会保存到 `.archive/`
5. **目标守卫** — 完成声明必须通过确定性证据校验后才能结束运行（见下文）
6. **循环检测** — 一个回合内重复的工具调用会被检测到，运行将被终止并以固定上下文重启，
   而不是浪费 token；跨回合的工作区停滞则通过提示阶梯逐级升级
7. **奥卡姆剃刀阶段** — 成功后，可选阶段让代理从最终工作区的归档副本中剥离冗余代码
8. **任务状态日志** — `TASK_STATE.md` 进度日志在上下文重置之间保留"已经尝试过什么"

---

## 目标守卫（证据门控完成）

评审模型的「all targets met」只是一个*提议*，而非最终接受。运行结束前，目标守卫会
按「提议 → 校验 → 执行 → 审计」的流程验证它（工程本体论第八章）：

- **证据锚定评判** — 评判器不再只看代理的文字陈述：`JudgeHarness` 注册表会把廉价的
  确定性证据（变更文件、工作区树、审计链尾部）自动渲染进每条评判提示词；评判器还可以
  按需申请昂贵证据（在结构化裁决中加入 `NEED_EVIDENCE: <name>`，例如跑一次测试或读取
  某个文件），循环会在重新评判前解析一次该请求。
- **结构化裁决** — 评审每条回复末尾附带逐条目标清单（`[MET]`/`[UNMET]` + 证据）、
  转发给代理的 `NEXT_ACTION` 指令，以及 `DONE: yes|no` 标志。自由文本回复仍可通过
  旧的完成短语匹配兜底。
- **出口校验** — 仍有 `[UNMET]` 目标、或运行未产生任何可观察的工作区文件变更时，
  DONE 提议会被拦截（代理自己的声明永远不构成证据）。
- **拦截上限** — 超过 `max_blocked_stops` 次拦截后，守卫接受完成并标记为未验证，
  循环既不会提前停止、也不会永远空转。
- **停滞提示阶梯** — 工作区指纹连续多个评审回合未变化时，先提示、再强制换策略、
  最后重启会话；纯阅读/跑测试的合法阶段不受惩罚（只看工作区指纹）。
- **审计链** — 每条裁决与决定都会追加到 `.opencode/goal_audit.jsonl`
  （时间戳、目标、证据、决定、原因），运行可事后重放审计。
- **进度记忆** — 方向追踪与 Reflexion 式经验教训持久化在
  `.opencode/target_state.json`，并重新注入评审与重启提示词，重启后指导不丢失。

配置项（`SupervisorConfig`）：`enable_goal_guard`（默认开启）、`max_blocked_stops`
（默认 `2`）、`goal_require_changes`（默认开启；纯分析类目标可关闭）、
`goal_verify_tests`（默认关闭；开启后以工作区测试套件作为最终门槛）。

---

## 循环与停滞检测

两个纯逻辑、不依赖 LLM 的检测器让运行保持诚实：

- **回合内（`LoopDetector`）** — 监视单个流式回合内的实时工具标记。同一工具重复
  （含参数）、小型工具循环、或没有文字说明的工具风暴都会触发"终止并以固定上下文恢复"，
  而不是把整个回合烧在循环上。
- **跨回合（`StagnationDetector`）** — 跨评审回合比较工作区指纹（文件内容哈希）。
  一个看似忙碌但工作区从未变化的代理会按 提示 → 强制换策略 → 重启循环 逐级升级；
  纯阅读/跑测试的合法阶段不受惩罚（只看工作区指纹）。

---

## 奥卡姆剃刀阶段

运行成功后，可选的后处理阶段（`enable_occam_razor`，默认关闭）让代理剥离冗余代码和
逻辑。它绝不编辑实时工作区：最终代码会被复制到一个由归档系统接管的工作区中，代理只在
该副本内精简冗余代码/逻辑。

---

## 任务状态日志

压缩会写入单个 `summary.md` 快照并在全新会话中重启代理，因此跨越多次重置的长任务会
丢失"已经尝试过什么"的线索。为此，监督器在每个评审回合向工作区中的 `TASK_STATE.md`
追加一条紧凑记录（回合号、时间、阶段和一行摘要）。上下文重置时，日志尾部会被注入
重启提示词，让代理带着连续性恢复工作。

---

## Web UI 页面

三个 NiceGUI 页面，分别位于 `/` 与 `/wizard`（协议向导）、`/run`、`/harvest`。
启动任务会通过 `JobManager` 入队一个后台作业 —— 作业状态与日志持久化在
`.job_store/` 下，运行不依赖浏览器标签页。左侧抽屉显示实时队列统计，并在连接性
测试通过前锁定「实时运行」。

作业日志默认为「要点式」：每条事件只落盘一行关键行，大块内容（提示词 / 输出 /
评审结论 / 建议）的全文写入 `<job_id>.full` 侧车文件（实时屏幕上通过 **查看全文**
按事件号取回），error 事件永远全文内联，重复警告合并为 ×N 标记。终态作业超过
「Stored jobs」上限后，下次入队时丢弃最旧者的日志载荷（状态摘要保留）。

### ① 协议向导
用自然语言填写 INPUT / TARGET / RESTRICTIONS → 点击 **用 AI 优化** → 查看生成的 `protocol.md` → 接受并保存。

向导功能包括：
- **配置面板** — 执行引擎（OpenCode、Codex 或 pi）、API 密钥与基础 URL、工作区路径、
  监督与代理模型（含备用模型）、最大重试次数、上下文阈值、回合超时、最大 token 数、
  用于 CLI 升级的 npm registry，以及奥卡姆剃刀、监督器技能、jev 评审、
  阻塞救援的开关。
  **清理产物**按钮可移除工作区中的运行残留。
- **受保护文件** — 标记代理无法修改或删除的文件
- **.opencodeignore** — 配置从上下文检索中排除的文件忽略模式
- **连接性测试** — 一键测试代理与监督模型的连通性；两项全部通过前，
  实时运行保持锁定
- **实时质量分析** — 在输入时实时评分协议的清晰度、可测试性和完整性

### ② 实时运行
针对任何项目工作区启动监督循环。每次启动会打开整页实时任务屏幕
（`/run/{job_id}`），每 2 秒自动刷新；页面上的任务看板列出活动与已完成的任务。

功能特性：
- 逐步进度跟踪，带阶段面包屑
- 计划模式 — 在启动表单中配置规划轮数（默认 1，0 为禁用）
- **并行实例** — 将「Parallel instances」设为大于 1 即可把工作区克隆 N 份
  （或只播种 protocol.md）并作为同一研究批次启动；副本位于
  `<workspace>_studies/<batch>_r1..rN`，在看板上渲染为一张分组批次卡
  （带 **Cancel batch** 一键取消），并受「Max parallel jobs」并发上限约束
  （超出的排队为 PENDING）
- token 使用进度条，按阈值变色（绿 / 黄 / 红）
- 紧凑滚动的 **Latest** 条（最新几条事件）+ 折叠展开的完整日志面板；
  要点事件通过「事件号 + 查看全文」取回侧车里的全文
- 日志搜索、噪声过滤与下载
- 随时停止正在运行的任务
- 运行完成后提供最终报告与下载按钮

### ③ 结果收割
把已完成的运行干净地收集到一个文件夹。可以勾选运行，或输入字符串自动匹配
（匹配作业 id、工作区、批次 id 与最终报告），匹配到的运行会被自动选中。
自定义收割提示词与输出位置；每次启动都会新建一个 `harvest_<timestamp>/` 文件夹：

- `<workspace>__<job>/report.md` — 该运行的最终报告（总会写入）
- `<workspace>__<job>/run.json` — 来源信息与收集状态
- `<workspace>__<job>/harvest.md` — 代理提炼的结果，或回退说明
- `INDEX.md` — 所有选中运行、状态与收集情况
- `HARVEST.md` — 跨运行的综合摘要

配置的代理（codex / opencode / pi）会对每个运行执行收割提示词。代理失败或
未写文件时，该运行仍会留下报告、来源信息与回退说明，因此收集结果不会残缺。
收割任务拥有独立的任务看板与实时屏幕。

---

## 架构

```
app.py                              Web UI（3 个页面：向导、实时运行、结果收割）
supervisor/
  __init__.py                       包标记
  memory_policy_evaluator.py        持久记忆策略的固定测试套件门控

  core/
    loop.py                         SupervisorLoop — 主监督代理循环
    loop_base.py                    BaseLoop — 通用状态机，事件生成
    goal_guard.py                   证据门控完成（提议 → 校验 → 执行 → 审计）
    goal_progress.py                进度信号（通过门数、连续计数），驱动停滞提示
    decision_service.py             可选的外部评审（jev）：DONE / 对齐 / 救援决策
    unblock_rescue.py               运行被阻塞时的一次性救援（可选）
    skill_bank.py                   监督器技能库（可选）
    occam_razor.py                  成功后在归档副本上运行的冗余精简阶段
    task_state.py                   只追加的 TASK_STATE.md 日志，用于重启连续性
    target_state.py                 持久化 TARGET 证据与方向追踪
    llm_supervisor.py               LLM 评判器，评估代理输出
    judge_harness.py                锚定评判器的证据注册表（自动 + NEED_EVIDENCE 资源）
    llm_support/                    评判器内部模块：chat、context、history、
                                    history_compaction、judgement、models

  analyzers/
    codebase_analyzer.py            为 LLM 上下文生成源代码树快照
    opencode_step_detector.py       检测代理输出中的步骤进度
    loop_detector.py                回合内循环检测（重复工具调用）
    stagnation_detector.py          跨回合工作区停滞与提示阶梯

  protocols/
    protocol.py                     解析/验证 protocol.md / protocol.jsonl
                                    （INPUT、TARGET、RESTRICTIONS）
    protocol_wizard.py              基于 OpenAI SDK 的协议优化器
    protocol_analyzer.py            质量评分（清晰度、可测试性、完整性）
    target_audit.py                 TARGET 部分的确定性预检审计
    feasibility_gate.py             feasibility_facts.json 门与状态追踪
    feasibility_wizard.py           LLM 辅助起草可行性门
    gate_rubric.py                  给评判器的封闭清单门记分卡
    alignment.py                    协议对齐/违规检查
    analyzer_support.py             共享分析辅助

  security/
    access_policy.py                确定性读/写路径策略（白名单）
    access_profile.py               持久化在工作区之外的策略快照
    engine_guards.py                各引擎原生闸门安装（opencode/pi/codex）
    shell_scan.py                   供策略使用的保守 shell 命令解析器
    preflight.py                    运行前策略演练（fail closed）
    guard_cli.py                    由 codex 钩子调用的闸门 CLI
    whitelist_advisor.py            AI 辅助白名单建议（本地重新校验）

  runners/
    factory.py                      根据 config.engine 构造 OpencodeRunner / CodexRunner / PiRunner
    base_runner.py                  共享运行器基类：完整回合/会话/步骤/归档表面；
                                    各引擎只实现自己的接缝（_run_prompt、工作区准备）
    opencode_runner.py              opencode CLI 的子进程包装器
    codex_runner.py                 Codex CLI 的子进程包装器
    pi_runner.py                    pi CLI 的子进程包装器
    opencode_support/               opencode 专属：命令构建、定位器、进程、流、会话、
                                    检查、结果
    codex_support/                  codex 专属：命令构建、定位器、进程、流
    pi_support/                     pi 专属：命令构建、定位器、进程、流
    command_common.py               共享命令构建辅助
    process_common.py               三引擎共享的子进程驱动（stdin 提示词、事件流消费、
                                    中止/循环/超时处理、模型回退）
    locator_common.py               共享可执行文件发现
    process_utils.py                进程生命周期辅助
    stream_driver.py                共享 JSON 事件流读取器（线程 + 队列，超时安全）
    test_runner.py                  运行 pytest / 语法检查；结构化结果

  utils/
    config.py                       不可变 SupervisorConfig 数据类
    experience_tracker.py           跟踪跨运行的成功模式
    llm_stream.py                   LLM 调用的共享流式辅助
    text_utils.py                   文本处理工具
    filesystem/                     file_ops、file_permissions、gitignore_utils、path_filters

  prompts/
    __init__.py                     提示模板包导出
    templates.py                    所有提示模板（初始化和评判指令）
    commands.py                     行为命令块（简洁模式、工具规则）

  monitoring/
    token_estimator.py              Token 计算（tiktoken）和提示截断
    session_tracker.py              上下文窗口使用情况（分级警告）、会话状态和生命周期跟踪

  workspace/
    workspace_guard.py              阻止对工作区外路径的引用
    workspace_archiver.py           在 .archive/ 中保留工作区版本
    git_archiver.py                 .archive/ 快照的 git 对象库后端
    cleanup_candidates.py           建议可清理的备份/临时文件
    ignore_patterns.py              .opencodeignore 解析和模式匹配

mcp_server/
  codehelp.py                       代码辅助与依赖研究 MCP 服务器
  web_tools.py                      MCP 网页搜索/抓取工具
  codehelp_support/                 工具实现（文档字符串、包、依赖、文档、示例、http）

services/
  config/                           设置持久化、opencode/codex 配置写入器、
                                    代理模型列举、连接性测试、SupervisorConfig 构建器
  jobs/                             JobManager、StateStore + 存储客户端、作业工作进程、
                                    收割循环、hownow 扫描、事件记录器
                                    （后台作业，持久化于 .job_store/）
  runtime/                          应用引导（自动升级）、guardian 看门狗、
                                    崩溃现场捕获、工作区清理
  webui/                            NiceGUI 外壳：布局/主题、持久化应用状态、作业管理器
                                    单例、批量启动、启动/实时接线、日志格式化、启动任务
    components/                     任务看板、实时任务屏幕、日志面板、进度组件、
                                    可行性面板、hownow 面板、忙碌指示器
    pages/                          wizard、run、harvest 页面

codex_compat_proxy.py               面向 Codex CLI 的本地 OpenAI 兼容代理
start_suprun.ps1                    幂等启动器（代理 + 应用 + 工作进程 + guardian）

checks/                             集成与端到端检查脚本
docs/                               研究笔记

pyproject.toml                      使 `supervisor` 成为可安装包（核心依赖）
requirements.txt                    备用依赖列表（核心 + 静态分析工具链）
```

---

## 协议系统

`protocol.md` 文件是你与 supervisor 之间的核心契约。
它必须包含恰好三个部分：

```markdown
## INPUT

描述已有的内容 —— 文件、目录、入口点、当前状态。

## TARGET

列出代理必须产生的编号、可测试的交付物。
好的例子："./tests/ 中的所有 pytest 测试通过"
不好的例子："代码应该能正常工作"

每个 TARGET 必须包含交付物、验收证据、完成态和失败/重规划条件。未量化的
"improve" 或 "enhance" 目标会被拒绝。

## RESTRICTIONS

代理绝不能违反的硬性规则。
- 不要操作 ./src 之外的文件
- 不安装系统包
- 代码保持在 300 行以下
```

### 协议质量分析

系统在三个维度上分析协议质量：
- **清晰度** — 避免模糊语言，使用结构化格式
- **可测试性** — 包含可衡量的验收标准
- **完整性** — 涵盖所有必要的上下文和约束

质量评级：`excellent`（≥90%）→ `good`（≥75%）→ `fair`（≥50%）→ `poor`

---

## 配置

| 设置 | 默认值 | 描述 |
|---------|---------|-------------|
| API 密钥 | — | 您的 API 密钥（OpenAI 或任何兼容提供商）|
| Base URL | *(留空 = OpenAI)* | 覆盖本地/代理端点，例如 `http://localhost:11434/v1` |
| 工作区路径 | — | 项目目录的绝对路径 |
| Supervisor / 向导模型 | — | 您的提供商接受的任何模型字符串 |
| 执行引擎 | opencode | 代理后端：`opencode`、`codex` 或 `pi` |
| Agent 模型 | *(引擎默认)* | 转发到 opencode/codex/pi CLI |
| 最大重试次数 | 3 | 强制停止前的连续失败次数 |
| 上下文阈值 | 60% | 压缩在此估算最大值的比例时触发 |
| 最大 token 数 | 150,000 | 模型上下文窗口大小 |
| 超时 | 120 分钟 | 超过此时间沉默则视为 opencode 无响应 |
| 受保护文件 | *(空)* | opencode 无法修改的用户定义文件 |

### 高级配置（SupervisorConfig）

| 设置 | 默认值 | 描述 |
|---------|---------|-------------|
| truncation_enabled | True | 接近限制时启用提示截断 |
| max_history_turns | 40 | 压缩前的最大对话历史轮数 |
| compact_intermediate_steps | False | 压缩中间步骤输出 |
| max_protected_files_for_suggestions | 5 | 建议中显示的最大受保护文件数 |
| read_external_feedback | False | 允许外部反馈注入 |
| log_level | "INFO" | 日志详细程度（DEBUG、INFO、WARNING、ERROR） |
| plan_mode_rounds | 1 | 执行前的规划轮数（0 = 无规划阶段，直接执行） |
| enable_occam_razor | False | 在归档副本上运行成功后的冗余精简阶段 |

### 自定义模型与提供商

向导中的 **Agent 模型** 下拉列表通过 **获取模型 / 刷新模型** 查询已安装的 opencode CLI
来填充。可从列表中选择任意条目，选择 **(custom)** 使用自由填写的模型字符串，并通过
**Agent 备用模型** 设置回退模型。

如需注册自定义的 OpenAI 兼容提供商，请将其添加到 opencode 自己的配置文件中
（类 Unix 系统为 `~/.config/opencode/opencode.json`，Windows 为
`%APPDATA%\opencode\opencode.json`）—— 可以手动编辑，也可以使用
`services/config/opencode_config.py` 中的 `add_custom_provider_to_config` 辅助函数。
注册后，其模型会被拉取到下拉列表中，并以 `service-name/model-name` 格式引用
（例如 `my-custom-service/qwen3-max`）。

---

## 工作区保护

Supervisor 强制执行多层保护：

### 系统保护目录
- `.opencode/` — Supervisor 配置（自动创建）
- `.checkpoints/` — 系统检查点
- `.archive/` — 版本归档

### 用户保护文件
通过 UI 标记特定文件为只读。这些文件：
- 被排除在 opencode 的写操作之外
- 在每个发送给 opencode 的提示中列出
- 在任何修改尝试前进行验证

### .opencodeignore
在工作区根目录配置 `.opencodeignore` 文件以从上下文检索中排除文件。支持：
- 精确文件名匹配：`debug.py`
- 前缀匹配：`prefix*`
- 后缀匹配：`*_test.py`
- 通配符模式：`**/*.pyc`
- 目录模式：`build/`

---

## 上下文监控

Supervisor 通过分级阈值跟踪 token 使用情况：

| 阈值 | 操作 |
|-----------|--------|
| 50% | 记录上下文使用情况 |
| 60% | 接近压缩阈值 |
| 70% | 上下文升高 — 密切关注 |
| 80% | 警告 — 建议压缩 |
| 90% | 紧急 — 需要立即压缩 |

Token 估算在可用时使用 `tiktoken`（o200k_base 编码），
否则回退到基于字符的估算（4 字符/token）。

自动压缩在配置的 `context_threshold`（默认 60%）触发，
提示 opencode 清理不必要的文件。

---

## 工作区归档

每次运行都在 `.archive/` 中保留工作区状态：

**git 后端（默认，`archive_backend: "git"`）** — 快照存放在私有裸仓库
`.archive/snapshots.git` 中，通过 `--git-dir/--work-tree` 驱动，绝不触碰
工作区自带的 `.git`：

- 一次快照 = `git add -A` → `write-tree` → 一个**无父提交**（orphan
  commit）→ 轻量标签 `snap/run_<ts>_<counter>_<slug>`；未变更文件按内容
  哈希跨快照共享，工作区没变时快照几乎零成本
- `keep_last` 保留策略删除最旧的标签并执行 `git gc --prune=now`，
  被删快照独有的对象会被真正回收（没有分支钉住历史）
- 恢复为覆盖式回放（绝不删除较新的文件），与旧版恢复语义一致；
  遗留的 `run_*` 目录仍然可见、可恢复
- 排除规则每次快照前重写进仓库的 `info/exclude`：规范跳过目录、点前缀
  条目、`.opencodeignore`，外加 `*.log`（滚动日志不是代码状态——
  64 MB 日志每快照重新入blob正是此后端要消灭的膨胀）
- git 缺失或连续失败 2 次时，本进程内自动回退到全量复制后端

**copy 后端（`archive_backend: "copy"`）** — 旧行为：带时间戳的 `run_*`
目录，含 `code/`、`results/`、`logs/`、`other/` 子目录与
`archive_metadata.json`。

两种后端下：`.archive/` 目录本身受保护，无法修改；版本文件从不删除 ——
只归档。

---

## 原生文件编辑

Super-Opencode 使用执行后端原生的文件工具。OpenCode 提供 `read`、精确匹配
`edit`、`apply_patch` 和 `write`；Codex 使用 `apply_patch`；pi 提供 `read`、
精确匹配 `edit`、`write` 以及 `bash` 工具。监督器会在这些
修改之外提供协议检查、工作区归档和回滚。

旧版本创建的 OpenCode 配置会在启动时迁移：移除受本应用管理的 Hashline MCP
条目，并恢复原生 `read` / `edit` 权限。自定义 MCP 服务器和用户自行设置的权限
保持不变。

---

## 代码辅助 MCP 服务器

系统内置一个 MCP 服务器（`mcp_server/codehelp.py`，工具实现位于
`mcp_server/codehelp_support/`），提供代码辅助和文档查找工具：

- **search_docstrings** — 搜索本地代码库中包、模块、类、函数或方法的文档字符串
- **search_package_version** — 查询 PyPI/npm 的最新发布版本、发布日期和文档 URL
- **analyze_dependency** — 读取 Python/Node 清单与锁文件，返回声明版本、锁定版本、来源和兼容性风险标签
- **fetch_official_docs** — 从 PyPI/npm 元数据声明的公共 HTTPS 文档地址读取短摘录，并附注册表/缓存来源
- **search_package_examples** — 查找 Stack Overflow 和 Real Python 的使用示例和最佳实践；仅作社区补充

这些工具会在启动时自动配置给 OpenCode 和 Codex。它们只提供研究能力；
文件编辑仍使用各后端的原生工具。pi 引擎不会自动配置 Codehelp —— pi
监督运行只使用其内置工具。

## 计划模式

当 `plan_mode_rounds` > 0 时，监督器在执行前运行专用的规划阶段：

1. 只读 opencode 实例分析协议和工作区
2. LLM 监督器评估计划（规划期间永远不会将目标标记为已完成，
   因此该阶段始终运行配置的轮数）
3. 最终计划和监督器反馈被存储并前置到构建模式的初始提示中，
   在 opencode 开始编写代码之前为其提供清晰的路线图

在实时运行的启动表单中配置规划轮数 —— 表单默认 1 轮，设为 0 表示无规划阶段
（直接执行）。

---

## 安全特性

1. **工作区边界强制** — 所有路径引用都针对工作区根目录进行验证
2. **受保护路径检测** — 系统目录和用户保护文件无法修改或删除
3. **协议对齐验证** — 每次迭代都检查是否符合协议
4. **自动回滚** — 不良更改会恢复到上一个良好的检查点
5. **心跳监控** — 检测停滞进程，并在取得进展时延长超时
6. **归档保留** — 历史版本被保留，从不删除
7. **进程归属** — 终止只能作用于本 run 自己的进程树（见下）

### 进程归属硬规则

每个 run 启动的进程在 spawn 时即登记到 `.job_store/<run_id>.procs.jsonl`
（`pid` + `create_time`），并放入该 run 的 Windows Job Object（POSIX 下为
独立 session/进程组）。所有终止统一走
`supervisor/runtime/process_control.py`：发信号前重新校验归属，不满足即拒绝
并写入 `<run_id>.kill_refusals.jsonl`。按名字、关键字、通配符、父进程树猜测
的 kill 已无任何代码路径。

- **MATLAB** 必须经受控入口启动（`bin\matlab.exe` 会派生 `MATLAB.exe`，
  只有成组清理才安全）：

  ```powershell
  python -m supervisor.tools.run_child matlab --script run_factor --timeout 3600
  ```

- **Python / Rust** 子进程走同一入口（或 `PythonProcess` / `RustProcess`
  类）：`run_child python --module pytest -- -q tests`、
  `run_child cargo --sub test -- --release`。`rustc` 与测试可执行文件是 job
  根的后代，自动在作用域内。
- **agent 的 shell kill** 只允许字面 PID 且须台账证明归属
  （`taskkill /PID <n>`、`Stop-Process -Id <n>`、`kill <n>`）；`/IM`、
  `-Name`、`/T`、`-f` 与变量 PID 一律被 shell 门禁拒绝。
- **崩溃恢复不杀进程**：重启只做台账 reconcile 并报告遗留；guardian 通过
  `.job_store/worker.stop` 请求 worker 自行退出，而不是去杀一个它并不拥有的
  进程。

---

## 待办事项

- [ ] 消除 `pip install -e . --force-reinstall`
- [ ] 添加多代理协作/竞争
- [x] 将基于关键词的进程清理（按进程名 taskkill/pkill）替换为基于 PID 的子进程跟踪管理
