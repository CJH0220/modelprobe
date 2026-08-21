# -*- coding: utf-8 -*-
"""只读的本地界面服务。`http.server`，零第三方依赖。

## 只绑回环，且没有开关可以改

`--host` 这个参数**故意不提供**。这台机器上跑着一个能读全部测评历史、
能看到端点配置的服务，绑到 0.0.0.0 就等于把它交给整个网段。
要远程看就 `ssh -L 8790:127.0.0.1:8790`。

把「安全」做成一个默认值，用户随手就能改掉；做成**没有那个参数**，
才是真的关掉了这条路。

## 只读

这一版所有接口都是 GET，不改任何东西（实施计划第 3.7 项 的「浏览器里
增删端点、直接发起测评」放在后面做 —— 一个能写的本地服务需要
先想清楚 CSRF 和确认闸门，不该和只读界面一起草草上线）。
"""

import json
import mimetypes
import os
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .. import BANK_REV, __version__, paths, profiles, tiers
from ..engine import bank as bankmod, dims
from ..monitor import notify
from ..store import db

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

#: 只绑回环。**不要加 host 参数。**
HOST = "127.0.0.1"
DEFAULT_PORT = 8790

#: 超过这么久没更新就把 state 打成 stalled。
#: 依据 DESIGN 第五章：opus 大档几小时的量级下，进度条真正的价值
#: 不是好看，是**让人知道它没死**。
STALL_SEC = 90


# --------------------------------------------------------------------------
# 数据读取：全部只读，一个数都不重算
# --------------------------------------------------------------------------

def _con():
    if not os.path.isfile(paths.DB):
        return None
    return db.connect(paths.DB)


def api_overview(q):
    """最近若干轮 + 趋势序列。

    趋势按 (model, tier, bank_rev, endpoint_sha) 分组 ——
    **四个键任一不同就不是同一条线**。少一个键就会把不可比的点连起来，
    而图看上去完全正常。
    """
    con = _con()
    if con is None:
        return {"empty": True, "runs": [], "series": []}
    limit = int((q.get("limit") or ["40"])[0])
    runs = [dict(r) for r in db.list_runs(con, limit=limit)]

    groups = {}
    for r in runs:
        key = (r["model_key"], r["tier"], r["bank_rev"], r["endpoint_sha"])
        groups.setdefault(key, []).append(r)
    series = []
    for (mk, tier, rev, sha), rs in sorted(groups.items()):
        rs = sorted(rs, key=lambda x: x["started_at"])
        series.append({
            "model": mk, "tier": tier, "bank_rev": rev,
            "endpoint_sha": sha,
            "label": "%s · %s · 题库 %s · 端点 %s" % (mk, tier, rev, sha[:8]),
            "points": [{"run_id": x["run_id"], "t": x["started_at"],
                        "score": x["score"], "conservative": x["conservative"]}
                       for x in rs],
        })
    con.close()
    return {"runs": runs, "series": series,
            "note": ("每条线只包含 模型+档位+题库版本+端点指纹 全同的轮次。"
                     "四者任一不同就另起一条线，**绝不连在一起**。")}


def api_run(q):
    rid = (q.get("run_id") or [""])[0]
    if not rid:
        return {"error": "缺 run_id"}
    d = os.path.join(paths.RUNS, rid)
    out = {"run_id": rid}
    for name in ("summary.json", "progress.json"):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            with open(p, encoding="utf-8-sig") as f:
                out[name.split(".")[0]] = json.load(f)
    rep = os.path.join(d, "report.md")
    if os.path.isfile(rep):
        with open(rep, encoding="utf-8-sig") as f:
            out["report_md"] = f.read()
    if len(out) == 1:
        return {"error": "找不到 run %s" % rid}

    # 逐维加上「能不能画、画什么线型」——**这是硬规矩，不是前端自己决定的**
    s = out.get("summary") or {}
    # 逐题矩阵：把截断标记合并进来。一道题 0 分，是"答错"还是"被截断"，
    # 记账上一模一样，但原因完全不同 —— 前者是能力，后者是配额。
    trunc = set((s.get("runtime") or {}).get("truncated_ids") or [])
    for iid, row in (s.get("items") or {}).items():
        row["truncated"] = iid in trunc
    for d_code, row in (s.get("dims") or {}).items():
        show, style, why = tiers.dim_display(row.get("n_items") or 0)
        row["display"] = {"show": show, "style": style, "why": why,
                          "name": dims.name(d_code),
                          "in_score": dims.in_score(d_code)}
    return out


