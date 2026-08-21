# -*- coding: utf-8 -*-
"""stdio JSON-RPC 的 MCP 服务器。手写，不引 SDK。

## 这一层做什么、不做什么

**做**：拼命令行、起子进程、把 CLI 的 `--json` 输出转手给宿主。
**不做**：任何判分、算分、聚合、判定。一行都不做。

`DESIGN.md` 第一章：MCP 工具和 Skill 如果各自实现一遍打分，
迟早出现「两个界面给出不同的分」。本项目曾发生
「同一个判分器 bug 存在于两处、只修了一处」的亏。

## 为什么 eval 要拆成 start + status

MCP 工具调用是**阻塞**的：工具返回之前，模型什么都看不到。
而一轮全量测评是几小时量级。所以：

    eval_start   → 立刻返回 run_id，真正的跑放进独立子进程
    eval_status  → 读 data/runs/<id>/progress.json

会话里做不到逐帧刷新，这是协议决定的，不假装能做到。
进度条真正的价值不是好看，是**让人知道它没死** —— 所以
`eval_status` 会把 `label`（当前题号）和 stalled 状态一起给出来。

`run_id` 由**本层生成并用 `--run-id` 传下去**：run 目录是子进程建的，
靠猜时间戳会有竞态。

## 花钱的闸门在哪

`eval_start` 默认 **dry_run=true**，只出报价不跑。要真跑必须显式
`confirm=true`。这是 实施计划第 2.7 项 那条「该拒绝时要拒绝」的落点：
对话里一句「帮我测一下」不应该直接开始烧钱。
"""

import json
import os
import subprocess
import sys
import time

from .. import BANK_REV, __version__, paths

PROTOCOL = "2024-11-05"
SERVER = {"name": "mprobe", "version": __version__}

#: 子进程用同一个解释器，避免宿主的 PATH 里是另一个 Python。
PY = sys.executable or "python"

#: 只读命令的超时。这些命令不发网络请求，几秒就该回来。
READ_TIMEOUT = 60


def _root():
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))


def _run_cli(argv, timeout=READ_TIMEOUT):
    """调一次 CLI，返回 (退出码, 解析后的 JSON 或 None, 原始输出)。

    一律加 `--json`。CLI 的退出码有语义（0 正常 / 1 告警 / 2 用法错 /
    3 结果不可用），**要原样带回去** —— 把告警压成"成功"是这类工具
    最不该犯的错。
    """
    cmd = [PY, "-m", "mprobe"] + list(argv) + ["--json"]
    try:
        p = subprocess.run(cmd, cwd=_root(), capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, None, "命令超过 %d 秒未返回" % timeout
    out = (p.stdout or "").strip()
    data = None
    if out:
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            data = None
    return p.returncode, data, out or (p.stderr or "").strip()


def _spawn_cli(argv, log_path):
    """起一个**脱离**的子进程跑长任务，立刻返回。

    stdout/stderr 落到 log_path —— 不能丢：连通闸门失败时
    错误只在那里，而那种情况下不会产生 run 目录，`eval_status`
    就只能靠这个日志回答"为什么没跑起来"。
    """
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    f = open(log_path, "w", encoding="utf-8")
    kw = {}
    if os.name == "nt":
        # 不继承控制台，否则宿主退出时会把子进程一起带走
        kw["creationflags"] = 0x00000008 | 0x00000200   # DETACHED | NEW_GROUP
    else:
        kw["start_new_session"] = True
    p = subprocess.Popen([PY, "-m", "mprobe"] + list(argv),
                         cwd=_root(), stdout=f, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, **kw)
    return p.pid


# --------------------------------------------------------------------------
# 工具定义
# --------------------------------------------------------------------------

def _tools():
    return [
        {
            "name": "list_models",
            "description": (
                "列出已配置的模型端点：名称、实际模型、单价、是否配了密钥。"
                "不发任何请求、不花钱。想知道「能测哪些模型」时先调这个。"),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "bank_info",
            "description": (
                "题库概览：bank_rev、题数、逐维题数、有多少题能进监控。"
                "不发请求、不花钱。回答「考什么题」「这个维度有几道题」。"),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "tiers",
            "description": (
                "各档位能下什么结论：题量、请求数、最小可检出退化、"
                "以及**这一档允许说什么话／禁止说什么话**。"
                "不发请求、不花钱。跑之前想清楚「跑完能得到什么」时调这个。"),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "eval_start",
            "description": (
                "测评一个模型。**默认只报价不执行**（dry_run=true）："
                "返回这一轮要花多少钱、跑多久、跑完能下什么结论。"
                "确认要花钱之后再带 confirm=true 调一次，才会真的开跑，"
                "并立刻返回 run_id —— 用 eval_status 轮询进度，不要等它。"),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "model": {"type": "string",
                              "description": "端点 key，见 list_models"},
                    "tier": {"type": "string",
                             "description": "档位：monitor/small/medium/large/probe",
                             "default": "small"},
                    "confirm": {"type": "boolean", "default": False,
                                "description": "true 才真的花钱执行"},
                    "dim": {"type": "string",
                            "description": "只跑这些维度，逗号分隔（可选）"},
                    "out_tokens": {"type": "integer",
                                   "description": "估算用的每次输出 token；"
                                                  "推理模型要调大（可选）"},
                },
                "required": ["model"],
            },
        },
        {
            "name": "eval_status",
            "description": (
                "查一轮测评的进度。给 run_id 就查那一轮，不给就列最近几轮。"
                "只读，不花钱。跑了几小时的任务靠这个确认它没死。\n"
                "返回结构是 {progress:{...}, summary:...} —— **进度字段在 "
                "progress 里**，不在顶层：state / done / total / label / "
                "eta_sec / elapsed_sec。state 为 stalled 表示超过 90 秒"
                "没更新，那是可能死了，不是慢。\n"
                "注意 progress.cost_so_far 在跑动中是 0，费用要等 summary "
                "出来才有（这是已知限制）。"),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                },
            },
        },
        {
            "name": "check",
            "description": (
                "降智判定：拿当前表现和基线比，给出正常／观察／告警。"
                "**默认只报价不执行**，同 eval_start。"
                "没有基线时会拒绝判定并告诉你怎么建 —— 那是正确行为，不是故障。"),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "model": {"type": "string"},
                    "tier": {"type": "string", "default": "monitor"},
                    "confirm": {"type": "boolean", "default": False},
                },
                "required": ["model"],
            },
        },
        {
            "name": "baseline",
            "description": (
                "查看或建立基线。**两个动作都不花钱**：build 用的是已有的"
                "历史轮次，不发新请求。轮数不够时它会说不够，"
                "那时才需要先用 eval_start 补轮次。"
                "基线是判定的锚点：题库版本或端点配置一变就作废。"),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "model": {"type": "string"},
                    "tier": {"type": "string", "default": "monitor"},
                    "action": {"type": "string", "enum": ["show", "build"],
                               "default": "show"},
                },
                "required": ["model"],
            },
        },
        {
            "name": "compare",
            "description": (
                "对比两轮结果。只读、不花钱。"
                "注意：不同 bank_rev 的两轮**会被拒绝比较**，不是警告。"),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "run_a": {"type": "string"},
                    "run_b": {"type": "string"},
                },
                "required": ["run_a", "run_b"],
            },
        },
    ]


