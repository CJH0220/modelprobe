# 部署说明

> 本文描述**如何把 mprobe 装到一台机器上并投入运行**。
> 日常操作流程见 [`GUIDE.md`](GUIDE.md)，题库与指标定义见
> [`BANK_AND_METRICS.md`](BANK_AND_METRICS.md)，口径论证见
> [`MEASUREMENT.md`](MEASUREMENT.md)。

---

## 一、环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| Python | ≥ 3.9 | 本机实测 3.14.7 |
| 运行时依赖 | **无** | `requirements.txt` 故意为空，由 `tools/check_deps.py` 强制 |
| 构建期依赖 | 仅「安装为包」需要 | `pip install -r requirements-dev.txt` |
| 磁盘 | 代码与题库约 1.5 MB，运行产物按需增长 | 单轮 `probe` 档约 800 KB，`monitor` 档约 50 KB |
| 网络 | 仅出站 HTTPS，指向被测端点 | 工具自身不联网取价、不取汇率、不上报 |
| 操作系统 | Windows / Linux / macOS | 定时任务仅实现 Windows `schtasks`，其他平台见 4.2 |

**零依赖不是简洁偏好，而是可比性的前提。** 依赖树中任何一个包的版本变动
都可能改变判分结果，而这种变动不体现在 `bank_rev` 上，
于是由此产生的不可比性无法被检测到。

> **不要清理 `data/runs/*/raw.jsonl`。** 判分器变更后需用它重放重算，
> 否则只能重新采样。`data/` 已在 `.gitignore` 中，不进版本管理。

---

## 二、安装

### 2.1 方式一：直接运行（推荐）

```bash
cd 06_modelprobe
python -m mprobe --help
```

无需安装。题库、档案、配置、产物目录均按仓库根定位。

### 2.2 方式二：安装为包

```bash
pip install -r requirements-dev.txt   # setuptools，3.12 起不再随发行版附带
pip install -e .
mprobe --help                         # 等价于 python -m mprobe
```

安装后仍须**在仓库根执行**——路径关系不变。
因此方式二相对方式一并无实质收益，仅在需要把 `mprobe` 暴露为系统命令时使用。

### 2.3 题库不走 pip 分发

题库是需要随版本审阅与冻结的数据，其变更等同于改题，会使既有结果不可比。
经 pip 分发意味着依赖解析器可以在使用者不知情的情况下更换题库版本，
这与「题库随版本冻结、由工具拒绝跨版本比较来保证可比性」直接冲突。
故题库随仓库分发，`bank_rev` 与代码版本强绑定，`sha256` 每次加载校验。

### 2.4 自检（强制门槛）

```bash
python tools/check_all.py
```

**通过判据**：末行 `总计 N 项，通过 N，未通过 0`。九组检查，零请求零成本。

**自检未通过时不要继续部署。** 其中「一致性」与「依赖」两组检查的是
声明与实际是否一致——这类不一致不会在运行时报错，只会使全部结论偏移。

> `tools/` 下另有两个**题库构建期**脚本（`build_public_bank.py`、
> `replay_round_sd.py`）。二者需以 `--source` / `--archive` 显式指定外部
> 数据路径，其产物已随仓库发布，运行时无需重新生成。不提供路径时它们
> 给出提示并退出，不影响其余功能。

---

## 三、配置

### 3.1 端点定义

```bash
cp config/models/_template.json config/models/myendpoint.json
```

| 字段 | 说明 |
|---|---|
| `model.base_url` `model.model` | 端点地址与模型标识 |
| `model.api_style` | `openai` 或 `anthropic` |
| `model.api_key_env` | 密钥所在的环境变量名 |
| `model.max_tokens` | 推理模型须给足，见 3.3 |
| `run.trials` | 每题采样次数，默认 3 |
| `run.qps` | 限速网关须填，否则耗时估算失真 |
| `pricing` | 单价与币种。缺失时工具拒绝执行 |

**该文件纳入版本管理，不得写入密钥。**
它的四个字段（模型、地址、温度、`max_tokens`）构成端点指纹，
任一变更都会使既有基线失效。

### 3.2 密钥

```powershell
# Windows：用户级环境变量
[Environment]::SetEnvironmentVariable("MY_API_KEY","sk-xxx","User")
```

```bash
# Linux / macOS
echo 'export MY_API_KEY=sk-xxx' >> ~/.bashrc
```

或写入 `config/secrets.local.json`（已 gitignore）：`{ "MY_API_KEY": "sk-xxx" }`

**定时任务必须用用户级环境变量或 `secrets.local.json`。**
会话级变量对计划任务不可见，失败表现为静默无输出。

验证用 `python -m mprobe config list`。工具**只输出掩码**，任何路径下不输出明文。

### 3.3 `max_tokens` 的确定

推理模型的输出 token 绝大部分消耗于推理过程，不可见。实测某端点
`max_tokens = 16000` 时 26 道题被截断，其中多数可见输出为 0 字符。

**截断导致的 0 分是配额问题而非能力问题**，据此判定题目过难会删除本可用的题目。
方法：跑一轮 `monitor` 档，检查输出中的「截断」计数，非 0 则提升后重测。

**该值须在建基线之前确定**——变更它会改变端点指纹，已建基线随之失效。

### 3.4 汇率（多币种时）

`config/pricing.json` 的 `fx` 字段，**手工维护，工具永不联网获取**——
测评中拉取可能不可用的汇率接口会导致本可完成的轮次失败。
折算值在输出中一律附带汇率与来源。

---

## 四、投入运行

### 4.1 首次基线

按 [`GUIDE.md`](GUIDE.md) 第 3–7 步执行。部署场景下额外加一步核对：

