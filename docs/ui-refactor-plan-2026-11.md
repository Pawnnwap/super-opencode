# Supervisor Console UI 系统性改进方案

> 日期：2026-11-16
> 对象：`services/webui/`（6,687 行）+ `app.py`（314 行），NiceGUI 3.17 / Quasar 2 / Tailwind（浏览器端 JIT）
> 来源：三份独立审计（视觉 30 条 / 体验 28 条 / 工程 30 条，共 88 条，去重后约 74 条独立问题）
> 原始证据：`.workbuddy/audit/{visual,ux,engineering}.md` + `.workbuddy/audit/shots/`（37 张截图）

---

## 0. 一句话结论

**这套 UI 不是"丑"，是"没有权威"**：样式层没有层叠权威（谁能压谁靠考古）、状态层没有语义权威（7 个 job state 各自 if/else）、启动层没有真值权威（前置条件只写在文案里、丢弃返回值）。三者叠加，导致今天任何一处修补都会被另外两处吃掉——所以本轮的关键不是修 74 条，而是**先把三条权威立起来，再让 74 条自然收敛**。

---

## 1. 三份审计的交叉矩阵

### 1.1 独立命中（两份以上同时发现 → 最高置信）

| 问题 | 视觉 | 体验 | 工程 | 裁决 |
|---|---|---|---|---|
| CANCELLED 被渲染成失败红 | V02 **P0** | UX-07 P1 | — | 采纳视觉口径：**升为 P0**（实测 20 卡中 13 张红） |
| 卡片唯一入口 `View` 常驻 opacity .55 | V04 **P0** | UX-28 P1 | E-07 相关 | 合并，按 P0 处理 |
| 全量日志无级别着色 | V05 **P0(v2)** | UX-15 P1 | E-10/E-11 | 三方命中，根因是 `ui.log` 纯文本通道 |
| 5 步进度条不可点 | V18 P1 | UX-05 **P0** | — | 合并为一条：`✓ + 可点 + aria-current` 一次修完 |
| 零无障碍语义 | V09/V17 | 无障碍清单 12 行 | E-22 | 合并；实测 `h1..h4=0`、`aria-live=0`、`aria-label=0` |
| 指标条计数语义混乱 | V07/V08 P1 | UX-06/07 P1 | E-23 P2 | 合并：5 枚收敛为 3 枚 |
| `--sop-text-3` 小字号 | V09/V17 | 对比度行 | — | 已统一口径：s2 上 4.62（余量 0.12，**不是缺陷**） |
| 内联 `.style()` 绕过类系统 | V10 P1 | — | E-07 **34 处** | 合并 |
| 按钮系统失去二级色 | V27 **P0** | — | E-02 同源 | 合并：同一根因的两种可见后果（outline 失效 + 55 个按钮同色） |
| 同一状态重复展示 | V16 P1 | UX-06 P1 | — | 合并：保留 2 处 |

### 1.2 单方命中但必须采纳（另两份未覆盖的盲区）

- **工程 E-01/E-02（CSS 未分层，双向失效）**——这是本次审计**最重要的单点发现**，视觉与体验都无法从渲染结果反推。它解释了为什么"改一个颜色要试三遍"。更严重的后果由视觉组实测补上：**55 个按钮的 computed color 全部是 accent**，`flat` / `outline` 的二级色从未生效——意味着"查看"与"删除"在视觉上无法区分，这不是审美问题而是安全问题。
- **工程 E-03（每个击键同步全量写盘）**——与 2026-11-13 事件循环阻塞同型，是唯一有"历史故障对照"的 P0。
- **工程 E-04（render 期副作用）**——`wizard.py:573` 用 `create=True`，"只是打开向导页看一眼"就会在用户工作区写 `access_protection.json`。对一个跑实盘任务的工作区，这是不该有的副作用，也会弄脏 git status。
- **体验 UX-01（Launch 丢弃 readiness 返回值）**——代码级确认的"可点但必失败"。
- **体验 UX-03（探针结果不持久化）**——与向导"步骤 1–4 只做一次"的承诺直接矛盾。

### 1.3 冲突与裁决

