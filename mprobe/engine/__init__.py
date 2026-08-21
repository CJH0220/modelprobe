# -*- coding: utf-8 -*-
"""测量层。这一层不知道 CLI、MCP、界面的存在，也不打印任何东西。

模块职责：

    dims       维度码表（三命名空间，未登记即报错）
    bank       题库加载 + 冻结校验（MANIFEST sha256）
    endpoint   端点配置与密钥解析、端点指纹
    checkers   22 个自动判分器
    client     HTTP 客户端（openai / anthropic，限速与重试）
    runner     并发执行
    metrics    逐题 / 逐维聚合、Wilson 区间、校准
    aggregate  Beta-Binomial 收缩、保守分、短板分
    cost       token 计价
    signals    输出特征（拒答、免责声明、啰嗦度）
    progress   进度文件的原子写与读
    report     产物落盘
"""

from . import (aggregate, bank, checkers, client, cost, dims, endpoint,
               metrics, progress, report, runner, signals)

__all__ = ["aggregate", "bank", "checkers", "client", "cost", "dims",
           "endpoint", "metrics", "progress", "report", "runner", "signals"]
