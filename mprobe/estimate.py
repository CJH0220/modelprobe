# -*- coding: utf-8 -*-
"""事前估算：这一轮要花多少钱、要跑多久。

存在的理由很具体：opus 的大档一轮是 57.8 元、约 6.1 小时。这种量级
不能等跑起来才知道。所以凡是花钱的命令，执行前必须先把账摆出来。

估算一定不准，因此这里只保证两件事：

    输入 token 是**数出来的**   题目文本此刻就在手上，可以精确统计
    输出 token 是**假设的**     模型还没答，没人知道它要写多长

两者分开报，并把假设值明写在结果里。把猜的部分混进「精确」的总数里，
会让人按一个其实不知道的数字做决策——这比不给估算更糟。
"""

import math

from .engine import bank as bankmod, cost as costmod

#: 每次请求假设的输出 token 数。700 来自 既有测评引擎 的历史记录：
#: 非推理模型的中位数在 400~900 之间，推理模型会高一个量级，
#: 所以推理模型要靠 out_tokens 参数显式抬高，不要指望默认值。
DEFAULT_OUT_TOKENS = 700

#: 假设的输出速度（token/秒）。用于估时间，不影响估钱。
DEFAULT_TPS = 25.0

#: 每次请求的固定开销（秒）：连接、排队、首 token 延迟。
OVERHEAD_SEC = 2.0

#: 超过这个金额就要人确认。按币种分开。
CONFIRM_ABOVE = {"CNY": 2.0, "USD": 0.3, "EUR": 0.3}

#: 记账本位币。跨模型汇总时把各币种折算到这里。
BASE_CURRENCY = "CNY"

#: 兜底汇率（1 单位外币 = 多少本位币）。**永不联网取**——
#: 测评过程中去拉一个可能挂掉的汇率接口，会让本来能跑完的一轮直接失败。
#: 正式值写在 `config/pricing.json` 的 `fx` 字段里，手工维护。
#:
#: 为什么必须有折算：三个端点分别记 CNY / CNY / USD，而 `currency`
#: 此前只是个标签。实施计划第 1.4 项 跑完时，「一共花了多少钱」只能手算 ——
#: 这不是小事，成本闸门就是靠这个数决定要不要拦人的。
FX_FALLBACK = {"CNY": 1.0, "USD": 7.2, "EUR": 7.8}


def fx_rate(currency, fx=None):
    """返回 1 单位该币种折合多少本位币，以及这个汇率是哪来的。"""
    cur = (currency or BASE_CURRENCY).upper()
    if cur == BASE_CURRENCY:
        return 1.0, "本位币"
    table = fx or {}
    if cur in table:
        return float(table[cur]), "config/pricing.json 的 fx"
    if cur in FX_FALLBACK:
        return FX_FALLBACK[cur], "代码内兜底值（建议在 pricing.json 里写实际汇率）"
    return None, "未知币种，无法折算"


def to_base(amount, currency, fx=None):
    """折算到本位币。返回 (金额, 汇率, 汇率来源)；折不了就返回 (None, ...)。"""
    if amount is None:
        return None, None, "金额未知"
    rate, src = fx_rate(currency, fx)
    if rate is None:
        return None, None, src
    return amount * rate, rate, src


