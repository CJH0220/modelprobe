# -*- coding: utf-8 -*-
"""进度：单一写者，多个读者。

为什么要有这个文件
------------------
MCP 工具调用是**阻塞**的——工具返回之前，模型什么都看不到。
所以进度不可能「流」给会话里的模型，只能落盘让它轮询。

三个读者看到的东西不一样，这是对的：

    CLI 前台      逐题刷新的字符进度条
    浏览器        1 秒轮询
    MCP 会话      模型每隔一会儿汇报一次「37/150，预计还有 1.2 分钟」

opus 大档 160 多分钟的量级下，进度条真正的价值不是好看，
是**让人知道它没死**。所以 label 必须写当前题号，
超过 STALL_SEC 没更新就把 state 打成 stalled。
"""

import json
import os
import tempfile
import time

STALL_SEC = 90

PENDING, RUNNING, DONE, FAILED, STALLED = (
    "pending", "running", "done", "failed", "stalled")


class Progress:
    """写者。每完成一题原子替换一次文件。

    原子替换（先写临时文件再 os.replace）不是洁癖：读者是另一个进程，
    直接覆写会让它读到半截 JSON，然后在解析失败时报「运行异常」——
    而运行其实好好的。
    """

    def __init__(self, path, total, run_id, tier=None, model=None):
        self.path = path
        self.total = total
        self.run_id = run_id
        self.tier = tier
        self.model = model
        self.done = 0
        self.cost = 0.0
        self.t0 = time.time()
        self.state = PENDING
        self.error = None
        self.label = ""

    def _write(self):
        el = time.time() - self.t0
        eta = (el / self.done * (self.total - self.done)) if self.done else None
        data = {
            "run_id": self.run_id,
            "state": self.state,
            "done": self.done,
            "total": self.total,
            "label": self.label,
            "tier": self.tier,
            "model": self.model,
            "started_at": self.t0,
            "updated_at": time.time(),
            "elapsed_sec": round(el, 1),
            "eta_sec": round(eta, 1) if eta is not None else None,
            "cost_so_far": round(self.cost, 4),
            "error": self.error,
        }
        d = os.path.dirname(self.path)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return data

    def start(self):
        self.state = RUNNING
        return self._write()

    def tick(self, label="", cost=0.0):
        self.done += 1
        self.label = label
        self.cost += cost or 0.0
        return self._write()

    def finish(self, cost=None):
        self.state = DONE
        if cost is not None:
            self.cost = cost
        return self._write()

    def fail(self, msg):
        self.state = FAILED
        self.error = str(msg)[:500]
        return self._write()


def read(path):
    """读者。文件不存在或损坏都返回 None，不抛异常——读进度不该让调用方崩。"""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
    except Exception:
        return None
    if d.get("state") == RUNNING:
        idle = time.time() - (d.get("updated_at") or 0)
        if idle > STALL_SEC:
            d["state"] = STALLED
            d["stalled_sec"] = round(idle, 1)
            d["error"] = ("已 %.0f 秒没有新进展。可能是某条请求卡在超时里，"
                          "也可能 runner 进程已经没了。"
                          "查 data/runs/%s/raw.jsonl 看最后跑到哪。"
                          % (idle, d.get("run_id")))
    return d


def render_bar(d, width=28):
    """一行字符进度条。给 CLI 前台和 Skill 汇报共用。"""
    if not d:
        return "（还没有进度）"
    done, total = d.get("done") or 0, d.get("total") or 1
    n = int(done / total * width) if total else 0
    eta = d.get("eta_sec")
    return "[%-*s] %d/%d  已用 %s  预计剩余 %s  %s" % (
        width, "#" * n, done, total,
        hms(d.get("elapsed_sec") or 0),
        hms(eta) if eta is not None else "?",
        (d.get("label") or "")[:16])


def hms(s):
    s = int(s or 0)
    if s >= 3600:
        return "%d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60)
    return "%d:%02d" % (s // 60, s % 60)
