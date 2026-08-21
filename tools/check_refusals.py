#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""实施计划第 2.7 项 —— 验收「该拒绝时要拒绝」。零请求、零花费。

## 为什么这关最重要

此类工具的事故几乎全部来自「该拒绝时照做了」，故拒绝能力需单独验收。

能拒绝的分两类，这里都要验：

  **机制上的拒绝**（工具自己就会拒）——可以自动测，这个脚本干这个
  **文本上的拒绝**（Skill 教模型别做）——只能检查说明书写了没有

## 机制上必须拒绝的

| 场景 | 期望 |
|---|---|
| 题库被改一个字节 | 拒绝运行，报出两个 sha256 |
| 跨 `bank_rev` 比较 | 抛异常，不是返回空 |
| 没有基线就判定 | 拒绝并给出建基线的命令 |
| 监控口径配额填不满 | 拒绝执行，不是警告 |
| `check.type = manual` 的题 | 加载期报错，指出题号 |
| 未登记的维度代号 | 加载期报错，指出题号 |
| 已退役的维度代号（`I`） | 报错并说明该用哪个替代 |
| MCP 花钱工具不带 confirm | 只报价，不产生 run 目录 |
| `--run-id` 带路径穿越 | 拒绝 |

用法：
    python tools/check_refusals.py
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

PY = sys.executable or "python"
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %s %s%s" % ("✓" if ok else "✗ 没拒绝！", name,
                         ("  —— " + detail) if detail else ""))


def cli(*argv):
    p = subprocess.run([PY, "-m", "mprobe"] + list(argv), cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


# --------------------------------------------------------------------------

def t_bank_tamper():
    """题库被改一个字节 → 拒绝运行。"""
    src = os.path.join(ROOT, "banks", "core.jsonl")
    bak = src + ".refusal-test"
    shutil.copy2(src, bak)
    try:
        with io.open(src, "a", encoding="utf-8") as f:
            f.write("\n")
        rc, out = cli("bank", "info")
        check("题库被改一个字节 → 拒绝运行",
              rc != 0 and ("拒绝运行" in out or "已被改动" in out),
              "退出码 %d" % rc)
    finally:
        shutil.move(bak, src)


def t_manual_item():
    """人工判分题 → 加载期报错。"""
    from mprobe.engine import bank
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "t.jsonl")
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps({"id": "X1", "dim": "D", "prompt": "x",
                            "check": {"type": "manual"}},
                           ensure_ascii=False) + "\n")
    try:
        bank.load_file(p)
        check("check.type=manual 的题 → 加载报错", False, "居然加载成功了")
    except bank.BankError as e:
        check("check.type=manual 的题 → 加载报错", "X1" in str(e),
              "报出了题号" if "X1" in str(e) else "没指出题号")


def t_unknown_dim():
    from mprobe.engine import bank
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "t.jsonl")
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps({"id": "Z9", "dim": "ZZ", "prompt": "x",
                            "check": {"type": "exact", "expect": "a"}},
                           ensure_ascii=False) + "\n")
    try:
        bank.load_file(p)
        check("未登记的维度代号 → 加载报错", False, "居然加载成功了")
    except bank.BankError as e:
        check("未登记的维度代号 → 加载报错", "Z9" in str(e))


def t_retired_dim():
    from mprobe.engine import dims
    try:
        dims.resolve("I")
        check("已退役的维度代号 I → 报错", False, "居然解析成功了")
    except dims.DimError as e:
        check("已退役的维度代号 I → 报错",
              "LG" in str(e) or "IF" in str(e), "并说明了该用哪个替代")


def t_cross_bank_rev():
    """跨 bank_rev 的比较 → 抛 BankRevMismatch，不是返回空。

    这个测试**自己曾经是假通过的**：早先它调的是一个不存在的函数名，
    `except Exception` 把 AttributeError 当成了「正确拒绝」。
    所以现在先断言接口存在，再断言它抛的是**那个特定异常**。
    一个只会通过的测试，什么都没验。
    """
    from mprobe.store import db
    from mprobe import paths
    if not hasattr(db, "compare_runs"):
        check("跨 bank_rev 比较 → 拒绝", False, "db.compare_runs 不存在")
        return
    if not hasattr(db, "BankRevMismatch"):
        check("跨 bank_rev 比较 → 拒绝", False, "没有 BankRevMismatch 异常类")
        return
    if not os.path.isfile(paths.DB):
        check("跨 bank_rev 比较 → 拒绝", False, "没有结果库，测不了")
        return
    con = db.connect(paths.DB)
    pairs = con.execute(
        "select a.run_id, b.run_id from runs a, runs b "
        "where a.bank_rev <> b.bank_rev limit 1").fetchall()
    if not pairs:
        check("跨 bank_rev 比较 → 拒绝", False,
              "库里找不到两个不同 bank_rev 的轮次，这一条**没被验到**")
        return
    a, b = pairs[0]
    try:
        db.compare_runs(con, a, b)
        check("跨 bank_rev 比较 → 拒绝", False, "居然没抛异常")
    except db.BankRevMismatch:
        check("跨 bank_rev 比较 → 拒绝", True, "抛了 BankRevMismatch")
    except Exception as e:
        check("跨 bank_rev 比较 → 拒绝", False,
              "抛的是 %s，不是 BankRevMismatch" % type(e).__name__)


