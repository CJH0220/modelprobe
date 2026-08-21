"""从历史基线存档重放计算轮间标准差与信噪比。零请求。

题库构建期工具。发布版的 `banks/snr_core41.json` 已由本脚本生成，
常规使用无需再次运行；仅在引入新的历史存档时使用。

## 输入

  --archive  基线存档根目录。其下每个子目录为一轮，目录名需匹配
             `<日期>-<时间>_daily-bl<轮次>_<模型>`，内含 `raw.jsonl`
  --probe    跨模型探针矩阵（JSON Lines：model / item / dim / n / k），可选。
             缺省时仅计算同模型轮间标准差，不计算跨模型极差

## 为何必须重放而不能直接使用存档中的分数

判分器变更等同于改题。存档中的分数由当时的判分器给出，
而本脚本用**当前** `mprobe/engine/checkers.py` 重放响应原文，
并单独报出与存档记录的分歧条数。分歧非零时须逐条确认变化是否预期。

## 两个口径均需计算

同一批数据存在两种通过率定义：

  · 平均分：各次得分的均值（含部分分）。`aggregate.py` 按此计总分
  · 满分率：得满分的次数 / 采样次数

对 `chain_compound` / `checkpoints` / `calibration` 等含部分分的判据，
两个口径的轮间标准差可相差数倍。故两套均计算，
`monitor_ok` 取四个信噪比口径中的最小值（保守）。

## 口径定义

  · 单轮平均分 = 该轮该题各次得分的均值
  · 单轮满分率 = 该轮该题得满分的次数 / 采样次数
  · 轮间标准差 = 各轮值的**总体标准差（ddof=0）**
  · 跨模型极差 = 各模型总均值的 max - min
  · 信噪比     = 极差 / 轮间标准差；噪声取各模型中的最大值（保守）

用法：
    python tools/replay_round_sd.py --archive <dir> [--probe <file>]
    python tools/replay_round_sd.py --archive <dir> --write
"""

import argparse
import json
import math
import os
import re
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from mprobe.engine import bank, checkers          # noqa: E402

ARCHIVE = None      # 由 --archive 指定
PROBE = None        # 由 --probe 指定
BANKS = os.path.join(ROOT, "banks")
OUT_JSON = os.path.join(BANKS, "snr_core41.json")

SKIP_MODELS = ("mock",)
RUN_RE = re.compile(r"^\d{8}-\d{6}_daily-bl(\d)_(.+)$")
SNR_GATE = 3.0
TRIALS = 3

#: 噪声下限，与 `monitor/baseline.py` 的 `sd_floor = 0.5/n` 同规则：
#: 半次采样的分值，即量化地板。
#:
#: 必要性：少量轮次下测得标准差为 0 并不等于方差为零，只表示波动低于
#: 当前采样量的分辨率。若不设下限，0 将作为分母，任何非零极差都会得到
#: 无穷大的信噪比，使饱和题通过闸门。
SD_FLOOR = 0.5 / TRIALS


#: 模型名归一表。探针矩阵与基线存档可能使用不同的模型标识，
#: 需映射到同一名称后才能配对。
PROBE_ALIAS = {}

#: 自校验参照值：题号 -> {跨模型极差, 指定模型的满分率标准差}。
#: 用于确认本脚本的计算口径与既有结论一致。为空时跳过自校验。
PUBLISHED = {
    "M2":    {"spread": 1.000, "sd_full": 0.000},
    "D3":    {"spread": 0.952, "sd_full": 0.000},
    "D2":    {"spread": 0.714, "sd_full": 0.000},
    "P1":    {"spread": 0.952, "sd_full": 0.133},
    "P3":    {"spread": 0.286, "sd_full": 0.267},
    "S-M24": {"spread": 0.167, "sd_full": 0.163},
}


def pop_sd(xs):
    """总体标准差（ddof=0）。

    与样本标准差（ddof=1）不同。轮间标准差描述的是已观测到的这几轮
    自身的离散程度，而非由样本推断总体，故取 ddof=0。
    """
    if not xs:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def sd_binomial(p):
    """单轮通过率自身的二项抽样标准差。仅作参照，不参与闸门判定。

    不纳入闸门的理由：退化时通过率变化量 Δp = p(1-p)·Δθ，
    信号与噪声均在 p=0.5 处最大，信噪比在该点取得最优（Fisher 信息量）。
    若将小样本下的二项标准差计入噪声，区分度最高的题目会被排除。

    闸门要拦截的是**超出二项预期的波动**——判分器对格式敏感所致的抖动，
    该类波动的误判方向只会是「误报退化」。故闸门取
    max(实测标准差, 量化地板)，二项值单独列出供对照。
    """
    if p is None:
        return None
    return math.sqrt(max(p * (1 - p), 0.0) / TRIALS)