def count_tokens(text):
    """粗估 token 数。中日韩字符按 1.5 字/token，其余按 4 字符/token。

    这个比例对中文题库够用了：真实值在 1.3~1.8 之间浮动，
    而输入成本本来就只占总成本的一小部分（输出单价通常是输入的 4 倍）。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿"
              or "　" <= ch <= "〿"
              or "＀" <= ch <= "￯")
    other = len(text) - cjk
    return int(math.ceil(cjk / 1.5 + other / 4.0))


def input_tokens(items, system=""):
    """把每道题真正会发出去的消息拼出来数——不是数题面字符数。

    差别在多轮题和素材题上：一道挂了 4500 字长文档的题，
    题面只有两行，实际输入是 3000 多 token。
    """
    total = 0
    per_item = {}
    for it in items:
        msgs = bankmod.to_messages(it, system)
        n = sum(count_tokens(m.get("content") or "") for m in msgs)
        per_item[it["id"]] = n
        total += n
    return total, per_item


def estimate(items, cfg, trials=None, out_tokens=None, tps=DEFAULT_TPS,
             measured_pace=None):
    """返回这一轮的账单与工期估算。

    cfg 是 endpoint.load() 的结果。pricing 不全时 cost 为 None
    ——宁可说「不知道多少钱」，也不给一个凭空造出来的数。
    """
    trials = trials or cfg["run"]["trials"]
    conc = max(int(cfg["run"].get("concurrency") or 1), 1)
    out_per = out_tokens or DEFAULT_OUT_TOKENS
    max_tok = cfg["model"].get("max_tokens") or 0
    if max_tok and out_per > max_tok:
        out_per = max_tok

    n_items = len(items)
    requests = n_items * trials
    tin_one, per_item = input_tokens(items, cfg["model"].get("system") or "")
    tin = tin_one * trials
    tout = requests * out_per

    p = costmod.normalize(cfg.get("pricing"))
    if p:
        # 估算不假设任何缓存命中：缓存命中率取决于服务商和并发时序，
        # 猜高了会低报成本，而低报成本正是这个函数要防的事。
        money = (tin * p["input_per_mtok"] + tout * p["output_per_mtok"]) / 1e6
        currency = p["currency"]
    else:
        money, currency = None, (cfg.get("pricing") or {}).get("currency", "?")

    per_req_sec = out_per / max(tps, 1.0) + OVERHEAD_SEC
    seconds = requests / conc * per_req_sec

    # 限速下界：qps 卡住的是**整体**吞吐，并发再高也没用。
    #
    # 不加这一条会错得很离谱：opus 的限速网关 qps=0.67，工具按并发 8
    # 估出 4 分钟，实测 4.1 小时 —— 差 60 倍。而且实测后段退避到
    # 119 秒/请求（网关每次把间隔翻倍），所以 qps 算出来的也只是**下界**。
    qps = cfg["run"].get("qps") or 0
    rate_limited = False
    if qps and qps > 0:
        floor_sec = requests / float(qps)
        if floor_sec > seconds:
            seconds, rate_limited = floor_sec, True

    # **本机实测优先。** 依据 MEASUREMENT.md 6.3：
    # 「对没有本机实测的组合必须标注估算值，并优先用本机历史实测中位数」。
    #
    # 为什么公式不够：opus 的限速网关会自动退避，实测单请求从 19.3 秒
    # 退到 43.6 秒再到 119 秒 —— 同一轮里就变了 6 倍。
    # qps 下界（759/0.67 = 19 分钟）比并发公式（47 分钟）还小，两个都不binding，
    # 而真实耗时是 4 小时起。**没有任何先验公式能捕捉退避**，只能拿实测数。
    pace, pace_note = (measured_pace if isinstance(measured_pace, tuple)
                       else (measured_pace, None))
    timing_src = "公式估算"
    if pace and pace > 0:
        # 有实测就用实测，**哪怕它比公式快** —— 6.3 的原话是「优先用
        # 本机历史实测中位数」。取 max 看似保守，实际是拿一个已知不准的
        # 公式去覆盖一个已知更准的观测。
        seconds = requests * float(pace)
        timing_src = pace_note or ("本机实测 %.1f 秒/请求" % pace)
    elif pace_note:
        timing_src = "公式估算（%s）" % pace_note

    money_base, fx_used, fx_src = to_base(money, currency,
                                          (cfg.get("pricing") or {}).get("fx"))

    return {
        "n_items": n_items,
        "trials": trials,
        "requests": requests,
        "concurrency": conc,
        "in_tokens": tin,
        "out_tokens": tout,
        "out_tokens_per_request": out_per,
        "cost": money,
        "currency": currency,
        "cost_base": money_base,
        "base_currency": BASE_CURRENCY,
        "fx_rate": fx_used,
        "fx_source": fx_src,
        "seconds": seconds,
        "minutes": seconds / 60.0,
        "rate_limited": rate_limited,
        "qps": qps or None,
        "timing_source": timing_src,
        "per_item_in": per_item,
        "assumptions": [
            "输入 %d token：按实际消息文本统计，误差主要来自分词差异（±20%%）"
            % tin,
            "输出 %d token/次：**假设值**，推理模型可能是这个数的数倍"
            % out_per,
            ("耗时用 %s" % timing_src)
            if timing_src.startswith("本机") else
            (("耗时按限速 qps=%s 的**下界**算（%d 请求 / %s）。"
              "限速网关会自动退避——实测 opus 后段降到 119 秒/请求，"
              "真实耗时可能是这个下界的数倍" % (qps, requests, qps))
             if rate_limited else
             ("耗时按 %.0f token/秒、并发 %d、每请求固定开销 %.0f 秒推算 —— %s"
              % (tps, conc, OVERHEAD_SEC,
                 pace_note or "没有本机实测数可用"))),
            "不计缓存命中（命中越多越便宜，所以这是上界不是下界）",
        ],
        "pricing_missing": p is None,
    }


def needs_confirm(est, threshold=None):
    """要不要拦一下让人确认。价格未知时**一律拦**——
    不知道多少钱比知道很贵更值得停一下。"""
    if est.get("cost") is None:
        return True, "价格未配置，无法估算花费"
    thr = threshold if threshold is not None else CONFIRM_ABOVE.get(
        est["currency"], 2.0)
    if est["cost"] > thr:
        return True, "预估 %.2f %s，超过 %.2f %s 的确认线" % (
            est["cost"], est["currency"], thr, est["currency"])
    return False, ""


def render(est):
    """给终端看的账单。两行就够，第三行起是假设。"""
    money = ("%.2f %s" % (est["cost"], est["currency"])
             if est["cost"] is not None else "未知（价格未配置）")
    # 非本位币时把折算值一起给出，并**明写汇率**——
    # 一个没标汇率的折算数，读的人没法判断它是什么时候的价。
    if (est.get("cost_base") is not None
            and est.get("currency") != est.get("base_currency")):
        money += " ≈ %.2f %s（汇率 %.3f，%s）" % (
            est["cost_base"], est["base_currency"],
            est["fx_rate"], est["fx_source"])
    m = est["minutes"]
    dur = ("%.0f 分钟" % m) if m < 90 else ("%.1f 小时" % (m / 60.0))
    if est.get("rate_limited"):
        dur = "≥ " + dur + "（受 qps=%s 限速，实测可能是数倍）" % est["qps"]
    L = ["预估花费 %s ｜ 预估耗时 %s" % (money, dur),
         "%d 题 x %d 次 = %d 次请求，并发 %d，输入约 %s token、输出约 %s token"
         % (est["n_items"], est["trials"], est["requests"], est["concurrency"],
            f"{est['in_tokens']:,}", f"{est['out_tokens']:,}"),
         "假设："]
    L += ["  - " + a for a in est["assumptions"]]
    return L
