#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""端到端测验：边界与异常路径。**零请求、零花费。**

前面几个自检脚本查的是「该拒绝的拒绝了吗」「命令存在吗」。
这个脚本查的是**边界和异常路径**——那些正常用不到、
出事时才走到、因此从来没人跑过的分支：

  · 界面：空库、缺 run、坏 run 目录、路径穿越、POST、并发、未知路由
  · MCP ：run 没有 progress、compare 跨 bank_rev、baseline 无基线
  · CLI ：每个子命令的 --json 都真的是合法 JSON
  · 报告：结论卡的分辨率必须按计分请求算（这个 bug 出现过四次）

用法：python tools/check_e2e.py
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

PY = sys.executable or "python"
OK, BAD = [], []


def check(name, ok, detail=""):
    (OK if ok else BAD).append((name, detail))
    print("  %s %s%s" % ("✓" if ok else "✗", name,
                         ("  —— " + detail) if detail else ""))


def cli(*argv):
    p = subprocess.run([PY, "-m", "mprobe"] + list(argv), cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180)
    return p.returncode, p.stdout or "", p.stderr or ""


# --------------------------------------------------------------------------
# 1. CLI：每个子命令的 --json 都必须是合法 JSON
# --------------------------------------------------------------------------

def g_cli_json():
    print("\n[1/4] CLI 的 --json 输出")
    cases = [
        (["status"], True),
        (["tiers"], True),
        (["bank", "info"], True),
        (["config", "list"], True),
        (["schedule", "status"], True),
        (["baseline", "--model", "deepseek", "--tier", "monitor"], True),
        # 故意的错误路径：--json 时错误也必须是 JSON，不能是裸文本
        (["status", "--run", "no-such-run"], False),
        (["compare", "no-a", "no-b"], False),
    ]
    for argv, expect_ok in cases:
        rc, out, err = cli(*(argv + ["--json"]))
        label = " ".join(argv)
        try:
            json.loads(out)
            good = True
        except Exception:
            good = False
        check("`%s --json` 输出是合法 JSON" % label, good,
              ("退出码 %d" % rc) if good else
              ("stdout 前 60 字：%r" % out[:60]))


# --------------------------------------------------------------------------
# 2. 界面的异常路径
# --------------------------------------------------------------------------

