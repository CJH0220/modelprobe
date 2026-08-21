# DESIGN · 架构与交互

> 本文回答"东西长什么样、怎么被叫起来"。
> 统计口径（档位含义、阈值、题库冻结、判分规则）在 [`MEASUREMENT.md`](MEASUREMENT.md)。

---

## 一、需求原文里需要修正的三处

先把三处必须澄清的地方说清楚，否则后面的设计会建在错的前提上。

### 1.1 "在 claude 和 codex 上装 MCP，获得几个 Skill"

**MCP 和 Skill 是两套不同的机制，不能互相获得。**

| | MCP Server | Claude Code Skill |
|---|---|---|
| 形态 | 一个进程，stdio JSON-RPC | 一个 `SKILL.md` 文本文件 |
| Claude Code | ✅ 支持 | ✅ 支持 |
| Codex | ✅ 支持 | ❌ **不支持** |
| 触发方式 | 模型看工具描述决定调用 | 模型看 `description` 决定加载 |

所以正确的做法是**同时做两层，但只有一份实现**：

```
                    python -m mprobe   ← 唯一真源，全部逻辑在这
                       ▲          ▲
          ┌────────────┘          └────────────┐
   mprobe/mcp/server.py                  skills/*/SKILL.md
   （stdio MCP，给 Codex               （给 Claude Code，每份 ~40 行，
     和任何 MCP 宿主）                   只描述何时触发 + 调哪条命令）
```

**为什么必须只有一份实现**：本项目曾发生
「同一个判分器 bug 存在于两处、只修了一处」的亏，测量逻辑因此只保留一处。
MCP 工具和 Skill 如果各自实现一遍打分，迟早出现"两个界面给出不同的分"。

### 1.2 "读取当前模型名称，配置"

**做不到，而且不该做。** 两个原因：

1. MCP 服务器是被宿主拉起的子进程，**没有可靠途径知道是哪个模型在驱动这场对话**。
2. 就算知道了也没用——**测的是 API 端点，不是当前会话**。
   用户在 Claude Code 里跟 Opus 说话，测的可能是配置里的 deepseek 端点。
   默认成"当前模型"会造成典型的「跑得很顺、结论是错的」。

**改成**：读取 `config/models/*.json` 列出已配置端点，
用 `default: true` 的那个作默认，**每次运行都在输出里回显实际打到的
`base_url` + `model` 字段**。若宿主确实透出了模型标识，只用作默认值的提示，
不作为端点来源。

用户说"测一下现在用的模型"时，Skill 必须回答
"只能测已配置的端点，现在有 A/B/C 三个，你要测哪个？"——而不是猜。

### 1.3 "基线建立，小、中、大都进行一次测评"

**一次跑不出基线。** 阈值 `μ − 2σ` 里的 σ 用三取大规则
`max(观测SD, 二项SD, 下限)`，跑一轮**没有观测 SD**，只剩二项估计。

二项项在历史上确实是那个较大的（实测 4.193 > 3.993），所以一轮基线
**能用，但对"这套环境本身就抖"毫无防御**——网关不稳、限速降级、
渠道换后端，这些都只体现在轮间方差里。

**改成**（既保留一次点火的体验，又不掩盖统计弱点）：

| 档 | 建基线轮数 | 理由 |
|---|---:|---|
| 小 | **5 轮** | 只有 60 请求，最便宜的一档，把观测 SD 买回来 |
| 中 | 1 轮 | 记一个参考点，不用于判定 |
| 大 | 1 轮 | 同上 |

`baselines` 表记 `rounds`。`rounds < 5` 时：
- 界面和 Skill 输出一律标 **「临时基线」**
- 告警条件从**连续两轮**收紧为**连续三轮**（弱统计就少下判断）

这不是加功能，是**让弱的地方显形**。

---

## 二、目录结构

全英文命名。中文只出现在**内容**里（题面、维度中文名、报告正文），
不出现在**任何路径、文件名、字段名、维度代号**里。

