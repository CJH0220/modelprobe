# -*- coding: utf-8 -*-
"""基线构建。

基线 = (均值, σ, 阈值)，锚定在一个四元组上：

    (model_key, endpoint_sha, bank_rev, tier)

四个都是身份的一部分。03 的旧实现只按 (档案) 存基线，
换个模型建基线会直接覆盖上一个，然后旧模型的运行被拿去和新模型的
阈值比——这不是假设，实际发生过。这里把四元组写进主键，
让那种事在数据结构层面就不可能。

σ 的三取大规则
--------------
    σ = max(sd_observed, sd_binomial, sd_floor)

    sd_observed  实测轮间标准差。轮数少的时候会系统性低估。
                 极端情况：2 轮碰巧分数一样，sd = 0，阈值 = 均值，
                 之后任何一点点下降都告警。
    sd_binomial  100 * sqrt(p(1-p)/n)。二项抽样的理论下界，
                 只跑一轮时唯一能算的东西。
    sd_floor     100 * 0.5 / n。半道题的分值。这是量化下界：
                 分数只能以 1/n 为步长变动，比这更细的阈值没有意义。

取最大是因为**每一个单独用都会低估**，而低估 σ 的代价是误报，
误报的代价是这套系统被弃用。
"""

import math
import time

SIGMA_OBSERVED = "observed"
SIGMA_BINOMIAL = "binomial"
SIGMA_FLOOR = "floor"

#: 少于这个轮数的基线标记为临时（provisional），告警条件收紧为连续三轮
FULL_ROUNDS = 5


class BaselineError(Exception):
    pass


def sigma(scores, n_requests, mean=None):
    """三取大。返回 (sigma, source, 明细)。"""
    if n_requests < 1:
        raise BaselineError("n_requests 必须 >= 1")
    m = mean if mean is not None else (sum(scores) / len(scores) if scores else 0.0)

    if len(scores) >= 2:
        var = sum((x - m) ** 2 for x in scores) / (len(scores) - 1)
        sd_obs = math.sqrt(var)
    else:
        sd_obs = 0.0

    p = max(min(m / 100.0, 1 - 1e-9), 1e-9)
    sd_binom = 100.0 * math.sqrt(p * (1 - p) / n_requests)
    sd_floor = 100.0 * 0.5 / n_requests

    cands = ((sd_obs, SIGMA_OBSERVED), (sd_binom, SIGMA_BINOMIAL),
             (sd_floor, SIGMA_FLOOR))
    sd, src = max(cands, key=lambda t: t[0])
    return sd, src, {"sd_observed": sd_obs, "sd_binomial": sd_binom,
                     "sd_floor": sd_floor}


def build(model_key, endpoint_sha, bank_rev, tier, rounds):
    """从若干轮结果构建基线。

    rounds: [{"run_id":…, "score":…, "requests":…, "dims":{dim: score}}, …]
            score 用保守分还是量化分由调用方决定，但**必须前后一致**。
    """
    if not rounds:
        raise BaselineError("没有可用的轮次")
    scores = [r["score"] for r in rounds if r.get("score") is not None]
    if not scores:
        raise BaselineError("这些轮次里没有有效分数——先看请求健康度")

    mean = sum(scores) / len(scores)
    n_req = max(int(rounds[0].get("requests") or 0), 1)
    sd, src, parts = sigma(scores, n_req, mean)
    thr = max(0.0, mean - 2 * sd)

    dim_means = {}
    for d in {k for r in rounds for k in (r.get("dims") or {})}:
        vals = [(r.get("dims") or {}).get(d) for r in rounds]
        vals = [v for v in vals if isinstance(v, (int, float))]
        if vals:
            dim_means[d] = sum(vals) / len(vals)

    bl = {
        "model_key": model_key,
        "endpoint_sha": endpoint_sha,
        "bank_rev": bank_rev,
        "tier": tier,
        "mean": mean,
        "sigma": sd,
        "sigma_source": src,
        "threshold": thr,
        "rounds": len(scores),
        "provisional": len(scores) < FULL_ROUNDS,
        "run_ids": [r.get("run_id") for r in rounds],
        "scores": [round(s, 2) for s in scores],
        "dim_means": dim_means,
        "requests_per_round": n_req,
        "created_at": time.time(),
    }
    bl.update(parts)
    return bl


def explain(bl):
    """给报告和结论卡用的人话解释。"""
    src = {SIGMA_OBSERVED: "实测轮间标准差",
           SIGMA_BINOMIAL: "二项理论下界（轮数不足，实测 σ 不可信）",
           SIGMA_FLOOR: "量化地板 0.5 题（分数的最小步长）"}[bl["sigma_source"]]
    L = [
        "基线均值 %.1f 分，σ = %.2f（取自：%s）" % (bl["mean"], bl["sigma"], src),
        "告警阈值 = 均值 − 2σ = **%.1f 分**" % bl["threshold"],
        "建自 %d 轮，每轮 %d 次请求" % (bl["rounds"], bl["requests_per_round"]),
    ]
    if bl["provisional"]:
        L.append("**这是临时基线**（轮数 %d < %d）。σ 大概率被低估，"
                 "所以告警条件收紧为**连续三轮**低于阈值。"
                 "补满 %d 轮后会自动转正。"
                 % (bl["rounds"], FULL_ROUNDS, FULL_ROUNDS))
    if bl["sigma_source"] != SIGMA_OBSERVED and bl["rounds"] >= 2:
        L.append("实测 σ 是 %.2f，比取用值小——直接用它会让阈值贴着均值，"
                 "正常波动就会触发告警。" % bl["sd_observed"])
    return L