| 冲突 | 甲方 | 乙方 | 裁决 |
|---|---|---|---|
| **层序放哪里** | 工程：`sop` 放最后（普通声明压 utility） | 视觉：theme 放最前（`!important` 压 Quasar） | **两者都对但互斥**——普通声明与 `!important` 的层序要求相反。裁决为**双层**：`sop` 放最后 + `sop-important` 放最前，已按规范推演验证三种方案（见 M1 第 1 点） |
| 日志着色方向 | 视觉 v1：颜色涂太多（violet 铺满正文） | 视觉 v2 自纠 + 体验：级别色根本没生效 | **采信 v2**：8 个级别色只活下来 2 个，且只在 5 行摘要条。v1 是缩略图误判，已勘误 |
| 修复路径 | 体验原稿：换 `ui.html` 逐行着色 | 工程：`ui.log` 纯文本才能扛 1000 行流式 | **收敛为单档**：`[ERR]/[WRN]` 文本标记。彩色 column 被三方共同否决（性能推演见 M5），不排期 |
| 禁用态 `filter: opacity()` | 视觉：这是规避层冲突的正确手法 | 工程 E-29：`filter` 建新包含块、降抗锯齿，且 hover 会盖掉禁用态 | **裁决为过渡手段**：@layer 入层后换回 `opacity`；过渡期必须同时锁 `.disabled:hover` |
| 正文 13.5 → 13 | 视觉：提升密度 | 视觉自注：需同批提亮 text-2 补偿 | **采纳但标为可选**（低风险版只做 12.5→13 与 10→10.5） |

---

## 2. 五个根因（症状 → 根因 → 证据）

### R1 样式层没有层叠权威 → 正反馈恶化环

```
theme.CSS 30KB 未分层注入
  ├─→ 同属性冲突 5/5 全由 theme 胜出（gap-3 → 8px 而非 12px）
  └─→ theme 自己写的 !important 又输给 quasar_importants（outline 按钮 #e9eef6 → 实测 rgb(79,140,255)）
        ↓
开发者无法预测结果 → 转投内联 .style()（34 处）+ 加 !important（15 处）
        ↓
视觉令牌进一步漂移（12 个背景值 / 14 级字号 / 20 档间距 / 7 种控件高 / 9 种圆角）
        ↓
更需要 !important 维持现状 —— 环闭合
```
**证据**：`theme.py:644`、CSSOM 层序实测、`_cascade3.py` 输出。
**这是元问题**：不修它，本方案里所有视觉改动都会被吃掉一半。

### R2 状态层没有语义权威

7 个 job state × 5 个语义色 × 4 个展示位置（卡左条 / 徽标 / 指标 chip / 筛选）各自判断：

- CANCELLED 借失败红（`theme.py:465` + `layout.py:29`）→ 看板 13/20 卡红，43% 成功率读成全线崩溃
- PENDING 卡左条走默认灰、徽标走 running 蓝（`theme.py:459-466` 无 `--pending`）→ 同一卡片自我否认
- ATTENTION 混装 FAILED+CANCELLED+ABANDONED（`job_states.py:37`）→ 最大数字永远报警，57% 需要"关注"，告警贬值
- 指标 5 枚里 2 枚语义重叠（V08）

**根因**：没有一张 `state → (tone, icon, label, counted_in_attention)` 的真值表。

### R3 启动层没有真值权威

- `run.py:245` 调 `_render_readiness(workspace)` **丢弃返回值**，`locked` 只由 `tests_ok()` 决定
- `launch.py:37-45` 入队前不校验协议文件存在 → 按钮亮着，点完必失败
- `state.py:31-34` 探针 flag 标注 `not persisted` → 重启回 0/5、Launch 重锁
- `run.py:36-46` 与 `64-84` 两个不同病因、同名 CTA 叠在同一屏
- `wizard.py:1540`（1/5）与 `1580-1586`（3 step(s) left）两套分母并存

**根因**：前置条件被当作"文案"而不是"可求值的数据"，四处各自手写。

### R4 诊断能力缺失：最需要看清楚的时刻最瞎

- FAILED 页无 Failure 面板，Latest 被截成 `❌ Unhandled exception:`（`log_panel.py:49-56`）
- 全量日志无级别着色（`ui.log` 纯文本），500 行里靠 emoji 找 error
- `event #` 输入框永远无法填写（界面上从不显示编号）
- 日志面板每次变化全量 `clear()+push()` 最多 1000 条，伴随 3 次 `run_javascript`（20,783 B / 5 条消息）
- 更新签名不含首条 ts → 同毫秒批量写入时**漏更新**

**根因**：这个工具的核心价值是"盯长任务"，但工程投入集中在"推送"，没集中在"出事时怎么读"。

### R5 语义结构与密度没有账本

- `h1..h4` = 0、`nav` = 0、`aria-live` = 0、`aria-label` = 0（业务代码；仅 Quasar 自带 7 个 role）
- 最低对比度全落在承载结构的文本上：nav caption 3.48、抽屉页脚 2.77、相位箭头 **1.59**
- 设计系统只有"命名"，没有"数学"：令牌覆盖率 73.8%，44 个唯一 `rgba()`，251 处字面 px

---

## 3. 改进方案：四条主线 + 一条横切

### M1 样式权威链（元问题，最先做）

