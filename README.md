# modelprobe

> **版本 1.0.0 · 首个正式版本。**
> 题库已定版（`bank_rev 1.0.0`，253 题）；MCP 服务器、三份 Claude Code Skill、
> 安装脚本、只读浏览器界面均已就位；监控闭环已在真实端点上完成
> 采集 → 建基线 → 判定的全流程验证。
>
> 实施计划第 3.7 项（浏览器内写操作）**定为不做**，界面保持只读，理由见
> 实施计划。
>
> 上手请读 [`GUIDE.md`](GUIDE.md)。代码或题库变更后执行
> `python tools/check_all.py`（九组自检，零请求零成本）。

## 项目定位

一个独立可安装的工具，将两类问题的测量能力封装为 MCP 工具与 Claude Code Skill，
并附一个本地只读界面：

| 问题 | 对应能力 |
|---|---|
| 这个 API 端点是什么水平 | 单次测评，不需要基线 |
| 这个端点是否发生了静默退化 | 相对判定，需要基线 |

本工具为完整重新实现。题库与代码随版本冻结，**不继承任何历史测评结果**——
首次使用须在本机采集基线轮次。

## 常用命令

零第三方依赖，Python ≥ 3.9 标准库即可运行（`requirements.txt` 故意为空）。

```bash
python install.py                                                  # 装 MCP 与 Skill（先预演）
python -m mprobe config list                                       # 端点与密钥状态
python -m mprobe tiers                                             # 各档位的结论许可
python -m mprobe eval --model deepseek --tier monitor --dry-run    # 事前报价，不发请求
python -m mprobe eval --model deepseek --tier monitor --yes        # 执行一轮
python -m mprobe baseline --build --model deepseek --tier monitor  # 建基线，零成本
python -m mprobe check --model deepseek --tier monitor --yes       # 降智判定
python -m mprobe status --run <run_id>                             # 单轮进度与结果
python -m mprobe compare <run_a> <run_b>                           # 对比两轮（只读）
python -m mprobe bank info                                         # 题库现状
python -m mprobe web                                               # 只读界面
python tools/check_all.py                                          # 回归自检，零成本
```

**退出码契约**：`0` 正常（含观察态）｜`1` 告警｜`2` 用法或配置错误｜`3` 结果不可用。
计划任务据此判定告警，故该码必须原样传递至 shell。

密钥不写入任何配置文件，只走用户级环境变量或 `config/secrets.local.json`：

```powershell
[Environment]::SetEnvironmentVariable("DEEPSEEK_API_KEY","sk-xxx","User")
```

用户级变量仅对新开终端生效。

## 档位契约

deepseek 实测值。耗时取本机同档历史实测中位数。

| 档位 | 题数 | 计分请求 | 最小可检出退化 | 费用 | 耗时 |
|---|---:|---:|---:|---:|---:|
| `monitor` | 14 | 36 | 15.4 分 | 0.06 元 | 5 分 |
| `small` | 20 | 54 | 12.6 分 | 0.19 元 | 16 分 |
| `medium` | 50 | 144 | 7.7 分 | 0.48 元 | 39 分 |
| `large` | 120 | 354 | 4.9 分 | 1.16 元 | 1.6 时 |
| `probe` | 253 | 678 | 3.6 分 | 2.82 元 | 2.0 时 |

**计分请求数与总请求数不同**：冒烟维与观测题不计入总分，
故不计入 σ 的样本量。按总请求数计算会高估分辨率。

## 文档

| 文档 | 内容 | 读者 |
|---|---|---|
| [`DEPLOY.md`](DEPLOY.md) | **部署说明**：环境要求、安装、配置、投入运行、升级回滚、部署后核对清单 | 部署者 |
| [`GUIDE.md`](GUIDE.md) | **操作指南**：10 步完整流程、日常三条命令、五个档位、排错表 | 使用者 |
| [`BANK_AND_METRICS.md`](BANK_AND_METRICS.md) | **考什么题、分怎么算**：253 题的构成与逐维盘点、判分三级、总分公式、σ 与档位、两道准入闸门 | 需要判断数值可信度者 |
| [`MEASUREMENT.md`](MEASUREMENT.md) | **口径论证**：档位的统计含义、题库冻结、判分规则、结论许可的推导 | 同上 |
| [`DESIGN.md`](DESIGN.md) | 架构、MCP 与 Skill 的分工、进度机制、结果库、触发路由 | 开发者 |
| [`CHANGELOG.md`](CHANGELOG.md) | 变更记录。版本号即 `bank_rev`，任何版本变化都意味着此前结果不可直接比较 | 全部 |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | 四条硬约束、跨模块取值规范、测试编写规范。每条均对应一次已发生的事故 | 开发者 |

> `MEASUREMENT.md` 论证「为何如此定义」，`BANK_AND_METRICS.md` 陈述「实际定成什么」。
> 二者数值冲突时以后者为准——它直接取自 `banks/MANIFEST.json`。

## 三条设计约束

1. **CLI 是唯一实现。** MCP 服务器、Skill、浏览器界面均为其门面，不含测量逻辑。
   曾发生「同一判分器缺陷存在于两处、仅修复其一」的事故，
   故测量逻辑只保留一处。
2. **题库随版本冻结。** 用户端不可增删题目。变更题库等同发布新版本，
   既有结果随之不可比——该约束由工具**拒绝比较**来保证，不依赖使用者记忆。
3. **每个结论自带边界。** 档位报告「正常」时，输出必须写为
   「未发现 X 分以上的退化」，不得写为「模型无问题」。

## 代码结构

```
mprobe/
  cli.py          唯一实现。十个子命令，全部支持 --json
  estimate.py     事前估算：输入 token 为实测统计，输出 token 为假设值，分开报告
  tiers.py        五档定义与「本档允许下的结论」
  profiles.py     档位配额选题（确定性，不随机）
  engine/         题库、22 个判分器、客户端、指标、聚合、成本、报告
  monitor/        baseline / judge / notify / schedule
  store/db.py     SQLite 四表，bank_rev 强制约束
  mcp/server.py   stdio JSON-RPC，八个工具，零测量逻辑
  web/            只读界面，仅绑 127.0.0.1
banks/            题库 + MANIFEST.json（sha256 冻结）+ 退役题 + 实测台账
profiles/         monitor / small / medium / large / probe
config/           端点定义与价格表。**密钥不在此处**
skills/           三份 SKILL.md
tools/            离线工具与自检，全部零请求
install.py        装 MCP 配置 + 同步 Skill：幂等、备份、打印 diff、不覆盖
data/             运行产物（已 gitignore）
```

## 题库与实现的来源

| 项 | 说明 |
|---|---|
| 引擎设计 | 题库加载、判分器、客户端、指标、聚合、成本、报告，在本项目中重新实现 |
| 监控实现 | 基线、阈值、三态判定、告警推送、计划任务 |
| 公开基准题源 | 验证集 210 道公开能力题，清理后并入 176 道 |
| 跨模型探针数据 | 跨模型极差的信号源，逐题结果记于 `banks/MANIFEST.json` |
| **不继承** | 任何历史测评结果与基线 |

> 不继承历史结果意味着**基线必须在本机重新建立**，
> 见 [`GUIDE.md`](GUIDE.md) 第 4–6 步。这不是限制：
> 基线与端点指纹（模型、地址、温度、`max_tokens`）绑定，
> 换一台机器或换一个网关，他人的基线本就不适用。
