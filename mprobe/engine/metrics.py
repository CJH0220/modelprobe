# -*- coding: utf-8 -*-
"""量化指标计算。

一句话原则：任何点估计都必须带不确定性。20 题的 85% 和 200 题的 85%
不是一回事，只报点估计会让人把噪声当成变化。
"""

import math
from collections import defaultdict

from . import dims

Z95 = 1.959964


def wilson(k, n, z=Z95):
    """Wilson 得分区间。小样本下比正态近似准，且不会越出 [0,1]。"""
    if n <= 0:
        return 0.0, 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z / d * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return p, max(0.0, c - h), min(1.0, c + h)


def pass_hat_k(scores, k, threshold=1.0):
    """pass^k：k 次全部达标的比例（组合无偏估计，采样不足时退化为二项外推）。"""
    n = len(scores)
    c = sum(1 for s in scores if s is not None and s >= threshold - 1e-9)
    if n == 0:
        return 0.0
    if n >= k:
        return math.comb(c, k) / math.comb(n, k)
    return (c / n) ** k


def pass_at_k(scores, k, threshold=1.0):
    n = len(scores)
    c = sum(1 for s in scores if s is not None and s >= threshold - 1e-9)
    if n == 0:
        return 0.0
    if n >= k:
        return 1 - math.comb(n - c, k) / math.comb(n, k)
    return 1 - (1 - c / n) ** k


def percentile(xs, q):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    i = (len(xs) - 1) * q
    lo, hi = int(math.floor(i)), int(math.ceil(i))
    return xs[lo] if lo == hi else xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


# --------------------------------------------------------------------------

def per_item(records):
    """按题聚合。返回 {id: {...}}，保持题库顺序。"""
    by = defaultdict(list)
    for r in records:
        by[r["id"]].append(r)

    out = {}
    for iid, rs in by.items():
        rs = sorted(rs, key=lambda r: r.get("trial", 0))
        scored = [r["score"] for r in rs if r["score"] is not None]
        ok = [r for r in rs if r.get("ok")]
        row = {
            "id": iid,
            "dim": rs[0].get("dim"),
            "tier": rs[0].get("tier"),
            "title": rs[0].get("title", ""),
            "weight": rs[0].get("weight", 1.0),
            "observe": bool(rs[0].get("observe")),
            "trials": len(rs),
            "ok_trials": len(ok),
            "failed_requests": len(rs) - len(ok),
            "scores": scored,
            "mean": (sum(scored) / len(scored)) if scored else None,
            "min": min(scored) if scored else None,
            "max": max(scored) if scored else None,
            "spread": (max(scored) - min(scored)) if scored else None,
            "full_pass": sum(1 for s in scored if s >= 1 - 1e-9),
            "latency_p50": percentile([r.get("latency_ms") for r in rs], 0.5),
            "out_tokens_mean": _mean([r.get("completion_tokens") for r in rs]),
            "confidences": [r.get("confidence") for r in rs if r.get("confidence") is not None],
            "details": [r.get("detail") for r in rs],
        }
        out[iid] = row
    return out


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def per_dim(item_rows, weight_fn=None):
    """按维度聚合。

    没有分数的题（纯观测题、整题请求全失败）不进 scored，但仍计入 n_items
    ——否则报告里会出现「这个维度只有 2 道题」而题库里明明有 5 道。
    """
    by = defaultdict(list)
    for row in item_rows.values():
        if row.get("observe"):
            continue          # 观测题只记录，不进任何分数，也不算这个维的题数
        by[row["dim"]].append(row)

    out = {}
    for dim, rows in sorted(by.items()):
        auto = [r for r in rows if r["mean"] is not None]
        w = sum((r.get("weight") or 1.0) for r in auto)
        score = sum(r["mean"] * (r.get("weight") or 1.0) for r in auto) / w if w else None
        # 用「加权后的等效通过数」构造区间，粗略但足以反映样本量
        n_eff = sum(len(r["scores"]) for r in auto)
        k_eff = sum(sum(r["scores"]) for r in auto)
        _, lo, hi = wilson(k_eff, n_eff) if n_eff else (0, 0, 0)
        all_scores = [s for r in auto for s in r["scores"]]
        out[dim] = {
            "dim": dim,
            "k_eff": k_eff, "n_eff": n_eff,     # 供 compare 做跨维汇总检验
            "n_items": len(rows),
            "n_scored": len(auto),
            "score": score,
            "ci_lo": lo, "ci_hi": hi,
            "n_trials": n_eff,
            "pass_hat_k": pass_hat_k(all_scores, min(3, max(1, len(all_scores)))),
            "spread_mean": _mean([r["spread"] for r in auto]),
            "weight": (weight_fn(dim) if weight_fn else 1.0),
            "in_score": dims.in_score(dim),
        }
    return out