```bash
python -m mprobe status --json      # 核对各轮 endpoint_sha 与 bank_rev 全同
```

**该步不可跳过。** 存在 `endpoint_sha` 或 `bank_rev` 不一致的轮次
即不得纳入同一基线——不同配置下的测量结果不可合并。

### 4.2 定时任务

Windows：

```bash
python -m mprobe schedule install --model <端点> --tier monitor \
       --cadence daily --at 09:00
python -m mprobe schedule status --json     # 核对 exists 为 true
```

状态读取 `schtasks /query` 的实际结果，不读配置文件——配置声明「已启用」
而任务实际未安装，是此类工具最常见的静默失效形态。

Linux / macOS 用 cron 直接调用 CLI：

```cron
0 9 * * * cd /path/to/06_modelprobe && /usr/bin/python3 -m mprobe check \
          --model deepseek --tier monitor --yes >> /var/log/mprobe.log 2>&1
```

**必须携带 `--yes`**：无人在场时缺少该参数一律视为拒绝。
**退出码即告警信号**（`1` = 告警）。

### 4.3 告警推送（可选）

`config/notify.json`，不存在则不推送且不报错：

```json
{ "enabled": true, "webhook": "https://open.feishu.cn/...", "policy": "abnormal" }
```

`policy` 取 `abnormal` 时仅在观察／告警态推送。**webhook 必须为 https。**
显示时 URL 仅保留域名与前两段路径——常见平台的 token 位于路径中。

推送失败不影响判定：一次已完成并已出判定的 check 不应因 webhook 故障而失败。

### 4.4 浏览器界面（可选）

```bash
python -m mprobe web            # http://127.0.0.1:8790
```

**仅绑定 `127.0.0.1`，不提供 `--host` 参数。** 该服务可读取全部测评历史
与端点配置；绑定 `0.0.0.0` 等于将其暴露给整个网段。
将安全性实现为可修改的默认值等于未实现。远程访问走端口转发：

```bash
ssh -L 8790:127.0.0.1:8790 <user>@<host>
```

界面为**只读**（POST 一律返回 405）。发起测评走 CLI 或 MCP。

### 4.5 MCP 与 Skill（可选）

```bash
python install.py            # 预演，不动文件
python install.py --apply    # 写入
```

写入五处用户级配置，均先备份并打印 diff，已存在且内容不同时不覆盖。
Skill 与 MCP 是两套独立机制，二者均为 CLI 的门面，不含测量逻辑。
安装后须**新开会话**才会被发现。

---

## 五、升级与回滚

版本号即 `bank_rev`。升级意味着：

| 影响 | 处置 |
|---|---|
| 既有基线失效 | 重新采集轮次并构建基线 |
| 跨版本分数不可比 | 工具直接拒绝该比较，非警告 |
| 判分器可能变更 | 用 `raw.jsonl` 重放可重算历史，无需重新采样 |

升级前先读 [`CHANGELOG.md`](CHANGELOG.md)。

**判分器变更后的历史重算**：判分器变更等同于改题。实测一次判分器缺陷修复
使同一批存档响应的通过率由 79.5% 变为 89.5%——该虚假变化的量级超过
多数真实的模型间差异。重算不需要重新采样：

```bash
python tools/probe_analyze.py data/runs/<run_id> --write
```

该脚本用**当前**判分器重放 `raw.jsonl`，并报出与历史记录的分歧条数。

**回滚**用 `git checkout <旧版本 tag>`。`data/` 不进版本管理，运行产物不受影响；
但基线与 `bank_rev` 绑定，回滚后须确认二者一致，否则判定会被拒绝。

---

## 六、部署后核对清单

| 项 | 命令 | 通过判据 |
|---|---|---|
| 自检 | `python tools/check_all.py` | 未通过 0 |
| 依赖 | `python tools/check_deps.py` | 零第三方依赖，三处声明一致 |
| 题库完整 | `python -m mprobe bank info` | 显示预期的 `bank_rev` 与题数 |
| 端点可用 | `python -m mprobe config list` | 目标端点密钥为 `✓` |
| 连通与档位 | `python -m mprobe eval --model X --tier monitor --dry-run` | 无「渠道不通」，且能读出最小可检出退化与费用 |
| 基线就绪 | `python -m mprobe baseline --model X --tier monitor --json` | 返回基线且 `provisional` 为 false |
| 判定可执行 | `python -m mprobe check --model X --tier monitor --dry-run` | 不报「选不出足够的题」 |
| 定时任务 | `python -m mprobe schedule status --json` | `exists` 为 true |
| 清单一致 | `python tools/inventory.py` | 代码、题库、端点、产物与预期一致 |

---

## 七、部署期常见问题

运行期的排错见 [`GUIDE.md`](GUIDE.md) 第五节。以下为部署阶段特有的：

| 症状 | 原因与处置 |
|---|---|
| `pip install -e .` 报 `BackendUnavailable` | 缺 setuptools。`pip install -r requirements-dev.txt` |
| `找不到端点配置` | `config/models/` 下无对应 json，或文件名与 `--model` 参数不一致（参数取文件名，不含扩展名） |
| 在其他目录执行 `mprobe` 报找不到 `banks/` | 预期行为。须在仓库根执行 |
| `题库校验失败，拒绝运行` | 预期行为。本机修改一个字节即导致该机分数与他机不可比。若确需改题，改题库并升版本号 |
| 定时任务未执行 | 依次核对：`schedule status` 的 `exists`、命令中是否有 `--yes`、密钥是否为用户级 |
| 构建期脚本报找不到数据 | 需以 `--source` / `--archive` 指定外部数据路径。其产物已随仓库发布，部署时不需要它们 |