class Web(object):
    def __init__(self, port=8850):
        from mprobe.web import server as srv
        self.srv = srv
        self.httpd, self.port = srv._free_port(port)
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()
        self.base = "http://127.0.0.1:%d" % self.port

    def get(self, path, timeout=20):
        req = urllib.request.Request(self.base + path)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)

    def post(self, path):
        req = urllib.request.Request(self.base + path, data=b"{}",
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def g_web():
    print("\n[2/4] 界面的异常路径")
    w = Web()
    try:
        # 正常路由
        for p in ("/api/overview", "/api/monitor", "/api/models",
                  "/api/bank", "/api/tiers"):
            st, body, _h = w.get(p)
            ok = st == 200
            try:
                json.loads(body)
            except Exception:
                ok = False
            check("GET %s → 200 且是 JSON" % p, ok, "状态 %s" % st)

        # 首页与静态资源
        st, body, hdr = w.get("/")
        check("GET / → 200 HTML", st == 200 and "<title>" in body,
              "Content-Type=%s" % hdr.get("Content-Type"))
        for f in ("/app.js", "/style.css"):
            st, _b, hdr = w.get(f)
            check("GET %s → 200" % f, st == 200,
                  "Content-Type=%s" % hdr.get("Content-Type"))

        # 安全响应头
        st, _b, hdr = w.get("/")
        check("响应带 X-Frame-Options: DENY",
              hdr.get("X-Frame-Options") == "DENY")
        check("响应带 nosniff",
              hdr.get("X-Content-Type-Options") == "nosniff")
        check("响应禁用缓存（避免看到旧数据）",
              "no-store" in (hdr.get("Cache-Control") or ""))

        # 缺参数 / 不存在的 run
        st, body, _h = w.get("/api/run")
        check("GET /api/run 缺 run_id → 有 error 字段",
              st == 200 and "error" in json.loads(body))
        st, body, _h = w.get("/api/run?run_id=definitely-not-there")
        check("GET /api/run 不存在的 run → 有 error",
              "error" in json.loads(body))
        st, body, _h = w.get("/api/progress?run_id=definitely-not-there")
        check("GET /api/progress 不存在的 run → 有 error",
              "error" in json.loads(body))

        # 坏的 run 目录：有目录、没文件
        broken = os.path.join(ROOT, "data", "runs", "_e2e_broken")
        os.makedirs(broken, exist_ok=True)
        try:
            st, body, _h = w.get("/api/run?run_id=_e2e_broken")
            d = json.loads(body)
            check("空的 run 目录 → 报 error 而不是抛异常", "error" in d,
                  str(d)[:70])
        finally:
            shutil.rmtree(broken, ignore_errors=True)

        # run 目录里放一个坏 JSON
        broken2 = os.path.join(ROOT, "data", "runs", "_e2e_badjson")
        os.makedirs(broken2, exist_ok=True)
        io.open(os.path.join(broken2, "progress.json"), "w",
                encoding="utf-8").write("{ 不是 JSON")
        try:
            st, body, _h = w.get("/api/progress?run_id=_e2e_badjson")
            d = json.loads(body)
            check("坏 JSON 的 progress → 返回 transient，不是 500",
                  "transient" in d or "error" in d, str(d)[:70])
            st, body, _h = w.get("/api/run?run_id=_e2e_badjson")
            check("坏 JSON 的 run → 有 error，服务不挂",
                  st == 200, "状态 %s" % st)
        finally:
            shutil.rmtree(broken2, ignore_errors=True)

        # 路径穿越
        for evil in ("/../server.py", "/..%2fserver.py",
                     "/static/../../cli.py"):
            st, _b, _h = w.get(evil)
            check("路径穿越 %s 被拒" % evil, st in (403, 404),
                  "状态 %s" % st)

        # 未知路由
        st, _b, _h = w.get("/api/nope")
        check("未知 /api 路由 → 404", st == 404, "状态 %s" % st)

        # 只读：POST 一律 405
        st, body = w.post("/api/overview")
        check("POST → 405 且说明只读",
              st == 405 and "只读" in body, "状态 %s" % st)

        # 并发：ThreadingHTTPServer 下同时打十个请求不应互相干扰
        errs = []

        def hit():
            try:
                s, b, _ = w.get("/api/bank")
                if s != 200 or "bank_rev" not in b:
                    errs.append(s)
            except Exception as e:
                errs.append(type(e).__name__)
        ths = [threading.Thread(target=hit) for _ in range(10)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        check("10 个并发请求全部正常", not errs, str(errs[:3]))

        # 空库：把 DB 路径临时指到一个空目录，界面必须给「还没有数据」
        from mprobe import paths
        old_db = paths.DB
        tmp = tempfile.mkdtemp()
        try:
            paths.DB = os.path.join(tmp, "nope.db")
            st, body, _h = w.get("/api/overview")
            d = json.loads(body)
            check("结果库不存在时 → empty 而不是报错",
                  d.get("empty") is True or d.get("runs") == [], str(d)[:70])
        finally:
            paths.DB = old_db
            shutil.rmtree(tmp, ignore_errors=True)
    finally:
        w.stop()


# --------------------------------------------------------------------------
# 3. MCP 的异常路径
# --------------------------------------------------------------------------

def mcp(msgs):
    inp = "\n".join(json.dumps(m, ensure_ascii=False) for m in msgs) + "\n"
    p = subprocess.run([PY, "-m", "mprobe.mcp.server"], cwd=ROOT, input=inp,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    got = {}
    for line in (p.stdout or "").splitlines():
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if o.get("id") is not None:
            got[o["id"]] = o
    return got, p


def _payload(o):
    r = o.get("result") or {}
    txt = ((r.get("content") or [{}])[0]).get("text", "{}")
    try:
        return json.loads(txt), r.get("isError")
    except Exception:
        return {"raw": txt}, r.get("isError")


def g_mcp():
    print("\n[3/4] MCP 的异常路径")
    base = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}]

    got, p = mcp(base + [
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "eval_status", "arguments": {"run_id": "nope-x"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "compare",
                    "arguments": {"run_a": "nope-a", "run_b": "nope-b"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "baseline",
                    "arguments": {"model": "deepseek", "tier": "large"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "eval_start", "arguments": {"tier": "monitor"}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "check", "arguments": {"model": "不存在的端点"}}},
    ])
    check("MCP stderr 干净", not (p.stderr or "").strip(),
          (p.stderr or "")[:80])

    d, err = _payload(got.get(2, {}))
    check("eval_status 查不存在的 run → 有 error/hint",
          bool(d.get("error") or d.get("hint")), str(d)[:70])

    d, err = _payload(got.get(3, {}))
    check("compare 两个不存在的 run → isError", err is True, str(d)[:70])

    d, err = _payload(got.get(4, {}))
    check("baseline 查没有基线的档位 → 不崩，给出说明",
          got.get(4) is not None, str(d)[:70])

    d, err = _payload(got.get(5, {}))
    check("eval_start 缺 model → isError + 说明", err is True and d.get("error"),
          str(d.get("error"))[:60])

    d, err = _payload(got.get(6, {}))
    check("check 指向不存在的端点 → 报错且不开跑",
          bool(d.get("error") or d.get("_exit_code") == 2), str(d)[:70])

    # 花钱工具默认不开跑（再验一次，因为这是最贵的错）
    from mprobe import paths
    before = set(os.listdir(paths.RUNS)) if os.path.isdir(paths.RUNS) else set()
    got, _p = mcp(base + [
        {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
         "params": {"name": "eval_start",
                    "arguments": {"model": "deepseek", "tier": "probe"}}}])
    after = set(os.listdir(paths.RUNS)) if os.path.isdir(paths.RUNS) else set()
    d, _e = _payload(got.get(9, {}))
    check("eval_start(probe) 不带 confirm → 只报价、零 run 目录",
          "还没有花钱" in json.dumps(d, ensure_ascii=False)
          and not (after - before),
          "新增目录 %d" % len(after - before))


# --------------------------------------------------------------------------
# 4. 结论卡的分辨率口径（出现过四次的 bug）
# --------------------------------------------------------------------------

def g_card():
    print("\n[4/4] 结论卡与基线的样本量口径")
    from mprobe import paths, tiers
    import glob
    bad = []
    for p in glob.glob(os.path.join(paths.RUNS, "*", "summary.json")):
        try:
            s = json.load(io.open(p, encoding="utf-8"))
        except Exception:
            continue
        c = s.get("card") or {}
        if not c.get("scored_requests"):
            continue
        want = tiers.min_detectable(c["scored_requests"])
        if abs((c.get("min_detectable") or 0) - want) > 0.05:
            bad.append((os.path.basename(os.path.dirname(p)),
                        c.get("min_detectable"), want))
    check("所有结论卡的分辨率都按计分请求算", not bad,
          "不一致：%s" % bad[:2] if bad else "")

    # 基线的 requests_per_round 必须是计分请求数
    con = None
    try:
        from mprobe.store import db
        if os.path.isfile(paths.DB):
            con = db.connect(paths.DB)
            rows = list(con.execute(
                "select tier, detail from baselines where active = 1"))
            off = []
            for tier, detail in rows:
                d = json.loads(detail or "{}")
                n = d.get("requests_per_round")
                try:
                    from mprobe import profiles
                    exp = profiles.resolve(tier)["scored_requests"]
                except Exception:
                    continue
                if n and n != exp:
                    off.append((tier, n, exp))
            check("基线的每轮请求数 == 该档计分请求数", not off,
                  "不一致：%s" % off[:2] if off else "%d 条基线" % len(rows))
    finally:
        if con:
            con.close()


def main():
    print("端到端测验：边界与异常路径 —— 零请求、零花费")
    for fn in (g_cli_json, g_web, g_mcp, g_card):
        try:
            fn()
        except Exception as e:
            import traceback
            check(fn.__name__, False, "%s: %s" % (type(e).__name__, e))
            traceback.print_exc(limit=3)
    print("\n" + "=" * 60)
    print("总计 %d 项，通过 %d，未通过 %d"
          % (len(OK) + len(BAD), len(OK), len(BAD)))
    if BAD:
        print("\n未通过：")
        for n, d in BAD:
            print("  ✗ %s %s" % (n, ("—— " + d) if d else ""))
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