def noise_eff(sd_obs):
    """进闸门的噪声：实测 SD 与量化地板取大。"""
    if sd_obs is None:
        return None
    return max(sd_obs, SD_FLOOR)


def snr_of(spread, noise):
    if spread is None or noise is None or noise <= 0:
        return None
    return spread / noise


def two_prop_z(k1, n1, k2, n2):
    """两比例 z 检验。阈值 1.96 对应双侧 95% 显著性。"""
    if not n1 or not n2:
        return None
    p1, p2 = k1 / n1, k2 / n2
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1.0 / n1 + 1.0 / n2))
    if se == 0:
        return None
    return abs(p1 - p2) / se


def find_rounds():
    out = defaultdict(dict)
    if not os.path.isdir(ARCHIVE):
        sys.exit(
            "找不到存档目录：%s\n"
            "本脚本是**题库构建期**工具，需要 上游数据仓库的 "
            "历史基线存档/`。\n"
            "独立部署 mprobe 时不需要它 —— 它的产物 banks/snr_core41.json "
            "已随仓库发布。" % ARCHIVE)
    for name in sorted(os.listdir(ARCHIVE)):
        m = RUN_RE.match(name)
        if not m:
            continue
        rnd, model = m.groups()
        if any(s in model for s in SKIP_MODELS):
            continue
        raw = os.path.join(ARCHIVE, name, "raw.jsonl")
        if os.path.exists(raw):
            out[model][int(rnd)] = raw
    return out