**目标**：把"谁能压谁"从考古变成读一行声明；把设计令牌从命名变成数学。

1. **定两层，不是一层**（这是对两份审计建议的合并修正）：

```css
/* 层序声明：sop-important 在最先，sop 在最后 */
@layer sop-important, theme, base, quasar, nicegui, components, utilities, overrides, quasar_importants, sop;
```

   - `@layer sop { … }`：全部普通规则放这里。普通声明中**后**声明的层胜出，`sop` 排最后 → 压住 Tailwind utilities（修 E-01 的正向失效：`gap-3` 不再被 `.sop-*` 静默吞掉）
   - `@layer sop-important { … }`：只放需要压 Quasar `!important` 的少数规则（约 8 条，如 `.q-btn.bg-primary{color:#e9eef6!important}`）。**`!important` 的层序是反转的——先声明的层胜出**，所以必须放最前（修 E-02 / V27：outline 与 flat 按钮恢复二级色，破坏性操作不再和中性操作同色）
   - **为什么不能只做一层**：工程建议"sop 放最后"只能让普通声明赢，theme 里那 8 处 `!important` 仍输给 `quasar_importants`，55 个按钮继续全部渲染成 accent；视觉建议"theme 放最前"则反过来让 Tailwind utilities 压住所有 `.sop-*`。两个单层方案各修一半、各坏一半。已按规范推演验证：A 方案 `!important` 输、B 方案 normal 输、**C 双层两者皆赢**
   - **落地顺序**：先只加层（行为不变）→ 截图对比 → 再把对抗性 `!important` 逐条迁进 `sop-important` → 最后删掉不再需要的
2. **outline 契约**：`theme.outline_button()` 统一走 `.props("outline color=none")`，消除 Quasar 注入的 `text-primary`
3. **五张令牌表**（这是"视觉不漂"的唯一办法）：

| 表 | 收敛为 | 现状 |
|---|---|---|
| 背景阶梯 | 5 档（canvas / s1 / s2 / s3 / recess） | 12 个值 |
| 字号 | 5 级（h1 23 / panel 11.5 / body 13 / meta 11 / label 10.5 大写） | 14 级 |
| 间距 | 6 档（4/8/12/16/24/40） | 20 档 |
| 控件高 | 3 档（34 / 28 / 44） | 7 档 |
| 圆角 | 3 档（6 / 8 / 10） | 9 档 |

4. **s2 抬升与 text-3 必须同批**：`--sop-surface-2: #1b2430` 后，text-3 在 s2 上从 4.62 掉到 4.47（跌破 AA），必须同步 `--sop-text-3: #8794a8`（5.09）。单独做会"修好层级、弄坏可读性"。
   - **实测受害面**：`/` 页遍历"计算色为 text-3 且最近不透明祖先为 surface-2"的元素共 **27 个**——其中 **23 个是 `.q-field__label`**（每个表单字段的标签本体，10.5px），4 个是 `.sop-step__num`；`/run` 页另有 4 个。也就是说单独抬 s2 会直接打击"用户不知道该填什么"的那层文字，不能靠局部补色绕过。
5. **alpha 派生**：用 `color-mix(in srgb, var(--sop-accent) 14%, transparent)` 替代 44 个手写 rgba。
6. **永久护栏：text-3 不得落在 surface-3 上**。现状 4.02、把 text-3 提到 `#8794a8` 后也只有 **4.34**，两者都不过 AA——s3 上的副标题/metadata 只能用 text-2。注意 `.q-item:hover` 的背景就是 s3，任何下拉菜单、选择器的 hover 副标题都会踩到，这条要写进注释而不是靠记性。
7. **字体栈必须可兑现**：`--sop-mono` 首选 `JetBrains Mono`，本机未安装且全仓无 `@font-face`（实测落 Cascadia，字宽差 52%）。要么把确定存在的 `Cascadia Mono` 提到首位，要么补 `@font-face`；否则换一台机器日志换行与表格对齐全变。同时修正 `apply()` docstring 里"Install fonts"的表述。
8. **第三方表面必须收编**：`.q-notification` / `.q-tooltip` 规则数为 0，而全站有 27 处 `ui.notify` 与 20+ 处 `.tooltip()`——toast 用 Quasar 默认"实色底 + 白字"，白字压 warn 实测 **1.77:1**、压 bad 2.78:1。补 3 条规则（notification / tooltip / dialog 卡片），这是最高频反馈面的最低成本修复。

### M2 状态契约

**目标**：一张真值表决定所有展示点，新增状态不再改 4 个文件。

