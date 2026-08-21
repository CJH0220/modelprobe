# -*- coding: utf-8 -*-
"""聚合算法：把各维度分数合成总分。

为什么不用简单加权平均
----------------------
加权平均有三个问题，在「每维只有几道题」的场景下均会实际发生：

1. **不区分样本量。** 2 道题的维度和 11 道题的维度同等对待，前者的噪声
   会被原样搬进总分。多跑一轮就可能让总分跳几分，但模型什么都没变。
2. **不惩罚不确定性。** 数据越少的模型反而可能因为运气好而排名靠前。
3. **短板会被掩盖。** 某维度接近 0 分，只要其他维度够高，总分依然好看——
   但实际使用时，一个维度崩掉往往就意味着不能用。

对策
----
- **收缩**：每个维度用 Beta-Binomial 后验，小样本维度被拉向中性先验
  （默认 0.5）。样本越多，越相信数据本身。靶心默认取 0.5 而非全局均值，
  理由见 aggregate() 的 prior_mode 说明——那里记录了一个实测踩到的坑。
- **保守分 μ − kσ**：把不确定性折算成扣分。样本不足 → σ 大 → 扣得多。
  这样「多跑几次」和「真的更好」不会被混淆。
- **短板分（加权几何平均）**：任一维度接近 0 会把它拉垮，用来暴露被平均
  掩盖的致命弱项。

三个分数一起报，不合成一个数——它们回答的是不同问题。
"""

import math

from . import dims

EPS = 0.01          # 几何平均的下限，避免 ln(0)


def shrink(k, n, prior_mean, prior_strength):
    """Beta-Binomial 后验。

    k 可以是小数（判分器返回连续分时，k = 各次得分之和）。
    prior_strength 相当于「先验伪样本数」：n 远大于它时几乎不收缩，
    n 与它同量级时收缩明显。
    """
    m = max(prior_strength, 1e-9)
    a = m * prior_mean
    if n <= 0:
        return prior_mean, math.sqrt(prior_mean * (1 - prior_mean) / (m + 1))
    mu = (k + a) / (n + m)
    mu = min(max(mu, 1e-9), 1 - 1e-9)
    var = mu * (1 - mu) / (n + m + 1)
    return mu, math.sqrt(var)


def aggregate(dim_rows, weight_fn, prior_strength=3.0, k_sigma=2.0,
              prior_mode="neutral"):
    """返回聚合结果。dim_rows 需含 k_eff / n_eff / score。

    prior_mode 决定收缩靶心：

      neutral（默认，0.5）
          纯粹为了压小样本的方差，不引入任何跨维度信息。

      global（全局通过率）
          经典经验贝叶斯。**但它在这里通常是错的**：经验贝叶斯假设各组
          可交换（来自同一分布），而所需的维度是刻意测不同东西的，难度
          本就不同。用全局均值当先验会让不相关的维度互相污染——实测中
          出现过「某维度 9 次全对被拉到 80%，只因为另外两个无关维度崩了」。
          仅在你确信各维度难度相近时才用。

    只统计有自动判分数据的维度；全为人工判分的维度不参与量化分。
    """
    # 冒烟维不进保守分，理由与 metrics.overall 相同：
    # 一批恒满分的题会同时抬高分数、压小方差。
    usable = [d for d in dim_rows.values()
              if d.get("n_eff") and d["n_eff"] > 0
              and d.get("score") is not None
              and dims.in_score(d["dim"])]
    if not usable:
        return None

    tot_k = sum(d["k_eff"] for d in usable)
    tot_n = sum(d["n_eff"] for d in usable)
    if prior_mode == "global":
        prior_mean = min(max(tot_k / tot_n, 1e-6), 1 - 1e-6)
    else:
        prior_mean = 0.5

    per_dim, W = {}, 0.0
    for d in usable:
        w = weight_fn(d["dim"])
        mu, sd = shrink(d["k_eff"], d["n_eff"], prior_mean, prior_strength)
        per_dim[d["dim"]] = {
            "raw": d["score"], "mu": mu, "sd": sd, "weight": w,
            "n_eff": d["n_eff"],
            "shrink_pull": mu - d["score"],      # 正=被拉高，负=被拉低
        }
        W += w
    if W <= 0:
        return None

    mu_tot = sum(v["mu"] * v["weight"] for v in per_dim.values()) / W
    # 独立性假设下的方差传播。维度之间实际存在相关，因此这是偏乐观的下界，
    # 真实不确定性只会更大——保守分因此是「至少扣这么多」。
    var_tot = sum((v["weight"] ** 2) * (v["sd"] ** 2) for v in per_dim.values()) / (W ** 2)
    sd_tot = math.sqrt(var_tot)
    conservative = max(0.0, mu_tot - k_sigma * sd_tot)

    geo = math.exp(sum(v["weight"] * math.log(max(v["mu"], EPS))
                       for v in per_dim.values()) / W)

    weakest = min(per_dim.items(), key=lambda kv: kv[1]["mu"])
    return {
        "expected": 100.0 * mu_tot,
        "conservative": 100.0 * conservative,
        "geometric": 100.0 * geo,
        "sd": 100.0 * sd_tot,
        "k_sigma": k_sigma,
        "prior_mean": prior_mean,
        "prior_strength": prior_strength,
        "prior_mode": prior_mode,
        "n_dims": len(per_dim),
        "n_trials": tot_n,
        "per_dim": per_dim,
        "weakest_dim": weakest[0],
        "weakest_score": 100.0 * weakest[1]["mu"],
        "masking_gap": 100.0 * (mu_tot - geo),   # 算术与几何之差＝短板被掩盖的程度
    }


def explain(agg):
    """给报告用的判读文字。"""
    if not agg:
        return []
    out = []
    gap = agg["masking_gap"]
    if gap > 8:
        out.append("**短板明显**：期望分与短板分相差 %.1f 分，说明总分被平均效应"
                   "抬高了。最弱维度 %s 仅 %.1f 分——若该维度对你的用途关键，"
                   "总分不能作为决策依据。"
                   % (gap, agg["weakest_dim"], agg["weakest_score"]))
    elif gap > 3:
        out.append("各维度较均衡，短板分低于期望分 %.1f 分，属正常范围。" % gap)
    else:
        out.append("各维度高度均衡（期望分与短板分仅差 %.1f 分）。" % gap)

    if agg["sd"] > 3:
        out.append("**样本量偏少**：聚合标准差 %.1f 分，保守分比期望分低 %.1f 分。"
                   "要缩小这个差距，加大 --trials 比换模型更有效。"
                   % (agg["sd"], agg["expected"] - agg["conservative"]))

    pulled = sorted(agg["per_dim"].items(),
                    key=lambda kv: -abs(kv[1]["shrink_pull"]))[:3]
    big = [(d, v) for d, v in pulled if abs(v["shrink_pull"]) > 0.05]
    if big:
        out.append("收缩影响最大的维度：" + "、".join(
            "%s（%.1f%% → %.1f%%，n=%d）"
            % (d, v["raw"] * 100, v["mu"] * 100, v["n_eff"]) for d, v in big)
            + "。样本越少拉得越狠，这是刻意的。")
    return out
