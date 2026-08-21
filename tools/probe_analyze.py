#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""实施计划第 1.3 项 / 1.4 —— 分析 probe 档的逐题结果，决定哪些题进 1.4。**零请求。**

输入：一个或多个 `data/runs/<id>/raw.jsonl`（probe 档跑出来的响应原文）
输出：`banks/probe_<model>.json` 逐题台账 + 一张表

## 为什么必须重放，不能用 runner 当时写的分

铁律六：判分器变更等同改题。本轮跑到一半发现 `math_equal` 的
`\\boxed{}` 提取被 `\\[ ... \\]` 的收尾定界符打断，导致 LiveBench 数学题
**答对了却判 0**。修完之后 runner 已经写下的分是旧判分器给的，
必须用当前判分器重放响应原文重算。

## 截断题必须单独归类，不能当「太难」

1.3 的用途是决定哪些题留在题库里。一道被 `max_tokens` 截断的题，
记账上和「模型答错」一模一样（都是 0 分），但**原因完全不同**：
前者是配额问题，抬高 `max_tokens` 就可能答对；后者才是能力问题。
把截断题当饱和题剔除，等于因为自己配置给得小而删掉好题。

实测：`DA` 24%、`MA` 20% 的请求撞上 16000 上限，
其中相当一部分**可见输出为 0 字符**（全部 token 烧在思考上）。

## 单模型能得出什么、不能得出什么

**能**：这道题在这个模型上的通过率、稳定性（采样极差）、是否被截断。
**不能**：这道题能不能**分辨模型**。那需要跨模型极差，也就是 1.4。
所以本脚本只做「明显没救的剔掉」这一步，判定留给 1.4。

用法：
    python tools/probe_analyze.py data/runs/<run_id>            # 单模型（1.3）
    python tools/probe_analyze.py <run_a> <run_b> <run_c>       # 多模型（1.4）
    python tools/probe_analyze.py <run> --write