```
modelprobe/
├── README.md  GUIDE.md  DEPLOY.md  DESIGN.md  MEASUREMENT.md
│
├── mprobe/                      ← Python 包，零第三方依赖
│   ├── __main__.py              `python -m mprobe`
│   ├── cli.py                   唯一真源：eval / monitor / serve / config / pricing
│   ├── engine/                  从 04/engine 复制，改英文名
│   │   ├── bank.py checkers.py client.py config.py cost.py dims.py
│   │   └── metrics.py aggregate.py scoring.py signals.py report.py runner.py secrets.py
│   ├── monitor/                 从 03/core 拆开
│   │   ├── baseline.py judge.py notify.py schedule.py
│   ├── store/
│   │   └── db.py                SQLite（stdlib sqlite3）
│   ├── mcp/
│   │   └── server.py            stdio JSON-RPC，手写，不引 SDK
│   └── web/
│       ├── server.py            http.server，只绑 127.0.0.1
│       └── static/ index.html app.js style.css
│
├── banks/
│   ├── core.jsonl               自建行为题（全自动判分）
│   ├── pub_v2.jsonl             公开基准题源筛出的新一代公开题
│   ├── retired/                 退役题，只进不出
│   └── MANIFEST.json            bank_rev + sha256 + 每题准入台账
│
├── profiles/  small.json  medium.json  large.json
├── config/
│   ├── models/*.json            端点配置（**不含密钥**）
│   ├── pricing.json             价格表 + fetched_at
│   └── secrets.local.json       密钥，**gitignore**
│
├── skills/                      Claude Code Skill 源文件
│   ├── mprobe-eval/SKILL.md
│   ├── mprobe-monitor/SKILL.md
│   └── mprobe-dashboard/SKILL.md
│
├── install.py                   装 MCP 配置 + 同步 skills 到 ~/.claude/skills/
└── data/                        运行时生成，**gitignore**
    ├── mprobe.db
    └── runs/<run_id>/{raw.jsonl, matrix.jsonl, progress.json, report.md}
```

`banks/` 与 `profiles/` **随版本发布，用户端只读**。理由见 `MEASUREMENT.md` 第三章。

---

## 三、CLI：唯一真源

```
python -m mprobe eval      --model X --tier small|medium|large [--rounds N] [--commit]
python -m mprobe status    --run RUN_ID
python -m mprobe commit    --run RUN_ID            把这次结果并入可比序列
python -m mprobe monitor baseline --model X
python -m mprobe monitor check    --model X --tier small
python -m mprobe monitor schedule --model X --daily small --weekly medium --monthly large
python -m mprobe monitor schedule --off --model X
python -m mprobe serve     [--port 8790]
python -m mprobe config    list | add | test
python -m mprobe pricing   show | refresh
```

三条设计约束：

1. **所有子命令都能 `--json`。** MCP 层和 Skill 层只解析 JSON，不解析人类文本。
2. **只读命令不产生副作用。** `status` / `config list` / `pricing show` 绝不写盘。
3. **任何要花钱的命令，先打印钱和时间，再问。** `--yes` 可跳过，但默认必须问。

---

## 四、三个 Skill

### 4.1 `mprobe-eval` —— 测评

用户说：`帮我测一下 opus 什么水平` / `评估一下这个模型` / `跑个能力测试`

```
① 确认端点  → 列出已配置模型，回显 base_url + model 字段
② 选档      → 小(20题/60请求) 中(50/150) 大(120/360)
③ 报价      → "deepseek 中档：约 0.96 元，约 2 分钟" / "opus 中档：约 10.7 元，约 68 分钟"
④ 跑        → 立刻返回 run_id，之后轮询进度
⑤ 出结论    → 三层结论卡（见 4.4）
⑥ 问入库    → "这次结果要并入可比序列吗？"（原始存档无论如何都保留）
```

第 ⑥ 步的设计要点：**原始响应永远存盘，入库与否只决定它算不算数。**
`data/runs/<id>/raw.jsonl` 无条件写——这个留档已经两次救回数据
（判分器修复后重放、基线误覆盖后重建）。
`--commit` 只是往 `results` 表插一行，把它纳入趋势线和对比。
**永远不给"丢掉原始数据"这个选项。**

### 4.2 `mprobe-monitor` —— 监控

用户说：`盯着 deepseek，每天看一下有没有变笨` / `设置监控` / `取消监控`

```
① 建基线   → 小档 5 轮 + 中/大各 1 轮（见 1.3）；先报总价和总时长
② 排班     → 默认 日→小 / 周→中 / 月→大，可改
③ 告警     → webhook URL 或本地端口，可选；不填就只记录不推送
④ 落地     → 写 Windows 计划任务（复用 03/install_schedule.ps1 的思路）
⑤ 取消     → --off，同时说清楚基线保不保留
```

**一条必须写进 SKILL.md 的边界**：
Skill 和 MCP 都**不是常驻进程**，会话一结束就没了。
真正的定时靠操作系统的计划任务。Skill 只负责**生成并安装**它，
以及**读回 `schtasks /query` 的真实状态**——不是读配置文件里写的那个。
配置文件说"已开启"而任务其实没装，是这类工具最常见的静默失效。

### 4.3 `mprobe-dashboard` —— 浏览器

用户说：`打开界面` / `要看图` / `最近的测评结果`

起 `http://127.0.0.1:8790`，五个页签：

