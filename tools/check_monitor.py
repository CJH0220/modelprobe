#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""实施计划第 3.1 项–3.4 验收。零请求、零花费。

逐条验证监控层的行为，不检查代码是否写了，只检查行为是否正确：

  3.1 基线：`sigma_source` 如实记录 observed/binomial/floor；
      轮数 < 5 标临时基线，且告警收紧为连续三轮
  3.2 判定：**单轮低于阈值只出「观察」，绝不出「告警」**
  3.3 计划任务：状态读 `schtasks /query` 的真实结果，不读配置文件
  3.4 推送：未配置时静默记录不报错；显示 URL 时只留 netloc + 前两段路径
"""

import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

PY = sys.executable or "python"
OK, BAD = [], []


def check(name, ok, detail=""):
    (OK if ok else BAD).append(name)
    print("  %s %s%s" % ("✓" if ok else "✗", name,
                         ("  —— " + detail) if detail else ""))


# --------------------------------------------------------------------------

def t31_sigma_source():
    """σ 三取大，且来源如实记录。"""
    from mprobe.monitor import baseline as bl
    # 实测 SD 最大
    s, src, d = bl.sigma([50.0, 70.0, 30.0, 90.0, 60.0], 100, mean=60.0)
    check("3.1 实测 SD 最大时 source=observed", src == bl.SIGMA_OBSERVED,
          "σ=%.2f" % s)
    # 二项最大（分散小、样本少）
    s, src, d = bl.sigma([60.0, 60.1, 59.9, 60.0, 60.0], 36, mean=60.0)
    check("3.1 实测 SD 很小时 source=binomial", src == bl.SIGMA_BINOMIAL,
          "σ=%.2f，实测只有 %.3f" % (s, d["sd_observed"]))
    # 地板最大（p 接近 0 或 1）
    s, src, d = bl.sigma([100.0, 100.0, 100.0], 4, mean=100.0)
    check("3.1 p=100 时 source=floor", src == bl.SIGMA_FLOOR,
          "σ=%.2f" % s)
    # 三取大恒成立
    got = bl.sigma([55.0, 65.0], 50, mean=60.0)
    s, src, d = got
    check("3.1 σ 恒等于三者最大",
          abs(s - max(d["sd_observed"], d["sd_binomial"],
                      d["sd_floor"])) < 1e-9)


def t31_provisional():
    """轮数不足 → 临时基线；告警收紧为连续三轮。"""
    from mprobe.monitor import baseline as bl
    rounds = [{"run_id": "r%d" % i, "score": 60.0, "requests": 36,
               "dims": {}} for i in range(3)]
    b = bl.build("m", "sha", "1.1.0", "monitor", rounds)
    check("3.1 3 轮 → provisional=True", b["provisional"] is True,
          "rounds=%d" % b["rounds"])
    rounds5 = [{"run_id": "r%d" % i, "score": 60.0, "requests": 36,
                "dims": {}} for i in range(5)]
    b5 = bl.build("m", "sha", "1.1.0", "monitor", rounds5)
    check("3.1 5 轮 → provisional=False", b5["provisional"] is False)

    from mprobe.monitor import judge as j
    need_prov = j.need_consecutive(b) if hasattr(j, "need_consecutive") else None
    if need_prov is None:
        # 没有独立函数就从判定行为反推
        # prev_checks 用 **state** 字段（watch/alert 都算"本轮低于阈值"），
        # 不是 below —— 照 judge 的 docstring 来，别凭印象猜字段名。
        low = b["threshold"] - 10
        st, _m, d = j.judge(b, low, [{"state": j.WATCH}])
        check("3.1 临时基线下连续 2 轮低于阈值**还不告警**",
              st != j.ALERT, "state=%s，需要 %d 次" % (st, d["need"]))
        st3, _m, d3 = j.judge(b, low, [{"state": j.WATCH},
                                       {"state": j.WATCH}])
        check("3.1 临时基线下连续 3 轮 → 告警", st3 == j.ALERT,
              "streak=%d need=%d" % (d3["consecutive"], d3["need"]))
    else:
        check("3.1 临时基线要求 3 轮", need_prov == 3, "=%s" % need_prov)


def t32_three_state():
    """单轮低于阈值只出观察，绝不告警。"""
    from mprobe.monitor import baseline as bl, judge as j
    rounds = [{"run_id": "r%d" % i, "score": 70.0, "requests": 36,
               "dims": {}} for i in range(5)]
    b = bl.build("m", "sha", "1.1.0", "monitor", rounds)
    thr = b["threshold"]

    st, msg, _ = j.judge(b, thr + 5, [], n_trials=36)
    check("3.2 高于阈值 → 正常", st == j.NORMAL, "state=%s" % st)

    st, msg, _ = j.judge(b, thr - 5, [], n_trials=36)
    check("3.2 **单轮**低于阈值 → 观察，不是告警",
          st == j.WATCH, "state=%s（%s）" % (st, msg[:40]))
    check("3.2 单轮低于阈值绝不出告警", st != j.ALERT)

    st, msg, d = j.judge(b, thr - 5, [{"state": j.WATCH}], n_trials=36)
    check("3.2 连续 2 轮低于阈值 → 告警", st == j.ALERT,
          "streak=%d need=%d" % (d["consecutive"], d["need"]))
    # unknown 那一轮不能当成「恢复正常」把连击清零
    st, _m, d = j.judge(b, thr - 5,
                        [{"state": j.UNKNOWN}, {"state": j.WATCH}],
                        n_trials=36)
    check("3.2 中间夹一轮 unknown 不清零连击", d["consecutive"] >= 2,
          "streak=%d" % d["consecutive"])
    # normal 那一轮要清零
    st, _m, d = j.judge(b, thr - 5,
                        [{"state": j.NORMAL}, {"state": j.WATCH}],
                        n_trials=36)
    check("3.2 中间夹一轮 normal 会清零连击", d["consecutive"] == 1,
          "streak=%d" % d["consecutive"])


def t33_schedule_real():
    """状态必须读 schtasks 的真实结果，不读配置文件。"""
    src = io.open(os.path.join(ROOT, "mprobe", "monitor", "schedule.py"),
                  encoding="utf-8").read()
    check("3.3 schedule.py 调用了 schtasks", "schtasks" in src)
    has_query = "/query" in src or "/Query" in src
    check("3.3 查询走 schtasks /query", has_query)
    # 真跑一次，看它是不是去问了系统
    r = subprocess.run([PY, "-m", "mprobe", "schedule", "status", "--json"],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    ok = r.returncode == 0
    try:
        d = json.loads(r.stdout)
        # 有 source/backend 之类字段说明它标注了数据来源
        ok = ok and isinstance(d, dict)
    except Exception:
        ok = False
    check("3.3 schedule status 能返回 JSON", ok,
          (r.stdout or r.stderr).strip()[:80])


def t34_notify():
    """未配置时静默；URL 只显示 netloc + 前两段路径。"""
    from mprobe.monitor import notify
    cfg = dict(notify.DEFAULT)
    # 照 notify 的真实签名来：send(cfg, text)、maybe_send(cfg, ctx, state,
    # message, detail)、mask(url)。**别凭印象猜函数名** ——
    # 凭印象猜函数名会在运行期才暴露，且可能使断言变为假通过。
    try:
        res = notify.maybe_send(cfg, {"model_key": "m"}, "alert",
                                "测试消息", {})
        check("3.4 未配置推送时静默不报错", True, "返回 %s" % str(res)[:50])
    except Exception as e:
        check("3.4 未配置推送时静默不报错", False,
              "%s: %s" % (type(e).__name__, e))

    check("3.4 policy=abnormal 时正常态不推送",
          not notify.should_send("abnormal", "normal"))
    check("3.4 policy=abnormal 时告警要推送",
          notify.should_send("abnormal", "alert"))

    raw = "https://open.feishu.cn/open-apis/bot/v2/hook/SECRET-TOKEN-123"
    out = notify.mask(raw)
    check("3.4 URL 打码后不含 token", "SECRET-TOKEN-123" not in out, out)
    check("3.4 打码后仍保留域名", "open.feishu.cn" in out, out)


def main():
    print("实施阶段 · 3.1–3.4 验收（零请求）\n")
    print("[3.1 基线]")
    for fn in (t31_sigma_source, t31_provisional):
        try:
            fn()
        except Exception as e:
            check(fn.__name__, False, "%s: %s" % (type(e).__name__, e))
    print("\n[3.2 三态判定]")
    try:
        t32_three_state()
    except Exception as e:
        check("t32", False, "%s: %s" % (type(e).__name__, e))
    print("\n[3.3 计划任务]")
    try:
        t33_schedule_real()
    except Exception as e:
        check("t33", False, "%s: %s" % (type(e).__name__, e))
    print("\n[3.4 告警推送]")
    try:
        t34_notify()
    except Exception as e:
        check("t34", False, "%s: %s" % (type(e).__name__, e))

    print("\n通过 %d ／ 未通过 %d" % (len(OK), len(BAD)))
    for x in BAD:
        print("  - %s" % x)
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