"""

import argparse
import json
import math
import os
import statistics
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from mprobe.engine import bank, checkers, dims        # noqa: E402

BANKS = os.path.join(ROOT, "banks")

#: 饱和判据。单模型只能筛掉「这个模型上完全没有区分空间」的题。
#: 注意 p=1.0 **不**等于没有区分度 —— 另一个模型可能做不出来。
#: 所以这里只标记，剔除与否由 --drop-ceiling / --drop-floor 显式决定。
CEILING = 1.0
FLOOR = 0.0

#: 有效采样次数低于这个数，该题的分数不可用。
#:
#: 早先的规则是「截断率 >= 1/3 即整题不可用」，那是双重惩罚：
#: 被截断的那一次先按 0 分计入，拉低了通过率，然后整题又被判不可用。
#: 一道 3 次里截断 1 次、另外 2 次都答对的题，按那条规则会被扔掉。
#:
#: 现在改成：**分数只用未截断的次数算**，截断率单独记录。
#: 只有有效次数不足 2 次时才判不可用 —— 1 次采样不构成判定（铁律三）。
MIN_VALID_TRIALS = 2


def load_bank():
    assets = bank.load_assets(BANKS)
    items = {}
    for fn in ("core.jsonl", "public_v1.jsonl"):
        for it in bank.load_file(os.path.join(BANKS, fn), assets)[0]:
            # `_bank_file` 只有 bank.load() 会塞，load_file() 不塞
            it["_bank_file"] = fn
            items[it["id"]] = it
    return items


def read_run(path):
    """读一个 run 目录，返回 (model_key, endpoint_sha, [记录])。"""
    if os.path.isdir(path):
        raw = os.path.join(path, "raw.jsonl")
        summ = os.path.join(path, "summary.json")
    else:
        raw, summ = path, None
    recs = []
    with open(raw, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    # run_id 的格式是 `<模型>-<档位>-<kind>-<时间戳>`，取第一段当模型名。
    # 被中途停掉的轮次没有 summary.json，只能从目录名回退 ——
    # 不能整段当模型名，否则台账里的 models 字段会变成一串目录名。
    rid = os.path.basename(os.path.dirname(raw))
    model, sha = rid.split("-")[0], None
    if summ and os.path.exists(summ):
        s = json.load(open(summ, encoding="utf-8"))
        model, sha = s.get("model_key") or model, s.get("endpoint_sha")
    return model, sha, recs


def two_prop_z(k1, n1, k2, n2):
    if not n1 or not n2:
        return None
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1.0 / n1 + 1.0 / n2))
    if se == 0:
        return None
    return abs(k1 / n1 - k2 / n2) / se


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--supersede", action="append", default=[],
                    help="补测轮次：其中出现的题目，用它的记录**替换**主轮次的。"
                         "用于抬高 max_tokens 后重测截断题 —— 同一道题的两批"
                         "记录不能混算，配额不同就是两次不同的测量。")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    items = load_bank()

    # 先读补测轮次，登记它覆盖了哪些题
    # 按**模型**分别登记，不能全局替换 —— 否则 deepseek 的补测记录会被
    # 套到 qwen / opus 头上，把别人的回答算成它的分数，跨模型比较直接失效。
    superseded = defaultdict(dict)          # model -> {item_id: [记录]}
    for path in args.supersede:
        mk, _s, recs = read_run(path)
        for r in recs:
            superseded[mk].setdefault(r["id"], []).append(r)
    for mk, d in superseded.items():
        print("补测覆盖：模型 %s 的 %d 道题" % (mk, len(d)))
    per = defaultdict(dict)            # per[item_id][model] = {...}
    models, drift = [], 0
    n_records = 0

    for path in args.runs:
        model, sha, recs = read_run(path)
        models.append(model)
        by_item = defaultdict(list)
        for r in recs:
            by_item[r["id"]].append(r)
        # 被补测覆盖的题，整题换成补测的记录。**不合并**——
        # 两批记录的 max_tokens 不同，混算等于把两次不同条件的测量当成一次。
        # 只替换**同一个模型**的记录。
        for iid, rs in (superseded.get(model) or {}).items():
            if iid in by_item:
                by_item[iid] = rs

        for iid, rs in by_item.items():
            it = items.get(iid)
            if it is None:
                continue
            scores, trunc, outs, n_all = [], 0, [], 0
            for r in rs:
                n_records += 1
                if r.get("truncated"):
                    trunc += 1
                if r.get("completion_tokens"):
                    outs.append(r["completion_tokens"])
                new, _d = checkers.run_check(r.get("response") or "",
                                             it["check"])
                if new is None:
                    continue
                old = r.get("score")
                if old is not None and abs(float(old) - float(new)) > 1e-9:
                    drift += 1
                # 被截断的这一次**不进分数**：可见输出不完整，判分结果
                # 没有意义。按 0 计会把配额问题伪装成能力问题。
                if not r.get("truncated"):
                    scores.append(float(new))
                n_all += 1
            if not n_all:
                continue
            per[iid][model] = {
                "n": len(scores),
                "n_all": n_all,
                "p": (sum(scores) / len(scores)) if scores else None,
                "k_full": sum(1 for s in scores if s >= 1.0),
                "spread": (max(scores) - min(scores)) if scores else None,
                "truncated": trunc,
                "trunc_rate": trunc / n_all,
                "out_tokens_mean": (statistics.mean(outs) if outs else None),
                "out_tokens_max": (max(outs) if outs else None),
            }

    print("重放 %d 条响应，%d 个模型：%s"
          % (n_records, len(models), "、".join(models)))
    print("与 runner 当时写的分不一致：%d 条 —— %s"
          % (drift,
             "本轮跑到一半修了 math_equal，所以这个数不为零是预期的"
             if drift else "判分器没有变化"))

    # ---------------- 逐题定级 ----------------
    rows = []
    for iid in sorted(per, key=lambda x: (items[x]["dim"], x)):
        it = items[iid]
        ms = per[iid]
        tr = max(v["trunc_rate"] for v in ms.values())
        n_valid = min(v["n"] for v in ms.values())
        ps = {m: v["p"] for m, v in ms.items() if v["p"] is not None}
        p_mean = (sum(ps.values()) / len(ps)) if ps else None
        spread = (max(ps.values()) - min(ps.values())) if len(ps) > 1 else None

        z = None
        if len(ps) > 1:
            hi = max(ps, key=ps.get)
            lo = min(ps, key=ps.get)
            z = two_prop_z(ms[hi]["k_full"], ms[hi]["n"],
                           ms[lo]["k_full"], ms[lo]["n"])

        # 定级。顺序很重要：截断优先于饱和 ——
        # 一道被截断的题，它的 0 分不代表难度。
        if n_valid < MIN_VALID_TRIALS or p_mean is None:
            grade, why = "truncated", ("有效采样只剩 %d 次（%.0f%% 撞 "
                                       "max_tokens 上限），不足以判定"
                                       % (n_valid, 100 * tr))
        elif p_mean >= CEILING:
            grade, why = "ceiling", "该模型全对，单模型看不出区分空间"
        elif p_mean <= FLOOR:
            grade, why = "floor", "该模型全错，需确认不是判据不可达"
        else:
            grade, why = "live", "有区分空间，进 1.4"
        rows.append({
            "id": iid, "dim": it["dim"], "checker": it["check"]["type"],
            "robustness": bank.robustness(it), "bank_file": it["_bank_file"],
            "p_mean": p_mean, "per_model": ms,
            "spread": spread, "z": None if z is None else round(z, 3),
            "trunc_rate": tr, "n_valid": n_valid,
            "grade": grade, "reason": why,
        })

    # ---------------- 汇总 ----------------
    g = Counter(r["grade"] for r in rows)
    print("\n逐题定级（共 %d 道）：" % len(rows))
    for k, label in (("live", "有区分空间 → 进 1.4"),
                     ("ceiling", "天花板（全对）"),
                     ("floor", "地板（全错）"),
                     ("truncated", "截断，分数不可用")):
        print("  %-22s %3d" % (label, g[k]))

    print("\n%-4s %-8s %4s | %5s %5s %5s %5s | %s"
          % ("维", "名称", "题数", "live", "天花", "地板", "截断", "该维还剩几道能用"))
    bydim = defaultdict(Counter)
    for r in rows:
        bydim[r["dim"]][r["grade"]] += 1
        bydim[r["dim"]]["n"] += 1
    for d in sorted(bydim, key=lambda x: (dims.namespace(x), x)):
        c = bydim[d]
        print("%-4s %-8s %4d | %5d %5d %5d %5d | %d"
              % (d, dims.name(d), c["n"], c["live"], c["ceiling"],
                 c["floor"], c["truncated"], c["live"]))

    # 截断题清单：这些要抬 max_tokens 重测，不能剔
    tr_items = [r for r in rows if r["grade"] == "truncated"]
    if tr_items:
        print("\n截断题 %d 道（**不要剔除**，抬 max_tokens 重测）：" % len(tr_items))
        for r in tr_items:
            mm = list(r["per_model"].values())[0]
            print("  %-22s %-4s 有效 %d 次 / 截断 %.0f%%  输出均值 %s / 最大 %s"
                  % (r["id"], r["dim"], r["n_valid"], 100 * r["trunc_rate"],
                     int(mm["out_tokens_mean"] or 0), mm["out_tokens_max"]))

    # 地板题：要区分「太难」和「判据不可达」
    fl = [r for r in rows if r["grade"] == "floor"]
    if fl:
        print("\n地板题 %d 道（全错）。按判分器分组 —— "
              "黑名单判据的全错更可能是判据不可达，不是题太难：" % len(fl))
        byck = defaultdict(list)
        for r in fl:
            byck["%s/%s" % (r["checker"], r["robustness"])].append(r["id"])
        for k in sorted(byck):
            print("  %-22s %2d 道  %s" % (k, len(byck[k]), byck[k][:5]))

    live = [r for r in rows if r["grade"] == "live"]
    if len(models) == 1:
        # 1.4 的报价：只在 live 题上跑，且要按实测 token 算
        tok = sum((list(r["per_model"].values())[0]["out_tokens_mean"] or 0) * 3
                  for r in live)
        print("\n1.4 报价（只跑 %d 道 live 题 x 3 次 = %d 请求）："
              % (len(live), len(live) * 3))
        print("  按本轮实测逐题输出 token：约 %.0f token → %.2f 元/模型"
              % (tok, tok * 2 / 1e6))
        print("  跑两个模型约 %.2f 元" % (tok * 2 / 1e6 * 2))
        print("  对比：全部 %d 道都跑两个模型要 %.2f 元"
              % (len(rows), sum((list(r["per_model"].values())[0]
                                 ["out_tokens_mean"] or 0) * 3
                                for r in rows) * 2 / 1e6 * 2))

    if args.write:
        out = args.out or os.path.join(
            BANKS, "probe_%s.json" % "_".join(models))
        payload = {
            "generated_by": "tools/probe_analyze.py",
            "runs": args.runs,
            "models": models,
            "regraded_with": "mprobe/engine/checkers.py（重放响应原文）",
            "grader_disagreement_vs_runner": drift,
            "rules": {
                "truncated": ("分数只用未截断的次数算；有效次数 < 2 才判不可用。"
                              "被截断的一次不按 0 计 —— 那会把配额问题伪装成能力问题"),
                "ceiling": "p == 1.0",
                "floor": "p == 0.0",
                "note": ("单模型只能筛掉明显没救的题。"
                         "p=1.0 不等于没有区分度 —— 另一个模型可能做不出来，"
                         "所以判定留给 1.4 的跨模型极差。"),
            },
            "counts": dict(g),
            "items": {r["id"]: r for r in rows},
        }
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print("\n已写 %s" % os.path.relpath(out, ROOT))


if __name__ == "__main__":
    main()
