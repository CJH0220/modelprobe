# -*- coding: utf-8 -*-
"""告警推送。可选，未配置时静默记录不报错。

推送策略默认 abnormal（只推观察和告警）
--------------------------------------
每天推一条「一切正常」，两周之后没人会看那条消息。
等真出事的时候，那条也一样被划过去。这不是理论——这是所有
「每日健康报告」邮件的结局。

URL 打码
--------
飞书和 Slack 的 webhook token 在**路径里**，不在参数里：

    https://open.feishu.cn/open-apis/bot/v2/hook/<这一段就是密钥>

所以显示时只保留 netloc + 前两段路径，剩下的一律打码。
只打 query 参数是不够的。
"""

import json
import urllib.error
import urllib.request
from urllib.parse import urlparse

from .judge import ALERT, STATE_ICON, STATE_LABEL, WATCH

POLICIES = {
    "abnormal": "只推观察和告警（默认）",
    "alert_only": "只推告警",
    "always": "每轮都推",
    "never": "不推",
}
DEFAULT = {"webhook": "", "policy": "abnormal", "enabled": False,
           "timeout": 10}


def mask(url):
    """webhook URL 打码。永远不返回可用的 URL。"""
    if not url:
        return "（未配置）"
    try:
        u = urlparse(url)
    except Exception:
        return "（无法解析的 URL）"
    segs = [s for s in (u.path or "").split("/") if s]
    keep = "/".join(segs[:2])
    tail = "/***" * max(0, len(segs) - 2)
    return "%s://%s/%s%s" % (u.scheme or "https", u.netloc or "?", keep, tail)


def should_send(policy, state):
    if policy == "never":
        return False
    if policy == "always":
        return True
    if policy == "alert_only":
        return state == ALERT
    return state in (WATCH, ALERT)          # abnormal


def build_text(ctx, state, message, detail, steps=None):
    """纯文本消息体。故意不带链接——收到的人第一反应应该是去看命令行输出，
    而不是点一个可能已经失效的本地地址。"""
    L = ["%s 模型监控 · %s" % (STATE_ICON.get(state, "?"),
                              STATE_LABEL.get(state, state)),
         "模型：%s（%s）" % (ctx["model_key"], ctx["endpoint_sha"]),
         "题库：%s ｜ 档位：%s" % (ctx["bank_rev"], ctx.get("tier") or "-"),
         ""]
    if detail:
        L.append("本轮 %.1f ｜ 基线 %.1f ｜ 阈值 %.1f"
                 % (detail.get("mean", 0) + detail.get("delta", 0),
                    detail.get("mean", 0), detail.get("threshold", 0)))
    L.append(message)
    if state == ALERT and steps:
        L += ["", "排查："] + ["%d. %s" % (i + 1, s.replace("**", ""))
                               for i, s in enumerate(steps)]
    return "\n".join(L)


def send(cfg, text):
    """返回 (是否发送, 说明)。**任何失败都不抛异常**——
    推送挂了不该让监控本身失败，那是本末倒置。"""
    cfg = dict(DEFAULT, **(cfg or {}))
    if not cfg.get("enabled"):
        return False, "推送未启用"
    url = (cfg.get("webhook") or "").strip()
    if not url:
        return False, "未配置 webhook"
    if not url.lower().startswith("https://"):
        return False, "拒绝发送：webhook 不是 https（%s）" % mask(url)

    body = json.dumps({"msg_type": "text", "content": {"text": text}},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=cfg.get("timeout", 10)) as r:
            return True, "已推送到 %s（HTTP %d）" % (mask(url), r.status)
    except urllib.error.HTTPError as e:
        return False, "推送失败 HTTP %s（%s）" % (e.code, mask(url))
    except Exception as e:
        return False, "推送失败：%s（%s）" % (type(e).__name__, mask(url))


def maybe_send(cfg, ctx, state, message, detail, steps=None):
    """返回 (是否发送, 说明)。**永不抛异常。**

    顺序很重要：先判「要不要发」，再判「发得出去吗」，**最后**才拼文本。
    早先的顺序是 policy → build_text → send，于是推送根本没配的时候
    也会先去拼一遍文本 —— 而 build_text 要读 ctx 的多个字段，
    ctx 缺字段就抛 KeyError。
    后果不是「少发一条通知」，而是**一次已经花钱跑完、已经出了判定的
    check 死在推送这一步上**，判定结果跟着丢。本末倒置。
    """
    cfg = dict(DEFAULT, **(cfg or {}))
    if not should_send(cfg.get("policy", "abnormal"), state):
        return False, "按策略 %s 不推送 %s" % (cfg.get("policy"),
                                              STATE_LABEL.get(state, state))
    if not cfg.get("enabled"):
        return False, "推送未启用"
    if not (cfg.get("webhook") or "").strip():
        return False, "未配置 webhook"
    try:
        text = build_text(ctx, state, message, detail, steps)
    except Exception as e:
        return False, "拼推送文本失败：%s: %s" % (type(e).__name__, e)
    return send(cfg, text)
