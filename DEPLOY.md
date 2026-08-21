# 部署手册

> 从上往下一条一条敲。每步都给了**敲完应该看到什么**。
> 需要 Python ≥ 3.9，不需要装任何第三方包。
> 装完之后日常怎么用，见 [`GUIDE.md`](GUIDE.md)。

---

# 第一部分 · 装

## 1. 取代码

```bash
git clone https://github.com/CJH0220/modelprobe.git
cd modelprobe
git checkout v1.1.0
```

第三行不要省。查有哪些版本用 `git tag`。

## 2. 自检

```bash
python tools/check_all.py
```

看最后两行：

```
============================================================
总计 43 项，通过 43，未通过 0
```

**`未通过` 必须是 0**，不是 0 就停下，别往下走。

中间会有一条 `⚠ 未验证`，刚装完出现这条是正常的：

```
  ⚠ 未验证 跨 bank_rev 比较 → 拒绝  —— 本机还没有结果库（全新 clone 时属正常，跑过一轮后即可验）
  通过 16 ／ 未通过 0 ／ 未验证 1
```

## 3. 确认能跑

```bash
python -m mprobe bank info
```

```
== 题库 1.1.0 ==
冻结于 2026-08-21 15:07:37 ｜ 253 题 ｜ 监控池 224 题（其中轮间 SD 已实测 35 题）
```

看到 `题库 1.1.0` 和 `253 题` 就对了。

> 报 `题库校验失败，拒绝运行` 的话，用 `git clone` 重新取一次，不要用 zip 解压。

## 4.（可选）装成系统命令

```bash
pip install -r requirements-dev.txt
pip install -e .
mprobe --help
```

装完 `mprobe` 等于 `python -m mprobe`，但**仍然要在仓库目录里执行**。
不装也能用，跳过这步没有任何影响。

---

# 第二部分 · 配

## 5. 看现有端点

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

`密钥` 那列写 `未设置：XXX` 就是缺密钥。

## 6. 填密钥

```bash
python -m mprobe config key --model deepseek
```

输入时不回显。存进 `config/secrets.local.json`，这个文件不进版本管理。

再跑一次第 5 步，密钥那列变成 `sk-…xxxx（file）` 就成了：

```
| deepseek | deepseek-v4-flash | c1079e441bbb | sk-…a278（file） | ✓ |
```

**不用关终端重开，计划任务也读得到。**

> 也可以直接编辑 `config/secrets.local.json`：
> `{ "DEEPSEEK_API_KEY": "你的密钥" }`
>
> 想用环境变量也行（那列会显示 `（env）`），但要新开终端才生效。
> 同名时文件优先。

## 7. 加自己的端点

```bash
cp config/models/_template.json config/models/myapi.json
```

打开 `config/models/myapi.json`，填这几项：

| 要填的 | 填什么 |
|---|---|
| `model.base_url` | 端点地址 |
| `model.model` | 模型名 |
| `model.api_style` | `openai` 或 `anthropic` |
| `model.api_key_env` | 密钥的名字，例如 `MYAPI_API_KEY` |
| `model.max_tokens` | 先填 32000 |
| `run.qps` | 端点有限速就填，没有就删掉这行 |
| `pricing` | **可选**。不填就不报价，其余照常 |

**这个文件里不要写密钥。** 填完跑第 5 步确认它出现在表里，
文件名就是以后 `--model` 用的名字（`myapi.json` → `--model myapi`）。
然后回第 6 步填密钥。

---

# 第三部分 · 跑

下面把 `deepseek` 换成你自己的端点名。

## 8. 先看要花多少钱

```bash
python -m mprobe eval --model deepseek --tier monitor --dry-run
```

```
== 本轮计划 ==
模型 deepseek（c1079e441bbb）· 档位 monitor · 题库 1.1.0
最小可检出退化 8.6 分 ｜ 95% 区间半宽 ±8.4 分
预估花费 0.19 CNY ｜ 预估耗时 4 分钟
119 题 x 1 次 = 119 次请求，并发 4，输入约 20,753 token、输出约 83,300 token
```

`--dry-run` 不发请求、不花钱。

> **耗时这个数第一次会偏低。** 它靠本机跑过的历史数据推算，
> 一个档位没跑过就只能按理论算。`monitor` 档实测约 13 分钟，
> 跑过一轮后估值就准了。花费那个数不受影响。

想看别的档位有多大多贵：

