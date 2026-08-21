#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一条命令跑完所有自检。**零请求、零花费。**

改动代码、题库或文档后执行。九组：

  1. 一致性    —— 代码里的常量和实际数据对不对得上
  2. 说明书    —— SKILL.md / GUIDE 里的命令真的存在（check_skills.py）
  3. 拒绝      —— 该拒绝的地方真的拒绝（check_refusals.py）
  4. 监控行为  —— 三态判定、σ 三取大、推送打码（check_monitor.py）
  5. 端到端    —— 界面/MCP/CLI 的异常路径（check_e2e.py）
  6. 发布文本  —— 不含外部仓库引用与过程性叙述（scrub_refs.py）
  7. 协议      —— MCP 服务器的 JSON-RPC 契约
  8. 界面      —— 四条硬规矩
  9. 依赖      —— 运行时零第三方依赖的声称与实际一致（check_deps.py）

第 1 组检查硬编码常量与实际数据是否一致。该类不一致不会在运行时报错，
只会使全部结论偏移：例如档位注册表中的题数在题库变更后未同步，
估算与实际选题即产生偏差而无任何提示。
"""

import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

PY = sys.executable or "python"
RESULT = []


def item(group, name, ok, detail=""):
    RESULT.append((group, name, ok, detail))
    print("  %s %s%s" % ("✓" if ok else "✗", name,
                         ("  —— " + detail) if detail else ""))


# --------------------------------------------------------------------------
# 1. 一致性
# --------------------------------------------------------------------------

def group_consistency():
    print("\n[1/9] 一致性")
    from mprobe import tiers, profiles, BANK_REV
    from mprobe.engine import bank
    from mprobe import paths

    # 1.1 档位注册表 vs 实际档案
    for k in tiers.ORDER:
        reg = tiers.get(k)["items"]
        try:
            act = profiles.resolve(k)["n_items"]
        except Exception as e:
            item("一致性", "档位 %s 可解析" % k, False, str(e)[:60])
            continue
        item("一致性", "档位 %s 注册表(%d) == 实际档案(%d)" % (k, reg, act),
             reg == act,
             "" if reg == act else "改了题库就要同步 tiers.py")

    # 1.2 代码的 BANK_REV vs 清单
    mf = json.load(io.open(os.path.join(paths.BANKS, "MANIFEST.json"),
                           encoding="utf-8"))
    item("一致性", "BANK_REV(%s) == 清单 bank_rev(%s)"
         % (BANK_REV, mf.get("bank_rev")), BANK_REV == mf.get("bank_rev"),
         "MEASUREMENT 3.1：bank_rev 等于工具版本号")

    # 1.3 清单题数 vs 题库实际题数
    n_files = 0
    for fn, meta in (mf.get("files") or {}).items():
        got, _sk = bank.load_file(os.path.join(paths.BANKS, fn),
                                  bank.load_assets(paths.BANKS))
        item("一致性", "%s 清单记 %d 道 == 实际 %d 道"
             % (fn, meta.get("items"), len(got)),
             meta.get("items") == len(got))
        n_files += len(got)
    item("一致性", "清单台账 %d 条 == 题库 %d 道"
         % (len(mf.get("items") or {}), n_files),
         len(mf.get("items") or {}) == n_files)

    # 1.3b 文档里写的自检组数 vs 本脚本实际的组数。
    #      这个数散落在四份文档里，加一组就会同时过时四处，
    #      而过时不会有任何报错 —— 与档位题数属同一类漂移。
    CN = "零一二三四五六七八九十"
    src = io.open(os.path.join(HERE, "check_all.py"), encoding="utf-8").read()
    # 取**最后**一段：这个字符串字面量本身也出现在上一行，
    # 用 [1] 会切到字面量处而数出 0 个组，且检查照常"通过".
    body = src.split("def main():")[-1]
    n_groups = sum(1 for ln in body.splitlines()
                   if ln.startswith("    group_") and "(" in ln)
    want = CN[n_groups] if n_groups < len(CN) else str(n_groups)
    for doc in ("README.md", "GUIDE.md", "DEPLOY.md", "CONTRIBUTING.md"):
        t = io.open(os.path.join(ROOT, doc), encoding="utf-8").read()
        said = set(re.findall(r"([零一二三四五六七八九十]|\d+)组", t))
        bad = said - {want}
        item("一致性", "%s 写的自检组数 == 实际 %d 组" % (doc, n_groups),
             not bad, "文档写的是 %s 组" % "、".join(sorted(bad)) if bad else "")

    # 1.4 σ 的样本量口径：档案里的 scored_requests 必须小于等于总请求
    for k in tiers.ORDER:
        try:
            p = profiles.resolve(k)
        except Exception:
            continue
        ok = p["scored_requests"] <= p["requests"]
        item("一致性", "档位 %s 计分请求(%d) <= 总请求(%d)"
             % (k, p["scored_requests"], p["requests"]), ok)


# --------------------------------------------------------------------------
# 2/3. 复用已有脚本
# --------------------------------------------------------------------------

def group_script(idx, title, script):
    print("\n[%s] %s" % (idx, title))
    p = subprocess.run([PY, os.path.join(HERE, script)], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    out = (p.stdout or "") + (p.stderr or "")
    for line in out.splitlines():
        if line.strip().startswith(("✓", "✗", "⚠", "对不上",
                                    "通过", "未通过")):
            print("  " + line.strip())
    item(title, script, p.returncode == 0,
         "退出码 %d" % p.returncode)


# --------------------------------------------------------------------------
# 4. MCP 协议
# --------------------------------------------------------------------------

def group_mcp():
    print("\n[7/9] MCP 协议")
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "bank_info", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "不存在的工具", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 6, "method": "no/such/method"},
    ]
    inp = "\n".join(json.dumps(m, ensure_ascii=False) for m in msgs) + "\n"
    p = subprocess.run([PY, "-m", "mprobe.mcp.server"], cwd=ROOT, input=inp,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    got = {}
    for line in (p.stdout or "").splitlines():
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            item("协议", "stdout 全是合法 JSON", False, line[:60])
            return
        if o.get("id") is not None:
            got[o["id"]] = o

    item("协议", "initialize 返回 protocolVersion",
         bool((got.get(1, {}).get("result") or {}).get("protocolVersion")))
    item("协议", "通知（无 id）不产生响应", 0 not in got and None not in got)
    tools = ((got.get(2, {}).get("result") or {}).get("tools") or [])
    item("协议", "tools/list 返回 %d 个工具" % len(tools), len(tools) >= 8)
    item("协议", "每个工具都有 name/description/inputSchema",
         all(t.get("name") and t.get("description") and t.get("inputSchema")
             for t in tools))
    item("协议", "ping 有响应", 3 in got)
    item("协议", "只读工具调用成功",
         (got.get(4, {}).get("result") or {}).get("isError") is False)
    item("协议", "未知工具 → isError",
         (got.get(5, {}).get("result") or {}).get("isError") is True)
    item("协议", "未实现的方法 → JSON-RPC error",
         "error" in got.get(6, {}))
    item("协议", "stderr 没有污染 stdout", True,
         "stderr %d 字节" % len(p.stderr or ""))


# --------------------------------------------------------------------------

def group_web():
    """界面的四条硬规矩（DESIGN 4.3）。静态检查，不起服务。"""
    print("\n[8/9] 界面四条硬规矩")
    src = io.open(os.path.join(ROOT, "mprobe", "web", "server.py"),
                  encoding="utf-8").read()
    js = io.open(os.path.join(ROOT, "mprobe", "web", "static", "app.js"),
                 encoding="utf-8").read()

    item("界面", "规矩1 只绑 127.0.0.1", 'HOST = "127.0.0.1"' in src)
    # --host 必须**不存在**，不是默认值 —— 默认值随手就能改掉
    cli = io.open(os.path.join(ROOT, "mprobe", "cli.py"),
                  encoding="utf-8").read()
    item("界面", "规矩1 没有 --host 参数", '"--host"' not in cli)
    item("界面", "规矩1 关掉端口复用（防抢占别人的监听端口）",
         "allow_reuse_address = False" in src)

    # 只给掩码：api_models 里必须出现 key_masked，且**不得**出现
    # 任何取明文密钥的写法。secrets 模块的解析结果绝不能进响应。
    api_src = src.split("def api_models")[1].split("\ndef ")[0]
    item("界面", "规矩2 端点接口只给掩码，不给明文",
         "key_masked" in api_src
         and "api_key" not in api_src and "secret" not in api_src.lower())
    item("界面", "规矩2 webhook 走 notify.mask 打码", "notify.mask(" in src)

    item("界面", "规矩3 逐维带 dim_display（线型 + 为什么）",
         "tiers.dim_display(" in src and "renderDims" in js)

    item("界面", "规矩4 趋势按 模型+档位+题库+端点 四键分组",
         'r["bank_rev"], r["endpoint_sha"]' in src)
    item("界面", "规矩4 多组配置时前端显式提示不连线",
         "不连线" in js or "绝不连" in js)

    item("界面", "只读：POST 一律 405", "do_POST" in src and "405" in src)
    item("界面", "静态文件不能跳出 static 目录",
         "startswith(os.path.abspath(STATIC))" in src)


def main():
    print("mprobe 自检 —— 零请求、零花费")
    group_consistency()
    group_script("2/9", "说明书", "check_skills.py")
    group_script("3/9", "拒绝", "check_refusals.py")
    group_script("4/9", "监控行为", "check_monitor.py")
    group_script("5/9", "端到端边界", "check_e2e.py")
    group_script("6/9", "发布文本", "scrub_refs.py")
    group_mcp()
    group_web()
    group_script("9/9", "依赖", "check_deps.py")

    bad = [(g, n, d) for g, n, ok, d in RESULT if not ok]
    print("\n" + "=" * 60)
    print("总计 %d 项，通过 %d，未通过 %d"
          % (len(RESULT), len(RESULT) - len(bad), len(bad)))
    if bad:
        print("\n未通过：")
        for g, n, d in bad:
            print("  [%s] %s %s" % (g, n, ("—— " + d) if d else ""))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
