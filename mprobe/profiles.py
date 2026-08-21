# -*- coding: utf-8 -*-
"""档案：把「跑哪些题、跑几次、多久跑一次」固定下来。

档案存在的唯一理由是**每次测的是同一件事**。
没有档案，跑 20 题明天跑 50 题，两个分数不可比，
但它们长得一模一样，都是「一个 0-100 的数」——这正是最危险的形态。

选题是确定性的
--------------
按维度配额取题，同一维内**按题号排序后取前 N 道**。
不随机、不打乱。随机选题会让同一个档位每次考的题不同，
分数波动里混进选题噪声，而这部分噪声不可解释也不可复现。
"""

import json
import os

from . import paths, tiers
from .engine import bank as bankmod
from .engine import dims as dimsmod


class ProfileError(Exception):
    pass


def load(tier):
    p = paths.profile(tier)
    if not os.path.isfile(p):
        avail = ("、".join(sorted(f[:-5] for f in os.listdir(paths.PROFILES)
                                  if f.endswith(".json")))
                 if os.path.isdir(paths.PROFILES) else "（profiles/ 目录不存在）")
        raise ProfileError("找不到档案 %s。可用：%s" % (tier, avail))
    with open(p, "r", encoding="utf-8-sig") as f:
        prof = json.load(f)
    prof.setdefault("tier", tier)
    prof.setdefault("trials", 3)
    prof.setdefault("banks", ["core.jsonl"])
    prof.setdefault("monitor_only", False)
    return prof


def list_all():
    if not os.path.isdir(paths.PROFILES):
        return []
    out = []
    for fn in sorted(os.listdir(paths.PROFILES)):
        if fn.endswith(".json"):
            try:
                out.append(load(fn[:-5]))
            except Exception:
                continue
    return out


def select(items, prof):
    """按档案选题。返回 (选中的题, 配额说明)。"""
    mode = (prof.get("select") or {}).get("mode", "all")
    if mode == "all":
        return list(items), {"mode": "all", "note": "本档使用题库全部题目"}

    if mode == "ids":
        want = list((prof.get("select") or {}).get("ids") or [])
        by_id = {i["id"]: i for i in items}
        missing = [x for x in want if x not in by_id]
        if missing:
            raise ProfileError("档案 %s 点名的题不在题库里：%s。"
                               "题库换版本了就要同步改档案。"
                               % (prof["tier"], "、".join(missing)))
        return [by_id[x] for x in want], {"mode": "ids", "note": "按题号点名"}

    if mode != "quota":
        raise ProfileError("未知选题方式：%s" % mode)

    quota = (prof.get("select") or {}).get("quota") or {}
    by_dim = {}
    for it in items:
        by_dim.setdefault(it["dim"], []).append(it)
    for v in by_dim.values():
        v.sort(key=lambda x: x["id"])

    picked, short = [], []
    for dim in sorted(quota):
        want = int(quota[dim])
        have = by_dim.get(dim, [])
        picked.extend(have[:want])
        if len(have) < want:
            short.append("%s 要 %d 只有 %d" % (dim, want, len(have)))
    return picked, {"mode": "quota", "quota": quota, "short": short,
                    "note": ("配额未填满：" + "；".join(short)) if short
                            else "配额已填满"}


