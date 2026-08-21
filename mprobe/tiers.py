# -*- coding: utf-8 -*-
"""档位定义，以及「这个档位能下什么结论」。

三档的区别不是耗时，是最小可检出退化
------------------------------------
这是整个工具最容易被误读的地方。用户以为大档只是「跑得久一点、准一点」，
实际差别是**能不能看见**：

    小档 60 请求  → 12 分以下的退化看不见
    中档 150      → 7.6 分以下看不见
    大档 360      → 4.9 分以下看不见

「没告警」不等于「没退化」，只等于「退化幅度小于本档的分辨率」。
这句话必须印在每一张结论卡上，否则这个工具的输出就是误导性的。

数字从哪来
----------
分数是各题各次采样的加权通过率乘 100，本质是一个二项比例。

    sigma = 100 * sqrt(p(1-p)/n)          n = 题数 x 采样次数

历史实测 p 约 0.688，代入得 sigma = 46.331/sqrt(n)。
阈值取 mu - 2*sigma，所以最小可检出退化 = 2*sigma。

实际运行时 sigma 不用这个公式，而是取三者最大
（实测轮间 SD / 二项 SD / 地板），见 monitor/baseline.py。
这里的公式只用于**事前**告诉用户「这一档值不值得跑」。
"""

import math

#: 历史实测的整体通过率。只用于事前估算，不参与任何判定。
REF_P = 0.688

#: sigma = SIGMA_K / sqrt(n)
SIGMA_K = 100.0 * math.sqrt(REF_P * (1 - REF_P))

Z95 = 1.959964

TIERS = {
    "small": {
        "key": "small",
        "label": "小档",
        "items": 20,
        "trials": 3,
        "banks": ["core.jsonl"],
        "cadence": "daily",
        "purpose": "日常哨兵。确认渠道还通、行为倾向没有突变。",
        "baseline_rounds": 5,
    },
    "medium": {
        "key": "medium",
        "label": "中档",
        "items": 50,
        "trials": 3,
        "banks": ["core.jsonl", "public_v1.jsonl"],
        "cadence": "weekly",
        "purpose": "周度体检。能对单个维度下判断。",
        "baseline_rounds": 1,
    },
    "large": {
        "key": "large",
        "label": "大档",
        "items": 120,
        "trials": 3,
        "banks": ["core.jsonl", "public_v1.jsonl"],
        "cadence": "monthly",
        "purpose": "月度定量。唯一可以拿去做跨版本对比的档位。",
        "baseline_rounds": 1,
    },
    "monitor": {
        "key": "monitor",
        "label": "监控档",
        "items": 119,
        # 日常监控每题只跑 1 次。题量换分辨率：117 道计分题 x 1 次的
        # 计分请求数是 12 道 x 3 次的九倍多，最小可检出退化从 15.4 分
        # 降到约 8.6 分，且覆盖 19 个维度而非 4 个。
        "trials": 1,
        "banks": ["core.jsonl", "public_v1.jsonl"],
        "cadence": "daily",
        "purpose": ("日频哨兵，每题 1 次。按维度配额选题，覆盖 19 个维度；"
                    "其中多数题的轮间 SD 未实测，假告警率无测量约束，"
                    "该题数在报告与结论卡中写出。"),
        "baseline_rounds": 5,
    },
    "probe": {
        "key": "probe",
        "label": "全量探针",
        # 253 = bank_rev 1.1.0 的题数。这个数**会随题库版本变**，
        # 而注册表是硬编码的 —— 所以 tools/check_all.py 里有一条
        # 专门核对「注册表 vs 实际档案」的检查，防止它悄悄过期。
        # 曾经写着 287（0.2.0 的题数），退役 34 道之后就不对了。
        "items": 253,
        "trials": 3,
        "banks": ["core.jsonl", "public_v1.jsonl"],
        "cadence": "adhoc",
        "purpose": ("跑全部题，用来**测题**而不是测模型：算每题的通过率与"
                    "跨模型极差，供定版题库时使用。"
                    "只能 eval，不产基线。"),
        "baseline_rounds": 0,
    },
}

ORDER = ["monitor", "small", "medium", "large", "probe"]


class TierError(Exception):
    pass


def get(key):
    if key not in TIERS:
        raise TierError("档位只有 %s，收到 %r" % ("/".join(ORDER), key))
    return TIERS[key]


# --------------------------------------------------------------------------
# 分辨率
# --------------------------------------------------------------------------

def sigma(n_requests):
    """事前估计的总分标准差（0-100 刻度）。"""
    return SIGMA_K / math.sqrt(n_requests) if n_requests > 0 else float("inf")


