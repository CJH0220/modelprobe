#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按 1.3 的实测证据清理题库：退役废题、修正错判据。**零请求。**

每一条处置都写清**证据**和**处置理由**。没有证据的题一律不动 ——
「看着可疑」不是删题的理由，删错了要等下一个版本才能补回来。

## 退役（移出主库，存 `banks/retired/`，题号永不回收）

| 批次 | 题数 | 证据 |
|---|---|---|
| `ifeval` 不可判 | 4 | 判分器返回 `None`「本题的约束类型不支持自动判定」。在库里、花钱跑、不产分数 |
| `LA` connections | 30 | 全部黑名单（16 词定序正则）。抽样 3 道全 0：2 道撞 16000 上限且**可见输出 0 字符**，1 道正常答完、答案合理却被判据拒绝 |

`LA` 不是「改判据就能救」——它要的是「四组各四个词」的集合判定，
现有 22 种判据没有一种能表达。那属于新增判据类型，留给后续维护。

## 修正（题面不动，只改判据）

`LB-MA-356fa940d354` / `LB-MA-a622715b01d2`：题面明确要求
"duplicate that letter five times ... if the answer is F, then write FFFFF"，
模型照做输出 `AAAAA`，而判据写的是 `\\bA\\b` —— `AAAAA` 里没有独立的 `A`，
于是**答对了判 0**。这是 05 转换脚本的 bug，不是题的问题。
改成 `\\bAAAAA\\b`，与题面自己的指令一致。

用法：
    python tools/clean_bank.py            # 只报告
    python tools/clean_bank.py --write
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

BANKS = os.path.join(ROOT, "banks")
SRC = os.path.join(BANKS, "public_v1.jsonl")
RETIRED = os.path.join(BANKS, "retired", "public_v1_retired.jsonl")

#: 判分器返回 None 的 ifeval 题。1.3 实测：771 条响应里这 4 道一分未产。
IFEVAL_DEAD = [
    "LB-IN-35df8fcec24c",
    "LB-IN-40040515aeca",
    "LB-IN-64567c0d3c90",
    "LB-IN-c0ad0a8be5fe",
]

#: 判据修正：题号 -> (旧 expect, 新 expect, 理由)
EXPECT_FIX = {
    "LB-MA-356fa940d354": (
        r"\bA\b", r"\bAAAAA\b",
        "题面要求把答案字母重复五次（LiveBench 约定），模型输出 AAAAA 是正确的；"
        "旧判据 \\bA\\b 在 AAAAA 里匹配不到独立的 A，答对却判 0"),
    "LB-MA-a622715b01d2": (
        r"\bC\b", r"\bCCCCC\b",
        "同上，正确答案是 C，模型输出 CCCCC"),
}

REASONS = {
    "ifeval_dead": "ifeval 判分器返回 None：本题的约束类型不支持自动判定",
    "la_unjudgeable": ("LA connections：16 词定序正则不可达 + 16000 token 全部"
                       "烧在思考上被截断。需要新增「集合判定」类判据才能救"),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    rows = []
    with open(SRC, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    print("主库 public_v1.jsonl 现有 %d 道" % len(rows))

    keep, retire = [], []
    fixed = []
    for r in rows:
        if r["id"] in IFEVAL_DEAD:
            r["_retire_reason"] = REASONS["ifeval_dead"]
            retire.append(r)
            continue
        if r["dim"] == "LA":
            r["_retire_reason"] = REASONS["la_unjudgeable"]
            retire.append(r)
            continue
        if r["id"] in EXPECT_FIX:
            old, new, why = EXPECT_FIX[r["id"]]
            cur = r["check"].get("expect")
            if cur == old:
                r["check"]["expect"] = new
                fixed.append((r["id"], old, new, why))
            elif cur == new:
                pass                          # 已经修过，幂等
            else:
                sys.exit("题 %s 的 expect 是 %r，既不是预期的旧值也不是新值。"
                         "题库被别的改动碰过，先查清再跑。" % (r["id"], cur))
        keep.append(r)

    print("\n退役 %d 道：" % len(retire))
    byreason = {}
    for r in retire:
        byreason.setdefault(r["_retire_reason"], []).append(r["id"])
    for why, ids in byreason.items():
        print("  %d 道 —— %s" % (len(ids), why))
        print("     %s%s" % ("、".join(ids[:6]), " …" if len(ids) > 6 else ""))

    print("\n修正判据 %d 道：" % len(fixed))
    for iid, old, new, why in fixed:
        print("  %s  %r -> %r" % (iid, old, new))
        print("     %s" % why)

    print("\n主库剩 %d 道" % len(keep))
    from collections import Counter
    print("  按维:", dict(Counter(r["dim"] for r in keep)))

    if not args.write:
        print("\n（未写盘。加 --write 才落地）")
        return

    with open(SRC, "w", encoding="utf-8", newline="\n") as f:
        for r in keep:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    os.makedirs(os.path.dirname(RETIRED), exist_ok=True)
    # 退役库是**追加**的：题号永不回收，历史上退过的题要一直留着，
    # 否则下次有人用同一个题号建新题，历史数据就对不上了。
    seen = set()
    if os.path.exists(RETIRED):
        with open(RETIRED, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line:
                    seen.add(json.loads(line)["id"])
    with open(RETIRED, "a", encoding="utf-8", newline="\n") as f:
        for r in retire:
            if r["id"] not in seen:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print("\n已写 %s（%d 道）" % (os.path.relpath(SRC, ROOT), len(keep)))
    print("已追加 %s" % os.path.relpath(RETIRED, ROOT))

    # 写完立刻用加载器读回来
    from mprobe.engine import bank
    got, sk = bank.load_file(SRC, bank.load_assets(BANKS))
    print("加载器读回 %d 道（跳过 %d）" % (len(got), len(sk)))


if __name__ == "__main__":
    main()
