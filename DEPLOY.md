# 部署手册

> 本文给出从获取代码到投入运行的完整规程，并在每一步给出预期输出。
> 日常操作参见 [`GUIDE.md`](GUIDE.md)；题库与指标定义参见
> [`BANK_AND_METRICS.md`](BANK_AND_METRICS.md)。
>
> 要求 Python ≥ 3.9，无第三方运行时依赖。

---

# 一 · 安装

## 1. 获取代码

```bash
git clone https://github.com/CJH0220/modelprobe.git
cd modelprobe
git checkout v1.1.0
```

第三行不可省略：版本号即 `bank_rev`，跟随 `main` 会在某次 `git pull` 后
使既有基线失效。可用版本以 `git tag` 为准。

## 2. 自检

```bash
python tools/check_all.py
```

```
============================================================
总计 43 项，通过 43，未通过 0
```

**`未通过` 必须为 0，否则不得继续部署。**

首次安装时会出现一条 `未验证`，属正常：

```
  ⚠ 未验证 跨 bank_rev 比较 → 拒绝  —— 本机还没有结果库
  通过 16 ／ 未通过 0 ／ 未验证 1
```

该项需要结果库中存在两个不同 `bank_rev` 的轮次才能构造，
采集一轮之后即可验证。未验证不计入失败。

## 3. 校验题库

```bash
python -m mprobe bank info
```

```
== 题库 1.1.0 ==
冻结于 2026-08-21 15:07:37 ｜ 253 题 ｜ 监控池 224 题（其中轮间 SD 已实测 35 题）
```

题库每次加载校验 sha256。若报「题库校验失败，拒绝运行」，
以 `git clone` 重新获取，不要使用 zip 解压——换行转换会破坏校验。

## 4. 安装为系统命令（可选）

```bash
pip install -r requirements-dev.txt
pip install -e .
```

安装后 `mprobe` 等价于 `python -m mprobe`，但仍须在仓库目录内执行。
不安装不影响任何功能。

---

# 二 · 配置

## 5. 查看端点

```bash
python -m mprobe config list
```

```
== 端点 ==
| key | 模型 | 指纹 | 密钥 | 默认 |
|---|---|---|---|---|
| deepseek | deepseek-v4-flash | c1079e441bbb | 未设置：DEEPSEEK_API_KEY | ✓ |
| qwen | qwen3.6-flash | b3504bcdec06 | 未设置：DASHSCOPE_API_KEY |  |
```

密钥列显示 `未设置：<变量名>` 表示尚未配置。

## 6. 配置密钥

```bash
python -m mprobe config key --model deepseek
```

输入不回显，写入 `config/secrets.local.json`（不纳入版本管理）。
重新执行第 5 步，密钥列应为：

```
| deepseek | deepseek-v4-flash | c1079e441bbb | sk-…a278（file） | ✓ |
```

该文件优先级高于环境变量，计划任务同样可读，无需重启终端。
工具在任何路径下只输出掩码。

> 也可直接编辑 `config/secrets.local.json`：`{ "DEEPSEEK_API_KEY": "..." }`
> 使用环境变量亦可（密钥列显示 `（env）`），但须新开终端。

## 7. 新增端点

```bash
cp config/models/_template.json config/models/myapi.json
```

| 字段 | 说明 |
|---|---|
| `model.base_url` `model.model` | 端点地址与模型标识 |
| `model.api_style` | `openai` 或 `anthropic` |
| `model.api_key_env` | 密钥变量名 |
| `model.max_tokens` | 初值取 32000，见第 10 步 |
| `run.qps` | 限速网关须填，否则耗时估算失真 |
| `pricing` | 可选。缺失时不报价，其余功能不受影响 |

该文件纳入版本管理，**不得写入密钥**。文件名即 `--model` 参数值。
`model` 的四个字段构成端点指纹，任一变更使既有基线失效。

---

# 三 · 采集与判定

以下命令中的 `deepseek` 替换为目标端点。

## 8. 估算成本

```bash
python -m mprobe eval --model deepseek --tier monitor --dry-run
```

```
== 本轮计划 ==
模型 deepseek（c1079e441bbb）· 档位 monitor · 题库 1.1.0
最小可检出退化 8.6 分 ｜ 95% 区间半宽 ±8.4 分
预估花费 0.19 CNY ｜ 预估耗时 4 分钟
119 题 x 1 次 = 119 次请求，并发 4，输入约 19,779 token、输出约 83,300 token
```

