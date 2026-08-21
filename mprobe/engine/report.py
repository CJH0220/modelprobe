# -*- coding: utf-8 -*-
"""输出层。

产物（写在 data/runs/<run_id>/ 下）：

    report.md     给人看的结论
    raw.jsonl     每次采样的完整记录（含响应原文）—— 复现的唯一依据
    matrix.jsonl  答对矩阵，喂给题目分析（IRT / 稳健性）
    summary.json  机器可读汇总，给 check / compare / 界面用

和 既有测评引擎 的差别，两条都是刻意的：

1. **不再产 review.md 和 manual_scores.jsonl。** 判分全自动，
   没有"待人填分"这个状态，也就没有"填了一半的报告"。
2. **报告第一屏是三层结论卡，不是分数表。** 分数表回答"考了多少"，
   结论卡回答"这个数能拿来说什么话"——后者才是被误用的地方。
"""

import json
import os

from .. import tiers
from . import dims

BAR = "-" * 60


def _pct(x, nd=1):
    return "—" if x is None else ("%.*f%%" % (nd, x * 100))


def _num(x, nd=1):
    return "—" if x is None else ("%.*f" % (nd, x))


def _write(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _dump(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------

def write_all(outdir, ctx, records, item_rows, dim_rows, quant,
              calib, runtime, skipped, agg=None, cost_stats=None,
              health=None):
    """ctx 由 cli 组装，字段见 _header()。"""
    os.makedirs(outdir, exist_ok=True)
    _dump(os.path.join(outdir, "raw.jsonl"), records)
    _matrix(outdir, ctx, item_rows)
    _report(outdir, ctx, item_rows, dim_rows, quant, calib, runtime,
            skipped, agg, cost_stats, health)
    return _summary(outdir, ctx, item_rows, dim_rows, quant, calib,
                    runtime, skipped, agg, cost_stats, health)


def _matrix(outdir, ctx, item_rows):
    """答对矩阵。带上 bank_rev 和 endpoint_sha —— 缺了这两个字段，
    几个月后没人能确定这份矩阵是哪套题、打到哪个端点上跑出来的。"""
    rows = []
    for r in item_rows.values():
        if not r["scores"]:
            continue
        rows.append({
            "run_id": ctx["run_id"],
            "model": ctx["model_key"],
            "bank_rev": ctx["bank_rev"],
            "endpoint_sha": ctx["endpoint_sha"],
            "item": r["id"], "dim": r["dim"],
            "n": len(r["scores"]),
            "k": r["full_pass"],
            "mean": r["mean"],
        })
    _dump(os.path.join(outdir, "matrix.jsonl"), rows)


# --------------------------------------------------------------------------
# 三层结论卡
# --------------------------------------------------------------------------

def conclusion_card(ctx, quant, dim_rows, health=None, verdict=None):
    """事实层 / 判读层 / 许可层。三个宿主（CLI、MCP、界面）共用这一份。

    分层的理由：这三句话的可靠性完全不同。
    事实层是记账，错了就是 bug；判读层依赖统计假设；
    许可层是从题量算出来的硬边界。混在一段话里，读的人会把
    第三层的确定性套到第一层上，或者反过来。
    """
    n_req = ctx["requests"]
    n_items = ctx["n_items"]
    tier = ctx.get("tier") or "custom"

    # σ 与分辨率一律按**计分请求数**算，不是总请求数。
    #
    # 冒烟维和观测题不进总分，拿它们充样本量会让这张卡印出一个
    # 比实际更乐观的分辨率 —— 而这张卡的全部作用就是防止误读。
    # 实测：qwen 全量档 759 请求里只有 678 个计分，
    # 分辨率的真实值是 3.6 分而不是 3.4 分。
    n_scored_req = ctx.get("scored_requests") or 0
    if not n_scored_req:
        scored_dims = [d for d, v in (dim_rows or {}).items()
                       if dims.in_score(d)]
        n_scored_req = sum((dim_rows[d].get("n_eff") or 0)
                           for d in scored_dims) or n_req
    md = tiers.min_detectable(n_scored_req)
    ci = tiers.ci_half(n_scored_req)

    fact = [
        "模型 %s（%s）· 题库 %s · %d 题 x %d 次 = %d 次请求"
        % (ctx["model_key"], ctx["endpoint_sha"], ctx["bank_rev"],
           n_items, ctx["trials"], n_req),
        "量化总分 **%s**（满分 100）" % _num(quant),
    ]
    if n_scored_req < n_req:
        fact.append("其中**计分请求 %d 个**（冒烟维与观测题不进总分）"
                    % n_scored_req)
    if health:
        fact.append("请求成功 %d/%d，失败率 %.1f%%"
                    % (health["requests"] - health["failed"],
                       health["requests"], health["fail_rate"] * 100))

    read = []
    if quant is not None:
        # 截到 [0,100]：分数本身就在这个区间里，报出 -7 分只会让人
        # 怀疑是算错了，而不是理解成「样本量不够，区间很宽」。
        read.append("真实水平的 95%% 区间约为 **%.1f ~ %.1f 分**"
                    % (max(0.0, quant - ci), min(100.0, quant + ci)))
    read.append("本档（%s，%d 次请求）的最小可检出退化是 **%.1f 分**"
                % (tier, n_req, md))
    if health and health["verdict"] == "unusable":
        read.append("**本轮结果不可用**：" + health["note"])
    elif health and health["verdict"] == "degraded":
        read.append("请求失败偏多，" + health["note"])
    if verdict:
        read.append(verdict)

    dim_counts = {d: v["n_items"] for d, v in dim_rows.items()}
    perm = tiers.permissions(tier if tier in tiers.TIERS else "small",
                             n_items, n_scored_req, dim_counts)

    # 首行固定是**分辨率边界**，不是分数。依据 MEASUREMENT.md 第七章：
    # 越档误用的第一种形态就是「小档说正常，所以没事」—— 而小档对
    # 12 分以下完全沉默，多数真实退化落在 6–15 分区间。
    #
    # 把边界放在分数后面等于没放：人读到一个数就停了。
    nxt = _finer_tier(tier)
    headline = "本档只能发现 **%.1f 分以上**的退化。" % md
    if nxt:
        headline += "要覆盖 %.1f 分级别，请用 %s 档。" % (
            tiers.min_detectable(tiers.get(nxt)["items"]
                                 * tiers.get(nxt)["trials"]), nxt)
    else:
        headline += "这已经是分辨率最高的档。"

    return {"headline": headline,
            "fact": fact, "read": read,
            "allow": perm["allow"], "deny": perm["deny"],
            "min_detectable": md, "ci_half": ci,
            "scored_requests": n_scored_req, "requests": n_req}


def _finer_tier(tier):
    """比当前档分辨率更高的下一档。没有就返回 None。"""
    order = [k for k in tiers.ORDER if k in tiers.TIERS]
    try:
        cur = tiers.get(tier)
    except tiers.TierError:
        return None
    finer = [k for k in order
             if tiers.get(k)["items"] * tiers.get(k)["trials"]
             > cur["items"] * cur["trials"]]
    return finer[0] if finer else None


def render_card(card):
    # 首行是边界，不是分数。读到一个数就停下的人，至少先读到了边界。
    L = ["## 结论卡", ""]
    if card.get("headline"):
        L += ["> " + card["headline"], ""]
    L += ["### 事实层（照抄记账，不含推断）", ""]
    L += ["- " + s for s in card["fact"]]
    L += ["", "### 判读层（含统计假设）", ""]
    L += ["- " + s for s in card["read"]]
    L += ["", "### 许可层（这个数能说什么话）", "",
          "**可以说：**", ""]
    L += ["- " + s for s in card["allow"]]
    L += ["", "**不能说：**", ""]
    L += ["- " + s for s in card["deny"]]
    return L


# --------------------------------------------------------------------------

def _header(ctx, skipped):
    L = ["# 测评报告", "",
         "| 项 | 值 |", "|---|---|",
         "| run_id | `%s` |" % ctx["run_id"],
         "| 被测模型 | **%s** |" % ctx["model_key"],
         "| 接口 | `%s` · `%s` |" % (ctx["base_url"], ctx["model_name"]),
         "| 端点指纹 | `%s` |" % ctx["endpoint_sha"],
         "| 题库版本 | `%s` |" % ctx["bank_rev"],
         "| 档位 | %s |" % (ctx.get("tier") or "custom"),
         "| temperature | %s |" % ctx["temperature"],
         "| 采样 | %d 题 x %d 次 |" % (ctx["n_items"], ctx["trials"]),
         "| 开始时间 | %s |" % (ctx.get("started_str")
                                    or ctx.get("started_at") or "—"),
         ""]
    for w in ctx.get("warnings", []):
        L.append("> ⚠ %s" % w)
    if skipped:
        L += ["", "> 跳过 %d 题：%s" % (
            len(skipped), "；".join("%s(%s)" % s for s in skipped[:6]))]
    return L


def _dim_section(L, dim_rows):
    L += ["", "## 一、各维度得分", "",
          "| 维度 | 题数 | 得分 | 95% 区间 | 阈值 ±T | pass^3 | 采样极差 | 权重 |",
          "|---|---:|---:|:---:|---:|---:|---:|---:|"]
    hidden = []
    for d in sorted(dim_rows.values(), key=lambda x: x["dim"]):
        show, style, _why = tiers.dim_display(d["n_items"])
        if not show:
            hidden.append("%s(%d题)" % (dims.label(d["dim"]), d["n_items"]))
            continue
        t = tiers.dim_threshold(d["n_items"])
        mark = "" if style == "solid" else " ~"
        L.append("| %s%s | %d | %s | %s ~ %s | %.1f | %s | %s | %.1f |" % (
            dims.label(d["dim"]), mark, d["n_items"], _pct(d["score"]),
            _pct(d["ci_lo"], 0), _pct(d["ci_hi"], 0), t,
            _pct(d["pass_hat_k"], 0), _num(d["spread_mean"], 2), d["weight"]))
    L += ["",
          "> `阈值 ±T` = 这个维度上，多大的分差才不是采样噪声（T = 2σ）。",
          "> 维度名后带 `~` 的题数在 6~11 之间，只能看趋势，不能单独下判定。",
          "> `pass^3` = 连续 3 次全部满分的比例，即**有效可用率**；"
          "它和平均分差得多说明不稳定。"]
    if hidden:
        L += ["",
              "> **以下维度题数 <6，本报告不给分数**（2σ 阈值超过 30 分，"
              "任何数字都会被误读）：%s" % "、".join(hidden)]
    return L


def _item_section(L, item_rows):
    L += ["", "## 二、逐题结果", "",
          "| 题号 | 维 | 标题 | 得分 | 满分次数 | 极差 | P50 延迟 | 判分说明 |",
          "|---|---|---|---:|---:|---:|---:|---|"]
    for r in sorted(item_rows.values(), key=lambda x: (x["dim"], x["id"])):
        det = ""
        for d in r["details"]:
            if d:
                det = d[:46]
                break
        L.append("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r["id"], dims.label(r["dim"], "·"), (r.get("title") or "")[:22],
            _pct(r["mean"]),
            "%d/%d" % (r["full_pass"], len(r["scores"])) if r["scores"] else "—",
            _num(r["spread"], 2), _num(r["latency_p50"], 0),
            det.replace("|", "/")))

    obs = [r for r in item_rows.values() if r.get("observe")]
    if obs:
        L += ["", "### 观测项（不计分）", "",
              "| 题号 | 维 | 标题 | 记录值 |", "|---|---|---|---|"]
        for r in sorted(obs, key=lambda x: x["id"]):
            det = next((d for d in r["details"] if d), "")
            L.append("| %s | %s | %s | %s |" % (
                r["id"], dims.label(r["dim"], "·"), (r.get("title") or "")[:22],
                det[:60].replace("|", "/")))
        L += ["", "> 这些题只记录不打分——它们量的是倾向（比如思维链长度），"
              "没有对错，硬给个分数只会污染总分。"]

    weak = sorted([r for r in item_rows.values()
                   if not r.get("observe")
                   and r["mean"] is not None and r["mean"] < 0.6],
                  key=lambda x: x["mean"])
    if weak:
        L += ["", "## 三、失分集中处（得分 < 60%）", ""]
        for r in weak[:15]:
            det = next((d for d in r["details"] if d), "")
            L.append("- **%s**（%s）%s —— %s　`%s`" % (
                r["id"], dims.describe(r["dim"]), r.get("title", ""),
                _pct(r["mean"]), det[:70]))
    return L


def _runtime_section(L, runtime, health):
    L += ["", "## 四、运行时", "",
          "| 指标 | 值 |", "|---|---:|",
          "| 请求总数 | %d |" % runtime["requests"],
          "| 失败请求 | %d |" % runtime["failed"],
          "| 触发重试 | %d |" % runtime["retried"],
          "| 输出被截断 | %d（其中 %d 次完全没有可见内容） |" % (
              runtime.get("truncated", 0), runtime.get("truncated_empty", 0)),
          "| 延迟 P50 / P95 | %s / %s ms |" % (
              _num(runtime["latency_p50"], 0), _num(runtime["latency_p95"], 0)),
          "| 平均输出 token | %s |" % _num(runtime["out_tokens_mean"], 0),
          "| 输入 / 输出 token 合计 | %s / %s |" % (
              runtime["in_tokens_total"], runtime["out_tokens_total"])]
    if health:
        L += ["", "**请求健康度：%s** —— %s" % (health["verdict"], health["note"])]
    if runtime.get("truncated_empty"):
        L += ["",
              "> ⚠ 有 %d 次输出被 `max_tokens` 截断且没有可见内容，"
              "这些题记了 0 分，但那**不代表模型不会做**。"
              % runtime["truncated_empty"],
              "> 推理模型常把 token 全烧在思考上——把 `max_tokens` 调大"
              "（推理模型建议 16000 以上）后重测。",
              "> 涉及题号：`%s`" % "`、`".join(runtime.get("truncated_ids", [])[:12])]
    return L


def _calib_section(L, calib):
    if not calib:
        return L
    L += ["", "## 五、置信度校准", "",
          "| 指标 | 值 | 判读 |", "|---|---:|---|",
          "| Brier | %.4f | 越小越好，0.25 相当于瞎猜 |" % calib["brier"],
          "| 校准度 rel | %.4f | 越小越好 |" % calib["reliability"],
          "| 分辨度 res | %.4f | 越大越好 |" % calib["resolution"],
          "| ECE | %.4f | 越小越好 |" % calib["ece"],
          "| 高置信箱(≥90)正确率 | %s | 理想 ≥90%% |" % _pct(calib["high_conf_acc"])]
    if calib["avg_group_size"] < 3:
        L += ["", "> ⚠ 平均每个置信度值仅 %.1f 个样本，分辨度会系统性虚高。"
              % calib["avg_group_size"]]
    return L


def _agg_section(L, agg, ctx):
    from . import aggregate as agg_mod
    if not agg:
        return L
    L += ["", "## 六、聚合得分", "",
          "| 口径 | 分数 | 含义 |", "|---|---:|---|",
          "| 期望分 | **%.1f** | 收缩后的后验均值 |" % agg["expected"],
          "| **保守分 μ−%.0fσ** | **%.1f** | 把不确定性折成扣分，**跨轮对比用这个** |"
          % (agg["k_sigma"], agg["conservative"]),
          "| 短板分（几何平均） | **%.1f** | 任一维崩掉就会拉垮，暴露被平均掩盖的弱项 |"
          % agg["geometric"],
          "",
          "聚合标准差 %.1f 分　｜　参与维度 %d 个，采样 %d 次"
          % (agg["sd"], agg["n_dims"], agg["n_trials"]), ""]
    for line in agg_mod.explain(agg):
        L.append("- " + line)
    L += ["", "### 各维度收缩明细", "",
          "| 维度 | 原始 | 收缩后 | 样本数 | 拉动 | 权重 |",
          "|---|---:|---:|---:|---:|---:|"]
    for d, v in sorted(agg["per_dim"].items(), key=lambda kv: kv[1]["mu"]):
        L.append("| %s | %s | %s | %d | %+.1f%% | %.1f |" % (
            dims.label(d), _pct(v["raw"]), _pct(v["mu"]), v["n_eff"],
            v["shrink_pull"] * 100, v["weight"]))
    L += ["", "> 收缩把样本少的维度拉向全局均值（本轮先验中心 %.1f%%，强度 %.1f）。"
          % (agg["prior_mean"] * 100, agg["prior_strength"]),
          "> 这是刻意的：2 道题的维度不该和 11 道题的维度同等影响总分。"]
    return L


def _cost_section(L, cost_stats, quant):
    from . import cost as cost_mod
    L += ["", "## 七、成本", ""]
    if not cost_stats:
        L += ["未配置价格，本轮未计算成本。在 `config/pricing.json` 里补上即可。", "",
              "> 值得配。公开榜单的复现数据里，**得分最低的模型可能最贵**——"
              "SWE-bench 上 o1-mini 得分 27.2% 花了 $367，"
              "而 38.0% 的 Claude 3.5 Sonnet 只花 $67。"]
        return L
    c = cost_stats
    cur = c["currency"]
    L += ["| 指标 | 值 | 说明 |", "|---|---:|---|",
          "| 总花费 | **%.4f %s** | 本轮全部请求 |" % (c["total_cost"], cur),
          "| 每题成本 | %.5f %s | 预算规划看这个 |" % (c["cost_per_item"] or 0, cur),
          "| **每正确答案成本** | **%s %s** | **真实性价比** |" % (
              ("%.5f" % c["cost_per_correct"]) if c["cost_per_correct"] else "—", cur),
          "| 每请求输出 token | %s | 啰嗦程度，与延迟直接相关 |" % _num(
              c["out_tokens_per_request"], 0),
          "| 输入 / 缓存 / 输出 token | %s / %s / %s | |" % (
              c["in_tokens"], c["cached_tokens"], c["out_tokens"]), ""]
    note = cost_mod.frontier_note(quant, c)
    if note:
        L += ["> " + note, ""]
    L += ["> 成本**不计入总评分**。把质量和成本压成一个数会掩盖二者的权衡；"
          "正确做法是并列呈现，按预算自己取舍。"]
    return L


def _report(outdir, ctx, item_rows, dim_rows, quant, calib, runtime,
            skipped, agg, cost_stats, health):
    L = _header(ctx, skipped)
    L += [""] + render_card(conclusion_card(ctx, quant, dim_rows, health))
    _dim_section(L, dim_rows)
    _item_section(L, item_rows)
    _runtime_section(L, runtime, health)
    _calib_section(L, calib)
    _agg_section(L, agg, ctx)
    _cost_section(L, cost_stats, quant)
    L += ["", BAR, "",
          "原始记录：`raw.jsonl`（%d 条）　答对矩阵：`matrix.jsonl`　"
          "机器可读：`summary.json`" % runtime["requests"]]
    _write(os.path.join(outdir, "report.md"), L)


def _summary(outdir, ctx, item_rows, dim_rows, quant, calib, runtime,
             skipped, agg, cost_stats, health):
    card = conclusion_card(ctx, quant, dim_rows, health)
    data = {
        "run_id": ctx["run_id"],
        "model_key": ctx["model_key"],
        "endpoint_sha": ctx["endpoint_sha"],
        "bank_rev": ctx["bank_rev"],
        "tier": ctx.get("tier"),
        "trials": ctx["trials"],
        "n_items": ctx["n_items"],
        "requests": ctx["requests"],
        "started_at": ctx.get("started_at"),
        "finished_at": ctx.get("finished_at"),
        "endpoint": ctx.get("endpoint_redacted"),
        "quant_score": quant,
        "conservative": (agg or {}).get("conservative"),
        "aggregate": agg,
        "cost": cost_stats,
        "health": health,
        "card": card,
        "dims": {k: dict(v) for k, v in dim_rows.items()},
        "items": {k: {kk: vv for kk, vv in v.items() if kk != "details"}
                  for k, v in item_rows.items()},
        "calibration": calib,
        "runtime": runtime,
        "skipped": skipped,
    }
    path = os.path.join(outdir, "summary.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data
