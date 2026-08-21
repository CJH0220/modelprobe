# -*- coding: utf-8 -*-
"""监控层。

和测评的区别只有一句话：**测评是快照，监控是序列。**

测评回答「这个模型什么水平」，跑完就结束。
监控回答「它和以前一样吗」，因此多了三样东西：

    baseline  基线 —— 所有判定的锚点
    judge     三态判定 —— 正常 / 观察 / 告警
    schedule  节奏 —— 什么时候该跑
    notify    推送 —— 什么时候该吵醒人

贯穿这一层的取向：**允许漏报，不允许频繁误报。**
这与直觉相反，但监控系统只有一种死法——告警三次不准之后没人再看它。
"""

from . import baseline, judge, notify, schedule

__all__ = ["baseline", "judge", "notify", "schedule"]