`--dry-run` 不发请求。**耗时估值在该档位首次执行前偏低**——它依据本机
历史实测推算，无历史数据时按理论值计算。`monitor` 档实测约 13 分钟。
费用估值不受此影响。

各档位规模与分辨率以 `python -m mprobe tiers` 为准。

## 9. 采集首轮

```bash
python -m mprobe eval --model deepseek --tier monitor --yes
```

完成后核对 `data/runs/<run_id>/report.md` 的两项：

```
- 请求成功 119/119，失败率 0.0%
```

```
| 输出被截断 | 0（其中 0 次完全没有可见内容） |
```

失败率偏高的轮次不得纳入基线；截断非 0 转第 10 步。

## 10. 确定 `max_tokens`

推理模型的输出 token 绝大部分消耗于推理过程。截断导致的 0 分是配额问题，
不是能力问题。若第 9 步的截断计数非 0，调大 `config/models/<端点>.json`
的 `max_tokens`（32000 → 64000）后重新采集，直至截断为 0。

**该值须在建立基线之前确定。** 变更它会改变端点指纹，既有基线随之失效。

## 11. 补足基线轮次

`monitor` 档需 5 轮：

```bash
python -m mprobe eval --model deepseek --tier monitor --yes
```

重复至累计 5 轮，随后核对各轮配置一致：

```bash
python -m mprobe status
```

```
== 最近 15 轮 ==
| run_id | 类型 | 模型 | 档 | 分数 | 请求 | 失败 | 健康 |
|---|---|---|---|---:|---:|---:|---|
| deepseek-monitor-eval-20260821-095247 | eval | deepseek | monitor | 91.7 | 42 | 0 | ok |
```

`健康` 均为 `ok`、`失败` 均为 0 方可继续。存在 `endpoint_sha` 或
`bank_rev` 不一致的轮次即不得纳入同一基线。

## 12. 建立基线

```bash
python -m mprobe baseline --build --model deepseek --tier monitor
```

不发请求，复用第 9、11 步的数据。预期输出三行：

```
基线均值 <均值> 分，σ = <σ>（取自：…）
告警阈值 = 均值 − 2σ = **<阈值> 分**
建自 5 轮，每轮 116 次请求
```

出现 `**这是临时基线**` 表示轮数不足 5 轮，补足后自动转正。

## 13. 首次判定

```bash
python -m mprobe check --model deepseek --tier monitor --yes
```

输出 `正常` / `观察` / `告警` 之一。**部署至此完成。**

---

# 四 · 接入（均为可选）

## 14. 定时任务

Windows：

```bash
python -m mprobe schedule install --model deepseek --tier monitor --cadence daily --at 09:00
python -m mprobe schedule status --json
```

第二条命令输出中 `"exists": true` 方为安装成功。未安装时显示：

```
mprobe_deepseek_small：不存在
  note = 系统里没有这个任务（哪怕配置里写着有）
```

Linux / macOS 使用 cron：

```cron
0 9 * * * cd /path/to/modelprobe && /usr/bin/python3 -m mprobe check \
          --model deepseek --tier monitor --yes >> /var/log/mprobe.log 2>&1
```

**`--yes` 不可省略**：无人在场时缺少该参数一律视为拒绝。
退出码 `1` 即告警信号。

## 15. 告警推送

`config/notify.json`，不存在则不推送且不报错：

```json
{
  "enabled": true,
  "webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/...",
  "policy": "abnormal"
}
```

`policy` 取 `abnormal` 时仅在观察／告警态推送。webhook 必须为 https。
显示时仅保留域名与前两段路径。推送失败不影响判定结果。

## 16. 浏览器界面

```bash
python -m mprobe web
```

```
界面已起：http://127.0.0.1:8790
只绑 127.0.0.1 —— 要远程看请用 ssh -L 8790:127.0.0.1:8790
Ctrl-C 停止
```

仅绑定回环地址，不提供 `--host` 参数。界面为只读，POST 一律返回 405。

## 17. MCP 与 Skill

```bash
python install.py            # 预演，不改文件
python install.py --apply    # 写入
```