```python
# services/jobs/job_states.py（示意）
STATE_CONTRACT = {
    "PENDING":   Tone(idle_accent, "●", "QUEUED",   counted=False),
    "RUNNING":   Tone(accent,      "▶", "RUNNING",  counted=False),
    "SUCCESS":   Tone(ok,          "✓", "SUCCESS",  counted=False),
    "FAILED":    Tone(bad,         "✕", "FAILED",   counted=True),
    "BLOCKED":   Tone(warn,        "!", "BLOCKED",  counted=True),
    "ABANDONED": Tone(warn,        "…", "ABANDONED",counted=True),
    "CANCELLED": Tone(neutral,     "–", "CANCELLED",counted=False),  # 用户主动，不报警
}
```
三个消费点全部从表派生：卡片左条 / 徽标 / 指标计数。指标收敛为 3 枚：`54 tasks` / `23 success` / `8 needs you`。

**关键取舍**：CANCELLED 变灰会让"我取消了什么"更难扫到 —— 补偿是把 `cancelled` 做成筛选器里的独立项，而不是继续占用红色告警。反过来保留红色，代价是 43% 成功率的看板被读成全线崩溃。

### M3 启动契约

**目标**：点 Launch 之前就能预判会不会失败；失败后知道为什么、去哪修。

1. 新建 `services/webui/preflight.py`：`blockers(workspace) -> list[Blocker(code, label, fix_href, severity)]`，返回 5 类：`no_protocol` / `probes_stale` / `probes_failed` / `access_not_configured` / `policy_drill_failed`
2. 三处消费同一份数据：`run.py` 的 `locked` 与按钮 hint、向导 Hand-off 清单、顶栏 `ready/locked` popover
3. `launch_job()` 入口对同一份 blockers 硬校验，抛 `LaunchBlocked`，前端 catch 后跳 `fix_href`
4. 探针结果落盘 `{passed, at, engine, model, base_url, key_hash}`，`tests_ok()` 增加"配置指纹一致 + 未过期"校验；失效时顶栏写 `probes stale — re-run` 且可点
5. 协议草稿：三个 textarea 加 `on_value_change` 存 localStorage，reload 后回填并提示 `Restored unsaved draft · discard`；能局部 `refresh()` 的地方不要 `location.reload()`

**收益**：一次性消灭 UX-01 / UX-03 / UX-08 / UX-12 / UX-26（5 条），并把"预判"变成结构保证。

### M4 工作面向导

**问题**：向导实测 2808px（3.1 屏），步骤条不可点（`stepLinks=0`）却长成 tab 的样子。

**两期，不要一次动**：

- **一期（半天）**：步骤条每步变 `<a href="#section">` + 分节加 id + 步骤条 `position:sticky`，`NEXT →` 行常驻。干掉 UX-05 的 80% 痛感，零结构风险。
- **二期（2-3 天）**：单步聚焦——只渲染当前步，其余折叠为一行摘要 + `Edit`，底部固定 `Back / Next`。这一步才会真正解决"改模型 reload 丢草稿"（代价从 1400 字降到当屏）。
- **前置**：二期必须先有 M3 的 blockers + 草稿保护，否则切步会丢输入。

### M5 诊断与无障碍（横切）

**诊断**
- 终端态（FAILED/ABANDONED/BLOCKED）详情页顶部固定 `Failure` 面板：最后一条 error 的完整正文（40 行）+ 阶段高亮 + `Copy error` / `Open folder` / `Re-run`
- Latest 对 error 级别不截断，改 3 行 `line-clamp`
- 日志一期：`[ERR]` / `[WRN]` 文本标记 + 折叠标题显示 `N err`（约 15 行，无性能风险，读屏可用）
- **日志着色到此为止，不做彩色行**——工程给的推导值：彩色 column 下 1000 行 ≈ 150–190 KB/次，按 2 s tick 可达 75–95 KB/s，是当年"日志 burst 把 websocket 打死"的同型风险；视觉组据此主动撤回了自己的彩色 column 方案。同时删掉 6 个从未生效的 `.log-*` 规则，只保留摘要条真正在用的 2 个
- 日志传输：把 3 次 `run_javascript` 合并为 1 次，signature 补首条 ts（否则同毫秒批量写入会漏更新）
- 日志增量：只 push 新增行，不 `clear()` 全量重建（`board.py:444-453` 已有现成范式）

**无障碍**（成本极低，收益确定）
- `theme.page_header` → `<h1>`、`theme.panel` → `<h2>`、抽屉 → `<nav>`
- `notice` 加 `role=status aria-live=polite`（bad 态 `role=alert`）；状态 pill 加 `role=status`；指标条加 `aria-live`
- 13 个纯图标按钮补 `aria-label`（统一走 `theme.icon_button(icon, label)`）
- 步骤条 `role=list` + 当前步 `aria-current=step`；done 态换成 `✓` 图标（不只靠颜色）
- Skip link + `<main tabindex="-1">`
- 4 个快捷键：`g w/g r/g h` 切页、`/` 聚焦搜索、`Esc` 清空、`Ctrl+Enter` 启动

