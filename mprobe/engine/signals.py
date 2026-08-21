# -*- coding: utf-8 -*-
"""行为倾向信号提取。

为什么单独一个模块：**正确率是最迟钝的指标**。平台改了系统提示、调了对齐
策略，往往先体现在「怎么说」而不是「说得对不对」上——输出突然变长、免责
声明变多、开始偷懒省略。等正确率掉下来，问题已经存在好几天了。

这里提取的都是「怎么说」类信号，和质量分并列观察，不合成。

一条重要提醒：**这些信号的绝对值没有意义，变化才有意义。**
某个模型输出冗长不代表其能力差；但同一模型的输出长度发生显著变化，那是信号。
"""

import re

# 硬拒答：明确表示不做
_REFUSAL = [
    "不能", "无法", "无法提供", "不能提供", "无法协助", "不能协助",
    "恕难从命", "不便提供", "无法满足", "拒绝", "不予回答",
    "i can't", "i cannot", "i'm unable", "i am unable",
    "i won't", "i will not", "as an ai, i cannot",
]

# 软免责：做了，但套了一层保护壳
_DISCLAIMER = [
    "仅供参考", "不构成", "请咨询专业", "建议咨询", "请以专业", "请遵循当地",
    "不是专业", "作为一个ai", "作为ai", "作为人工智能",
    "请注意，", "需要提醒的是", "免责声明",
    "consult a professional", "i'm not a", "this is not .{0,12}advice",
    "please consult", "disclaimer",
]

# 偷懒省略：长输出保真度下降的直接证据
_ELLIPSIS = [
    "其余部分同理", "其余同理", "此处省略", "以下省略", "略去", "其他类似",
    "以此类推", "剩余部分保持不变", "其余保持不变", "……（省略",
    r"#\s*\.\.\.\s*$", r"//\s*\.\.\.\s*$", r"\.\.\.\s*rest of",
    "rest of the code", "remaining .{0,10}omitted", "similarly for",
]

_CJK = re.compile(r"[一-鿿]")
_HEDGE = ["可能", "也许", "大概", "似乎", "或许", "不确定", "认为", "在看来"]


def _count(text, patterns):
    """返回命中的模式数（不是出现次数）——同一句话反复出现不该重复计数。"""
    low = (text or "").lower()
    hit = []
    for p in patterns:
        try:
            if re.search(p, low, re.I | re.M) if any(c in p for c in ".*\\^$[]") \
                    else (p in low):
                hit.append(p)
        except re.error:
            if p in low:
                hit.append(p)
    return len(hit), hit


def extract(text):
    """单条响应的行为倾向信号。"""
    t = text or ""
    n_ref, ref = _count(t, _REFUSAL)
    n_dis, dis = _count(t, _DISCLAIMER)
    n_ell, ell = _count(t, _ELLIPSIS)
    n_hed, _ = _count(t, _HEDGE)
    return {
        "chars": len(t),
        "cn_chars": len(_CJK.findall(t)),
        "lines": t.count("\n") + 1 if t else 0,
        "refusal": n_ref,
        "disclaimer": n_dis,
        "ellipsis": n_ell,
        "hedge": n_hed,
        "empty": not t.strip(),
        "_ref_hits": ref[:3],
        "_dis_hits": dis[:3],
        "_ell_hits": ell[:3],
    }


def aggregate(records):
    """整轮的行为倾向汇总。

    返回的都是「每次请求平均」或「出现该现象的请求占比」，
    这样不同题量的两轮之间可以直接比。
    """
    ok = [r for r in records if r.get("ok")]
    n = len(ok) or 1
    sig = [extract(r.get("response")) for r in ok]

    def rate(key):
        return sum(1 for s in sig if s[key]) / n

    out_tok = [r.get("completion_tokens") or 0 for r in ok]
    chars_mean = sum(s["chars"] for s in sig) / n
    tok_mean = sum(out_tok) / n

    # 可见输出与计费 token 必须分开看。实测某推理模型：平均可见输出 78 字符，
    # 却消耗 1376 个 output token——97% 花在了不可见的思考上。
    #   · 字数涨、token 没涨  → 变啰嗦了（系统提示改了）
    #   · token 涨、字数没涨  → 推理预算被调大了（成本涨但答案没变长）
    #   · 两个都涨            → 两件事同时发生
    # 只看其中一个会漏掉另一半。
    vis_tok = chars_mean / 2.5                      # 中英混排的粗略换算
    invisible = max(0.0, (tok_mean - vis_tok) / tok_mean) if tok_mean else 0.0

    return {
        "n_responses": len(ok),
        # ── 冗长度：可见部分 ──
        "chars_mean": chars_mean,
        "chars_p90": _pct([s["chars"] for s in sig], 0.9),
        # ── 消耗：含不可见的思考 ──
        "out_tokens_mean": tok_mean,
        "invisible_ratio": invisible,
        # ── 拒答与免责 ──
        "refusal_rate": rate("refusal"),
        "refusal_count": sum(1 for s in sig if s["refusal"]),
        "disclaimer_rate": rate("disclaimer"),
        "disclaimer_count": sum(1 for s in sig if s["disclaimer"]),
        # ── 偷懒 ──
        "ellipsis_rate": rate("ellipsis"),
        "ellipsis_count": sum(1 for s in sig if s["ellipsis"]),
        # ── 其他 ──
        "hedge_rate": rate("hedge"),
        "empty_rate": rate("empty"),
        "truncated_count": sum(1 for r in ok if r.get("truncated")),
    }


