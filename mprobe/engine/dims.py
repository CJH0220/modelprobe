# -*- coding: utf-8 -*-
"""维度代号的唯一真源。

三个命名空间，互不重叠
----------------------
分开是因为它们回答的问题不同，混在一起会让「总分」失去意义：

    SMOKE      渠道自检。它不测能力，测的是「这条链路通不通」。
               它必须最先跑、必须便宜，且**不进能力总分**——
               冒烟题人人满分，混进总分只会把差异稀释掉。
    BEHAVIOR   自建行为题。测的是模型「怎么做事」：会不会瞎编、
               会不会被劫持、会不会谄媚。这类东西公开榜单基本不测。
    CAPABILITY 公开基准题。测的是「会不会做」：知识、推理、数学、代码。

历史包袱（两处，必须记住为什么这么改）
--------------------------------------
1. **`I` 一码两义。** 自建库里 `I` = 中文，公开基准题源里 `I` = 指令遵循。
   两边合库时会静默合并成一个维度，趋势图上是一条看着很正常的线。
   处理：自建的中文改为 `LG`（language generation），
   指令遵循沿用公开命名空间的 `IF`。**`I` 永久退役，不再分配给任何维度。**
2. **`DA` 未登记。** 05 里出现过但从没在维度表里注册，
   于是它的题目在报告里显示为一个光秃秃的字母。这里补登记。

`resolve()` 对未登记的代号**抛异常**而不是返回空串。旧实现返回空串，
结果是加错维度代号的题目一路跑到报告里才被发现——那时钱已经花掉了。
"""


class DimError(Exception):
    pass


SMOKE = {
    "S": "冒烟",
}

BEHAVIOR = {
    "A": "约束求解",
    "B": "长上下文",
    "C": "推理与符号",
    "D": "幻觉",
    "E": "知识时效",
    "F": "安全边界",
    "G": "抗谄媚",
    "H": "代码",
    "J": "结构化",
    "K": "自认知",
    "L": "鲁棒性",
    "M": "指令劫持",
    "N": "多轮状态",
    "O": "歧义澄清",
    "P": "思维链",
    "LG": "中文生成",
}

CAPABILITY = {
    "KN": "知识",
    "RS": "推理",
    "MA": "数学",
    "CS": "常识",
    "TR": "真实性",
    "CD": "代码",
    "RC": "阅读理解",
    "LC": "长上下文",
    "IF": "指令遵循",
    "DA": "数据分析",
    "LA": "语言",
    "TPL": "专业模板",
}

# 为什么 LA 和 LG 必须分开（别再合回去）：
#   LG「中文生成」是行为维，题是简繁转写这类中文语体问题（core 的 I2）。
#   LA「语言」是能力维，题是 LiveBench connections —— **全英文**的
#   16 词分组谜题（公开基准题源那 30 道）。
# MEASUREMENT.md 5.1 原本提议把 05 的 I 改名到 LG，但那会把
# 「一码两义」原样搬到新代号上：报告会打出一个维度名，下面挂两种
# 完全不同的题。所以 LG 只留给中文生成，公开题的语言类走 LA。

#: 退役代号。曾经用过、现在禁止使用，写进题库直接报错并说明原因。
RETIRED = {
    "I": "一码两义（自建=中文 / 公开=指令遵循）。中文用 LG，指令遵循用 IF",
}

ALL = {}
ALL.update(SMOKE)
ALL.update(BEHAVIOR)
ALL.update(CAPABILITY)

NAMESPACE = {}
NAMESPACE.update({d: "smoke" for d in SMOKE})
NAMESPACE.update({d: "behavior" for d in BEHAVIOR})
NAMESPACE.update({d: "capability" for d in CAPABILITY})

NS_LABEL = {"smoke": "冒烟", "behavior": "行为", "capability": "能力"}


def resolve(dim):
    """校验并返回中文名。未登记 / 已退役都抛 DimError。"""
    if dim in ALL:
        return ALL[dim]
    if dim in RETIRED:
        raise DimError("维度代号 %r 已退役：%s" % (dim, RETIRED[dim]))
    raise DimError("维度代号 %r 未登记。已登记：%s"
                   % (dim, "、".join(sorted(ALL))))


def name(dim):
    """只取名字，不校验。给已经过校验的数据用（报告、界面）。"""
    return ALL.get(dim, "")


def label(dim, sep=" "):
    n = ALL.get(dim)
    return "%s%s%s" % (dim, sep, n) if n else str(dim)


def describe(dim):
    n = ALL.get(dim)
    return "%s（%s）" % (dim, n) if n else str(dim)


def namespace(dim):
    return NAMESPACE.get(dim, "unknown")


def in_score(dim):
    """是否计入能力总分。冒烟维不计——见模块开头。"""
    return NAMESPACE.get(dim) in ("behavior", "capability")