# --------------------------------------------------------------------------
# 工具实现：每一个都只是 CLI 的转手
# --------------------------------------------------------------------------

def _t_list_models(_a):
    return _run_cli(["config", "list"])


def _t_bank_info(_a):
    return _run_cli(["bank", "info"])


def _t_tiers(_a):
    return _run_cli(["tiers"])


def _money_argv(a, base):
    argv = list(base)
    if a.get("tier"):
        argv += ["--tier", str(a["tier"])]
    if a.get("dim"):
        argv += ["--dim", str(a["dim"])]
    if a.get("out_tokens"):
        argv += ["--out-tokens", str(int(a["out_tokens"]))]
    return argv


def _t_eval_start(a):
    model = a.get("model")
    if not model:
        return 2, {"error": "必须给 model"}, ""
    argv = _money_argv(a, ["eval", "--model", str(model)])

    if not a.get("confirm"):
        # 报价路径：同步跑 --dry-run，几秒就回来
        rc, data, raw = _run_cli(argv + ["--dry-run"])
        if isinstance(data, dict):
            data["_next"] = ("这只是报价，**还没有花钱**。确认要跑就再调一次"
                            "eval_start 并带 confirm=true。")
        return rc, data, raw

    # 执行路径：本层定 run_id，子进程脱离运行
    rid = "%s-%s-eval-%s" % (model, a.get("tier") or "small",
                             time.strftime("%Y%m%d-%H%M%S"))
    log = os.path.join(paths.RUNS, "_mcp", rid + ".log")
    pid = _spawn_cli(argv + ["--run-id", rid, "--yes"], log)
    return 0, {
        "started": True, "run_id": rid, "pid": pid, "log": log,
        "note": ("已在后台开跑。用 eval_status 带这个 run_id 轮询；"
                 "**不要等它返回** —— 全量档是几小时量级。"),
    }, ""


def _t_eval_status(a):
    rid = a.get("run_id")
    if rid:
        rc, data, raw = _run_cli(["status", "--run", str(rid)])
        if rc != 0 or not data:
            # run 目录还没建起来（连通闸门可能刚失败），去日志里找原因
            log = os.path.join(paths.RUNS, "_mcp", str(rid) + ".log")
            if os.path.isfile(log):
                with open(log, "r", encoding="utf-8", errors="replace") as f:
                    tail = f.read()[-1500:]
                return rc, {"run_id": rid, "state": "unknown",
                            "hint": "还没有 progress.json，下面是子进程日志尾部",
                            "log_tail": tail}, raw
        return rc, data, raw
    return _run_cli(["status"])