---

## 4. 路线图

### Phase 0 — 止血（半天，~110 行，零结构变更）

| # | 动作 | 修掉 |
|---|---|---|
| 1 | 主色拆 `--sop-accent-fill:#3370e0`（白字 4.63:1），`ui.colors` 同步 | V03 |
| 2 | `.sop-jobcard__actions{opacity:1}` + `:focus-within`，只把删除图标降到 .8 | V04 / UX-28 |
| 3 | CANCELLED 退出红色家族（卡条灰 + `pill-cancelled`） | V02 视觉半边 |
| 4 | 结构文本提亮：nav caption / 抽屉页脚 / 相位箭头 | V17（1.59 → 3.05） |
| 5 | `locked = (not tests_ok) or (not proto_ok)`，`launch_job` 兜底校验 | UX-01 |
| 6 | 离开页面不丢草稿：`app.storage.general` 暂存协议草稿 + reload 前 `flush` + reload 后回填并提示 | UX-04 / E-16 |
| 7 | 步骤条 anchor + sticky + done 换 `✓` + `aria-current=step`（同一段代码一次改完） | UX-05 / V18 |
| 8 | `app_state.schedule_save()`：400ms 尾沿 debounce | E-03（历史故障同型） |
| 9 | 看板 Search 加 `debounce=350` + `Esc` 清空 | UX-16 / E-03 扩展 |
| 10 | `wizard.py:573` 的 `create=True` 改为首次编辑时创建 | E-04（打开页面即写盘） |
| 11 | 给 `delete` 按钮补 `color=red`（Stop / Cancel batch 已有，不可逆的删除反而没有） | V27 语义半边 |

**第 2 项的硬约束**：只做"opacity 常驻 + `:focus-within`"这一档，**不要顺手改成整卡可点**。若要做整卡可点，卡内 `folder_open`（`board.py:547-552`）与 `delete`（`board.py:570-572`）必须同批加 `on('click.stop')`，否则"打开文件夹"和"删除记录"会连带进入任务页——两个副操作变成陷阱。两档不可混做。

**第 10 项的理由**：`create=True` 在 render 期执行，意味着"只是打开向导页看一眼"就会在用户工作区生成 `access_protection.json`。对一个跑实盘任务的工作区，这是不该有的副作用（也会让 git status 变脏）。

**验收**：11 项全绿；`checks/` 现有 167 条 UI 相关测试不红。

### Phase 1 — 立权威（1-2 天）

1. **双层定层**：`sop` 放最后（普通声明）+ `sop-important` 放最前（`!important` 反转），8 条对抗规则迁入后者
2. `outline_button` 原语（`color=none`）
3. 五张令牌表定稿 + s2/text-3 同批
4. `STATE_CONTRACT` 真值表 + 三个消费点改造 + 指标收敛 3 枚
5. `preflight.blockers()` + 探针持久化 + 顶栏 popover
6. `should_poll()` 纯函数 + `visibilitychange`
7. 禁用态 `filter` → `opacity`
8. 补 `.q-notification` / `.q-tooltip` / `.sop-dialog` 三条规则（toast 白字压 warn 仅 1.77:1）
9. 字体栈改为可兑现（`Cascadia Mono` 提前，或补 `@font-face`）

**验收**：层叠冲突 0 条；`!important` ≤2 处；令牌覆盖率 ≥95%；`blockers` 3 条单测、`should_poll` 3 条单测通过。

### Phase 2 — 改结构（3-5 天）

1. Failure 面板 + Latest 不截断
2. 日志一期（文本标记 + 左边线 + 计数）→ 增量 push + 3 JS 合并
3. 看板：卡片第 4 行摘要 + duration；filter chip 可点
4. 向导二期（单步视图）
5. 无障碍基础：h1/h2、nav、aria-live、icon button label、skip link
6. 4 个快捷键

**验收**：FAILED 页 1 步可见原因；日志 error 行可定位；h1=1 / nav=1 / aria-live≥3 / 图标按钮 100% aria-label。

### Phase 3 — 固化（持续）

1. 测试护栏：`test_ui_cascade.py`（6 条不变量）、`test_ui_a11y.py`（4 条）、`test_preflight.py`（3 条）、日志不丢行（1 条）
2. `wizard.py` 三分（先搬零依赖的 `_advanced_section` 142 行验证模式）
3. CSS 落静态文件 + `immutable` 缓存
4. 静态 Tailwind 取代运行时 JIT（**必须最后做**，会重新洗牌层叠）

