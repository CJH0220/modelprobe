#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""代码与产物清单。只读，零请求。

用途：交接、复盘、以及确认「实际存在的东西」和「文档声称的东西」一致。
"""

import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SKIP_DIRS = {"__pycache__", "data", "node_modules", ".git", ".claude"}

GROUPS = [
    ("CLI 与档位", ("mprobe/cli.py", "mprobe/estimate.py", "mprobe/tiers.py",
                    "mprobe/profiles.py", "mprobe/paths.py",
                    "mprobe/__init__.py", "mprobe/__main__.py")),
    ("engine 测量层", ("mprobe/engine/",)),
    ("monitor 监控层", ("mprobe/monitor/",)),
    ("store 结果库", ("mprobe/store/",)),
    ("mcp 门面", ("mprobe/mcp/",)),
    ("web 界面", ("mprobe/web/",)),
    ("tools 离线工具", ("tools/",)),
    ("安装", ("install.py",)),
]


def lines(p):
    try:
        with io.open(p, encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def walk_py():
    out = []
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                full = os.path.join(root, f)
                rel = os.path.relpath(full, ROOT).replace(os.sep, "/")
                out.append((rel, lines(full)))
    return sorted(out)


def main():
    files = walk_py()
    by_rel = dict(files)
    used = set()

    print("== Python 代码 ==")
    total = 0
    for label, prefixes in GROUPS:
        sel = [(r, n) for r, n in files
               if any(r == p or r.startswith(p) for p in prefixes)]
        if not sel:
            continue
        used.update(r for r, _ in sel)
        s = sum(n for _, n in sel)
        total += s
        print("\n%s —— %d 个文件，%d 行" % (label, len(sel), s))
        for r, n in sorted(sel, key=lambda x: -x[1]):
            print("   %-44s %5d" % (r, n))
    rest = [(r, n) for r, n in files if r not in used]
    if rest:
        print("\n未归类 —— %d 个文件" % len(rest))
        for r, n in rest:
            print("   %-44s %5d" % (r, n))
        total += sum(n for _, n in rest)
    print("\nPython 合计 %d 行" % total)

    print("\n== 前端（界面）==")
    for r in ("mprobe/web/static/index.html", "mprobe/web/static/app.js",
              "mprobe/web/static/style.css"):
        p = os.path.join(ROOT, r)
        if os.path.isfile(p):
            print("   %-44s %5d" % (r, lines(p)))

    print("\n== 文档 ==")
    for f in sorted(os.listdir(ROOT)):
        if f.endswith(".md"):
            print("   %-44s %5d" % (f, lines(os.path.join(ROOT, f))))
    for d in sorted(os.listdir(os.path.join(ROOT, "skills"))):
        p = os.path.join(ROOT, "skills", d, "SKILL.md")
        if os.path.isfile(p):
            print("   %-44s %5d" % ("skills/%s/SKILL.md" % d, lines(p)))

    print("\n== 题库与档案 ==")
    banks = os.path.join(ROOT, "banks")
    mf = json.load(io.open(os.path.join(banks, "MANIFEST.json"),
                           encoding="utf-8"))
    print("   bank_rev %s，冻结于 %s，%d 道题"
          % (mf["bank_rev"], mf["created_at"], len(mf["items"])))
    for fn, meta in sorted(mf["files"].items()):
        print("   %-30s %3d 道  sha256 %s…"
              % (fn, meta["items"], meta["sha256"][:16]))
    for extra in ("snr_core41.json", "probe_deepseek.json",
                  "probe_2models.json", "probe_3models.json"):
        p = os.path.join(banks, extra)
        if os.path.isfile(p):
            d = json.load(io.open(p, encoding="utf-8"))
            print("   %-30s %3d 道实测台账"
                  % (extra, len(d.get("items") or {})))
    prof = os.path.join(ROOT, "profiles")
    print("   档案：%s" % "、".join(sorted(
        f[:-5] for f in os.listdir(prof) if f.endswith(".json"))))

    print("\n== 端点 ==")
    from mprobe.engine import endpoint
    for e in endpoint.list_all():
        ks = e.get("key_status") or {}
        # list_all() 的 model 是字符串；load() 返回的 cfg["model"] 才是字典。
        # 两个接口形状不同，取值前判类型，不要凭印象。
        m = e.get("model")
        m = m.get("model") if isinstance(m, dict) else m
        print("   %-9s %-20s %-38s 密钥 %s"
              % (e.get("key"), m, e.get("base_url"),
                 "✓" if ks.get("set") else "✗"))

    print("\n== 运行产物 ==")
    runs = os.path.join(ROOT, "data", "runs")
    if os.path.isdir(runs):
        ds = [d for d in os.listdir(runs) if not d.startswith("_")]
        print("   %d 个 run 目录" % len(ds))
    db = os.path.join(ROOT, "data", "mprobe.db")
    if os.path.isfile(db):
        import sqlite3
        con = sqlite3.connect(db)
        for t in ("runs", "results", "baselines", "checks"):
            try:
                n = con.execute("select count(*) from %s" % t).fetchone()[0]
                print("   表 %-11s %4d 行" % (t, n))
            except sqlite3.Error:
                pass
        con.close()
    return 0


if __name__ == "__main__":
    sys.path.insert(0, ROOT)
    sys.exit(main())