```bash
python -m mprobe tiers
```

## 9. 跑第一轮

```bash
python -m mprobe eval --model deepseek --tier monitor --yes
```

跑完打开 `data/runs/<run_id>/report.md`，看两处：

```
- 请求成功 119/119，失败率 0.0%
```

```
| 输出被截断 | 0（其中 0 次完全没有可见内容） |
```

失败率高的这轮不要用；截断不是 0 就去做第 10 步。

## 10. 如果有截断

打开 `config/models/<端点>.json`，把 `max_tokens` 调大（32000 → 64000），
然后重跑第 9 步，直到 `截断 0`。

**这一步必须在建基线之前做完。** 改完 `max_tokens` 之前跑的轮次不能再用。

## 11. 再跑四轮

```bash
python -m mprobe eval --model deepseek --tier monitor --yes
python -m mprobe eval --model deepseek --tier monitor --yes
python -m mprobe eval --model deepseek --tier monitor --yes
python -m mprobe eval --model deepseek --tier monitor --yes
```

跑完确认五轮的配置一致：

```bash
python -m mprobe status
```

```
== 最近 15 轮 ==
| run_id | 类型 | 模型 | 档 | 分数 | 请求 | 失败 | 健康 |
|---|---|---|---|---:|---:|---:|---|
| deepseek-monitor-eval-20260821-095247 | eval | deepseek | monitor | 91.7 | 42 | 0 | ok |
| deepseek-monitor-eval-20260821-094924 | eval | deepseek | monitor | 90.7 | 42 | 0 | ok |
```

`健康` 都是 `ok`、`失败` 都是 0 就往下走。

## 12. 建基线

```bash
python -m mprobe baseline --build --model deepseek --tier monitor
```

不发请求、不花钱，用第 9、11 步已经跑好的数据算。看第二行：

```
基线均值 <你的分数> 分，σ = <你的 σ>（取自：…）
告警阈值 = 均值 − 2σ = **<你的阈值> 分**
建自 5 轮，每轮 116 次请求
```

出现 `告警阈值` 那行就成了。

> 多出一行 `**这是临时基线**` 说明轮数还不够五轮，补够会自动转正。

## 13. 做一次判定

```bash
python -m mprobe check --model deepseek --tier monitor --yes
```

输出是 `正常` / `观察` / `告警` 三种之一。**到这里部署就算完成了。**

---

# 第四部分 · 挂上（都可选）

## 14. 每天自动跑

Windows：

```bash
python -m mprobe schedule install --model deepseek --tier monitor --cadence daily --at 09:00
python -m mprobe schedule status --json
```

第二条的输出里 `"exists": true` 才算装上。没装上会显示：

```
mprobe_deepseek_small：不存在
  note = 系统里没有这个任务（哪怕配置里写着有）
```

Linux / macOS 用 cron：

```cron
0 9 * * * cd /path/to/modelprobe && /usr/bin/python3 -m mprobe check \
          --model deepseek --tier monitor --yes >> /var/log/mprobe.log 2>&1
```

**`--yes` 不能省。** 退出码 `1` 就是告警。

## 15. 告警推送到群

新建 `config/notify.json`：

```json
{
  "enabled": true,
  "webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/你的地址",
  "policy": "abnormal"
}
```

`policy` 填 `abnormal` 就只在观察／告警时推。webhook 必须是 `https`。
不建这个文件就不推送，也不报错。

## 16. 浏览器界面

```bash
python -m mprobe web
```

```
界面已起：http://127.0.0.1:8790
只绑 127.0.0.1 —— 要远程看请用 ssh -L 8790:127.0.0.1:8790
Ctrl-C 停止
```

浏览器打开那个地址。界面只能看，不能改东西。

## 17. 装进 Claude

```bash
python install.py
```

先只打印将要改什么，不动文件：

```
mprobe 安装 —— 根目录 <仓库目录>
模式：预演（不改任何文件）

[1/4] 自检
  ✓ Python 3.14.7
  ✓ 目录结构完整
  ✓ data/ 可写
  ✓ 题库 1.1.0，253 道题
```

看着没问题再真写：

```bash
python install.py --apply
```

装完**新开一个 Claude 会话**，直接说「测一下 deepseek 什么水平」。

> 打印 `**不覆盖。**` 说明那一项已经存在且内容不同，需要手动处理。

---

# 第五部分 · 装完核对

一条一条跑，对上右边就行：

