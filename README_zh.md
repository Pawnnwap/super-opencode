# opencode-supervisor

---

[English](./README.md) | 中文

Streamlit UI + 模块化 Python 后端，运行一个 `opencode` 代理于监督反馈循环中 —— 并能将同一个循环作用于**自身**来调试和改进其源代码。

基于 MCP 的哈希锚定文件编辑系统、执行前的计划模式、多工具漏洞扫描，
以及带分级警告的自动上下文管理。

---

## 前置要求

- Python 3.11 或更高版本
- OpenAI 或任何兼容提供商（如 NVIDIA NIM、Ollama）的 API 密钥
- 已安装 opencode CLI（参见下方安装说明）

---

## Windows 安装

### 安装 Chocolatey

如果尚未安装 Chocolatey，请在**管理员** PowerShell 中运行以下命令：

```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force; [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072; iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))
```

更多详情请参阅 [Chocolatey 安装页面](https://chocolatey.org/install)。

### 安装 opencode

使用 Chocolatey 在 Windows 上安装 opencode：

```bash
choco install opencode
```

可执行文件安装到 `C:\ProgramData\chocolatey\bin\opencode.exe`。

对于 UI 配置，请使用路径 `C:\ProgramData\chocolatey\bin\opencode.exe`。

---

## 免费 API 推荐

- **[NVIDIA NIM](https://build.nvidia.com/models)** — 免费套餐可用于 AI 模型访问
- **[IFlow CN](https://platform.iflow.cn/models)** — 免费 API 用于 AI 模型访问

---

## 推荐模型

### Supervisor 模型
- 使用 NVIDIA NIM: **nvidia/nemotron-3-super-120b-a12b** 或 **qwen/qwen3.5-397b-a17b**
- 使用 IFlow: **qwen3-coder-plus**

### opencode 模型
- **opencode/big-pickle**

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

> **注意：** 需要 `pip install -e .`，以便 `supervisor` 包可以从任何位置导入 —— 包括当 Streamlit 从不同工作目录启动 `app.py` 时。

或者，您可以直接从 `requirements.txt` 安装：

```bash
pip install -r requirements.txt
```

所有依赖均在 `pyproject.toml` 中定义：`openai`、`streamlit`、`tiktoken`、`pytest`、`cryptography`、`rich` 和 `psutil`。

---

## 运行应用程序

```bash
streamlit run app.py
```

如果上述命令不起效，请尝试：

```bash
python -m streamlit run app.py
```

应用程序将在浏览器中打开，地址为 `http://localhost:8501`。

---

## Supervisor 的作用

监督系统在一个受控的反馈循环中运行 opencode 代理：

1. **协议驱动执行** — `protocol.md` 文件定义 INPUT、TARGET 和 RESTRICTIONS 来指导代理的行为
2. **实时监控** — 跟踪上下文窗口使用情况，并在需要时自动触发压缩
3. **工作区安全** — 系统阻止对工作区外路径的引用，以防止意外的修改
4. **检查点** — 每次成功的迭代都会被快照到 `.checkpoints/`
5. **工作区归档** — 每次运行前和每次迭代后，工作区状态都会保存到 `.archive/`
6. **自我演进** — 系统可以分析和改进其自己的代码库，在每次更改前后运行测试，并在回归时自动回滚

---

## Streamlit UI 页面

### ① 协议向导
用自然语言填写 INPUT / TARGET / RESTRICTIONS → 点击 **用 AI 优化** → 查看生成的 `protocol.md` → 接受并保存。

向导功能包括：
- **配置面板** — 设置 API 密钥、基础 URL、工作区路径、模型、最大重试次数、上下文阈值、超时、最大 token 数
- **受保护文件** — 标记 opencode 无法修改或删除的文件
- **.opencodeignore** — 配置从上下文检索中排除的文件忽略模式
- **实时质量分析** — 在输入时实时评分协议的清晰度、可测试性和完整性

### ② 实时运行
针对任何项目工作区启动监督循环。实时日志流随时可以停止。

功能特性：
- 逐步进度跟踪，带阶段检测
- 计划模式 — 执行前可配置规划轮数（在侧边栏设置 `plan_mode_rounds`）
- 分级阈值的 token 使用警告（50%、60%、70%、80%、90%）
- 详细/紧凑日志切换
- 上下文压缩与文件清理建议
- 心跳监控以检测停滞进程
- 运行完成后的最终监督报告，带下载按钮

### ③ 自我演进
将系统指向**自己的源代码树**。

1. 描述你想调试或改进的内容
2. 可选择添加额外的限制
3. **生成 meta_protocol.md** — LLM 读取实时源代码并编写精确的协议，包含准确的 INPUT 和可测试的 TARGET
4. 查看/编辑，然后**启动演进**

自我演进功能：

| 功能 | 详情 |
|---------|--------|
| 测试基线 | `pytest`（或语法检查）在 opencode 接触任何内容之前运行 |
| 迭代测试 | 每次监督判断后重新运行测试 |
| 回归保护 | 测试变差 → 自动回滚到上一个良好的检查点 |
| 检查点 | 每次无回归的迭代都会被快照到 `.checkpoints/` |
| 工作区归档 | 每次迭代都会归档到 `.archive/` 并附带元数据 |
| 演进报告 | `evolution_report.md` — 更改的文件、测试差异、最佳检查点 |

---

## 架构

```
app.py                              Streamlit UI  (3 个页面：向导、实时运行、自我演进)
supervisor/
  __init__.py                       包导出

  core/
    loop.py                         SupervisorLoop — 主要的监督代理循环
    loop_base.py                    BaseLoop — 通用状态机，事件生成
    self_evolution_loop.py          SelfEvolutionLoop — 带测试门控的自我修改
    llm_supervisor.py               LLM 评判器，评估 opencode 输出

  analyzers/
    codebase_analyzer.py            为 LLM 上下文生成源代码树快照
    opencode_step_detector.py       检测 opencode 输出中的步骤进度

  protocols/
    protocol.py                     解析/验证 protocol.md（INPUT、TARGET、RESTRICTIONS）
    protocol_wizard.py              OpenAI SDK 协议优化器
    protocol_analyzer.py            质量评分（清晰度、可测试性、完整性）
    meta_protocol_builder.py        从演进目标和快照生成 meta_protocol.md

  runners/
    opencode_runner.py              opencode CLI 的子进程包装器
    test_runner.py                  运行 pytest / 语法检查；结构化结果

  utils/
    config.py                       不可变 SupervisorConfig 数据类
    file_ops.py                     文件操作工具
    credentials_manager.py          凭证存储助手
    gitignore_utils.py              自动 .gitignore 更新助手

  prompts/
    __init__.py                     提示模板包导出
    templates.py                    所有提示模板（初始化、评判、哈希行指令）

  monitoring/
    context_monitor.py              跟踪上下文窗口使用情况，分级警告
    token_estimator.py              Token 计算（tiktoken）和提示截断

  workspace/
    workspace_guard.py              阻止对工作区外路径的引用
    workspace_archiver.py           在 .archive/ 中保留工作区版本
    opencodeignore_handler.py       .opencodeignore 文件管理
    ignore_patterns.py              .opencodeignore 解析和模式匹配

  vulnerability/
    python_scanner.py               Python 代码漏洞扫描器（静态分析）

  tests/                            测试套件（pytest）
    runners/
      test_opencode_runner.py       OpencodeRunner 测试

pyproject.toml                      使 `supervisor` 成为可安装包（同时定义所有依赖）
requirements.txt                    备用依赖列表，用于 pip install -r
mcp_server/
  hashline.py                       哈希锚定文件编辑的 MCP 服务器（hashline_read, hashline_edit）
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
| opencode 模型 | *(opencode 默认)* | 转发到 opencode CLI |
| 最大重试次数 | 3 | 强制停止前的连续失败次数 |
| 上下文阈值 | 60% | 压缩在此估算最大值的比例时触发 |
| 最大 token 数 | 128,000 | 模型上下文窗口大小 |
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
| plan_mode_rounds | 0 | 执行前的规划轮数（0 = 禁用） |

### 添加自定义模型

Streamlit UI 提供了内置表单来配置自定义模型，无需手动编辑文件：

1. 在 **协议向导** 页面中，向下滚动侧边栏找到 **"Add Custom Model for Opencode"**
2. 点击 **"➕ Add Custom Model for Opencode"** 打开配置表单
3. 填写以下信息：
   - **Service name**: 您提供商的唯一标识符（例如："my-custom-service"）
   - **Base URL**: 您自定义提供商的 API 端点（例如："https://api.example.com/v1"）
   - **API key**: 您提供商的身份验证密钥
   - **Model names**: 每行一个模型名称（例如："qwen3-coder-plus"、"qwen3-max"）
4. 点击 **"💾 Save Service"** 自动配置 opencode

系统将自动在适当位置创建和管理 opencode 配置文件（在类 Unix 系统上为 `~/.config/opencode/opencode.json`，在 Windows 上为 `%APPDATA%\opencode\opencode.json`）。

保存后，您可以直接从协议向导配置面板的下拉菜单中选择您的自定义模型，或使用格式 `service-name/model-name` 引用它们（例如：`my-custom-service/qwen3-max`）。

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
- 归档分为 `code/`、`results/`、`logs/`、`other/` 子目录
- 元数据存储在 `archive_metadata.json` 中
- 归档带有时间戳和计数器编号
- `.archive/` 目录本身受保护，无法修改
- 版本文件从不删除 —— 只归档

---

## 漏洞扫描

`vulnerability/python_scanner.py` 模块使用 9 个集成工具对 Python 源文件
执行全面的静态分析：**Bandit**、**Pylint**、**pyflakes**、**Semgrep**、
**pip-audit**、**Ruff**、**Vulture**、**deadcode** 和 **pyscn**。
它检测安全问题、代码质量缺陷、死代码、依赖 CVE 和克隆检测。
自动修复可通过 autoflake、isort、autopep8、pyupgrade 和 ruff 实现。
扫描器在自我演进循环中和每次监督判断后调用，在接受代码进入代码库之前
标记危险模式。

---

## 哈希锚定文件编辑

系统包含一个 MCP 服务器（`mcp_server/hashline.py`），提供哈希锚定的
文件编辑工具。从文件读取的每一行都会标注 `LINE#ID` 哈希
（例如 `42#VK| def process(data):`），编码行号和内容。这使得：

- **安全的并发编辑** — MCP 服务器在写入前验证所有 LINE#ID；如果任何
  ID 过时（因为另一个编辑更改了文件），整个操作会被拒绝并返回修正后的 ID
- **原子写入** — 编辑通过临时文件 + `os.replace` 应用，文件永远不会
  处于部分状态
- **编辑操作**：`replace`、`replace_range`、`delete`、`append`、
  `prepend` — 全部通过哈希锚定位置寻址
- **干运行模式** — 验证 ID 而不写入磁盘

哈希锚定 MCP 服务器在启动时自动配置到 opencode 的 `opencode.json` 中，
因此 opencode 在系统提示中接收哈希锚定编辑指令。

---

## 计划模式

当 `plan_mode_rounds` > 0 时，监督器在执行前运行专用的规划阶段：

1. 只读 opencode 实例分析协议和工作区
2. LLM 监督器评估计划（规划期间永远不会将目标标记为已完成，
   因此该阶段始终运行配置的轮数）
3. 最终计划和监督器反馈被存储并前置到构建模式的初始提示中，
   在 opencode 开始编写代码之前为其提供清晰的路线图

在实时运行侧边栏中配置规划轮数。默认为 0（禁用）。

---

## 安全特性

1. **工作区边界强制** — 所有路径引用都针对工作区根目录进行验证
2. **受保护路径检测** — 系统目录和用户保护文件无法修改或删除
3. **协议对齐验证** — 每次迭代都检查是否符合协议
4. **回归测试** — 自我演进在接受更改前将测试结果与基线进行比较
5. **自动回滚** — 不良更改会恢复到上一个良好的检查点
6. **心跳监控** — 检测停滞进程，并在取得进展时延长超时
7. **归档保留** — 历史版本被保留，从不删除

---

## 待办事项

- [ ] 消除 `pip install -e . --force-reinstall`，以便每次自我演进都能自动应用
- [ ] 添加多代理协作/竞争
- [ ] 更好的超时处理和进程跟踪
