# -*- coding: utf-8 -*-
"""三态判定。

    正常 normal   分数 >= 阈值
    观察 watch    单次低于阈值
    告警 alert    连续 N 次低于阈值（N = 2，临时基线 N = 3）

为什么不是「低于阈值就告警」
--------------------------
算过账：阈值取 μ−2σ 时，单轮误报率约 2.3%。日频跑一年 =
365 × 0.023 ≈ **8.4 次假警报**。连续两轮才报的话，
误报率降到 0.023² ≈ 0.05%，一年约 **0.2 次**。

代价是慢一天发现真问题。这个交换是划算的：真的降智不会只持续一天，
而一年八次假警报足以让整个团队不再看这个系统。

**允许漏报，不允许频繁误报。**

判定层不碰 LLM
--------------
这里没有、也不会有「让另一个模型看看答得对不对」。判分模型自己也在漂，
它的漂移不可复现（同样的存档明天判出不同的分），
用一个不受监控的移动靶去判定另一个靶有没有移动，是循环论证。
"""

NORMAL, WATCH, ALERT, UNKNOWN = "normal", "watch", "alert", "unknown"
STATE_LABEL = {NORMAL: "正常", WATCH: "观察", ALERT: "告警", UNKNOWN: "未知"}
STATE_ICON = {NORMAL: "○", WATCH: "△", ALERT: "●", UNKNOWN: "?"}

#: 有效采样低于这个数不做判定。低于 15 次时 σ 已经大到判定没有意义。
MIN_TRIALS = 15


def consecutive_needed(bl):
    """临时基线要连续三轮。

    理由：轮数不足时 σ 是低估的，阈值偏高，误报率高于名义的 2.3%。
    多要一轮把误报率再压一个量级，等基线补满 5 轮自动放宽回 2。
    """
    return 3 if (bl and bl.get("provisional")) else 2


def judge(baseline, score, prev_checks, n_trials=None, min_trials=MIN_TRIALS):
    """返回 (state, message, detail)。

    prev_checks: 该序列最近的判定记录，**最新在前**（db.recent_checks 的顺序）。
                 只用其中的 below 字段数连续次数。
    """
    if not baseline:
        return UNKNOWN, "尚未建立基线，无法判定。先建基线。", {}
    if score is None:
        return UNKNOWN, "本轮没有有效分数", {}
    if n_trials is not None and n_trials < min_trials:
        return UNKNOWN, ("有效采样仅 %d 次（<%d），样本太小不做判定"
                         % (n_trials, min_trials)), {}

    thr = baseline["threshold"]
    below = score < thr
    need = consecutive_needed(baseline)

    # 往回数连续低于阈值的轮数（含本轮）。
    # 判定表里 watch 和 alert 都代表「本轮低于阈值」，normal 代表没有；
    # unknown 不算重置——那一轮压根没判，不能当成「恢复正常」。
    streak = 1 if below else 0
    if below:
        for c in prev_checks or []:
            st = c.get("state")
            if st in (WATCH, ALERT):
                streak += 1
            elif st == UNKNOWN:
                continue
            else:
                break

    sd = baseline.get("sigma") or 0.0
    detail = {
        "threshold": thr, "mean": baseline["mean"], "sigma": sd,
        "sigma_source": baseline.get("sigma_source"),
        "below": below, "consecutive": streak, "need": need,
        "gap": score - thr,
        "delta": score - baseline["mean"],
        "z": ((score - baseline["mean"]) / sd) if sd else None,
        "provisional": bool(baseline.get("provisional")),
    }

    if not below:
        return NORMAL, "保守分 %.1f ≥ 阈值 %.1f" % (score, thr), detail

    if streak >= need:
        return ALERT, ("**连续 %d 次**低于阈值（本次 %.1f，阈值 %.1f）。"
                       "按排查三步处理：换渠道 → 换时段 → 看失败题分布。"
                       % (streak, score, thr)), detail

    return WATCH, ("本次 %.1f 低于阈值 %.1f，但还没连够 %d 次。"
                   "**单次下降不告警**——下次仍低于阈值才确认。"
                   % (score, thr, need)), detail


def render(state, message, detail, score=None, baseline=None):
    """判定结论的三层卡。和测评报告用同一套分层。"""
    fact, read, allow, deny = [], [], [], []
    if detail:
        if score is not None:
            fact.append("本轮 %.1f 分，基线 %.1f，阈值 %.1f（μ−2σ，σ=%.2f）"
                        % (score, detail.get("mean", 0),
                           detail.get("threshold", 0), detail.get("sigma", 0)))
        if detail.get("z") is not None:
            fact.append("偏离基线 %+.1f 分（%.2f σ）"
                        % (detail["delta"], detail["z"]))
        fact.append("连续低于阈值 %d 次，告警需要 %d 次"
                    % (detail.get("consecutive", 0), detail.get("need", 2)))
    read.append("**%s** —— %s" % (STATE_LABEL.get(state, state), message))

    if state == NORMAL:
        allow.append("可以说「没有发现退化」")
        deny.append("**不能说「没有退化」**——只能说没发现。"
                    "小于最小可检出量的退化在这个档位上是不可见的")
    elif state == WATCH:
        allow.append("可以说「出现了一次低于阈值的采样，正在观察」")
        deny.append("不能说「模型变笨了」——单次下降有 2.3% 的概率是纯噪声")
        deny.append("不能据此对外发通知或改配置")
    elif state == ALERT:
        allow.append("可以说「连续多轮低于阈值，存在真实下降」")
        deny.append("不能说「是模型厂商改了模型」——"
                    "连续下降也可能来自渠道、限速、上游代理。先跑排查三步")
    else:
        deny.append("**不能下任何判定**：" + message)

    if baseline and baseline.get("provisional"):
        deny.append("这是临时基线（%d 轮），σ 可能被低估，"
                    "结论的可信度低于正式基线" % baseline.get("rounds", 0))
    return {"state": state, "fact": fact, "read": read,
            "allow": allow, "deny": deny}


def triage_steps():
    """告警之后的排查顺序。顺序是有讲究的：先排除最便宜、最常见的原因。"""
    return [
        "**换渠道**：同一个模型换一个 base_url 再跑一轮。"
        "两个渠道一起掉才可能是模型本身。",
        "**换时段**：高峰期的限速和降级路由会让分数掉几个点。"
        "隔几小时再跑一轮。",
        "**看失败题分布**：掉分集中在某一两个维度 = 行为变了；"
        "全维均匀下滑 = 更像是渠道或参数问题。",
        "**核对端点指纹**：确认 base_url / model / temperature / max_tokens "
        "没被人改过。指纹变了就不是同一条序列。",
    ]
