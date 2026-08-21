# -*- coding: utf-8 -*-
"""成本核算。

为什么把成本做成一等指标：调研公开榜单时的一个反直觉发现是，**分数最低的
模型可能最贵**。SWE-bench Verified 的公开复现里，o1-mini 得分 27.2% 却花了
$367，而得分 38.0% 的 Claude 3.5 Sonnet 只花 $67。只看分数会做出完全错误
的选型决策。

三个指标，回答三个不同问题：
    每题成本      跑一遍要多少钱      —— 预算规划
    每正确答案成本 买到一个对的答案多少钱 —— 真实性价比，最该看的一个
    每题 token    输出啰嗦到什么程度    —— 与延迟直接相关
"""

DEFAULT_PRICING = {
    "input_per_mtok": None,
    "output_per_mtok": None,
    "cached_input_per_mtok": None,     # 未配置时按 input 价计
    "currency": "USD",
}


def normalize(pricing):
    if not pricing:
        return None
    p = dict(DEFAULT_PRICING)
    p.update(pricing)
    if p["input_per_mtok"] is None or p["output_per_mtok"] is None:
        return None                     # 价格不全就不算，避免给出误导性数字
    if p["cached_input_per_mtok"] is None:
        p["cached_input_per_mtok"] = p["input_per_mtok"]
    return p


def compute(records, item_rows, pricing):
    """返回成本统计；pricing 未配置时返回 None（报告里会提示如何配）。"""
    p = normalize(pricing)
    if not p:
        return None

    tin = tout = tcached = 0
    for r in records:
        tin += r.get("prompt_tokens") or 0
        tout += r.get("completion_tokens") or 0
        tcached += r.get("cached_tokens") or 0
    tin_billed = max(tin - tcached, 0)

    cost = (tin_billed * p["input_per_mtok"]
            + tcached * p["cached_input_per_mtok"]
            + tout * p["output_per_mtok"]) / 1_000_000

    n_req = len(records)
    auto = [r for r in item_rows.values() if r["mean"] is not None]
    n_items = len(auto)
    # 「正确答案」按满分次数算：部分得分不算买到了一个可用的答案。
    # 这会让部分得分型题目显得偏贵，是刻意的——半个答案不能直接用。
    n_correct = sum(r["full_pass"] for r in auto)
    n_auto_trials = sum(len(r["scores"]) for r in auto)

    return {
        "currency": p["currency"],
        "pricing": p,
        "total_cost": cost,
        "requests": n_req,
        "in_tokens": tin,
        "cached_tokens": tcached,
        "out_tokens": tout,
        "cost_per_request": cost / n_req if n_req else None,
        "cost_per_item": cost / n_items if n_items else None,
        "cost_per_correct": (cost * n_auto_trials / n_req / n_correct)
                            if (n_correct and n_req) else None,
        "out_tokens_per_request": tout / n_req if n_req else None,
        "n_correct": n_correct,
        "n_auto_trials": n_auto_trials,
        "correct_rate": n_correct / n_auto_trials if n_auto_trials else None,
    }


def score(cost_stats, ref_cost_per_item, floor=0.0):
    """把成本映射成 0-100 分，用于可选地纳入总评分。

    以 ref_cost_per_item 为基准（得 50 分），成本减半 +25 分，翻倍 -25 分，
    即对数刻度。用对数是因为成本跨度动辄两个数量级，线性刻度会让便宜的模型
    全部挤在一端。

    **默认不纳入总评分。** 把质量和成本压成一个数会掩盖二者的权衡，
    正确做法是并列呈现，由使用者按预算自己取舍。
    """
    import math
    if not cost_stats or not cost_stats.get("cost_per_item") or not ref_cost_per_item:
        return None
    ratio = cost_stats["cost_per_item"] / ref_cost_per_item
    s = 50.0 - 25.0 * math.log2(max(ratio, 1e-9))
    return max(floor, min(100.0, s))


def frontier_note(quality, cost_stats):
    """一句话性价比判读。"""
    if not cost_stats or quality is None:
        return None
    cpc = cost_stats.get("cost_per_correct")
    if cpc is None:
        return None
    cur = cost_stats["currency"]
    return ("质量 %.1f 分，每个正确答案 %.4f %s（正确率 %.1f%%）。"
            "换模型前先用这个数比，而不是比单价——单价便宜但需要更多 token "
            "或更多重试的模型，实际更贵。"
            % (quality, cpc, cur, (cost_stats["correct_rate"] or 0) * 100))


def compare(base, new):
    """两次运行的成本对比。"""
    if not base or not new:
        return None
    def _d(a, b):
        if a in (None, 0) or b is None:
            return None
        return (b - a) / a * 100
    return {
        "total": (base["total_cost"], new["total_cost"],
                  _d(base["total_cost"], new["total_cost"])),
        "per_item": (base.get("cost_per_item"), new.get("cost_per_item"),
                     _d(base.get("cost_per_item"), new.get("cost_per_item"))),
        "per_correct": (base.get("cost_per_correct"), new.get("cost_per_correct"),
                        _d(base.get("cost_per_correct"), new.get("cost_per_correct"))),
        "out_tokens_per_request": (base.get("out_tokens_per_request"),
                                   new.get("out_tokens_per_request"),
                                   _d(base.get("out_tokens_per_request"),
                                      new.get("out_tokens_per_request"))),
        "currency": new.get("currency", "USD"),
    }