| 页签 | 内容 |
|---|---|
| Overview | 最近 N 次测评/检测，三态色块，趋势线（**同一模型 + 同一 bank_rev 才画在一条线上**） |
| Run | 单次结果：维度雷达、逐题矩阵、成本、耗时、原始响应 |
| Monitor | 基线、阈值、连续低于阈值的轮次、告警历史 |
| Models | 增删端点、填 API Key、连通测试、**在浏览器里直接发起测评** |
| Bank | 当前 `bank_rev`、每维题数、哪些题进监控、哪些题被稳健性过滤掉了 |

四条界面硬规矩：

1. **只绑 `127.0.0.1`。** 不提供 `--host 0.0.0.0`。远程访问走 SSH 端口转发。
2. **API Key 只写 `config/secrets.local.json`，界面永不回显明文**，
   webhook 只显示 netloc + 前两段路径（飞书/Slack 的 token 在路径里）。
3. **题数不足以判定的维度画虚线**，并在 tooltip 写"本档 m=3，最小可检出 30.9 分"。
4. **不同 `bank_rev` 的点不连线**，中间断开并标一个版本分隔符。

### 4.4 三层结论卡

所有输出（Skill、MCP、浏览器）共用同一个结构：

```
┌ 事实层 ────────────────────────────────────
│ deepseek-v4-flash · 中档 · 50 题 × 3 次 = 150 请求
│ bank_rev v1.0.0 ·  14:32 · 用时 2.1 分 · 实测 0.94 元
│ 保守分 68.3 / 期望分 71.5 / 几何平均 66.1
├ 判读层 ────────────────────────────────────
│ 最弱维度：D 幻觉 42.1（本档 m=9，最小可检出 17.8 分）
│ 掩蔽差 12.4 —— 期望分掩盖了最弱维的问题
│ 行为信号：拒答率 2.0% · 免责声明率 8.0% · 思考占比 31%
├ 许可层 ────────────────────────────────────
│ ✅ 可以说："在本题库中档上得分 68.3 ± 7.6"
│ ❌ 不能说："这个模型 68 分"——±7.6 比多数模型间差异还宽
│ ❌ 不能说：和上周的分数比——除非 bank_rev 相同（工具会拒绝）
└────────────────────────────────────────────
```

许可层不是提示文案，是**按档位和题量算出来的**，见 `MEASUREMENT.md` 第二章。

---

## 五、进度条

需求写了"可观察进度条"。这里有一个绕不过去的机制问题：

> **MCP 工具调用是阻塞的：工具返回之前，模型什么都看不到。**
> 所以进度条不可能"流"给模型。

设计成**一次启动 + 多次轮询**：

```
mprobe.eval_start   → 立刻返回 {run_id}，真正的跑放进独立子进程
mprobe.eval_status  → 读 data/runs/<id>/progress.json，返回 {done,total,eta,cost}
```

`progress.json` 单一写者、三个读者：

| | 写 | 读 |
|---|:-:|:-:|
| runner 子进程 | ✅ 每完成一题写一次（原子替换） | |
| MCP `eval_status` | | ✅ |
| 浏览器 `/api/progress` | | ✅ |
| CLI 前台模式 | | ✅ 渲染真正的字符进度条 |

字段：`run_id / state / done / total / label / started_at / eta_sec / cost_so_far / error`。

三种宿主看到的东西不一样，这是对的：

| 宿主 | 用户看到什么 |
|---|---|
| **CLI 前台** | 真·进度条，逐题刷新 |
| **浏览器** | 真·进度条，1 秒轮询 |
| **Claude / Codex 会话** | 模型每隔一会儿汇报一次"37/150，预计还有 1.2 分钟" |

会话里做不到逐帧刷新，这是协议决定的，不要假装能做到。
MCP 的 progress notification 可以作为增强，但**不能作为设计前提**——宿主支持度不一。

opus 大档 163 分钟的量级下，"进度条"真正的价值不是好看，
是**让人知道它没死**。所以 `progress.json` 里 `label` 必须写当前题号，
超过 90 秒没更新就把 `state` 打成 `stalled`。

---

## 六、结果库

SQLite，`data/mprobe.db`。四张表。

| 表 | 关键字段 | 说明 |
|---|---|---|
| `runs` | `run_id, model_key, endpoint_sha, tier, bank_rev, bank_sha, rounds, started_at, state` | 每次跑都有，**不问是否入库** |
| `results` | `run_id, conservative, expected, geometric, sd, per_dim_json, signals_json, cost_measured` | **只有 commit 的才插** |
| `baselines` | `model_key, tier, bank_rev, mu, sigma, sigma_source, threshold, rounds, created_at` | `sigma_source ∈ {observed, binomial, floor}` |
| `checks` | `run_id, baseline_id, score, verdict, consecutive_low` | 三态判定结果 |

四条约束：

1. **`bank_rev` 是每一行的必填字段。** 跨 `bank_rev` 比较由 SQL 层
   （查询强制带 `bank_rev = ?`）+ API 层双重拒绝，**不是警告，是拒绝**。