def overall(dim_rows):
    """维度加权后的量化总分（0-100）。**冒烟维不计入。**

    为什么必须排除 S 冒烟：它测的是"这条链路通不通"，不是能力。
    26 道冒烟题在 1.1 的实测里三个模型**全部 p = 1.000**，是纯饱和题。
    把一批恒为满分的题摊进总分，有两个后果，而且都是往乐观的方向错：

      1. **分数虚高。** 实测：小档 6/20 是冒烟题，六轮真实数据上
         总分被抬高 5.0 ~ 8.0 分（均值 6.7）——这个量级和真实退化同级
         （既有研究结论里 D 维一次真实跨模型差异是 15 分）。
      2. **分辨率虚高，更隐蔽。** 恒定项摊薄方差：剔掉 S 后同五轮的
         观测 SD 从 2.54 升到 3.63，最小可检出退化从看起来的 9.4 分
         变成真实的 12.9 分。也就是说，混入冒烟题会让工具**声称自己
         比实际更灵敏**。

    `dims.in_score()` 早就写好了，但此前整个计分链路没有调用它 ——
    定义了规则却没接上，是"能算出结果、不报错、结论错"的典型。
    """
    usable = [d for d in dim_rows.values()
              if d["score"] is not None and dims.in_score(d["dim"])]
    if not usable:
        return None
    wsum = sum(d["weight"] for d in usable)
    return 100.0 * sum(d["score"] * d["weight"] for d in usable) / wsum if wsum else None


def runtime_stats(records):
    lat = [r.get("latency_ms") for r in records]
    out = [r.get("completion_tokens") for r in records]
    inp = [r.get("prompt_tokens") for r in records]
    trunc = [r for r in records if r.get("truncated")]
    trunc_empty = [r for r in trunc if not (r.get("response") or "").strip()]
    return {
        "requests": len(records),
        "failed": sum(1 for r in records if not r.get("ok")),
        "truncated": len(trunc),
        "truncated_empty": len(trunc_empty),
        "truncated_ids": sorted({r["id"] for r in trunc})[:20],
        "latency_p50": percentile(lat, 0.5),
        "latency_p95": percentile(lat, 0.95),
        "out_tokens_mean": _mean(out),
        "out_tokens_total": sum(x for x in out if x) or None,
        "in_tokens_total": sum(x for x in inp if x) or None,
        "retried": sum(1 for r in records if (r.get("attempts") or 1) > 1),
    }


def calibration(records):
    """置信度校准。需要题目要求模型输出「置信度: N」。"""
    pts = [(r["confidence"] / 100.0, r["score"])
           for r in records
           if r.get("confidence") is not None and r.get("score") is not None]
    if len(pts) < 4:
        return None
    n = len(pts)
    base = sum(o for _, o in pts) / n
    brier = sum((p - o) ** 2 for p, o in pts) / n

    groups = defaultdict(list)
    for p, o in pts:
        groups[round(p, 6)].append(o)
    rel = res = 0.0
    for p, os in groups.items():
        w = len(os) / n
        obar = sum(os) / len(os)
        rel += w * (p - obar) ** 2
        res += w * (obar - base) ** 2

    hi = [(p, o) for p, o in pts if p >= 0.9]
    bins, ece = [], 0.0
    for lo_, hi_ in [(0, .2), (.2, .4), (.4, .6), (.6, .8), (.8, .9), (.9, 1.01)]:
        g = [(p, o) for p, o in pts if lo_ <= p < hi_]
        if g:
            acc = sum(o for _, o in g) / len(g)
            conf = sum(p for p, _ in g) / len(g)
            ece += len(g) / n * abs(acc - conf)
            bins.append({"lo": lo_, "hi": hi_, "n": len(g), "conf": conf, "acc": acc})
    return {
        "n": n, "brier": brier, "reliability": rel, "resolution": res,
        "uncertainty": base * (1 - base), "ece": ece, "bins": bins,
        "high_conf_n": len(hi),
        "high_conf_acc": (sum(o for _, o in hi) / len(hi)) if hi else None,
        "avg_group_size": n / len(groups),
    }