---

## 5. 取舍与不做清单

| 不做 | 为什么 |
|---|---|
| 先改色值/字号，后修层叠 | 会被 R1 的正反馈环吃掉一半，下次改动继续漂。**顺序是这份方案的核心价值** |
| 静态 Tailwind 迁移提前做 | 会重新洗牌 `.sop-*` 与 utility 的相对胜负，且**无测试可捕** |
| 一次性重写 `wizard.py` | 1651 行里约 600 行要重排，12 个 refreshable 链路；按段搬、先验证模式 |
| 引入 React/Vue 重写 | 当前 974KB 前端已偏重，重写只会更重；问题不在框架 |
| 为 CANCELLED 保留红色 | 会持续贬值告警：57% 的"需要关注"里绝大多数是用户自己取消的 |
| 做完整主题切换（亮色） | 当前令牌体系不支持，成本远大于收益；先把暗色做对 |

---

## 6. 回归风险与护栏

| 改动 | 会打红的 checks | 先手 |
|---|---|---|
| CSS 入 `@layer sop` | 当前 0 条 | 先只入层（行为不变）→ 截图对比 → 再删 `!important` |
| 写盘 debounce | `test_access_profile`(28) / `test_wizard_access_gate`(44) / `test_feasibility_wizard` | `save()` 保持同步，debounce 只加在 `_change`；提供 `flush_now()` |
| `should_poll` 收口 | `test_ui_loop_offload:24`（子串）、`test_ui_silent_disconnect`(16) | 子串断言改 AST；stub timer 只依赖 `timer.active` 写入点 |
| 日志面板增量 | `test_wizard_connectivity_ux:26-34`（5 条子串） | 先把 `hidden` 构造抽成纯函数供测试调 |
| 看板增量 patch | `test_board_history_expansion`(35) | **这是全仓最值得保护的测试**，保留 `history_count_label` 文案契约 |
| `wizard.py` 拆模块 | 4 个 check 用「文件路径 + 函数名」定位 | 把 check 里的路径改成 `wizard*.py` glob |
| `app.py` 加 main 守卫 | `app_bootstrap_test` / `test_guardian` / smoke | 守卫写成 `__main__/__mp_main__` 双值，跑一遍 `smoke_suprun.ps1` |

**结构性短板**：690 条 UI 断言里 203 条（29.4%）是源码子串匹配，样式类断言仅 1 条，DOM 级 0 条 → 本次 3 个 P0 里 2 个是 CSS，**没有任何测试能发现**。Phase 3 的护栏不是可选项。

---

## 7. 目标态验收（一页纸）

| 维度 | 现在 | 目标 | 怎么测 |
|---|---|---|---|
| 层叠 | 未分层，双向失效 | 0 条未分层规则，`!important` ≤2 | CSSOM 枚举 + `test_ui_cascade` |
| 令牌 | 覆盖率 73.8%，12 背景 / 14 字号 / 20 间距 | ≥95%，5 / 5 / 6 | grep + 快照 |
| 对比度 | 最低 1.59（相位箭头） | 正文 ≥4.5，结构 ≥3.0，text-3 不上 s3（4.34） | 实算 + 截图采样 + `_cascade3.py` |
| 状态 | CANCELLED 红，`need attention` 57% | 中性 + 真失败占比可读 | 看板首屏截图 |
| 启动 | 无协议仍可点，重启即锁 | 100% 预判拦截 | `test_preflight` |
| 失败 | 只有 `❌ Unhandled exception:` | 1 步可见完整原因 | 详情页截图 |
| 日志 | 500 行同色，全量重建 | error 可定位，payload ≤1/5 | socket 帧计数 |
| 无障碍 | h1=0 / nav=0 / aria-live=0 | h1=1 / nav=1 / aria-live≥3 / 图标 100% label | `test_ui_a11y` |

---

## 8. 实施状态（2026-11-16，Phase 0）

### 已落地

