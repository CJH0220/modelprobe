# -*- coding: utf-8 -*-
"""端点配置：一次测评到底打到了哪里。

这个模块存在的理由，是需求原文里那句「读取当前模型名称」做不到
--------------------------------------------------------------
MCP 服务器是宿主拉起的子进程，它没有可靠途径知道是哪个模型在驱动对话；
就算知道也没用，因为**测的是 API 端点，不是当前会话**。用户在 Claude Code
里跟 Opus 说话，配置里指的可能是 deepseek 端点。默认成「当前模型」会
造成最难查的一类事故：跑得很顺，结论是错的。

所以这里的规矩是：端点必须显式选择，且**每次运行都在输出里回显
实际打到的 base_url + model**。

endpoint_sha
------------
同一个模型配两遍、temperature 悄悄漂了，是实际发生过的事。分数会跟着变，
但趋势图上看不出任何异常——它只会显示「模型退化了」。

所以把决定输出分布的四个参数哈希成一个短指纹，写进每一行结果。
指纹不同就是**另一条序列**，工具会拒绝把它们连成一条线。
"""

import copy
import hashlib
import json
import os

from .. import paths
from . import secrets

DEFAULT_RUN = {
    "trials": 3,            # 每题采样次数。低于 3 无法反映方差
    "concurrency": 4,
    "retries": 3,
    "retry_backoff": 2.0,
    "qps": 0,               # >0 时限速；0 表示不限（不代表对方不限）
    "max_retry_wait": 120,
    "fail_score": 0.0,
}

DEFAULT_MODEL = {
    "api_style": "openai",  # openai | anthropic
    "proxy": "auto",
    "temperature": 0.0,
    # 16000 而不是 4096：推理模型会把大量 token 花在思考上。4096 下实测出现过
    # 32 次「finish_reason=length 且无可见输出」，那些题被记 0 分，
    # 但模型其实会做。max_tokens 是上限不是消耗。
    "max_tokens": 16000,
    "timeout": 120,
    "system": "",
    "extra_headers": {},
    "extra_body": {},
}

DEFAULT_PRICING = {
    "input_per_mtok": None,
    "output_per_mtok": None,
    "cached_input_per_mtok": None,
    "currency": "CNY",
}


class EndpointError(Exception):
    pass


def _merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            out[k] = _merge(base[k], v)
        else:
            out[k] = v
    return out