def load_probe():
    """跨模型探针矩阵 -> {题号: {模型: (k, n)}}。满分率口径。"""
    per = defaultdict(dict)
    if not PROBE or not os.path.exists(PROBE):
        return per
    with open(PROBE, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            if o.get("n"):
                per[o["item"]][PROBE_ALIAS.get(o["model"], o["model"])] = (
                    o["k"], o["n"])
    return per


def main():
    global ARCHIVE, PROBE
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", required=True,
                    help="基线存档根目录，其下每个子目录为一轮")
    ap.add_argument("--probe", default=None,
                    help="跨模型探针矩阵（JSON Lines），可选")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--md", action="store_true")
    args = ap.parse_args()
    ARCHIVE, PROBE = args.archive, args.probe

    assets = bank.load_assets(BANKS)
    items, _sk = bank.load_file(os.path.join(BANKS, "core.jsonl"), assets)
    cur = {it["id"]: it for it in items}

    rounds = find_rounds()
    models = sorted(rounds)
    probe = load_probe()

    print("噪声源：%s"
          % "、".join("%s(%d 轮)" % (m, len(rounds[m])) for m in models))
    print("信号源：%s（%d 题）" % (PROBE or "（未提供）", len(probe)))

    mean_rate = defaultdict(lambda: defaultdict(list))
    full_rate = defaultdict(lambda: defaultdict(list))
    short_rounds, corrupt, drift = [], [], []
    observe_only, prompt_mismatch = set(), {}
    n_records = 0

    for model in models:
        for rnd in sorted(rounds[model]):
            by_item = defaultdict(list)
            with open(rounds[model][rnd], "r", encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        rec = json.loads(line)
                        by_item[rec["id"]].append(rec)

            for iid, recs in by_item.items():
                it = cur.get(iid)
                if it is None:
                    corrupt.append({"id": iid, "model": model, "round": rnd,
                                    "error": recs[0].get("error")})
                    continue

                arch_p, cur_p = recs[0].get("prompt"), it.get("prompt")
                if cur_p is None:
                    prompt_mismatch.setdefault(iid, "多轮题，存档无法逐字核对")
                    continue
                if arch_p is not None and arch_p != cur_p:
                    prompt_mismatch[iid] = ("存档 %d 字 vs 现库 %d 字"
                                            % (len(arch_p), len(cur_p)))
                    continue

                scores = []
                for rec in recs:
                    n_records += 1
                    new, detail = checkers.run_check(
                        rec.get("response") or "", it["check"])
                    if new is None:
                        continue
                    old = rec.get("score")
                    if old is not None and abs(float(old) - float(new)) > 1e-9:
                        drift.append({"item": iid, "model": model, "round": rnd,
                                      "old": float(old), "new": float(new),
                                      "detail": detail})
                    scores.append(float(new))

                if not scores:
                    observe_only.add(iid)
                    continue
                if len(scores) < 3:
                    short_rounds.append({"item": iid, "model": model,
                                         "round": rnd, "trials": len(scores)})
                mean_rate[iid][model].append(sum(scores) / len(scores))
                full_rate[iid][model].append(
                    sum(1 for s in scores if s >= 1.0) / len(scores))

    # ---------------- 逐题汇总 ----------------
    table = []
    for iid in sorted(mean_rate, key=lambda x: (cur[x]["dim"], x)):
        it = cur[iid]
        per, ok = {}, []
        for model in models:
            mr = mean_rate[iid].get(model) or []
            fr = full_rate[iid].get(model) or []
            if len(mr) < 2:
                per[model] = {"rounds": len(mr)}
                continue
            per[model] = {
                "rounds": len(mr),
                "mean_score": sum(mr) / len(mr), "sd_mean": pop_sd(mr),
                "full_rate": sum(fr) / len(fr), "sd_full": pop_sd(fr),
            }
            ok.append(model)

        def col(key):
            return [per[m][key] for m in ok]

        sp_mean = (max(col("mean_score")) - min(col("mean_score"))
                   if len(ok) >= 2 else None)
        sp_full = (max(col("full_rate")) - min(col("full_rate"))
                   if len(ok) >= 2 else None)
        sd_mean_max = max(col("sd_mean")) if ok else None
        sd_full_max = max(col("sd_full")) if ok else None
        worst = ok[col("sd_mean").index(sd_mean_max)] if ok else None
        # 进闸门的噪声：实测与量化地板取大
        nz_mean = noise_eff(sd_mean_max)
        nz_full = noise_eff(sd_full_max)

        pv = probe.get(iid) or {}
        rates = {m: k / n for m, (k, n) in pv.items()}
        sp_probe = (max(rates.values()) - min(rates.values())
                    if len(rates) >= 2 else None)

        # 两比例 z：探针里最高与最低的那对模型
        z = None
        if len(rates) >= 2:
            hi = max(rates, key=rates.get)
            lo = min(rates, key=rates.get)
            z = two_prop_z(pv[hi][0], pv[hi][1], pv[lo][0], pv[lo][1])

        snrs = {
            # 自洽口径：信号与噪声同出一批基线（工具按平均分计总分，这是主口径）
            "mean_vs_mean": snr_of(sp_mean, nz_mean),
            "full_vs_full": snr_of(sp_full, nz_full),
            # MEASUREMENT 3.5 的口径：信号取既有研究结论探针
            "probe_vs_full": snr_of(sp_probe, nz_full),
            "probe_vs_mean": snr_of(sp_probe, nz_mean),
        }
        finite = [v for v in snrs.values() if v is not None]
        spreads = [s for s in (sp_mean, sp_full, sp_probe) if s is not None]
        if spreads and max(spreads) == 0.0:
            snr_min, verdict = 0.0, "无信号"      # 三模型分数完全一样，饱和
        elif not finite:
            snr_min, verdict = None, "数据不足"
        else:
            snr_min = min(finite)
            verdict = "通过" if snr_min >= SNR_GATE else "不通过"

        # MEASUREMENT 3.3 的测评准入三条（监控准入建立在它之上）
        p_for_gate = sum(rates.values()) / len(rates) if rates else None
        eval_gates = {
            "unsaturated": (None if p_for_gate is None
                            else bool(0.20 <= p_for_gate <= 0.80)),
            "spread_ge_015": (None if sp_probe is None
                              else bool(sp_probe >= 0.15)),
            "z_ge_196": None if z is None else bool(z >= 1.96),
        }
        eval_ok = all(v for v in eval_gates.values() if v is not None) and \
            all(v is not None for v in eval_gates.values())

        rob = bank.robustness(it)
        table.append({
            "id": iid, "dim": it["dim"], "title": it.get("title", ""),
            "checker": it["check"]["type"], "robustness": rob,
            "models": per,
            "p_mean": sum(col("mean_score")) / len(ok) if ok else None,
            "p_probe": p_for_gate,
            "spread_mean": sp_mean, "sd_mean_max": sd_mean_max,
            "spread_full": sp_full, "sd_full_max": sd_full_max,
            "noise_mean_gated": nz_mean, "noise_full_gated": nz_full,
            "sd_binomial_at_p": sd_binomial(p_for_gate),
            "sd_worst_model": worst, "spread_probe": sp_probe,
            "z_probe": None if z is None else round(z, 3),
            "eval_gates": eval_gates, "eval_ok": eval_ok,
            "snr": {k: (None if v is None else
                        ("inf" if v == math.inf else round(v, 3)))
                    for k, v in snrs.items()},
            "snr_min": (None if snr_min is None else
                        ("inf" if snr_min == math.inf else round(snr_min, 3))),
            "verdict": verdict,
            # 监控准入 = 测评准入三条 + 判分器不黑 + 保守信噪比达标
            "monitor_ok": bool(rob != "black" and verdict == "通过"
                               and eval_ok),
        })

    # ---------------- 数据完整性 ----------------
    print("\n重放 %d 条响应，覆盖 %d 道计分题" % (n_records, len(table)))
    if observe_only:
        print("观测题（check 不返回分数，按设计不计分）：%s"
              % "、".join(sorted(observe_only)))
    if corrupt:
        print("存档损坏记录 %d 条（老 runner 在拿到题号前就异常，"
              "该次失败归属哪道题已丢失）：" % len(corrupt))
        for c in corrupt:
            print("   %s 第%d轮  id=%r  error=%s"
                  % (c["model"], c["round"], c["id"], c["error"]))
    if short_rounds:
        print("采样次数不足 3 的轮次 %d 个（按实际次数算，已如实记录）："
              % len(short_rounds))
        for s in short_rounds:
            print("   %s %s 第%d轮 只有 %d 次"
                  % (s["item"], s["model"], s["round"], s["trials"]))
    if prompt_mismatch:
        print("提示词不一致／无法核对，已排除：%s" % prompt_mismatch)

    print("\n判分器分歧（当前判分器 vs 存档分）：%d / %d = %.2f%%"
          % (len(drift), n_records, 100.0 * len(drift) / max(1, n_records)))

    # ---------------- 自校验 ----------------
    print("\n对 MEASUREMENT.md 3.5 已发表数值的复现校验：")
    print("  %-7s %-26s %-26s" % ("题号", "跨模型极差(探针)",
                                  "内部网关 满分率 SD"))
    allok = True
    byid = {r["id"]: r for r in table}
    for iid, exp in PUBLISHED.items():
        r = byid.get(iid)
        got_sp = r and r["spread_probe"]
        got_sd = r and (r["models"].get("内部网关") or {}).get("sd_full")
        ok1 = got_sp is not None and abs(got_sp - exp["spread"]) < 0.002
        ok2 = got_sd is not None and abs(got_sd - exp["sd_full"]) < 0.002
        allok = allok and ok1 and ok2
        print("  %-7s %8s vs %-8s %s        %8s vs %-8s %s"
              % (iid, _f(got_sp), exp["spread"], "OK" if ok1 else "!!",
                 _f(got_sd), exp["sd_full"], "OK" if ok2 else "!!"))
    print("  => %s" % ("六行全部逐位复现"
                       if allok else "有行对不上，先查口径再用这份数"))

    (print_md if args.md else print_text)(table)

    def gate(key):
        n = 0
        for r in table:
            v = r["snr"][key]
            if v == "inf" or (isinstance(v, (int, float)) and v >= SNR_GATE):
                n += 1
        return n

    n_pass = sum(1 for r in table if r["verdict"] == "通过")
    n_fail = sum(1 for r in table if r["verdict"] == "不通过")
    n_nos = sum(1 for r in table if r["verdict"] == "无信号")
    print("\n测评准入三条（MEASUREMENT 3.3）：全过 %d / %d"
          % (sum(1 for r in table if r["eval_ok"]), len(table)))
    for key, label in (("unsaturated", "非饱和 0.2<=p<=0.8"),
                       ("spread_ge_015", "跨模型极差 >= 0.15"),
                       ("z_ge_196", "两比例 z >= 1.96")):
        print("    %-22s 过 %d ／ 不过 %d"
              % (label,
                 sum(1 for r in table if r["eval_gates"][key] is True),
                 sum(1 for r in table if r["eval_gates"][key] is False)))
    print("\n信噪比闸门（>= %.0f）：" % SNR_GATE)
    print("  保守口径（四个信噪比取最小）：通过 %d ／ 不通过 %d ／ 无信号 %d ／ 共 %d"
          % (n_pass, n_fail, n_nos, len(table)))
    print("  自洽口径（基线信号 / 平均分噪声，工具实际用的口径）：通过 %d"
          % gate("mean_vs_mean"))
    print("  MEASUREMENT 3.5 口径（探针信号 / 满分率噪声）：通过 %d"
          % gate("probe_vs_full"))
    print("轮间 SD(平均分) > 0.15 的题：%d"
          % sum(1 for r in table
                if r["sd_mean_max"] and r["sd_mean_max"] > 0.15))
    print("最终 monitor_ok（判分器闸门 + 保守信噪比）：%d / %d"
          % (sum(1 for r in table if r["monitor_ok"]), len(table)))

    if args.write:
        payload = {
            "generated_by": "tools/replay_round_sd.py",
            # 只记来源的**性质**，不记本机路径 —— 台账是要发布的。
            "noise_source": "既有 5 轮基线存档的 raw.jsonl（重放判分）",
            "signal_source_probe": (os.path.basename(PROBE) if PROBE
                                    else "（未提供）"),
            "models": models,
            "rounds_per_model": {m: len(rounds[m]) for m in models},
            "trials_per_round": 3,
            "regraded_with": "mprobe/engine/checkers.py（重放响应原文）",
            "sd_definition": "总体标准差 ddof=0",
            "two_metrics": (
                "mean=平均分（含部分分，与 aggregate.py 的 k=得分之和 一致，"
                "工具判定用这个）；full=满分率（老 matrix.jsonl 的 k/n 口径，"
                "MEASUREMENT 3.5 的表用这个）"),
            "snr_gate": SNR_GATE,
            "monitor_ok_rule":
                "判分器不在黑名单 且 四个信噪比口径里最小的那个 >= 3",
            "grader_disagreement": {"records": n_records,
                                    "changed": len(drift)},
            "data_defects": {
                "corrupt_records": corrupt,
                "short_rounds": short_rounds,
                "observe_only": sorted(observe_only),
                "prompt_mismatch": prompt_mismatch,
            },
            "published_check_passed": allok,
            "items": {r["id"]: r for r in table},
        }
        with open(OUT_JSON, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print("\n已写 %s" % os.path.relpath(OUT_JSON, ROOT))


def _f(x, nd=3):
    if x is None:
        return "—"
    if x == math.inf:
        return "inf"
    return ("%%.%df" % nd) % x


def _s(v, nd=2):
    if v is None:
        return "—"
    if v == "inf":
        return "inf"
    return ("%%.%df" % nd) % float(v)


def _g(b):
    return {True: "Y", False: "n", None: "?"}[b]


def print_text(table):
    print("\n%-7s %-3s %-15s %6s | %6s %6s %6s | %6s %6s %6s | "
          "%6s %6s %5s | %-5s %6s  %s"
          % ("题号", "维", "判分器", "均值",
             "极差m", "SDm", "snr_m", "极差f", "SDf", "snr_f",
             "探针差", "SD理论", "z", "准入", "snr", "判定"))
    print("-" * 132)
    for r in table:
        g = r["eval_gates"]
        print("%-7s %-3s %-15s %6s | %6s %6s %6s | %6s %6s %6s | "
              "%6s %6s %5s | %-5s %6s  %s"
              % (r["id"], r["dim"], r["checker"], _f(r["p_mean"]),
                 _f(r["spread_mean"]), _f(r["sd_mean_max"]),
                 _s(r["snr"]["mean_vs_mean"]),
                 _f(r["spread_full"]), _f(r["sd_full_max"]),
                 _s(r["snr"]["full_vs_full"]),
                 _f(r["spread_probe"]), _f(r["sd_binomial_at_p"]),
                 _f(r["z_probe"], 1),
                 _g(g["unsaturated"]) + _g(g["spread_ge_015"])
                 + _g(g["z_ge_196"]),
                 _s(r["snr_min"]), r["verdict"]))
    print("准入三列 = 非饱和(0.2<=p<=0.8) / 极差>=0.15 / z>=1.96，"
          "Y 通过 n 不通过 ? 无数据")
    print("SD理论 = 该 p 下单轮率的二项 SD（n=3），只作对照不进闸门；"
          "闸门噪声 = max(实测SD, %.4f 量化地板)" % SD_FLOOR)


def print_md(table):
    print("\n| 题号 | 维 | 判分器 | 稳健 | 三模型均值 | 极差(均分) | SD(均分) | "
          "极差(满分率) | SD(满分率) | 探针极差 | 最抖模型 | 信噪比(保守) | 判定 |")
    print("|---|---|---|---|---:|---:|---:|---:|---:|---:|---|---:|---|")
    for r in table:
        print("| `%s` | %s | `%s` | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |"
              % (r["id"], r["dim"], r["checker"], r["robustness"],
                 _f(r["p_mean"]), _f(r["spread_mean"]), _f(r["sd_mean_max"]),
                 _f(r["spread_full"]), _f(r["sd_full_max"]),
                 _f(r["spread_probe"]), r["sd_worst_model"] or "—",
                 _s(r["snr_min"]), r["verdict"]))


if __name__ == "__main__":
    main()
