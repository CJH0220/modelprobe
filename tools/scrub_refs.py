#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""发布前检查：文档与代码中不得出现外部仓库引用或过程性叙述。

发布版的文本应当只描述**当前系统的行为与约束**，不描述它是怎么演化来的。
以下两类内容会使读者依赖本仓库之外的上下文：

  · 外部仓库路径（同级目录名、父仓库名）
  · 过程性叙述（开发阶段编号、内部报告代称、第一人称、日期化的经历）

另外校验**文档之间的链接指向真实存在的文件**。发布集会因裁剪而变动：
若某份文档被移出发布集而引用它的链接留下，读者点开得到 404，
而没有任何检查会报错 —— 此前 README 就曾同时留下两个死链。

用法：
    python tools/scrub_refs.py            # 只报告
    python tools/scrub_refs.py --list     # 逐行列出
"""

import argparse
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SKIP_DIRS = {"__pycache__", "data", ".git", "_dev_archive", "node_modules"}
EXTS = (".py", ".md", ".toml", ".json")

#: 禁止出现的模式 -> 说明
PATTERNS = [
    (r"0[1-5]_[一-鿿]+", "外部同级目录名"),
    # 完整目录名之外，简写形式同样使读者依赖外部上下文，且更易漏过：
    #   `03/data/*`（外部路径）、`` `03` ``（反引号里的裸编号）、`01`–`05`（区间）
    (r"0[1-5]/", "外部目录路径"),
    (r"`0[1-5]`", "外部目录编号"),
    (r"0[1-5]\s*[–—-]\s*0[1-5]", "外部目录区间"),
    (r"new_design|BANK\.md", "内部设计文档引用"),
    (r"AI_power", "父仓库名"),
    (r"报告[一二三]", "内部报告代称"),
    (r"ROADMAP", "开发阶段文档引用"),
    (r"阶段\s*[0-9]", "开发阶段编号"),
    (r"huanchang", "内部网关标识"),
    # 第一人称：仅检查叙述性文本。**引号内的用户原话是数据不是叙述**，
    # SKILL.md 的触发短语（「我要看图」「我觉得它变笨了」）必须保留原样，
    # 删去其中的第一人称会降低触发命中率。
    (r"(?<![「\"`|])我(?![们]?[a-zA-Z])(?![^「]*」)", "第一人称"),
    (r"今天|昨天|上午|下午", "日期化叙述"),
    (r"踩到过|踩过|吃过一次|又一次", "经历性叙述"),
]

#: 白名单：这些文件本身就是过程记录或外部数据，不参与检查
ALLOW_FILES = {
    "CHANGELOG.md",           # 变更记录必然含版本演进
    "tools/scrub_refs.py",    # 本脚本以这些模式为数据
}


def targets():
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if not f.endswith(EXTS):
                continue
            rel = os.path.relpath(os.path.join(root, f), ROOT)
            rel = rel.replace(os.sep, "/")
            if rel in ALLOW_FILES:
                continue
            yield rel, os.path.join(root, f)


def is_quoted(line, pos):
    """判断某位置是否位于「引用的用户原话」中。

    三种形式均视为数据而非叙述：中文引号内、反引号内、Markdown 表格单元格。
    表格中的「用户表述」列与 SKILL.md 的触发短语都属此类，
    删改其中的第一人称会改变触发行为。
    """
    if line.lstrip().startswith("|"):
        return True
    if line.count("`") >= 2 or line.count("「") >= 1 or line.count('"') >= 2:
        return True
    if line.lstrip().startswith(("description:", "用户说：", "需求：")):
        return True
    return False


LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def dead_links():
    """文档间的链接必须指向真实存在的文件。返回 [(来源, 行号, 目标)]。"""
    bad = []
    for rel, full in targets():
        if not rel.endswith(".md"):
            continue
        base = os.path.dirname(full)
        try:
            lines = io.open(full, encoding="utf-8").read().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for ln, line in enumerate(lines, 1):
            for tgt in LINK.findall(line):
                if tgt.startswith(("http://", "https://", "#", "mailto:")):
                    continue
                path = tgt.split("#")[0]
                if not path:
                    continue
                if not os.path.exists(os.path.join(base, path)):
                    bad.append((rel, ln, tgt))
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    hits = {}
    for rel, full in targets():
        try:
            text = io.open(full, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        for ln, line in enumerate(text.splitlines(), 1):
            for pat, why in PATTERNS:
                m = re.search(pat, line)
                if m and is_quoted(line, m.start()) and why in (
                        "第一人称", "日期化叙述"):
                    continue
                if m:
                    hits.setdefault(rel, []).append((ln, why, m.group(0),
                                                     line.strip()[:70]))
                    break

    dead = dead_links()
    if dead:
        print("死链 %d 处（链接指向不存在的文件）：" % len(dead))
        for rel, ln, tgt in dead:
            print("  ✗ %s:%d  ->  %s" % (rel, ln, tgt))
        print()

    if not hits:
        if dead:
            return 1
        print("未发现外部引用或过程性叙述；文档链接全部有效")
        return 0

    total = sum(len(v) for v in hits.values())
    print("发现 %d 处，涉及 %d 个文件：\n" % (total, len(hits)))
    for rel in sorted(hits, key=lambda x: -len(hits[x])):
        print("  %-32s %d 处" % (rel, len(hits[rel])))
        if args.list:
            for ln, why, tok, ctx in hits[rel]:
                print("      %4d  %-14s %-8s %s" % (ln, why, tok, ctx))
    return 1


if __name__ == "__main__":
    sys.exit(main())