def plan(prof, items, monitor_only=False):
    """事前算账：这一档会发多少请求、能看见多大的退化、哪些维度能下判定。"""
    picked, sel = select(items, prof)
    n_items = len(picked)
    # 计分题：冒烟维和纯观测题都不进总分，所以也不能算进 σ 的样本量。
    # 用全部请求数算 σ 会让工具声称自己比实际更灵敏 ——
    # 小档实测：60 请求里只有 42 个计分，最小可检出退化
    # 从看起来的 9.4 分变成真实的 12.9 分。
    scored = [it for it in picked
              if dimsmod.in_score(it["dim"]) and not it.get("observe")]
    n_scored = len(scored)
    n_scored_req = n_scored * prof["trials"]

    # 监控口径下配额必须填满，填不满就**拒绝**，不是警告。
    #
    # 档案存在的唯一理由是「每次测的是同一件事」。配额没填满时，
    # 跑出来的分既不等于本档的分，也不等于任何一个别的档的分 ——
    # 它是一个没有定义的量，但长得和正常分数一模一样。
    # 而监控的每一次判定都要和基线比，一个没有定义的量进了基线，
    # 之后所有判定都错，且不报错。
    target = prof.get("target_items")
    if monitor_only and (sel.get("short") or (target and n_items < target)):
        raise ProfileError(
            "档位 %s 在**监控口径**下选不出足够的题：设计 %s 道，实际 %d 道。\n"
            "  %s\n"
            "原因是监控池（判分器不在黑名单，且不是已实测 SD 超标）"
            "在这些维度上题不够。\n"
            "看每个维度有多少题：python -m mprobe bank info\n"
            "监控请改用 --tier monitor —— 它的配额就是按池子的"
            "实际题数定的。\n"
            "测评不受这道闸门约束：`eval` 用全部题，可以照常跑。"
            % (prof.get("tier"), target, n_items,
               "；".join(sel.get("short") or ["配额本身为空"])))
    n_req = n_items * prof["trials"]
    dim_counts = {}
    for it in picked:
        dim_counts[it["dim"]] = dim_counts.get(it["dim"], 0) + 1

    target = prof.get("target_items")
    warn = []
    if target and n_items < target:
        warn.append("本档设计题量是 %d，实际只选出 %d 道。"
                    "最小可检出退化因此从 %.1f 分放宽到 %.1f 分。"
                    % (target, n_items,
                       tiers.min_detectable(target * prof["trials"]),
                       tiers.min_detectable(n_scored_req)))
    if sel.get("short"):
        warn.append("配额没填满：" + "；".join(sel["short"]))
    if n_scored < n_items:
        warn.append("%d 道题里有 %d 道不计入总分（冒烟维／观测题），"
                    "σ 与最小可检出退化按 %d 个计分请求算，不是 %d 个。"
                    % (n_items, n_items - n_scored, n_scored_req, n_req))

    return {
        "tier": prof["tier"],
        "label": prof.get("label", prof["tier"]),
        "items": picked,
        "n_items": n_items,
        "trials": prof["trials"],
        "requests": n_req,
        "n_scored_items": n_scored,
        "scored_requests": n_scored_req,
        # σ 一律按**计分请求数**算
        "sigma": tiers.sigma(n_scored_req),
        "min_detectable": tiers.min_detectable(n_scored_req),
        "ci_half": tiers.ci_half(n_scored_req),
        "dim_counts": dim_counts,
        "dim_display": {d: tiers.dim_display(m, prof["trials"])
                        for d, m in dim_counts.items()},
        "selection": sel,
        "warnings": warn,
        "cadence": prof.get("cadence"),
        "baseline_rounds": prof.get("baseline_rounds", 1),
    }


def resolve(tier, manifest=None, monitor_only=None):
    """一步到位：读档案 → 加载题库 → 选题 → 算账。"""
    prof = load(tier)
    mf = manifest or bankmod.load_manifest(paths.BANKS)
    assets = bankmod.load_assets(paths.BANKS, mf)
    # monitor_only 由调用方（命令）决定。档案里的同名字段只作说明，
    # 不再参与判断 —— 见 cli._plan 的注释。
    mo = False if monitor_only is None else monitor_only
    items, skipped = bankmod.load(paths.BANKS, prof["banks"], assets=assets,
                                  monitor_only=mo, manifest=mf)
    p = plan(prof, items, monitor_only=mo)
    p["skipped"] = skipped
    p["profile"] = prof
    p["manifest"] = mf
    p["assets"] = assets
    return p
