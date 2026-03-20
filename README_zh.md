# opencode-supervisor

---

[English](./README.md) | 中文

Streamlit UI + 模块化 Python 后端，运行一个 `opencode` 代理于监督反馈循环中 —— 并能将同一个循环作用于**自身**来调试和改进其源代码。

---

## 前置要求

- Opencode
- Python 3.11 或更高版本
- OpenAI 或任何兼容提供商（如 Ollama）的 API 密钥

---

## Windows 安装

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
- 使用 Nvidia nim: **nvidia/nemotron-3-super-120b-a12b** 或 **qwen/qwen3.5-397b-a17b**
- 使用 iflow: **qwen3-coder-plus**

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
5. **自我演进** — 系统可以分析和改进其自己的代码库，在每次更改前后运行测试，并在回归时自动回滚

---

## 页面

### ① 协议向导
用自然语言填写 INPUT / TARGET / RESTRICTIONS → 点击 **用 AI 优化** → 查看生成的 `protocol.md` → 接受并保存。

### ② 实时运行
针对任何项目工作区启动监督循环。实时日志流随时可以停止。

### ③ 报告
运行后的最终监督报告，带有下载按钮。

### ④ 自我演进
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
| 演进报告 | `evolution_report.md` — 更改的文件、测试差异、最佳检查点 |

---

## 架构

```
app.py                          Streamlit UI  (4 pages)
supervisor/
  config.py                     Frozen config dataclass
  protocol.py                   Parse / validate protocol.md
  protocol_wizard.py            OpenAI-SDK protocol refiner
  llm_supervisor.py             OpenAI-SDK judge  (system prompt = protocol)
  opencode_runner.py            Subprocess wrapper for the opencode CLI
  context_monitor.py            Tracks context window usage
  workspace_guard.py            Blocks out-of-workspace path references
  loop.py                       Base state machine; yields Event dicts
  codebase_analyzer.py          Snapshots source tree for LLM context
  test_runner.py                Runs pytest / syntax check; structured results
  checkpoint.py                 File-copy snapshot / restore / diff
  meta_protocol_builder.py      Generates meta_protocol.md from goal + snapshot
  self_evolution_loop.py        Extends loop with test gating + rollback
pyproject.toml                  Makes `supervisor` an installable package
```

---

## 配置

| 设置 | 默认值 | 描述 |
|---------|---------|-------------|
| API 密钥 | — | 您的 API 密钥（OpenAI 或任何兼容提供商）|
| Base URL | *(留空 = OpenAI)* | 覆盖本地/代理端点，例如 `http://localhost:11434/v1` |
| Supervisor / 向导模型 | — | 您的提供商接受的任何模型字符串 |
| opencode 模型 | *(opencode 默认)* | 转发到 opencode CLI |
| 最大重试次数 | 3 | 强制停止前的连续失败次数 |
| 上下文阈值 | 60 % | 压缩在此估算最大值的比例时触发 |
| 超时 | 300 s | 超过此时间沉默则视为 opencode 无响应 |

---

## 待办事项

- [ ] 消除 `pip install -e . --force-reinstall`，以便每次自我演进都能自动应用
- [ ] 添加多代理协作/竞争
- [ ] 更好的超时处理和进程跟踪
