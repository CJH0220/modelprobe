# -*- coding: utf-8 -*-
"""执行器：把「题目 x 采样次数」铺平成任务队列并发执行。

两条硬规矩，直接对应题库的方法论：

  1. **一题一会话**——每个任务都新建消息列表，绝不携带其他题目的历史。
     复用上下文会让后面的题变简单（模型已经知道你在考它什么），
     分数会随题目顺序变化，而顺序是实现细节。
  2. **每题多次采样**——难题方差大，单次结果不作数。

一条容易踩的坑写在这里
----------------------
请求失败和判分为 0 必须分开记。请求失败记 None（不参与统计），
判分为 0 才拉低分数。混在一起的话，网关抖一下就会显示成「模型变笨了」。
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import bank, checkers


def run(items, client, cfg, progress=None, on_record=None):
    """执行全部题目，返回逐次采样的原始记录列表。

    progress: engine.progress.Progress，可为 None
    on_record: 每条记录完成后回调（用于流式落盘 raw.jsonl）
    """
    trials = cfg["run"]["trials"]
    system = cfg["model"].get("system") or ""

    tasks = [(it, t) for it in items for t in range(1, trials + 1)]
    results = []
    lock = threading.Lock()

    def work(item, trial):
        t0 = time.time()
        msgs = bank.to_messages(item, system)     # 每次都是全新的消息列表
        text, meta = client.chat(msgs)
        rec = {
            "id": item["id"],
            "dim": item["dim"],
            "tier": item.get("tier"),
            "title": item.get("title", ""),
            "weight": float(item.get("weight", 1.0)),
            "observe": bool(item.get("observe")),
            "trial": trial,
            "response": text,
            "ok": bool(meta.get("ok")),
            "error": meta.get("error"),
            "latency_ms": meta.get("latency_ms"),
            "completion_tokens": meta.get("completion_tokens"),
            "prompt_tokens": meta.get("prompt_tokens"),
            "cached_tokens": meta.get("cached_tokens"),
            "reasoning_tokens": meta.get("reasoning_tokens"),
            "finish_reason": meta.get("finish_reason"),
            "attempts": meta.get("attempts"),
            "started_at": t0,
        }
        truncated = (meta.get("finish_reason") == "length")

        if not meta.get("ok"):
            # 请求失败 -> 无分数。**不是 0 分**：0 分意味着模型答错了。
            rec["score"] = None
            rec["detail"] = "请求失败：%s" % meta.get("error")
        elif truncated and not (text or "").strip():
            # 推理模型把额度全烧在思考上、没留下可见答案时会走到这里。
            # 记 0 分，但要说清原因——只报「空输出」会让人以为模型不会做。
            rec["score"] = 0.0
            rec["detail"] = ("输出被 max_tokens 截断且无可见内容"
                             "（finish_reason=length，用了 %s token）。"
                             "推理模型请调大 max_tokens 后重测。"
                             % meta.get("completion_tokens"))
        else:
            try:
                score, detail = checkers.run_check(text, item.get("check"))
            except checkers.CheckerError as e:
                score, detail = None, "判分器配置错误：%s" % e
            rec["score"], rec["detail"] = score, detail
            if truncated and score is not None and score < 1:
                rec["detail"] = "[输出被截断] " + (detail or "")

        rec["truncated"] = truncated
        rec["confidence"] = checkers.extract_confidence(text)

        with lock:
            results.append(rec)
            if on_record:
                try:
                    on_record(rec)
                except Exception:
                    pass                          # 落盘失败不该中断测评
            if progress:
                progress.tick("%s#%d" % (item["id"], trial))
        return rec

    with ThreadPoolExecutor(max_workers=cfg["run"]["concurrency"]) as ex:
        futs = [ex.submit(work, it, t) for it, t in tasks]
        for f in as_completed(futs):
            exc = f.exception()
            if exc:                               # 兜底：单任务异常不中断整轮
                with lock:
                    results.append({
                        "id": "?", "dim": "?", "trial": 0, "ok": False,
                        "error": "任务异常: %r" % exc, "score": None,
                        "weight": 1.0, "detail": "任务异常", "truncated": False,
                    })
                    if progress:
                        progress.tick("EXC")

    order = {it["id"]: i for i, it in enumerate(items)}
    results.sort(key=lambda r: (order.get(r["id"], 10 ** 6), r.get("trial", 0)))
    return results


def health(records):
    """整轮的请求健康度。判定前必须看这个。

    失败率高的时候分数是没有意义的：跑了 150 条挂了 40 条，
    剩余记录算出的分数既不代表该模型的水平，也不可与历史轮次比较。
    """
    n = len(records)
    failed = sum(1 for r in records if not r.get("ok"))
    scored = sum(1 for r in records if r.get("score") is not None)
    rate = failed / n if n else 0.0
    if rate == 0:
        verdict, note = "ok", "全部请求成功"
    elif rate < 0.05:
        verdict, note = "ok", "失败 %d/%d，在正常抖动范围内" % (failed, n)
    elif rate < 0.20:
        verdict, note = "degraded", (
            "失败 %d/%d（%.0f%%）。分数偏低可能是渠道问题而非模型问题，"
            "换个时段重跑一次再下结论。" % (failed, n, rate * 100))
    else:
        verdict, note = "unusable", (
            "失败 %d/%d（%.0f%%）。**本轮结果不可用**——"
            "先查渠道、限速和密钥，不要拿这个分去比较。" % (failed, n, rate * 100))
    return {"requests": n, "failed": failed, "scored": scored,
            "fail_rate": rate, "verdict": verdict, "note": note}