def api_progress(q):
    """只回进度，给 1 秒轮询用。**不读 summary，不做任何计算。**

    单独一个接口是为了轮询便宜：`/api/run` 会把 report.md 整份读出来
    （几 KB 到几十 KB），1 秒一次地读它没有必要。

    `stalled` 在这里判，不依赖写方：写方要是卡死了，它自然也写不了
    「卡死了」。超过 90 秒没更新就是可能死了，不是慢。
    """
    rid = (q.get("run_id") or [""])[0]
    p = os.path.join(paths.RUNS, rid, "progress.json")
    if not rid or not os.path.isfile(p):
        return {"error": "没有这一轮的进度：%s" % (rid or "(缺 run_id)")}
    try:
        with open(p, encoding="utf-8-sig") as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        # 原子替换的瞬间可能读到半个文件，这不是错误，下一秒就好了
        return {"transient": "%s: %s" % (type(e).__name__, e)}
    import time as _t
    age = _t.time() - (d.get("updated_at") or 0)
    d["age_sec"] = age
    if d.get("state") == "running" and age > STALL_SEC:
        d["state"] = "stalled"
        d["stall_note"] = ("超过 %d 秒没有更新（实际 %.0f 秒）——"
                           "可能已经死了，不是慢" % (STALL_SEC, age))
    return d


def api_monitor(q):
    con = _con()
    if con is None:
        return {"empty": True}
    bls, checks = [], []
    try:
        for r in con.execute(
                "select * from baselines where active = 1 "
                "order by created_at desc"):
            b = dict(r)
            b.pop("detail", None)          # detail 是整份 JSON，界面用不到
            bls.append(b)
        for r in con.execute(
                "select * from checks order by created_at desc limit 60"):
            checks.append(dict(r))
    finally:
        con.close()
    cfg = dict(notify.DEFAULT)
    return {"baselines": bls, "checks": checks,
            "notify": {"enabled": bool(cfg.get("enabled")),
                       "policy": cfg.get("policy"),
                       # **只显示打码后的 URL**，token 在路径里
                       "webhook": notify.mask(cfg.get("webhook") or "")},
            "note": ("provisional 为真表示轮数不足 5 的临时基线，"
                     "告警条件收紧为连续三轮。")}


def api_models(q):
    """端点列表。**永不返回明文密钥。**"""
    from ..engine import endpoint
    out = []
    for e in endpoint.list_all():
        ks = e.get("key_status") or {}
        out.append({
            "key": e.get("key"), "label": e.get("label"),
            "model": (e.get("model") or {}).get("model")
            if isinstance(e.get("model"), dict) else e.get("model"),
            "base_url": e.get("base_url"),
            "endpoint_sha": e.get("endpoint_sha"),
            "default": e.get("default"),
            # 只给掩码和来源，绝不给明文
            "key_set": bool(ks.get("set")),
            "key_masked": ks.get("masked"),
            "key_source": ks.get("source_label") or ks.get("source"),
        })
    return {"endpoints": out,
            "note": "界面永不显示密钥明文，只显示掩码与来源。"}


