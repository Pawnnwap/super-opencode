# super-opencode 使用手册（中文，面向人工操作）

这是一个本地多智能体编程调度器：你写一份协议（protocol），它驱动 opencode /
codex / pi 反复执行、评审、回收，并用硬性路径策略限制代理只能读写指定目录。

---

## 1. 准备工作

1. Windows 10/11，PowerShell 5.1 以上。
2. 仓库内已有 `venv\`（Python 3.13）。若没有：

   ```powershell
   python -m venv venv
   venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

3. 一个可用的智能体命令行：`opencode`、`codex` 或 `pi`。
4. 两个 OpenAI 兼容的模型端点：一个给“监督模型”，一个给“执行代理”
   （推荐都用本机端点，例如 `http://127.0.0.1:8111`）。

---

## 2. 第一次启动

在仓库根目录打开 PowerShell：

```powershell
.\start_suprun.ps1
```

它会做三件事：

- 启动本地能力代理（默认 `127.0.0.1:8321`，上游默认 `http://127.0.0.1:8111`）；
- 启动任务工作进程（job worker）；
- 启动网页界面 `http://127.0.0.1:8501`。

同时注册三个计划任务（不要手动长期在终端里跑服务）：

| 任务 | 作用 |
|---|---|
| `soprun_detached_launch` | 启动入口（由 start_suprun.ps1 触发） |
| `soprun_guardian` | 常驻守护（pythonw，无黑窗），每 20 秒体检 |
| `soprun_guardian_backstop` | 每 15 分钟检查守护进程是否还在 |

常用启动参数：

```powershell
.\start_suprun.ps1 -Upstream http://127.0.0.1:8111   # 指定上游模型端点
.\start_suprun.ps1 -Restart                          # 只重启界面（运行中的任务不受影响）
.\start_suprun.ps1 -ProxyOnly                        # 只启动代理
.\start_suprun.ps1 -AppOnly                          # 只启动界面 + 工作进程
```

端口都可以改：`-AppPort`、`-ProxyPort`；代理上游可用 `-Upstream` 或环境变量
`SOP_UPSTREAM` 指定。端口与上游没有写死在代码里。

---

## 3. 打开界面并完成配置

浏览器打开 <http://127.0.0.1:8501>，进入 **Protocol Wizard** 页：

1. **Configuration**
   - 工作区路径：选一个空目录或已有项目目录；
   - 监督模型：模型名、API Base URL、API Key；
   - 执行引擎：opencode / codex / pi，以及对应模型与可执行文件路径。
2. **Connectivity Tests**
   - 点击 **RUN TESTS**，两项都通过后，Live Run 才会解锁。
   - 注意：界面重启后需要重新点一次（测试结果是本次会话的）。

---

## 4. 设置访问与保护（一个面板，一个按钮）

在同一页的 **Access & Protection** 面板：

1. 点击 **Auto-generate access & protection (LLM)**：
   - 自动建议只读参考目录（会校验：必须是绝对路径、存在于工作区之外）；
   - 自动生成 `.opencodeignore` 忽略规则；
   - 全部保存到工作区里的 `access_protection.json`，并把
     `protocol.jsonl`、`feasibility_facts.json`、`.opencodeignore` 等
     配置文件自动加入“受保护文件”，代理不能改。
2. 也可以手动增删白名单目录、受保护文件、忽略规则。
3. 面板里的 **策略体检（Policy drill）** 会自动运行：显示 `✅ Policy drill
   passed — N/N` 才说明路径策略正常；失败时按提示修正。

规则：工作区目录可读写；白名单目录只读；其余路径一律拒绝（读和写都拒绝）。

---

## 5. 编写协议

在同一页的 **Protocol** 区域：

1. 三个输入框：**INPUT（背景/已有材料）**、**TARGET（可测试的交付目标）**、
   **RESTRICTIONS（禁止事项）**。
2. 点击 **Refine with AI**：模型会把三段整理成规范协议，并自动做目标审计；
   结果会直接回填到三个输入框。
3. 检查无误后点击 **Accept & Save**：保存为工作区根目录的
   `protocol.jsonl`。

小提示：已有协议会自动载入这三个输入框（不再显示上一次编辑的残留内容）。

---

## 6. 可行性门禁（自动）

**Feasibility gates** 面板会从协议的目标条目生成“验收门禁”：

- Refine 成功后自动起草并保存（`feasibility_facts.json`）；
- 修改任意门禁都会立即自动保存，无需点保存按钮；
- 任何一条门禁标记为 `blocked`，说明目标在开跑前就不可达，先调整协议。

---

## 7. 启动运行

1. 打开 **Live Run**（`/run`），确认顶部显示 `✅ protocol.jsonl ready`。
2. 点击 **Launch**。可选项（默认折叠）：
   - 计划轮次（plan mode rounds）；
   - Occam Razor（成功后做一次精简）；
   - Supervisor skills；
   - 并行份数（>1 会复制工作区成多个副本并行跑，复制模式会带上协议、门禁和访问配置）。
3. 在任务板查看进度；点进任务可看实时日志。

---

## 8. 查看结果与回收

- **Harvester**（`/harvest`）：把完成的运行整理成一个干净目录。
- 每个工作区里会留下：`protocol.jsonl`、`feasibility_facts.json`、
  `access_protection.json`、`.opencodeignore`、运行日志等。

---

## 9. 日常维护

- **重启界面**：`.\start_suprun.ps1 -Restart`（任务不会丢；界面会重新要求连通性测试）。
- **日志**：`guardian.log`（守护）、`supervisor_app.err.log`（界面）、
  `job_worker.err.log`（任务）、`app.fatal.log`（崩溃取证）。
- **守护规则**：守护进程只修“确实坏了”的东西；**有 RUNNING 任务时绝不杀工作进程**，
  只在空闲且卡死时重启。
- **任务存储**：`.job_store\`（不要手动删正在跑的任务记录）。

---

## 10. 常见问题

| 现象 | 处理 |
|---|---|
| Live Run 被锁 | 回 Protocol Wizard 跑连通性测试 |
| 提示没有协议 | 在 Protocol 区域填写并 Accept & Save |
| 策略体检失败 | 按提示修改白名单或开关，再保存 |
| 端口被占用 | `.\start_suprun.ps1 -Restart`，或等守护进程修复 |
| codex 报 hooks 重复 | `config.toml` 与 `hooks.json` 只能保留一种钩子表示 |
| 任务排队不动 | 先看 `.job_store` 里是否有 RUNNING 任务；再查 `guardian.log` |
