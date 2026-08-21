#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把外部题源转换并并入 `banks/public_v1.jsonl`。零请求。

题库构建期工具。发布版的 `banks/public_v1.jsonl` 已由本脚本生成，
常规使用无需再次运行；仅在引入新题源时使用。

**不修改题面**（提示词逐字不变是可比性的前提）。只做三件事：

  1. 维度代号归一，见 `DIM_MAP`。未登记的代号一律拒绝。
  2. 丢弃加载器不接受的字段（`bank.VALID_TOP` 之外的），并报出丢弃项。
  3. 逐题执行 `bank.load_file()` 的校验；任何一道不合格则**整体不写**。

题号前缀保留题源原样，使一道题的出处可从题号直接判断。

输入格式：JSON Lines，每行一道题，字段需符合 `bank.VALID_TOP`。

用法：
    python tools/build_public_bank.py --source <path.jsonl>
    python tools/build_public_bank.py --source <path.jsonl> --write
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from mprobe.engine import bank, checkers, dims       # noqa: E402

OUT = os.path.join(ROOT, "banks", "public_v1.jsonl")

#: 维度代号归一表。键为题源使用的代号，值为 `dims.py` 已登记的代号。
#:
#: `I` → `LA`：题源的 `I` 维为英文词汇分组谜题，属能力维；
#: 而 `LG` 已用于中文生成（行为维）。二者为不同能力，不可合并到同一代号，
#: 否则报告会以单一维度名承载两类题目。
DIM_MAP = {"I": "LA"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True,
                    help="题源 JSON Lines 文件路径")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    SRC = args.source

    if not os.path.exists(SRC):
        sys.exit("找不到题源文件：%s" % SRC)

    rows = []
    with open(SRC, "r", encoding="utf-8-sig") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if line:
                rows.append((ln, json.loads(line)))
    print("来源 %d 道题" % len(rows))

    dropped = Counter()
    remapped = Counter()
    out, problems = [], []
    seen = {}

    for ln, o in rows:
        it = {}
        for k, v in o.items():
            if k in bank.VALID_TOP:
                it[k] = v
            else:
                dropped[k] += 1

        old = it.get("dim")
        if old in DIM_MAP:
            it["dim"] = DIM_MAP[old]
            remapped["%s -> %s" % (old, it["dim"])] += 1

        # 维度必须已登记
        try:
            dims.resolve(it.get("dim"))
        except dims.DimError as e:
            problems.append("第 %d 行（%s）: %s" % (ln, it.get("id"), e))
            continue

        # 判分器必须存在且不是 manual
        t = (it.get("check") or {}).get("type")
        if t not in checkers.available():
            problems.append("第 %d 行（%s）: 判分器 %r 不存在。可用：%s"
                            % (ln, it.get("id"), t,
                               "、".join(checkers.available())))
            continue

        if it.get("id") in seen:
            problems.append("第 %d 行: 题号 %s 与第 %d 行重复"
                            % (ln, it["id"], seen[it["id"]]))
            continue
        seen[it["id"]] = ln
        out.append(it)

    if dropped:
        print("丢弃加载器不认的字段：%s"
              % "、".join("%s x%d" % kv for kv in sorted(dropped.items())))
    if remapped:
        print("维度代号归一：%s"
              % "、".join("%s x%d" % kv for kv in sorted(remapped.items())))

    if problems:
        print("\n有 %d 道题不合格，**整体不写**：" % len(problems))
        for p in problems[:20]:
            print("   " + p)
        sys.exit(1)

    # 统计
    by_dim = Counter(it["dim"] for it in out)
    by_ck = Counter(it["check"]["type"] for it in out)
    rob = defaultdict(list)
    for it in out:
        g, why = bank.robustness_detail(it)
        rob[g].append(it["id"])

    print("\n合格 %d 道" % len(out))
    print("按维度：%s"
          % "、".join("%s %s(%d)" % (d, dims.name(d), n)
                      for d, n in sorted(by_dim.items())))
    print("按判分器：%s"
          % "、".join("%s(%d)" % kv for kv in sorted(by_ck.items())))
    print("稳健性：白 %d ／ 灰 %d ／ 黑 %d"
          % (len(rob["white"]), len(rob["grey"]), len(rob["black"])))

    # 字面匹配类判据（regex / contains_none）一律不得留在白名单：
    # 它们对措辞变化敏感，误判方向只会是「误报退化」。
    literal = [it for it in out
               if it["check"]["type"] in ("regex", "contains_none")]
    still_white = [it["id"] for it in literal
                   if bank.robustness_detail(it)[0] == "white"]
    print("\n[校验] 字面匹配类 %d 道（regex %d + contains_none %d），"
          "仍在白名单的：%d"
          % (len(literal), by_ck.get("regex", 0),
             by_ck.get("contains_none", 0), len(still_white)))
    print("           %s" % ("全部落级，通过" if not still_white
                             else "未落级：%s" % still_white[:10]))

    if args.write:
        with open(OUT, "w", encoding="utf-8", newline="\n") as f:
            for it in out:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        # 写完立刻用加载器读回来，确认它自己能过校验
        got, sk = bank.load_file(OUT, bank.load_assets(
            os.path.join(ROOT, "banks")))
        print("\n已写 %s，加载器读回 %d 道（跳过 %d）"
              % (os.path.relpath(OUT, ROOT), len(got), len(sk)))


if __name__ == "__main__":
    main()