| # | 改动 | 文件 | 说明 |
|---|---|---|---|
| 1 | 新增 `--sop-accent-fill:#3370e0`；`ui.colors(primary=)` 同步 | `theme.py` | 白字 3.22 → 4.63:1；当前步骤序号同步换用 fill |
| 2 | 卡片动作行 `opacity:1` + `:focus-within`；hover 去掉位移动画 | `theme.py` | 未做"整卡可点"，避免引入副操作陷阱 |
| 3 | `pill-cancelled` 中性样式；CANCELLED 卡条改 `--sop-line-2`；`PILL_MAP` 指向新类 | `theme.py` / `layout.py` | 看板 34 条 CANCELLED 不再显示为红 |
| 4 | nav caption / 抽屉页脚 / phase 箭头提亮 | `theme.py` | 3.48 / 2.77 / 1.59 → 5.76 / 5.76 / 3.05 |
| 5 | `locked` 并入协议存在性；`launch_job` 抛 `LaunchBlocked` 兜底 | `pages/run.py` / `launch.py` | 无协议时按钮禁用且文案列出具体原因；删掉重复 CTA |
| 6 | 协议草稿暂存/回填（`app.storage.general`）+ 三处 reload 前 `flush` | `pages/wizard.py` | 引擎/模型/拉取模型三条 reload 路径不再丢草稿 |
| 7 | 步骤条改 `<a href="#anchor">`、吸顶、done 换 ✓、`aria-current=step`；5 个分节加 id | `pages/wizard.py` / `theme.py` | `0/5` 计数移出 tab 行，避免读成第 6 步 |
| 8 | `app_state.schedule_save()`（400ms 尾沿、独立线程）+ `flush()` | `state.py` | `save()` 保持同步；launch/refine/auto-generate 改 `flush()` |
| 9 | 看板 Search `debounce=350` + `clearable` + Esc 清空 | `components/board.py` | 每字符全库扫描 → 每次停顿一次 |
| 10 | 向导页 render 期不再写 `access_protection.json` | `pages/wizard.py` | `create=True` 移除，首次编辑时由 `_profile_save` 创建 |
| 11 | `delete` 按钮补 `color=red` | `components/board.py` | 不可逆操作此前与 View 同色 |
| 附加 | `--sop-mono` 改 Cascadia 优先；`apply()` docstring 纠正 | `theme.py` | 本机无 JetBrains Mono 且无 `@font-face`，换机排版会变 |

### 与计划的偏离（1 处，已评估）

**左栏 `ui.link` 未改成 `ui.navigate.to`。** 原因：`ui.link` 渲染真实 `<a href>`，保留中键新标签、右键、键盘 Enter 与浏览器历史；改成 `on_click` 会丢掉这些。整页重载的真实代价是 416ms（热缓存）+ 重传 18KB gzip，而不是数据丢失——数据丢失已由第 6 项根治。**保留原生链接语义，把节省的时间留给 Phase 3 的静态 CSS 缓存。**

### 验证

- `checks/` 中与 webui 相关的 **13 个文件、194 个测试全绿**（含 `test_hownow_panel` 的 stub 补 `flush`）。
- 静态断言：新 CSS 规则全部存在、`_setup_steps` 三元组、步骤条使用 `<a>`。
- **未做**：运行中实例（PID 24740，`reload=False`）仍是旧代码，需重启后目视验收渲染结果。

### 遗留到 Phase 1

`.q-notification` / `.q-tooltip` 仍无主题规则（toast 白字压 warn 1.77:1）；`@layer` 双层尚未落地——这两项都需要重启后做视觉对照，放在下一批更稳妥。

---

## 9. Phase 1-3 实施记录（同日完成）

### Phase 1 — 立权威

| # | 改动 | 文件 | 实测结果 |
|---|---|---|---|
| 1 | **层叠双层**：`!important` 规则进 `@layer theme`（NiceGUI 声明的第一层），普通规则进新增 `@layer sop`（追加为最后一层） | `theme.py` | `flat` 按钮从 accent 蓝变为 `rgb(169,180,197)`；`outline` 首次拿到 `#e9eef6`；`delete` 为语义红 |
| 2 | `outline_button()` 原语 + 12 处 `props("outline...")` 补 `color=none` | `theme.py` 等 5 个文件 | 消除 Quasar 注入的 `.text-primary !important` |
| 3 | 令牌表：`--fs-*` / `--sp-*` / `--r-*` / `--ctl-h`；`s2 → #1b2430`、`text-3 → #8794a8`；头部渐变改 `color-mix` 派生 | `theme.py` | 字段标签 4.47 → **5.09:1**；12 个背景值合并为 5 档 |
| 4 | `STATE_CONTRACT` 真值表 + `PILL_TONE_CLASS`，`ATTENTION` 由表派生 | `services/jobs/job_states.py` / `layout.py` | `need attention` 从 **31 → 6**（CANCELLED 不再计入） |
| 5 | `preflight.blockers()` / `hard_blockers()` 单一真值 + 探针持久化（指纹 + 7 天 TTL） | `preflight.py` / `probes.py` / `run.py` / `launch.py` / `state.py` | 重启不再无条件重锁；Launch 与页面共用同一份判断 |
| 6 | 探针结果分两行显示（此前后一个覆盖前一个）+ 成功即落盘 | `pages/wizard.py` | UX-10 修复 |
| 7 | 禁用态 `filter` → `opacity`（层序修复后不再需要 filter）；hover 加 `:not(.disabled)` | `theme.py` | 消除包含块/抗锯齿副作用与"禁用按钮 hover 变亮" |
| 8 | `.q-notification` / `.q-tooltip` 主题化（深色面 + 语义左边条） | `theme.py` | 白字压 warn 1.77:1 → 正文 4.5+ |