def _t_check(a):
    model = a.get("model")
    if not model:
        return 2, {"error": "必须给 model"}, ""
    argv = ["check", "--model", str(model),
            "--tier", str(a.get("tier") or "monitor")]
    if not a.get("confirm"):
        rc, data, raw = _run_cli(argv + ["--dry-run"])
        if isinstance(data, dict):
            data["_next"] = ("这只是报价，**还没有花钱**，也还没有判定。"
                            "确认就带 confirm=true 再调一次。")
        return rc, data, raw
    rid = "%s-%s-check-%s" % (model, a.get("tier") or "monitor",
                              time.strftime("%Y%m%d-%H%M%S"))
    log = os.path.join(paths.RUNS, "_mcp", rid + ".log")
    pid = _spawn_cli(argv + ["--run-id", rid, "--yes"], log)
    return 0, {"started": True, "run_id": rid, "pid": pid, "log": log,
               "note": "判定结果要等这一轮跑完，用 eval_status 轮询。"}, ""


def _t_baseline(a):
    """查看或建立基线。

    **两个动作都不花钱。** `--build` 用的是**已有的历史轮次**，
    不发新请求 —— 所以没有 dry_run／confirm 这一套。
    轮数不够时它会说轮数不够，那时才需要先跑 eval_start 补轮次。
    """
    model = a.get("model")
    if not model:
        return 2, {"error": "必须给 model"}, ""
    tier = str(a.get("tier") or "monitor")
    argv = ["baseline", "--model", str(model), "--tier", tier]
    if (a.get("action") or "show") == "build":
        argv.append("--build")
    return _run_cli(argv)


def _t_compare(a):
    if not a.get("run_a") or not a.get("run_b"):
        return 2, {"error": "必须给 run_a 和 run_b"}, ""
    return _run_cli(["compare", str(a["run_a"]), str(a["run_b"])])


HANDLERS = {
    "list_models": _t_list_models,
    "bank_info": _t_bank_info,
    "tiers": _t_tiers,
    "eval_start": _t_eval_start,
    "eval_status": _t_eval_status,
    "check": _t_check,
    "baseline": _t_baseline,
    "compare": _t_compare,
}


# --------------------------------------------------------------------------
# JSON-RPC
# --------------------------------------------------------------------------

def _result(payload, is_error=False):
    return {
        "content": [{"type": "text",
                     "text": json.dumps(payload, ensure_ascii=False,
                                        indent=2, default=str)}],
        "isError": bool(is_error),
    }


def _call(name, args):
    fn = HANDLERS.get(name)
    if fn is None:
        return _result({"error": "未知工具 %r。可用：%s"
                        % (name, "、".join(sorted(HANDLERS)))}, True)
    try:
        rc, data, raw = fn(args or {})
    except Exception as e:                       # 工具异常不该弄死服务器
        return _result({"error": "%s: %s" % (type(e).__name__, e)}, True)

    payload = data if data is not None else {"output": raw}
    if isinstance(payload, dict):
        # 退出码原样带回。CLI 的 1 是「告警」而不是「失败」，
        # 压成 isError 会让宿主把一次真实告警当成工具坏了。
        payload = dict(payload)
        payload["_exit_code"] = rc
        payload["_exit_meaning"] = {
            0: "正常（含观察态）", 1: "告警", 2: "用法或配置错误",
            3: "结果不可用", 124: "超时",
        }.get(rc, "未知")
    return _result(payload, is_error=(rc in (2, 124)))


def handle(req):
    """处理一条 JSON-RPC 请求。通知（无 id）返回 None。"""
    method = req.get("method")
    rid = req.get("id")
    if method == "initialize":
        res = {"protocolVersion": PROTOCOL,
               "capabilities": {"tools": {}},
               "serverInfo": SERVER,
               "instructions": (
                   "这是模型能力观测工具 mprobe 的门面。题库版本 %s。\n"
                   "花钱的工具（eval_start / check / baseline build）"
                   "默认只报价不执行，要带 confirm=true 才真跑。\n"
                   "题库随版本冻结，**不能增删题目** —— 那会让此前所有"
                   "结果不可比。" % BANK_REV)}
    elif method in ("notifications/initialized", "initialized"):
        return None
    elif method == "tools/list":
        res = {"tools": _tools()}
    elif method == "tools/call":
        p = req.get("params") or {}
        res = _call(p.get("name"), p.get("arguments"))
    elif method == "ping":
        res = {}
    else:
        if rid is None:
            return None
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": "未实现的方法 %s" % method}}
    if rid is None:
        return None
    return {"jsonrpc": "2.0", "id": rid, "result": res}


def main():
    """stdio 主循环：一行一个 JSON-RPC 消息。"""
    # stdout 只能有协议消息。任何调试输出都必须走 stderr，
    # 否则会污染 JSON-RPC 流，宿主直接断连。
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            sys.stderr.write("收到非法 JSON：%s\n" % e)
            continue
        try:
            res = handle(req)
        except Exception as e:
            sys.stderr.write("处理失败：%s\n" % e)
            res = {"jsonrpc": "2.0", "id": req.get("id"),
                   "error": {"code": -32603, "message": str(e)}}
        if res is not None:
            sys.stdout.write(json.dumps(res, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
