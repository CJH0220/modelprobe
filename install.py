#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""安装 mprobe：写 MCP 配置 + 同步 Skill。**四件事都幂等。**

## 为什么这个脚本要这么啰嗦

它改的是**用户级配置文件**（`~/.claude.json`、`~/.codex/config.toml`、
`~/.claude/skills/`）。这些文件里可能有用户自己配了很久的别的东西。
一个安装脚本把它们覆盖掉，用户第二天才会发现，而且不知道是谁干的。

所以三条硬规矩：

  1. **改之前先备份**，备份路径打印出来
  2. **改之前先打印 diff**，让人看见到底改了什么
  3. **目标已存在且内容不同时不覆盖** —— 打印差异，让用户自己决定

## 密钥

这个脚本**从不写密钥**。它只检查密钥在不在，不在就打印设置命令。
密钥只走环境变量或 `config/secrets.local.json`（已 gitignore），
永远不进 `config/models/*.json`（那是要提交的）。

用法：
    python install.py                # 检查 + 打印将要做什么，**不改任何文件**
    python install.py --apply        # 真的写
    python install.py --apply --host claude     # 只装 Claude Code
    python install.py --apply --host codex      # 只装 Codex
"""

import argparse
import difflib
import io
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable or "python"

HOME = os.path.expanduser("~")
CLAUDE_JSON = os.path.join(HOME, ".claude.json")
CLAUDE_SKILLS = os.path.join(HOME, ".claude", "skills")
CODEX_TOML = os.path.join(HOME, ".codex", "config.toml")

SERVER_NAME = "mprobe"


# --------------------------------------------------------------------------
# 通用
# --------------------------------------------------------------------------

def _read(path):
    if not os.path.isfile(path):
        return None
    with io.open(path, encoding="utf-8-sig") as f:
        return f.read()


def _backup(path):
    """备份并返回备份路径。时间戳精确到秒，同一秒内多次装不会互相覆盖。"""
    if not os.path.isfile(path):
        return None
    bak = "%s.mprobe-bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
    n = 0
    while os.path.exists(bak):
        n += 1
        bak = "%s.%d" % (bak, n)
    shutil.copy2(path, bak)
    return bak


def _diff(old, new, path):
    a = (old or "").splitlines(keepends=True)
    b = (new or "").splitlines(keepends=True)
    return list(difflib.unified_diff(a, b, fromfile=path + " (现在)",
                                     tofile=path + " (装完)", n=2))


def _write(path, text, apply, log):
    """打印 diff；apply 为真时备份并写入。返回是否有改动。"""
    old = _read(path)
    if old == text:
        log.append("  = %s 已经是目标内容，不动" % path)
        return False
    d = _diff(old, text, path)
    log.append("  ~ %s %s" % (path, "（新建）" if old is None else "（修改）"))
    for line in d[:40]:
        log.append("      " + line.rstrip("\n"))
    if len(d) > 40:
        log.append("      …… 还有 %d 行差异" % (len(d) - 40))
    if apply:
        bak = _backup(path)
        if bak:
            log.append("    备份到 %s" % bak)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        log.append("    已写入")
    return True


# --------------------------------------------------------------------------
# 1. 自检
# --------------------------------------------------------------------------

def selfcheck(log):
    ok = True
    v = sys.version_info
    if v < (3, 9):
        log.append("  ✗ Python %d.%d 太旧，需要 >= 3.9" % (v[0], v[1]))
        ok = False
    else:
        log.append("  ✓ Python %d.%d.%d" % v[:3])

    for d in ("banks", "profiles", "config", "mprobe"):
        p = os.path.join(ROOT, d)
        if not os.path.isdir(p):
            log.append("  ✗ 缺目录 %s" % d)
            ok = False
    if ok:
        log.append("  ✓ 目录结构完整")

    data = os.path.join(ROOT, "data")
    try:
        os.makedirs(data, exist_ok=True)
        probe = os.path.join(data, ".write-test")
        with io.open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        log.append("  ✓ data/ 可写")
    except OSError as e:
        log.append("  ✗ data/ 不可写：%s" % e)
        ok = False

    # 题库能不能加载 —— 这是最容易在别人机器上炸的一步
    r = subprocess.run([PY, "-m", "mprobe", "bank", "info", "--json"],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        log.append("  ✗ 题库加载失败：%s"
                   % (r.stderr or r.stdout or "").strip()[:200])
        ok = False
    else:
        try:
            mf = json.loads(r.stdout)["manifest"]
            log.append("  ✓ 题库 %s，%d 道题"
                       % (mf.get("bank_rev"), len(mf.get("items") or {})))
        except Exception:
            log.append("  ✓ 题库可加载")
    return ok


# --------------------------------------------------------------------------
# 2. MCP 配置
# --------------------------------------------------------------------------

def mcp_entry():
    return {"command": PY, "args": ["-m", "mprobe.mcp.server"], "cwd": ROOT}


def install_claude_mcp(apply, log):
    path = CLAUDE_JSON
    old = _read(path)
    try:
        cfg = json.loads(old) if old else {}
    except json.JSONDecodeError:
        log.append("  ✗ %s 不是合法 JSON，**不动它**。请先自己修好。" % path)
        return False
    if not isinstance(cfg, dict):
        log.append("  ✗ %s 顶层不是对象，不动它" % path)
        return False

    servers = cfg.setdefault("mcpServers", {})
    cur = servers.get(SERVER_NAME)
    want = mcp_entry()
    if cur == want:
        log.append("  = Claude Code 已配好 mprobe，不动")
        return False
    if cur is not None:
        # 已存在但不同 —— 不覆盖，让用户决定
        log.append("  ! Claude Code 里已有一个叫 %s 的 MCP，且内容不同："
                   % SERVER_NAME)
        log.append("      现在: %s" % json.dumps(cur, ensure_ascii=False))
        log.append("      期望: %s" % json.dumps(want, ensure_ascii=False))
        log.append("    **不覆盖。** 要换就手动改，或先删掉那一项再装。")
        return False
    servers[SERVER_NAME] = want
    return _write(path, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                  apply, log)


def install_codex_mcp(apply, log):
    """Codex 用 TOML。这里**只做追加**，不解析整个 TOML。

    理由：不引第三方库的前提下写一个正确的 TOML 解析器不现实，
    而半正确的解析器会把用户的配置改坏。所以只判断段落在不在，
    不在就追加，在就不动。
    """
    path = CODEX_TOML
    old = _read(path) or ""
    header = "[mcp_servers.%s]" % SERVER_NAME
    if header in old:
        log.append("  = Codex 里已有 %s 段落，不动"
                   "（要改就手动编辑 %s）" % (header, path))
        return False
    block = (
        "\n# mprobe —— 模型能力观测工具（由 install.py 追加）\n"
        "%s\n"
        "command = %s\n"
        "args = [\"-m\", \"mprobe.mcp.server\"]\n"
        "cwd = %s\n"
    ) % (header, json.dumps(PY), json.dumps(ROOT))
    return _write(path, old + block, apply, log)


# --------------------------------------------------------------------------
# 3. 同步 Skill
# --------------------------------------------------------------------------

def install_skills(apply, log):
    src_root = os.path.join(ROOT, "skills")
    if not os.path.isdir(src_root):
        log.append("  ✗ 没有 skills/ 目录")
        return False
    changed = False
    for name in sorted(os.listdir(src_root)):
        src = os.path.join(src_root, name, "SKILL.md")
        if not os.path.isfile(src):
            continue
        dst = os.path.join(CLAUDE_SKILLS, name, "SKILL.md")
        text = _read(src)
        cur = _read(dst)
        if cur == text:
            log.append("  = %s 已是最新" % name)
            continue
        if cur is not None:
            # 已存在且不同：**不覆盖**，打印差异让用户决定。
            # 用户可能自己改过触发词，覆盖掉他不会知道。
            log.append("  ! %s 已存在且内容不同，**不覆盖**：" % name)
            for line in _diff(cur, text, dst)[:20]:
                log.append("      " + line.rstrip("\n"))
            log.append("    要用新版就先删掉 %s 再装" % dst)
            continue
        changed = _write(dst, text, apply, log) or changed
    return changed


# --------------------------------------------------------------------------
# 4. 端点与密钥
# --------------------------------------------------------------------------

def check_endpoints(log):
    r = subprocess.run([PY, "-m", "mprobe", "config", "list", "--json"],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    try:
        eps = json.loads(r.stdout).get("endpoints") or []
    except Exception:
        log.append("  ✗ 读端点失败：%s" % (r.stderr or r.stdout)[:200])
        return
    if not eps:
        log.append("  ! 一个端点都没配。复制 config/models/_template.json "
                   "改成你的渠道。")
        return
    for e in eps:
        # 字段名照 `mprobe config list --json` 的实际输出，不要凭印象写。
        # 掩码值（sk-…a278）可以打印，明文永远不打印。
        ks = e.get("key_status") or {}
        has = bool(ks.get("set"))
        log.append("  %s %-10s %-22s 密钥 %s"
                   % ("✓" if has else "!", e.get("key"),
                      e.get("model") or "?",
                      ("已配（%s %s）" % (ks.get("source_label") or
                                         ks.get("source") or "?",
                                         ks.get("masked") or ""))
                      if has else "**缺失**"))
        if not has and e.get("key_env"):
            log.append("      设置（用户级，新终端才生效）：")
            log.append("      [Environment]::SetEnvironmentVariable"
                       "(\"%s\",\"sk-xxx\",\"User\")" % e["key_env"])
    log.append("  密钥**只**走环境变量或 config/secrets.local.json，"
               "本脚本从不写密钥、也不打印明文。")


# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# 5. 卸载
# --------------------------------------------------------------------------
#
# 只删**本脚本装的东西**，且只在它没被人动过时才删。判据是「与本仓库
# 当前内容一致」：MCP 条目的 cwd 指向本仓库；Skill 文件与 skills/ 下
# 一字不差；Codex 段落带着本脚本追加时写下的注释标记。
# 不满足就跳过并说明原因，不猜。


def _rm(path, apply, log, why=""):
    if not os.path.exists(path):
        log.append("  = %s 不存在，跳过" % path)
        return False
    if not apply:
        log.append("  → 将删除 %s%s" % (path, ("（%s）" % why) if why else ""))
        return True
    if os.path.isdir(path):
        shutil.rmtree(path)
    else:
        os.remove(path)
    log.append("  ✓ 已删除 %s" % path)
    return True


def uninstall_claude_mcp(apply, log):
    path = CLAUDE_JSON
    old = _read(path)
    if not old:
        log.append("  = %s 不存在，跳过" % path)
        return False
    try:
        cfg = json.loads(old)
    except json.JSONDecodeError:
        log.append("  ✗ %s 不是合法 JSON，**不动它**" % path)
        return False
    servers = (cfg or {}).get("mcpServers") or {}
    cur = servers.get(SERVER_NAME)
    if cur is None:
        log.append("  = Claude Code 里没有 %s，跳过" % SERVER_NAME)
        return False
    if (cur or {}).get("cwd") != ROOT:
        log.append("  ! Claude Code 里的 %s 指向 %s，不是本仓库，**不动它**"
                   % (SERVER_NAME, (cur or {}).get("cwd")))
        return False
    del servers[SERVER_NAME]
    return _write(path,
                  json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                  apply, log)


def uninstall_codex_mcp(apply, log):
    path = CODEX_TOML
    old = _read(path)
    if not old:
        log.append("  = %s 不存在，跳过" % path)
        return False
    marker = "# mprobe —— 模型能力观测工具（由 install.py 追加）"
    if marker not in old:
        if ("[mcp_servers.%s]" % SERVER_NAME) in old:
            log.append("  ! %s 里有 mprobe 段，但没有本脚本的标记，"
                       "**不动它**。请手动删除该段。" % path)
        else:
            log.append("  = Codex 里没有 mprobe 段，跳过")
        return False
    lines = old.splitlines(True)
    i = next(k for k, ln in enumerate(lines) if ln.strip() == marker)
    # 从标记行删到**下一个**段头（不含），段头本身留给别人
    j = i + 1
    while j < len(lines):
        if lines[j].lstrip().startswith("[") and j > i + 1:
            break
        j += 1
    block = "".join(lines[i:j])
    # 和 Claude 那支同一条判据：cwd 不指向本仓库就是别处的另一份安装。
    # 少了这一步会删掉别人的配置 —— 标记只能证明「是本脚本写的」，
    # 不能证明「是这个仓库写的」。
    if json.dumps(ROOT) not in block:
        log.append("  ! Codex 里的 mprobe 段不指向本仓库，**不动它**")
        return False
    return _write(path, "".join(lines[:i] + lines[j:]), apply, log)


def uninstall_skills(apply, log):
    src_root = os.path.join(ROOT, "skills")
    names = sorted(os.listdir(src_root)) if os.path.isdir(src_root) else []
    changed = False
    for name in names:
        src = os.path.join(src_root, name, "SKILL.md")
        if not os.path.isfile(src):
            continue
        dst_dir = os.path.join(CLAUDE_SKILLS, name)
        dst = os.path.join(dst_dir, "SKILL.md")
        cur = _read(dst)
        if cur is None:
            log.append("  = %s 没装，跳过" % name)
            continue
        if cur != _read(src):
            log.append("  ! %s 与本仓库的不一致（你可能改过触发词），"
                       "**不删**：%s" % (name, dst))
            continue
        _backup(dst)
        changed = _rm(dst_dir, apply, log, "与仓库一致") or changed
    return changed


def uninstall_schedule(log):
    """计划任务不由本脚本装，只列现状并给出该跑的命令。"""
    r = subprocess.run([PY, "-m", "mprobe", "schedule", "list", "--json"],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        log.append("  ? 读不到计划任务状态。手动查：mprobe schedule list")
        return
    rows = data.get("tasks") or data.get("schedules") or []
    live = [t for t in rows if isinstance(t, dict) and t.get("exists")]
    if not live:
        log.append("  = 没有已安装的计划任务")
        return
    for t in live:
        log.append("  ! 计划任务 %s 还在。删它："
                   "python -m mprobe schedule remove --model %s --tier %s"
                   % (t.get("name"), t.get("model") or "<端点>",
                      t.get("tier") or "<档位>"))


def do_uninstall(args):
    print("mprobe 卸载 —— 根目录 %s" % ROOT)
    print("模式：%s\n" % ("**真删**" if args.apply
                           else "预演（不改任何文件）"))
    log = []
    print("[1/4] MCP 配置")
    if args.host in ("claude", "both"):
        uninstall_claude_mcp(args.apply, log)
    if args.host in ("codex", "both"):
        uninstall_codex_mcp(args.apply, log)
    _flush(log)

    print("\n[2/4] Skill")
    uninstall_skills(args.apply, log)
    _flush(log)

    print("\n[3/4] 计划任务")
    uninstall_schedule(log)
    _flush(log)

    print("\n[4/4] 仓库内的东西（本脚本**不动**，要删自己删）")
    for pth, what in ((os.path.join(ROOT, "data"), "全部测评结果与响应原文"),
                      (os.path.join(ROOT, "config", "secrets.local.json"),
                       "密钥")):
        print("  · %s —— %s%s"
              % (pth, what, "" if os.path.exists(pth) else "（不存在）"))
    print("  · 仓库目录本身 —— 直接删掉即可，工具不往别处写文件")

    if not args.apply:
        print("\n以上都没有真的删。确认无误后加 --apply 再跑一次。")
    else:
        print("\n卸载完成。改动过的文件旁边留有 .bak 备份。")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="真的写文件。不加只打印将要做什么")
    ap.add_argument("--host", choices=["claude", "codex", "both"],
                    default="both")
    ap.add_argument("--uninstall", action="store_true",
                    help="卸载：删掉本脚本装的 MCP 配置与 Skill")
    args = ap.parse_args()

    if args.uninstall:
        return do_uninstall(args)

    log = []
    print("mprobe 安装 —— 根目录 %s" % ROOT)
    print("模式：%s\n" % ("**写入**" if args.apply else "预演（不改任何文件）"))

    print("[1/4] 自检")
    ok = selfcheck(log)
    _flush(log)
    if not ok:
        print("\n自检没过，**不继续**。先把上面的 ✗ 修掉。")
        return 2

    print("\n[2/4] MCP 配置")
    if args.host in ("claude", "both"):
        install_claude_mcp(args.apply, log)
    if args.host in ("codex", "both"):
        install_codex_mcp(args.apply, log)
    _flush(log)

    print("\n[3/4] 同步 Skill 到 %s" % CLAUDE_SKILLS)
    install_skills(args.apply, log)
    _flush(log)

    print("\n[4/4] 端点与密钥")
    check_endpoints(log)
    _flush(log)

    if not args.apply:
        print("\n以上都没有真的写。确认无误后加 --apply 再跑一次。")
    else:
        print("\n装完了。新开一个会话，说「帮测一下 deepseek 什么水平」试试。")
    return 0


def _flush(log):
    for line in log:
        try:
            print(line)
        except UnicodeEncodeError:
            enc = sys.stdout.encoding or "ascii"
            print(line.encode(enc, "replace").decode(enc, "replace"))
    del log[:]


if __name__ == "__main__":
    sys.exit(main())