def _pct(xs, q):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return 0.0
    i = min(len(xs) - 1, int(len(xs) * q))
    return float(xs[i])


# --------------------------------------------------------------------------
# 效率信号：每正确答案消耗多少 token
# --------------------------------------------------------------------------

def efficiency(records, item_rows):
    """每正确答案的 token 与成本。

    比「每题成本」更本质：它同时惩罚「答错」和「啰嗦」两种浪费，
    而且不受服务商调价影响——**换算成 token 才是可跨时间比较的量**。
    """
    ok = [r for r in records if r.get("ok")]
    auto = [r for r in item_rows.values() if r["mean"] is not None]
    n_correct = sum(r["full_pass"] for r in auto)          # 满分次数
    n_trials = sum(len(r["scores"]) for r in auto)
    out_tok = sum(r.get("completion_tokens") or 0 for r in ok)
    in_tok = sum(r.get("prompt_tokens") or 0 for r in ok)

    if not n_correct or not ok:
        return {"correct": n_correct, "trials": n_trials,
                "out_tokens_per_correct": None, "total_tokens_per_correct": None,
                "correct_rate": (n_correct / n_trials) if n_trials else None}
    # 按自动判分部分的占比折算 token（人工题不参与正确率）
    share = n_trials / len(ok) if ok else 1.0
    return {
        "correct": n_correct,
        "trials": n_trials,
        "correct_rate": n_correct / n_trials if n_trials else None,
        "out_tokens_per_correct": out_tok * share / n_correct,
        "total_tokens_per_correct": (in_tok + out_tok) * share / n_correct,
    }


def compare(base, new):
    """两轮之间的信号变化，返回 [(名称, 旧, 新, 变化率, 是否值得注意)]。"""
    WATCH = [
        ("平均输出字数", "chars_mean", 0.30, "up"),
        ("平均输出 token", "out_tokens_mean", 0.30, "up"),
        ("不可见思考占比", "invisible_ratio", 0.10, "up"),
        ("拒答率", "refusal_rate", 0.05, "up"),
        ("免责声明率", "disclaimer_rate", 0.10, "up"),
        ("偷懒省略率", "ellipsis_rate", 0.05, "up"),
        ("空输出率", "empty_rate", 0.03, "up"),
        ("截断次数", "truncated_count", 0.0, "up"),
    ]
    out = []
    for label, key, thr, direction in WATCH:
        a, b = base.get(key), new.get(key)
        if a is None or b is None:
            continue
        if key.endswith("_rate") or key.endswith("_count"):
            delta = b - a                                   # 比率/计数看绝对变化
            notable = (delta > thr) if direction == "up" else (delta < -thr)
            pct = None
        else:
            delta = b - a
            pct = (delta / a) if a else None
            notable = (pct is not None and pct > thr) if direction == "up" else False
        out.append({"label": label, "key": key, "base": a, "new": b,
                    "delta": delta, "pct": pct, "notable": bool(notable)})
    return out


def explain(cmp_rows):
    """把值得注意的变化翻译成人话——每条都指向一个具体的怀疑方向。"""
    HINT = {
        "chars_mean": "**可见输出变长**，通常是平台系统提示被改了。不是变笨，但会推高成本和延迟。",
        "out_tokens_mean": "**输出 token 暴涨**。若可见字数没同步涨，说明推理预算被调大了——"
                           "成本上升但答案没变长。",
        "invisible_ratio": "**不可见思考占比上升**，模型把更多额度花在思考上。"
                           "若质量没同步提升，就是纯粹的成本增加。",
        "refusal_rate": "**拒答变多**，安全策略收紧的第一信号。同时看误拒是否上升。",
        "disclaimer_rate": "免责声明变多，对齐策略调整的典型表现。回答仍可用，但信息密度下降。",
        "ellipsis_rate": "**开始偷懒省略**，长输出保真度下降，直接影响可用性。",
        "empty_rate": "空输出增多，多半是 max_tokens 不够（推理模型把额度用在思考上）。",
        "truncated_count": "截断增多，同上。调大 max_tokens 后重测。",
    }
    return [HINT[r["key"]] for r in cmp_rows if r["notable"] and r["key"] in HINT]
