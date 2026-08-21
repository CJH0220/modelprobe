#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""校验 `skills/*/SKILL.md` 里写的命令**真的存在**。零请求。

为什么要有这个：Skill 是给模型看的说明书。它教错一条命令，
模型会照着调，然后拿到一个「未知子命令」的报错 —— 而用户看到的是
「这个工具坏了」。**说明书里的命令必须和 CLI 保持同步**，
而 CLI 是会改的，靠人记不住。

只做静态校验：子命令是否存在、参数是否在该子命令的 --help 里。
不执行任何命令的实际功能。
"""

import glob
import io
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

CMD = re.compile(r"python -m mprobe ([^\n`]+)")

#: 只扫**围栏代码块**里的命令。
#:
#: 围栏里的是「照着敲」，行内反引号里的是「在讨论这条命令」——
#: 后者可能正是在说明某条命令**还不存在**（比如 web 界面在实施阶段）。
#: 把两者一起当成待校验的命令，会逼着人为了过检查而删掉诚实的说明。
FENCE = re.compile(r"```[a-z]*\n(.*?)```", re.S)


def cli_help(*argv):
    p = subprocess.run([sys.executable, "-m", "mprobe"] + list(argv)
                       + ["--help"], cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return p.stdout or ""


def main():
    top = cli_help()
    known = set(re.findall(r"^\s{4}(\w[\w-]*)\s{2,}", top, re.M))
    print("CLI 子命令：%s" % "、".join(sorted(known)))

    helps = {}
    problems = []
    # 说明书不只是 SKILL.md：GUIDE.md 是给人照着敲的，
    # 教错一条命令的后果一样 —— 所以一起校验。
    targets = sorted(glob.glob(os.path.join(ROOT, "skills", "*", "SKILL.md")))
    for extra in ("GUIDE.md", "README.md"):
        p = os.path.join(ROOT, extra)
        if os.path.isfile(p):
            targets.append(p)
    for path in targets:
        name = (os.path.basename(os.path.dirname(path))
                if path.endswith("SKILL.md") else os.path.basename(path))
        text = "\n".join(FENCE.findall(io.open(path, encoding="utf-8").read()))
        for m in CMD.finditer(text):
            parts = m.group(1).split()
            if not parts:
                continue
            sub = parts[0]
            if sub not in known:
                problems.append((name, "子命令 `%s` 不存在" % sub))
                continue
            if sub not in helps:
                helps[sub] = cli_help(sub)
            for tok in parts[1:]:
                if tok.startswith("--"):
                    flag = tok.split("=")[0]
                    if flag not in helps[sub]:
                        problems.append(
                            (name, "`%s` 没有参数 `%s`" % (sub, flag)))

    n_docs = len(targets)
    seen, uniq = set(), []
    for x in problems:
        if x not in seen:
            seen.add(x)
            uniq.append(x)

    if uniq:
        print("\n对不上 %d 处：" % len(uniq))
        for n, w in uniq:
            print("   %-22s %s" % (n, w))
        return 1
    print("\n%d 份说明书（SKILL.md + GUIDE/README）引用的命令与参数全部存在"
          % n_docs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