def endpoint_sha(m):
    """决定输出分布的四个参数的短指纹。

    只取这四个是刻意的：改 concurrency 或 timeout 不影响模型给出什么，
    把它们算进去会让「调快一点」看起来像换了个模型。
    """
    raw = "|".join([
        str(m.get("base_url", "")).rstrip("/"),
        str(m.get("model", "")),
        repr(m.get("temperature")),
        repr(m.get("max_tokens")),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def list_all():
    """列出全部已配置端点（不解析密钥，不联网）。给 config list 和界面用。"""
    out = []
    for key in paths.list_model_keys():
        try:
            raw = _read(key)
        except EndpointError as e:
            out.append({"key": key, "error": str(e)})
            continue
        m = _merge(DEFAULT_MODEL, raw.get("model") or {})
        env = (raw.get("model") or {}).get("api_key_env")
        out.append({
            "key": key,
            "label": raw.get("label") or key,
            "base_url": m.get("base_url"),
            "model": m.get("model"),
            "api_style": m.get("api_style"),
            "temperature": m.get("temperature"),
            "max_tokens": m.get("max_tokens"),
            "endpoint_sha": endpoint_sha(m),
            "default": bool(raw.get("default")),
            "key_env": env,
            "key_status": secrets.status_of(env) if env else None,
        })
    return out


def default_key():
    for e in list_all():
        if e.get("default"):
            return e["key"]
    keys = paths.list_model_keys()
    return keys[0] if len(keys) == 1 else None


def _read(key):
    p = paths.model_config(key)
    if not os.path.isfile(p):
        known = paths.list_model_keys()
        raise EndpointError(
            "找不到端点配置 %s。已配置：%s\n新增：mprobe config add"
            % (key, "、".join(known) if known else "（一个都没有）"))
    with open(p, "r", encoding="utf-8-sig") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            raise EndpointError("%s 不是合法 JSON: %s" % (p, e))


def _load_fx():
    """读 config/pricing.json 的 fx 表。读不到就返回 {}，由调用方兜底。"""
    p = os.path.join(paths.CONFIG, "pricing.json")
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            return (json.load(f) or {}).get("fx") or {}
    except (OSError, json.JSONDecodeError):
        # 汇率读不出来不该拖垮整轮测评：折算只影响那行提示，
        # 原币种的金额和成本闸门都还在。
        return {}


def load(key, need_key=True):
    """加载并校验一个端点。need_key=False 时跳过密钥解析（只看配置）。"""
    raw = _read(key)
    cfg = {
        "key": key,
        "label": raw.get("label") or key,
        "default": bool(raw.get("default")),
        "model": _merge(DEFAULT_MODEL, raw.get("model")),
        "run": _merge(DEFAULT_RUN, raw.get("run")),
        "pricing": _merge(DEFAULT_PRICING, raw.get("pricing")),
        "note": raw.get("note", ""),
    }
    # 汇率表是**全局**的，不该在每个端点里重复一份 ——
    # 同一个 USD 汇率抄三遍，迟早有一份忘了改。
    # 它只从 config/pricing.json 读，永不联网取。
    fx = _load_fx()
    if fx:
        cfg["pricing"].setdefault("fx", fx)
    m = cfg["model"]
    for f in ("base_url", "model"):
        if not m.get(f):
            raise EndpointError("%s: model.%s 未配置" % (key, f))
    if m["api_style"] not in ("openai", "anthropic"):
        raise EndpointError("%s: api_style 只支持 openai / anthropic，当前 %r"
                            % (key, m["api_style"]))
    if cfg["run"]["trials"] < 1:
        raise EndpointError("%s: run.trials 至少为 1" % key)

    env = m.get("api_key_env")
    if not env:
        raise EndpointError(
            "%s: model.api_key_env 未配置。密钥用**变量名**引用，"
            "不直接写进配置文件——config/models/ 是要进版本管理的。" % key)

    if need_key:
        val, src = secrets.resolve(env)
        if not val:
            raise EndpointError(
                "密钥 %s 未设置。三选一：\n"
                "  PowerShell   $env:%s = \"你的key\"\n"
                "  持久（计划任务必须用这个）\n"
                "               [Environment]::SetEnvironmentVariable(\"%s\",\"你的key\",\"User\")\n"
                "  本地文件     mprobe config key --model %s\n"
                "注意：已经打开的终端看不到刚设的用户级变量，要重开一个。"
                % (env, env, env, key))
        m["_api_key"] = val
        m["_api_key_source"] = src

    cfg["endpoint_sha"] = endpoint_sha(m)
    cfg["_warnings"] = _warnings(cfg)
    return cfg


def _warnings(cfg):
    w = []
    if cfg["run"]["trials"] < 3:
        w.append("trials=%d：单次或两次采样反映不了方差，判定会不稳。建议 3。"
                 % cfg["run"]["trials"])
    p = cfg["pricing"]
    if p.get("input_per_mtok") is None or p.get("output_per_mtok") is None:
        w.append("未配置价格，本次不计算成本。要看性价比就把 pricing 填上。")
    if cfg["model"].get("temperature") not in (0, 0.0, None):
        w.append("temperature=%s 不为 0：同样的题每次可能给出不同答案，"
                 "轮间方差会变大，阈值随之放宽（也就是更迟钝）。"
                 % cfg["model"]["temperature"])
    return w


def redact(cfg):
    """去掉密钥的可存档副本。任何写盘、回显都必须先过这里。"""
    c = copy.deepcopy(cfg)
    c.get("model", {}).pop("_api_key", None)
    c.get("model", {}).pop("_api_key_source", None)
    return c


def echo(cfg):
    """一行「到底打到哪去了」。每次运行都必须打印。"""
    m = cfg["model"]
    return "%s  ->  %s  model=%s  temp=%s  max_tokens=%s  [sha %s]" % (
        cfg["key"], m["base_url"], m["model"],
        m.get("temperature"), m.get("max_tokens"), cfg["endpoint_sha"])