| 跑什么 | 要看到 |
|---|---|
| `python tools/check_all.py` | `未通过 0` |
| `python tools/check_deps.py` | `零第三方依赖` |
| `python -m mprobe bank info` | `题库 1.1.0`、`253 题` |
| `python -m mprobe config list` | 你的端点，密钥列是 `sk-…` |
| `python -m mprobe eval --model X --tier monitor --dry-run` | 有花费和耗时，不报 `渠道不通` |
| `python -m mprobe baseline --model X --tier monitor` | `告警阈值 = 均值 − 2σ` |
| `python -m mprobe check --model X --tier monitor --dry-run` | 不报 `选不出足够的题` |
| `python -m mprobe schedule status --json` | `"exists": true` |
| `python tools/inventory.py` | 跑完无报错 |

---

# 第六部分 · 出错对照表

| 报什么 | 怎么办 |
|---|---|
| `题库校验失败，拒绝运行` | 用 `git clone` 重新取，别用 zip |
| `找不到端点配置` | `--model` 后面要写 `config/models/` 里的**文件名**，不带 `.json` |
| 账单显示 `未知（价格未配置）` | 正常。要看花费就在端点 json 里补 `pricing` 段 |
| `渠道不通` 但端点确实能用 | 端点 json 里加一行 `"probe_timeout": 60` |
| 密钥填了还说 `未设置` | 用 `mprobe config key --model X` 重填；若用的是环境变量，要新开终端 |
| `选不出足够的题` | 换 `--tier monitor` |
| 某个维度全是 0 分 | 看有没有截断，有就调大 `max_tokens` 重跑 |
| `这条序列还没有基线` | 还没建过。先做第 12 步 |
| `没有可用于建基线的轮次` | 那几轮和当前配置对不上。四项必须全同：模型、端点指纹、**题库版本**、档位。用 `python -m mprobe status --json` 逐项核对 |
| `run 不存在` | run_id 打错了，用 `python -m mprobe status` 抄 |
| `pip install -e .` 报 `BackendUnavailable` | 先 `pip install -r requirements-dev.txt` |
| 在别的目录敲 `mprobe` 报找不到 `banks/` | 回到仓库目录再敲 |
| 界面打开是空白 | 换个端口：先看启动时打印的地址对不对 |
| 定时任务没跑 | 查三个：`"exists": true`、命令里有 `--yes`、密钥是用户级 |

---

# 卸载

## 1. 先看要删什么（不动文件）

```bash
python install.py --uninstall
```

```
mprobe 卸载 —— 根目录 <仓库目录>
模式：预演（不改任何文件）

[1/4] MCP 配置
[2/4] Skill
  → 将删除 C:\Users\<你>\.claude\skills\mprobe-eval（与仓库一致）
[3/4] 计划任务
  = 没有已安装的计划任务
[4/4] 仓库内的东西（本脚本**不动**，要删自己删）
```

只删本脚本装的东西，且只在它没被改过时才删。
指向别的目录、或你自己改过的，会打印 `**不动它**` 并跳过。

## 2. 真删

```bash
python install.py --uninstall --apply
```

改动过的文件旁边会留 `.bak` 备份。

## 3. 删定时任务

上一步会把还在的任务列出来，照着敲：

```bash
python -m mprobe schedule remove --model deepseek --tier monitor
python -m mprobe schedule status --json
```

`exists` 变成 `false` 就删掉了。

## 4. 卸掉 pip 包（只有装过方式二才需要）

```bash
pip uninstall mprobe
```

## 5. 删仓库

直接删掉这个文件夹。**会连带删掉两样东西**，删之前想清楚：

| 一起消失的 | 里面是什么 |
|---|---|
| `data/` | 全部测评结果、基线、响应原文。删了只能重新花钱跑 |
| `config/secrets.local.json` | 你填的密钥 |

想留结果就先把 `data/` 拷走。

## 6. 确认干净了

```bash
python -m mprobe --help
```

报找不到模块就是删干净了。

**工具不往别处写文件**——除了上面列的这些，系统里没有残留：
没有注册表项、没有服务、没有临时目录里的缓存。

---

# 升级和回退

```bash
git fetch --tags
git tag                      # 看有哪些版本
git checkout v<新版本>
python tools/check_all.py
```

**换版本后基线要重建**，回第 9 到 12 步。
回退用 `git checkout v<旧版本>`，`data/` 里的东西不受影响。

> 不要删 `data/` 目录。