def api_bank(q):
    mf = bankmod.load_manifest(paths.BANKS, expect_rev=BANK_REV)
    items = mf.get("items") or {}
    per = {}
    for iid, v in items.items():
        d = per.setdefault(v["dim"], {"dim": v["dim"], "name": dims.name(v["dim"]),
                                      "n": 0, "black": 0, "monitor_ok": 0,
                                      "in_score": dims.in_score(v["dim"])})
        d["n"] += 1
        if v.get("robustness") == "black":
            d["black"] += 1
        if v.get("monitor_ok"):
            d["monitor_ok"] += 1
    for d in per.values():
        show, style, why = tiers.dim_display(d["n"])
        d["display"] = {"show": show, "style": style, "why": why}
    blocked = {}
    for v in items.values():
        if not v.get("monitor_ok"):
            blocked[v.get("monitor_block") or "未标注"] = \
                blocked.get(v.get("monitor_block") or "未标注", 0) + 1
    return {"bank_rev": mf.get("bank_rev"), "created_at": mf.get("created_at"),
            "files": mf.get("files"), "n_items": len(items),
            "dims": sorted(per.values(), key=lambda x: -x["n"]),
            "monitor_blocked": blocked, "notes": mf.get("notes")}


def api_tiers(q):
    out = []
    for k in tiers.ORDER:
        row = dict(tiers.get(k))
        try:
            p = profiles.resolve(k)
            row.update({"n_items": p["n_items"],
                        "scored_requests": p["scored_requests"],
                        "min_detectable": p["min_detectable"],
                        "dim_counts": p["dim_counts"]})
        except Exception as e:
            row["error"] = str(e)[:200]
        out.append(row)
    return {"tiers": out, "bank_rev": BANK_REV}


ROUTES = {
    "/api/overview": api_overview,
    "/api/run": api_run,
    "/api/progress": api_progress,
    "/api/monitor": api_monitor,
    "/api/models": api_models,
    "/api/bank": api_bank,
    "/api/tiers": api_tiers,
}


# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "mprobe/" + __version__

    def log_message(self, fmt, *a):
        pass                     # 不往 stderr 刷访问日志

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 这是本地只读界面，不该被任何页面嵌入或跨站读取
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)

        if path in ROUTES:
            try:
                data = ROUTES[path](q)
            except Exception as e:
                data = {"error": "%s: %s" % (type(e).__name__, e)}
            self._send(200, json.dumps(data, ensure_ascii=False,
                                       default=str).encode("utf-8"),
                       "application/json; charset=utf-8")
            return

        # 静态文件。**不允许跳出 static 目录**
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC, rel))
        if not full.startswith(os.path.abspath(STATIC)):
            self._send(403, b"forbidden", "text/plain; charset=utf-8")
            return
        if not os.path.isfile(full):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)

    def do_POST(self):
        # 这一版是只读的。写操作（增删端点、发起测评）见 实施计划第 3.7 项 ——
        # 一个能写的本地服务要先想清楚 CSRF 和确认闸门。
        self._send(405, json.dumps(
            {"error": "本界面是只读的。发起测评请用命令行或 MCP。"},
            ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8")


class Server(ThreadingHTTPServer):
    """**关掉端口复用。**

    `ThreadingHTTPServer` 默认 `allow_reuse_address = True`，
    而 Windows 的 `SO_REUSEADDR` 允许**抢占一个正在监听的端口** ——
    实测踩到：8791 上还挂着几天前忘了关的 `mock_server.py`，
    新服务"绑成功"了，但请求全被旧进程接走，返回 501。
    表现是「界面起来了、打开是空的」，而没有任何一处报错。

    宁可起不来并说清「端口被占」，也不要悄悄和别人共享一个端口。
    """
    allow_reuse_address = False
    daemon_threads = True


def _free_port(start, tries=20):
    """真的把服务建起来试试，而不是用一个临时 socket 探一下。

    临时 socket 探测在这里不可靠：探测用的 socket 和真正的 server
    socket 选项不同（复用、监听队列），探得通不等于建得起来。
    返回 (server, port)。
    """
    last = None
    for p in range(start, start + tries):
        try:
            return Server((HOST, p), Handler), p
        except OSError as e:
            last = e
            continue
    raise SystemExit("从 %d 起连续 %d 个端口都被占用了：%s"
                     % (start, tries, last))


def serve(port=DEFAULT_PORT, open_browser=True):
    httpd, port = _free_port(port)
    url = "http://%s:%d" % (HOST, port)
    print("界面已起：%s" % url)
    print("只绑 %s —— 要远程看请用 ssh -L %d:%s:%d" % (HOST, port, HOST, port))
    print("Ctrl-C 停止")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0
