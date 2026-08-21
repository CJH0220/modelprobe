# 操作指南

> 本文只讲**怎么操作**。为什么这样设计见 [`MEASUREMENT.md`](MEASUREMENT.md)，
> 考什么题、分怎么算见 [`BANK_AND_METRICS.md`](BANK_AND_METRICS.md)，
> 换机器部署见 [`DEPLOY.md`](DEPLOY.md)。
>
> 需要 Python ≥ 3.9。**无需安装任何第三方包。**

---

## 一、完整流程（照着一行一行执行）

全部命令在仓库根目录下执行。

### 第 1 步：取得代码并自检

```bash
git clone https://github.com/CJH0220/modelprobe.git
cd modelprobe
git checkout v1.0.0
python tools/check_all.py
```

看到末行 `未通过 0` 才继续。不是 0 就先停下。
刚 clone 完会有一条显示 `未验证`，那是正常的（它要等你跑过一轮才能验）。
已经有代码的，只跑最后一条即可。

### 第 2 步：确认端点和密钥

```bash
python -m mprobe config list
```

要测的那一行前面是 `✓` 就行。若是 `!`（密钥缺失），执行：

```powershell
[Environment]::SetEnvironmentVariable("DEEPSEEK_API_KEY","你的密钥","User")
```

设完**关掉这个终端重新开一个**，再回到第 2 步确认。

### 第 3 步：看这次要花多少钱

```bash
python -m mprobe eval --model deepseek --tier monitor --dry-run
```

不发请求、不花钱。输出里看三个数：花多少钱、跑多久、**最小可检出退化多少分**。

### 第 4 步：试跑一轮

```bash
python -m mprobe eval --model deepseek --tier monitor --yes
```

约 5 分钟、0.06 元。跑完看两处：

- `请求成功 42/42` —— 失败多了这轮不算
- `截断 0` —— 不是 0 说明 `max_tokens` 不够，见第五节

报告在 `data/runs/<run_id>/report.md`。

### 第 5 步：再跑 4 轮（凑够 5 轮才能建基线）

```bash
python -m mprobe eval --model deepseek --tier monitor --yes
python -m mprobe eval --model deepseek --tier monitor --yes
python -m mprobe eval --model deepseek --tier monitor --yes
python -m mprobe eval --model deepseek --tier monitor --yes
```

### 第 6 步：建基线（不花钱）

```bash
python -m mprobe baseline --build --model deepseek --tier monitor
```

它用第 4、5 步已经跑好的数据算，不发新请求。
看到 `告警阈值 = 均值 − 2σ` 就成功了。

### 第 7 步：做一次判定

```bash
python -m mprobe check --model deepseek --tier monitor --yes
```

输出 `正常` / `观察` / `告警` 三种之一。

### 第 8 步：挂成每天自动跑（可选）

```bash
python -m mprobe schedule install --model deepseek --tier monitor --cadence daily --at 09:00
python -m mprobe schedule status --json
```

第二条命令输出里 `"exists": true` 才算装上了。

### 第 9 步：看图（可选）

```bash
python -m mprobe web
```

浏览器打开 `http://127.0.0.1:8790`。按 Ctrl-C 停止。

### 第 10 步：装进 Claude（可选）

```bash
python install.py
python install.py --apply
```

第一条只打印将要改什么，不动文件；第二条才真写。
装完**新开一个 Claude 会话**，直接说「测一下 deepseek 什么水平」即可。

---

## 二、日常只需要三条命令

```bash
python -m mprobe check --model deepseek --tier monitor --yes
python -m mprobe status
python -m mprobe web
```

依次是：本轮有没有退化、最近跑了哪些、看图。

---

## 三、五个可选档位

```bash
python -m mprobe tiers
```

| 档位 | 花费 | 耗时 | 能发现多大的退化 |
|---|---:|---:|---|
| `monitor` | 0.06 元 | 5 分 | 15.4 分以上 |
| `small` | 0.19 元 | 16 分 | 12.6 分以上 |
| `medium` | 0.48 元 | 39 分 | 7.7 分以上 |
| `large` | 1.16 元 | 1.6 时 | 4.9 分以上 |
| `probe` | 2.82 元 | 2.0 时 | 3.6 分以上 |

数值为 deepseek 实测。换端点后费用会变，跑 `--dry-run` 看准确值。

**跑长档位不要在前台等**：

```bash
python -m mprobe eval --model deepseek --tier probe --yes --run-id run-001
python -m mprobe status --run run-001
```

---

## 四、必须知道的六件事

| # | 事项 |
|---|---|
| 1 | **测的是配置里的 API 端点，不是正在对话的那个模型。** 二者链路不同，分数不可互推 |
| 2 | **「没告警」只等于「退化小于本档的最小可检出量」**，不等于「模型没问题」 |
| 3 | **告警不等于模型变差。** 先按顺序排查：换渠道 → 换时段 → 看是哪些题失败 |
| 4 | **单轮低于阈值只出「观察」**，连续两轮才告警。单轮不作数 |
| 5 | **改了 `max_tokens`、`temperature` 或模型名，基线就作废**，要重新走第 4–6 步 |
| 6 | **不要删 `data/` 目录。** 判分逻辑若有修订，靠它重算；删了只能重新花钱跑 |

---

## 五、出问题怎么办

| 遇到 | 怎么处理 |
|---|---|
| 报「渠道不通」但端点是好的 | 模型响应慢。在 `config/models/<端点>.json` 里加一行 `"probe_timeout": 60` |
| 报「选不出足够的题」 | 该档位可用于监控的题不够。改用 `--tier monitor`，或跑 `python -m mprobe bank info` 看可用题数 |
| 某个维度全是 0 分 | 先看有没有截断。有就把 `config/models/<端点>.json` 的 `max_tokens` 调大（如 16000 → 32000）后重跑 |
| 报「题库校验失败，拒绝运行」 | 题库被改动过。这是预期行为——题库是冻结的，不要手改 |
| 报「价格未配置」 | `config/models/<端点>.json` 缺 `pricing` 段。不知道花多少钱时工具一律拒绝执行 |
| 密钥设了还是说缺失 | 用户级环境变量只对**新开的**终端生效。关掉终端重开 |
| 定时任务没跑 | 依次查：`schedule status` 的 `exists`、命令里有没有 `--yes`、密钥是不是用户级 |
| 界面打开是空的 | 查是否有旧进程占着端口，核对启动时打印的实际端口号 |

---

## 六、改了代码之后

```bash
python tools/check_all.py
```

九组检查，不发请求、不花钱。`未通过 0` 才算改对。