**未做（评估后保留现状）**：`should_poll()` 收口。四个 timer 已各自有停止条件（board 空闲 / live 终态 / hownow busy），抽公共函数只能减少重复、不能改变行为；`visibilitychange` 绑定需要 NiceGUI→Python 的 JS 桥，可靠性低于收益。已在 §6 风险表保留该观察项。

### Phase 2 — 改结构

| # | 改动 | 验证 |
|---|---|---|
| 1 | **Failure 面板**：终端态顶部固定，展示最后一条 error 完整正文（截 4000 字）+ Copy / Open folder / Launch again | 实测 `run_27e910d2`：标题 `Failure · FAILED`，正文 **852 字符**完整 traceback，三个按钮就位 |
| 2 | Latest 对 error/blocked 保留前 3 行 + 换行不省略 | 实测出现 `[ERR] Unhandled exception: Traceback … loop_base.py:186` |
| 3 | 日志标记 emoji → `[ERR]/[WRN]/[OK]/[TOOL]/[THINK]` 文本标记 | 等宽对齐、读屏可读、色盲可用 |
| 4 | 卡片补 duration（`created_at → updated_at`） | 老记录无 `created_at` 时不显示（降级安全） |
| 5 | 无障碍：`h1` / `h2` / `<nav aria-label>` / `aria-current` / `aria-live` / 图标按钮 `aria-label` / `html[lang]` | 实测 `h1=[Protocol Wizard]`、`nav=1`、`ariaCurrent=2`、`aria-live=2`、`lang=en` |
| 6 | 快捷键 `g w/r/h`、`/`、`Esc` | 实测 `g+r` → `/run`；`/` → 聚焦首个可见输入框 |

**未做（评估后保留）**：
- **向导单步视图**（原 M4 二期）：步骤条可点 + 吸顶已覆盖审计所说"80% 痛感"，而单步视图要重排 12 个 `@ui.refreshable` 的刷新链，风险显著高于剩余收益。
- **卡片第 4 行摘要**：看板 54 张卡各需读 report/日志才能取摘要，等于把 1.5s 轮询变成 54 次 IO。duration 已补，摘要在"点进去"成本可接受前不做。

### Phase 3 — 固化

| # | 内容 |
|---|---|
| 1 | `checks/test_ui_contract.py`（8 条）：层序、`theme` 层内放对抗规则、accent-fill、令牌值、状态契约完整性、CANCELLED 中性、pill 从契约派生 |
| 2 | `checks/test_preflight.py`（5 条）：缺工作区/缺协议/探针豁免/每条 blocker 有 fix 目标 |
| 3 | 回归：webui 相关 **22 个文件、264 个测试全绿** |

**明确不做**：
- **静态 Tailwind 取代运行时 JIT**：会重新洗牌层叠顺序，且现有测试无法捕获视觉回归（方案 §6 已列为"唯一可能大范围回归"的改动）。
- **CSS 落静态文件**：收益是每次导航省 6.3KB gzip，代价是样式单点失效 + 动 `app.py` 启动链；`@layer` 已解决它的主要动机。
- **`wizard.py` 三分**：4 个 check 用「文件路径 + 函数名」定位，拆分需同批改这些常量，适合单独批次而非本次收尾。
- **日志彩色 column**：三方共同否决（1000 行彩色 ≈150–190 KB/次，2s tick 达 75–95 KB/s）。

### 总体验收（重启后实测）

| 维度 | 目标 | 实测 |
|---|---|---|
| 层叠 | 0 条未分层规则 | `@layer theme {}` + `@layer sop {}`，无 `sop-important` 残留 |
| 按钮语义 | flat/outline 有二级色 | View `rgb(169,180,197)`、delete `rgb(255,107,107)`、outline `rgb(233,238,246)` |
| 状态 | CANCELLED 中性、不计入告警 | 12 张灰卡、`need attention` 6 |
| 对比度 | 字段标签 ≥4.5 | 5.09:1（`#8794a8` on `#1b2430`） |
| 失败可读 | 1 步看到原因 | Failure 面板 852 字符 + 3 个动作 |
| 语义骨架 | h1≥1 / nav=1 / aria-live≥3 | 1 / 1 / 2（notice + pill） |
| 键盘 | g+字母切页 | 已验证 |
| 回归 | 不破坏既有 | 264 passed |