2. **`endpoint_sha`** = `sha256(base_url + model + temperature + max_tokens)` 前 12 位。
   端点参数一变就是另一条序列。这一条来自实测教训：
   同一个模型配两遍，迟早在 `temperature` 上漂移。
3. **`sigma_source` 必须存。** 用户要能看出这个阈值是观测来的还是二项估的。
4. **`cost_measured` 存实测**，不存按价目表算的估计值。见第七章。

---

## 七、价格

需求说"价格可上网搜寻"。可以做，但有两个坑必须在设计里堵掉。

### 7.1 目录价 ≠ 实付成本

实测数据（同一负载 126 请求）：

| 模型 | 实测单轮 | 每请求 |
|---|---:|---:|
| deepseek-v4-flash | 0.80 元 | 0.0064 元 |
| qwen3.6-flash | 1.18 元 | 0.0094 元 |
| claude-opus-5（限速网关） | ≈ 9.0 元 | 0.0714 元 |

而按 210 题验证集外推的比值是 **70×**，实测是 **11.3×**，**差 6 倍**。
两个数都对，负载画像不同。

所以：**估价优先用本机历史实测**（`runs` 表里同模型同档的中位数），
目录价只在没有历史时兜底，且输出必须标明用的哪一种：

```
预计 0.94 元（依据：本机 3 次同档实测中位数）
预计 9.0 元（依据：目录价估算，误差可达数倍）   ← 必须带这句
```

### 7.2 联网取价不能挡住跑测评

`mprobe pricing refresh` 是独立命令，手动或定期触发，写 `config/pricing.json`
（含 `fetched_at` + `source_url`）。**`eval` / `check` 永远不联网取价**，
只读缓存。取不到就用旧的并注明日期。

理由：一个测模型的工具，自己因为查价失败而跑不起来，很荒唐。

---

## 八、任务判别（触发）

需求：`帮我测一下 opus 什么水平` 要能找到对应 Skill。

### 8.1 路由表

| 用户说的话 | 路由到 | 关键区别 |
|---|---|---|
| "测一下 X 什么水平" · "评估" · "能力如何" · "跑个测试" | `mprobe-eval` | 要绝对水平，**不需要基线** |
| "有没有变笨" · "降智了吗" · "还正常吗" · "掉智商" | `mprobe-monitor check` | 要相对判定，**必须有基线** |
| "每天盯着" · "定时监控" · "设个监控" · "取消监控" | `mprobe-monitor schedule` | 装/卸计划任务 |
| "打开界面" · "看图" · "最近的结果" · "报告呢" | `mprobe-dashboard` | 只读 |

### 8.2 反例同样要写进 SKILL.md

只写正例，Skill 会过度触发。每份 `SKILL.md` 必须有一段"**不要触发**"：

| 不要触发 | 因为 |
|---|---|
| "哪个模型比较好" | 这是选型，本工具给不出排名（要同日多模型 + 控制变量） |
| "帮我优化这段提示词" | 不是测评 |
| "解释一下什么是幻觉" | 概念问题 |
| "我觉得它变笨了" | 这是**观察不是指令**。应回答"要跑一次检测吗"，而不是直接花钱开跑 |

最后一条最重要：**任何要花钱的动作都不能被一句感慨触发。**

### 8.3 eval 和 check 撞车时怎么办

"测一下 deepseek 现在怎么样" 两边都像。规则：

- 该模型**有基线** → 默认 `check`（便宜、快、直接回答"有没有变差"），
  并附一句"要看绝对水平的话，可以跑 `eval`"
- 该模型**没有基线** → 只能 `eval`，并提示"要长期盯的话先建基线"
- **两个都不猜的情况**：用户同时提到了具体分数或维度 → 问一句

---

## 九、安装

`python install.py` 做四件事，每件都幂等：

1. 自检 Python ≥ 3.9、写权限、`data/` 可建
2. 写 MCP 配置
   - Claude Code：`claude mcp add` 或写 `~/.claude.json`
   - Codex：写 `~/.codex/config.toml` 的 `[mcp_servers.mprobe]`
   - **改任何用户级配置前先备份，并打印 diff**
3. 同步 `skills/*` 到 `~/.claude/skills/`，
   **目标已存在且内容不同时不覆盖，打印 diff 让用户决定**
4. 引导配置第一个模型端点 + 连通测试

密钥处理（沿用现有铁律，不放松）：

- 密钥**从不写进** `config/models/*.json`（那是要进版本管理的）
- 只走环境变量、或 `config/secrets.local.json`（gitignore）
- 计划任务下必须设成**用户级**环境变量
  （`[Environment]::SetEnvironmentVariable("NAME","sk-xxx","User")`），
  已打开的终端看不到——这一条要写进安装向导的输出里
- 任何回显走 `secrets.mask()`，**永不返回明文**