def t_check_without_baseline():
    """没有基线 → judge 返回 UNKNOWN 并说要先建基线。

    直接测判定层，不靠命令行输出里有没有某个词 ——
    早先那版断言弱到永远会过。
    """
    from mprobe.monitor import judge as j
    state, msg, _ = j.judge(None, 80.0, [])
    check("没有基线 → 判定层返回 UNKNOWN",
          state == j.UNKNOWN and "基线" in msg, "state=%s" % state)
    # 有效采样太少也必须拒绝判定（铁律三：单次不作数）
    fake_bl = {"mean": 80.0, "sigma": 4.0, "threshold": 72.0,
               "rounds": 5, "provisional": False}
    state2, msg2, _ = j.judge(fake_bl, 80.0, [], n_trials=1)
    check("有效采样只有 1 次 → 拒绝判定",
          state2 == j.UNKNOWN, "state=%s（%s）" % (state2, msg2[:30]))


def t_quota_shortfall():
    """监控口径配额填不满 → 拒绝执行，不是警告。"""
    rc, out = cli("check", "--model", "deepseek", "--tier", "small",
                  "--dry-run")
    check("监控口径配额填不满 → 拒绝",
          rc != 0 and "选不出足够的题" in out, "退出码 %d" % rc)


def t_run_id_traversal():
    from mprobe import cli as climod
    try:
        climod._run_id({"key": "x"}, "monitor", "eval", "../evil")
        check("run-id 路径穿越 → 拒绝", False, "居然接受了")
    except SystemExit as e:
        check("run-id 路径穿越 → 拒绝", e.code == 2)


def t_mcp_money_gate():
    """MCP 花钱工具不带 confirm → 只报价，不产生 run 目录。"""
    from mprobe import paths
    before = set(os.listdir(paths.RUNS)) if os.path.isdir(paths.RUNS) else set()
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "eval_start",
                    "arguments": {"model": "deepseek", "tier": "monitor"}}},
    ]
    inp = "\n".join(json.dumps(m, ensure_ascii=False) for m in msgs) + "\n"
    p = subprocess.run([PY, "-m", "mprobe.mcp.server"], cwd=ROOT, input=inp,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    after = set(os.listdir(paths.RUNS)) if os.path.isdir(paths.RUNS) else set()
    said = False
    for line in (p.stdout or "").splitlines():
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if o.get("id") == 2:
            txt = ((o.get("result") or {}).get("content") or [{}])[0].get(
                "text", "")
            said = "还没有花钱" in txt
    check("MCP 不带 confirm → 只报价、不开跑",
          said and not (after - before),
          "新增 run 目录 %d 个" % len(after - before))


def t_skill_negatives():
    """文本上的拒绝：三份 SKILL.md 都要写「不要触发」，且要覆盖关键反例。"""
    import glob
    need = {
        "哪个模型比较好": "选型排名",
        "优化": "提示词优化",
        "变笨了": "一句感慨不是指令",
    }
    # 断言的是**性质**（存在一个讲反例的小节），不是某个措辞。
    #
    # 早先这里写死了「不要触发」四个字，结果文档改名为「不予触发的表述」
    # 之后三份 Skill 全部报错 —— 而反例段一直都在。
    # 一个断言措辞的检查会逼着人为了过检查而不敢改文案。
    import re
    heading = re.compile(r"^#{1,3}\s*.*(不要|不予|禁止).*触发", re.M)
    for f in sorted(glob.glob(os.path.join(ROOT, "skills", "*", "SKILL.md"))):
        name = os.path.basename(os.path.dirname(f))
        t = io.open(f, encoding="utf-8").read()
        check("%s 有反例段（不予触发）" % name, bool(heading.search(t)))
    allt = "\n".join(io.open(f, encoding="utf-8").read()
                     for f in glob.glob(os.path.join(ROOT, "skills", "*",
                                                     "SKILL.md")))
    for k, why in need.items():
        check("反例覆盖：%s（%s）" % (k, why), k in allt)
    check("反例覆盖：拒绝往题库加题",
          "加进题库" in allt and "拒绝" in allt)


def main():
    print("实施计划第 2.7 项 —— 该拒绝时要拒绝\n")
    print("[机制上的拒绝]")
    for fn in (t_bank_tamper, t_manual_item, t_unknown_dim, t_retired_dim,
               t_cross_bank_rev, t_check_without_baseline, t_quota_shortfall,
               t_run_id_traversal, t_mcp_money_gate):
        try:
            fn()
        except Exception as e:
            check(fn.__name__, False, "测试自身报错 %s: %s"
                  % (type(e).__name__, e))
    print("\n[文本上的拒绝（Skill 说明书）]")
    t_skill_negatives()

    print("\n通过 %d ／ 未通过 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("未通过：")
        for x in FAIL:
            print("  - %s" % x)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