写入五处用户级配置，均先备份并打印 diff；已存在且内容不同时不覆盖，
打印 `**不覆盖。**` 并跳过。安装后须新开会话才会被发现。

---

# 五 · 部署后核对

| 命令 | 通过判据 |
|---|---|
| `python tools/check_all.py` | `未通过 0` |
| `python tools/check_deps.py` | 零第三方依赖 |
| `python -m mprobe bank info` | `题库 1.1.0`、`253 题` |
| `python -m mprobe config list` | 目标端点密钥列为 `sk-…` |
| `python -m mprobe eval --model X --tier monitor --dry-run` | 不报「渠道不通」 |
| `python -m mprobe baseline --model X --tier monitor` | 输出 `告警阈值` |
| `python -m mprobe check --model X --tier monitor --dry-run` | 不报「选不出足够的题」 |
| `python -m mprobe schedule status --json` | `"exists": true` |
| `python tools/inventory.py` | 无报错 |

---

# 六 · 故障对照

| 现象 | 处置 |
|---|---|
| `题库校验失败，拒绝运行` | 以 `git clone` 重新获取，勿用 zip |
| `找不到端点配置` | `--model` 取 `config/models/` 下的文件名，不含扩展名 |
| 账单显示 `未知（价格未配置）` | 属正常。需要报价则补 `pricing` 段 |
| `渠道不通` 但端点可用 | 端点 json 增加 `"probe_timeout": 60` |
| 密钥已配置仍报未设置 | 以 `mprobe config key` 重新写入；使用环境变量时须新开终端 |
| `选不出足够的题` | 监控口径下该档位配额无法填满，改用 `--tier monitor` |
| 某维度得分全为 0 | 先排查截断，调大 `max_tokens` 后重新采集 |
| `这条序列还没有基线` | 执行第 12 步 |
| `没有可用于建基线的轮次` | 四项须全同：模型、端点指纹、题库版本、档位。以 `status --json` 逐项核对 |
| `run 不存在` | run_id 有误，以 `python -m mprobe status` 的输出为准 |
| `pip install -e .` 报 `BackendUnavailable` | 先 `pip install -r requirements-dev.txt` |
| 在仓库外执行 `mprobe` 报找不到 `banks/` | 须在仓库目录内执行 |
| 界面页面为空 | 核对启动日志中打印的实际端口 |
| 定时任务未执行 | 依次核对 `"exists": true`、命令含 `--yes`、密钥位于 `secrets.local.json` |

---

# 七 · 升级与回退

```bash
git fetch --tags
git tag
git checkout v<目标版本>
python tools/check_all.py
```

版本号即 `bank_rev`。**换版本后基线须重建**（第 9–12 步）。
`data/` 不纳入版本管理，回退不影响运行产物；但基线与 `bank_rev` 绑定，
二者不一致时判定会被拒绝。

判分器变更后可用存档重算，无需重新采样：

```bash
python tools/probe_analyze.py data/runs/<run_id> --write
```

**不要删除 `data/runs/*/raw.jsonl`。**

---

# 八 · 卸载

## 1. 预演

```bash
python install.py --uninstall
```

```
[1/4] MCP 配置
[2/4] Skill
  → 将删除 <用户目录>\.claude\skills\mprobe-eval（与仓库一致）
[3/4] 计划任务
[4/4] 仓库内的东西（本脚本**不动**，要删自己删）
```

仅删除本脚本写入且未被修改的内容。指向其他目录或内容已变更的条目
打印 `**不动它**` 并跳过。

## 2. 执行

```bash
python install.py --uninstall --apply
```

被修改的文件旁保留 `.bak` 备份。

## 3. 定时任务

```bash
python -m mprobe schedule remove --model deepseek --tier monitor
python -m mprobe schedule status --json
```

`"exists": false` 即已移除。

## 4. pip 包

```bash
pip uninstall mprobe
```

仅在执行过第 4 步时需要。

## 5. 删除仓库目录

将连带删除两项内容，确认后再执行：

| 一并删除 | 内容 |
|---|---|
| `data/` | 全部测评结果、基线、响应原文 |
| `config/secrets.local.json` | 密钥 |

需保留结果则先转移 `data/`。

## 6. 确认

```bash
python -m mprobe --help
```

报找不到模块即已清除。工具不在其他位置写入文件：无注册表项、
无服务、无临时目录缓存。