def min_detectable(n_requests):
    """最小可检出退化 = 2 sigma。低于这个幅度的变化，本档看不见。"""
    return 2.0 * sigma(n_requests)


def ci_half(n_requests):
    """单次测评总分的 95% 置信区间半宽。"""
    return Z95 * sigma(n_requests)


def dim_threshold(n_items, trials=3):
    """某个维度的最小可检出退化（0-100 刻度）。

    m 道题 x trials 次 = n 次采样，2*sigma = 2 * SIGMA_K / sqrt(m*trials)。
    trials=3 时化简为 53.50 / sqrt(m)。
    """
    n = max(n_items * trials, 1)
    return 2.0 * SIGMA_K / math.sqrt(n)


#: 维度展示规则。题太少的维度画出来就是在展示噪声。
DIM_HIDE = 6        # m < 6：不展示，只在题库页列出
DIM_DASHED = 12     # 6 <= m < 12：画虚线，标注最小可检出


def dim_display(n_items, trials=3):
    """返回 (是否展示, 线型, 一句话说明)。

    trials 必须传真实的采样次数：阈值是 2*sigma/sqrt(m*trials)，
    每题只跑 1 次时单题得分是 0/1，比 3 次的均值抖得多。
    用默认值 3 去读一轮 1 次采样的结果，阈值会低估 sqrt(3) 倍，
    于是维度级的「显著下降」频繁误报。
    """
    t = dim_threshold(n_items, trials)
    if n_items < DIM_HIDE:
        return False, "none", ("m=%d，最小可检出 %.1f 分——比多数模型之间的"
                               "真实差距还大，画出来只是噪声" % (n_items, t))
    if n_items < DIM_DASHED:
        return True, "dashed", "m=%d，最小可检出 %.1f 分，只能看趋势不能下判定" % (n_items, t)
    return True, "solid", "m=%d，最小可检出 %.1f 分，可对本维度单独下判定" % (n_items, t)


# --------------------------------------------------------------------------
# 结论许可
# --------------------------------------------------------------------------

def permissions(tier_key, n_items, n_requests, dim_counts=None):
    """这一档跑完之后，允许说什么、禁止说什么。

    返回 {"allow": [...], "deny": [...]}，直接渲染进结论卡的许可层。
    这不是提示文案，是从题量和采样数算出来的。
    """
    t = get(tier_key)
    md = min_detectable(n_requests)
    ci = ci_half(n_requests)
    allow, deny = [], []

    allow.append("在本题库 %s 上的得分及其 ±%.1f 分区间" % (t["label"], ci))
    allow.append("与**同一模型、同一 bank_rev**的历史分数比较")

    if md <= 5.0:
        allow.append("对总分下「退化了 / 没退化」的判定（可检出 %.1f 分以上）" % md)
    else:
        deny.append("不能说「没有退化」——本档只能发现 %.1f 分以上的变化，"
                    "更小的退化在这里是不可见的" % md)

    deny.append("不能报成「这个模型 %s 分」这样的裸点估计——±%.1f 比多数"
                "模型之间的真实差距还宽" % ("XX", ci))
    deny.append("不能和另一个 bank_rev 的分数比（工具会直接拒绝，不是警告）")
    deny.append("不能和别的模型比出「谁更好」——那需要同日、同题、同参数的"
                "横向对照，不是本工具的输出")

    if tier_key != "large":
        deny.append("不能用于跨版本对比（模型换代）——只有大档的分辨率够")

    if dim_counts:
        weak = sorted(d for d, m in dim_counts.items() if m < DIM_HIDE)
        if weak:
            deny.append("不能对这些维度下任何判定（题数 <%d）：%s"
                        % (DIM_HIDE, "、".join(weak)))
    return {"allow": allow, "deny": deny,
            "min_detectable": md, "ci_half": ci,
            "sigma_prior": sigma(n_requests)}


def plan(tier_key, n_items=None, trials=None):
    """事前规划：这一档要发多少请求、能看见多大的退化。"""
    t = get(tier_key)
    items = n_items if n_items is not None else t["items"]
    tr = trials if trials is not None else t["trials"]
    n = items * tr
    return {
        "tier": tier_key,
        "label": t["label"],
        "items": items,
        "trials": tr,
        "requests": n,
        "sigma": sigma(n),
        "min_detectable": min_detectable(n),
        "ci_half": ci_half(n),
        "baseline_rounds": t["baseline_rounds"],
        "baseline_requests": n * t["baseline_rounds"],
        "purpose": t["purpose"],
    }


def table():
    """三档对照表，给 --help 和界面用。"""
    return [plan(k) for k in ORDER]
