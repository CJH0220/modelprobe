# -*- coding: utf-8 -*-
"""命令行：唯一真源。

MCP 工具、Skill、浏览器界面都只是这一层的门面，不允许有第二套逻辑。
理由很实际：同一件事有两处实现，就一定会在某次改动后给出两个不同的
答案，而这类分歧几乎不可能被发现——两边看起来都在正常工作。

三条硬规矩（DESIGN 第 3 章）
---------------------------
1. 每个子命令都支持 ``--json``。人读的和机器读的是同一份数据，
   不是两次各自组装的。
2. 只读命令**不写任何文件**。``status`` / ``compare`` / ``bank info``
   在只读目录下必须能跑。
3. 花钱的命令，执行前先打印钱和时间，超过确认线要人点头。

退出码
------
    0   正常（含「观察」态）
    1   告警：连续多轮低于阈值
    2   用法或配置错误
    3   本轮结果不可用（请求失败率过高、无有效分数）
"""

import argparse
import json
import os
import sys
import time

from . import BANK_REV, __version__, estimate as estmod, paths, profiles, tiers
from .engine import aggregate as aggmod
from .engine import bank as bankmod
from .engine import client as clientmod
from .engine import cost as costmod
from .engine import dims
from .engine import endpoint
from .engine import metrics
from .engine import progress as progmod
from .engine import report
from .engine import runner
from .engine import secrets
from .engine import signals
from .monitor import baseline as blmod
from .monitor import judge as judgemod
from .monitor import notify
from .monitor import schedule
from .store import db

OK, ALERT_CODE, USAGE, UNUSABLE = 0, 1, 2, 3

#: 连通探针的超时。断网时 DNS/连接会立刻失败，这个数只在
#: 「能连上但对方不回」的情况下起作用——那种情况也没必要等满 120 秒。
#:
#: 从 10 秒抬到 30 秒。10 秒对推理模型太紧：qwen3.6-flash 实测平均
#: 22.5 秒/请求（校准 24 条里 23 条成功），探针却在 10 秒判它「渠道不通」，
#: **而这道闸门挡在建 run 目录之前，整轮直接不跑**。
#: 把「慢但活着」误判成「不通」，是这道闸门自己的假阳性 ——
#: 它拦掉的不是坏渠道，是好渠道。
#:
#: 单个端点可以在 config/models/*.json 里用 `probe_timeout` 覆盖。
PROBE_TIMEOUT = 30


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------

def _p(*lines):
    for x in lines:
        if isinstance(x, (list, tuple)):
            _p(*x)
        else:
            try:
                print(x)
            except UnicodeEncodeError:
                # Windows 控制台代码页装不下的字符，用替代符号打出来，
                # 但不要因此让整条命令失败——文件里的内容是完整的。
                enc = sys.stdout.encoding or "ascii"
                print(str(x).encode(enc, "replace").decode(enc, "replace"))


def _emit(args, data, lines):
    if getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        _p(lines)
    return data


def _readonly_db(args):
    """只读命令用。库不存在就返回 None——**不创建**。

    DESIGN 第 3 章第 2 条：只读命令不写任何文件。connect() 会顺手
    建库建表，在只读目录下会失败，在正常目录下会留下一个空库，
    让「从没跑过」看起来像「跑过但没数据」。
    """
    if not os.path.isfile(paths.DB):
        return None
    return db.connect(paths.DB)


def _no_db(args, what="记录"):
    return _emit(args, {"runs": [], "empty": True},
                 ["还没有任何%s（%s 不存在）。先跑一轮 "
                  "`mprobe eval --model <端点> --tier small`。"
                  % (what, paths.DB)])


def _die(msg, code=USAGE, as_json=False):
    if as_json:
        print(json.dumps({"error": str(msg)}, ensure_ascii=False, indent=2))
    else:
        sys.stderr.write("错误：%s\n" % msg)
    raise SystemExit(code)


def _confirm(prompt):
    """交互确认。非交互环境（计划任务、CI）一律视为「不同意」，
    因为那里没有人可以点头，默认放行等于把闸门拆了。"""
    if not sys.stdin or not sys.stdin.isatty():
        return False
    try:
        return input(prompt).strip().lower() in ("y", "yes", "是")
    except (EOFError, KeyboardInterrupt):
        return False


# --------------------------------------------------------------------------
# 公共装配
# --------------------------------------------------------------------------

def _endpoint(args, need_key=True):
    key = args.model or endpoint.default_key()
    if not key:
        _die("没有配置任何端点。看 config/models/_template.json，"
             "复制一份改成你的渠道。", as_json=getattr(args, "json", False))
    try:
        cfg = endpoint.load(key, need_key=need_key)
    except endpoint.EndpointError as e:
        _die(e, as_json=getattr(args, "json", False))
    if getattr(args, "trials", None):
        cfg["run"]["trials"] = int(args.trials)
    if getattr(args, "concurrency", None):
        cfg["run"]["concurrency"] = int(args.concurrency)
    return cfg


def _plan(args, cfg, monitor_default=False):
    """monitor_default 由**命令**决定，不由档案决定。

    依据 MEASUREMENT.md 3.3：「测评准入是监控准入的超集，
    监控在**加载时**按 MANIFEST 的 monitor_ok 过滤」。
    所以 `eval` 永不过滤（要测的是全部能力），
    `check` / `baseline --build` 一律过滤（判定只能用验证过的题）。

    早先这个开关写在 `profiles/*.json` 里，后果是 `eval --tier small`
    也被监控闸门过滤了 —— 两件事共用一个开关，改一个必然影响另一个。
    """
    mo = getattr(args, "monitor_only", None)
    if mo is None:
        mo = monitor_default
    try:
        plan = profiles.resolve(args.tier, monitor_only=mo)
    except (profiles.ProfileError, bankmod.BankError) as e:
        _die(e, as_json=getattr(args, "json", False))

    items = plan["items"]
    if getattr(args, "items", None):
        want = [x.strip() for x in args.items.split(",") if x.strip()]
        by_id = {i["id"]: i for i in items}
        missing = [x for x in want if x not in by_id]
        if missing:
            _die("这些题不在本档选题里：%s。用 `mprobe bank info` 看题号。"
                 % "、".join(missing), as_json=getattr(args, "json", False))
        items = [by_id[x] for x in want]
    if getattr(args, "dim", None):
        keep = {x.strip().upper() for x in args.dim.split(",") if x.strip()}
        items = [i for i in items if i["dim"] in keep]
        if not items:
            _die("按维度 %s 筛完一道题都不剩" % args.dim,
                 as_json=getattr(args, "json", False))
    plan["items"] = items
    plan["n_items"] = len(items)
    plan["trials"] = cfg["run"]["trials"]
    plan["requests"] = len(items) * cfg["run"]["trials"]
    return plan


def _measured_pace(model_key, tier=None):
    """本机历史实测「秒/请求」中位数。返回 (秒, 来源说明) 或 (None, 说明)。

    依据 MEASUREMENT.md 6.3：耗时和成本**不可跨档外推**
    （已实测到成本差 6 倍、耗时差 18 倍）。所以：

      · 优先同档历史 —— 小档的题短、全量档的题长，混在一起取中位数没有意义
      · 没有同档数据时用跨档的，但**必须说明这是跨档外推**
      · 一条都没有就如实说「这台机器还没跑过」

    公式捕捉不到限速网关的自动退避（实测同一轮里单请求耗时变了 6 倍），
    所以有实测就用实测。
    """
    if not os.path.isfile(paths.DB):
        return None, "本机还没有任何历史（结果库不存在）"
    try:
        con = db.connect(paths.DB)
        rows = con.execute(
            "select tier, started_at, finished_at, requests from runs "
            "where model_key = ? and requests > 0 and finished_at is not null "
            "order by started_at desc limit 40", (model_key,)).fetchall()
    except Exception:
        return None, "读历史失败，退回公式估算"

    def med(rs):
        v = sorted((f - s) / r for _t, s, f, r in rs
                   if f and s and r and f > s)
        return v[len(v) // 2] if v else None

    same = [r for r in rows if tier and r[0] == tier]
    if same:
        p = med(same)
        if p:
            return p, "本机同档（%s）%d 轮实测中位数 %.1f 秒/请求" % (
                tier, len(same), p)
    if rows:
        p = med(rows)
        if p:
            return p, ("本机 %d 轮实测中位数 %.1f 秒/请求，"
                       "**但不是同档——跨档外推，实测偏差可达数倍**"
                       % (len(rows), p))
    return None, "这台机器还没跑过这个端点，没有实测数可用"


def _plan_lines(plan, est, cfg):
    L = ["", "== 本轮计划 ==",
         "模型 %s（%s）· 档位 %s · 题库 %s"
         % (cfg["key"], cfg["endpoint_sha"], plan["tier"], BANK_REV),
         # 一律用**计分请求数**，不是总请求数：冒烟维和观测题不进总分，
         # 拿它们充样本量会让这里印出一个比实际更乐观的分辨率。
         "最小可检出退化 %.1f 分 ｜ 95%% 区间半宽 ±%.1f 分"
         % (plan["min_detectable"], plan["ci_half"])]
    L += estmod.render(est)
    for w in plan.get("warnings", []):
        L.append("⚠ " + w)
    for w in cfg.get("_warnings", []):
        L.append("⚠ " + w)
    if plan.get("skipped"):
        L.append("跳过 %d 题：%s" % (
            len(plan["skipped"]),
            "；".join("%s(%s)" % s for s in plan["skipped"][:6])))
    return L


def _gate(args, est):
    """成本闸门（实施计划第 0.9 项）。"""
    need, why = estmod.needs_confirm(est, getattr(args, "confirm_above", None))
    if not need or getattr(args, "yes", False):
        return
    if not _confirm("\n%s。继续？[y/N] " % why):
        _die("已取消。确认后可加 --yes 直接执行（计划任务里必须加）。",
             code=USAGE, as_json=getattr(args, "json", False))


def _probe(cfg, as_json=False):
    """连通闸门（实施计划第 0.10 项）。**必须在建 run 目录之前调用**——
    渠道不通时留下一个半截目录，下次 status 会把它当成跑挂了的任务。"""
    m = dict(cfg["model"])
    limit = m.get("probe_timeout") or PROBE_TIMEOUT
    m["timeout"] = min(m.get("timeout") or 120, limit)
    m["max_tokens"] = min(m.get("max_tokens") or 64, 64)
    rc = dict(cfg["run"])
    rc["retries"] = 0          # 探针不重试：要的就是「不通」这个结论来得快
    t0 = time.time()
    try:
        text, meta = clientmod.ModelClient(m, rc).probe()
    except Exception as e:
        _die("渠道不通（%.1f 秒后放弃）：%s\n"
             "先查：网络/代理、base_url 是否写对、密钥是否过期。"
             "本次没有产生任何 run 目录。" % (time.time() - t0, e),
             code=USAGE, as_json=as_json)
    return {"latency_ms": meta.get("latency_ms"), "sample": text}


#: run_id 允许的字符。只用于外部传入的 id ——
#: 它会直接拼进文件路径，不校验等于给了一条路径穿越的口子。
RUN_ID_OK = set("abcdefghijklmnopqrstuvwxyz"
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


def _run_id(cfg, tier, kind, want=None):
    """run_id 精确到秒，但秒不够用：跑单题的快测一秒能跑完两轮，
    撞号的两轮会 INSERT OR REPLACE 成一条，前一轮**静默消失**。
    撞上就往后加 b、c…… 让它可见。

    want 是调用方指定的 id（`--run-id`）。MCP 层需要它：
    `eval_start` 必须**立刻**返回 run_id，而 run 目录是子进程建的，
    靠猜时间戳会有竞态。让调用方定 id，两边就都不用猜。
    """
    if want:
        bad = set(want) - RUN_ID_OK
        if bad or not want:
            _die("run-id 只允许字母数字和 - _ .，收到 %r（非法字符：%s）"
                 % (want, "".join(sorted(bad))))
        if os.path.exists(paths.run_dir(want)):
            _die("run-id %s 已存在。**不覆盖** —— 覆盖会让前一轮静默消失。"
                 % want)
        return want
    base = "%s-%s-%s-%s" % (cfg["key"], tier, kind,
                            time.strftime("%Y%m%d-%H%M%S"))
    rid, n = base, 0
    while os.path.exists(paths.run_dir(rid)):
        n += 1
        rid = "%s-%s" % (base, chr(ord("b") + n - 1))
    return rid


def _weigh(dim_rows):
    """维度权重 = 该维的有效题数。

    等权是错的：题库里 S 维 26 题、E 维 1 题，等权会让 1 道题的
    维度和 26 道题的维度对总分影响一样大。而 σ 的整套算法（二项下界、
    最小可检出量）都建立在「总分是全部请求上的通过率」这个口径上，
    按题数加权才和它自洽。
    """
    for d in dim_rows.values():
        d["weight"] = float(d.get("n_scored") or 0)
    return lambda k: float(dim_rows[k]["n_scored"] or 0)


def _execute(args, cfg, plan, kind):
    """跑一轮，落盘，入库。eval 和 check 共用。"""
    est = estmod.estimate(plan["items"], cfg, trials=cfg["run"]["trials"],
                          out_tokens=getattr(args, "out_tokens", None),
                          measured_pace=_measured_pace(cfg["key"],
                                                       plan.get("tier")))
    if not getattr(args, "json", False):
        _p(_plan_lines(plan, est, cfg))
    if getattr(args, "dry_run", False):
        return None, None, est
    _gate(args, est)
    probe = _probe(cfg, as_json=getattr(args, "json", False))

    run_id = _run_id(cfg, plan["tier"], kind,
                     getattr(args, "run_id", None))
    outdir = paths.run_dir(run_id, create=True)
    started = time.time()
    prog = progmod.Progress(os.path.join(outdir, "progress.json"),
                            total=plan["requests"], run_id=run_id,
                            tier=plan["tier"], model=cfg["key"])
    prog.start()

    raw_path = os.path.join(outdir, "raw.jsonl")
    raw_fh = open(raw_path, "w", encoding="utf-8")

    def on_record(rec):
        raw_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        raw_fh.flush()

    if not getattr(args, "json", False):
        _p("", "== 开始 == run_id %s" % run_id,
           "探针 %s ms ｜ 输出目录 %s" % (probe.get("latency_ms"), outdir))

    client = clientmod.ModelClient(cfg["model"], cfg["run"])
    try:
        records = runner.run(plan["items"], client, cfg,
                             progress=prog, on_record=on_record)
    except KeyboardInterrupt:
        prog.fail("用户中断")
        raw_fh.close()
        _die("已中断。部分结果留在 %s/raw.jsonl，但**不入库**——"
             "不完整的一轮进了曲线就是一次假的下降。" % outdir,
             code=USAGE, as_json=getattr(args, "json", False))
    except Exception as e:
        prog.fail(str(e))
        raw_fh.close()
        _die("运行失败：%s（进度见 %s/progress.json）" % (e, outdir),
             code=UNUSABLE, as_json=getattr(args, "json", False))
    finally:
        raw_fh.close()

    item_rows = metrics.per_item(records)
    dim_rows = metrics.per_dim(item_rows)
    wf = _weigh(dim_rows)
    quant = metrics.overall(dim_rows)
    agg = aggmod.aggregate(dim_rows, wf)
    calib = metrics.calibration(records)
    runtime = metrics.runtime_stats(records)
    health = runner.health(records)
    cost_stats = costmod.compute(records, item_rows, cfg["pricing"])

    finished = time.time()
    ctx = {
        "run_id": run_id,
        "kind": kind,
        "model_key": cfg["key"],
        "base_url": cfg["model"]["base_url"],
        "model_name": cfg["model"]["model"],
        "endpoint_sha": cfg["endpoint_sha"],
        "bank_rev": plan["manifest"]["bank_rev"],
        "tier": plan["tier"],
        "temperature": cfg["model"]["temperature"],
        "n_items": plan["n_items"],
        "trials": cfg["run"]["trials"],
        "requests": len(records),
        "started_at": started,
        "started_str": time.strftime("%Y-%m-%d %H:%M:%S",
                                     time.localtime(started)),
        "finished_at": finished,
        "elapsed_sec": round(finished - started, 1),
        "warnings": list(plan.get("warnings", [])) + list(cfg.get("_warnings", [])),
        "endpoint_redacted": endpoint.redact(cfg),
        "estimate": {k: v for k, v in est.items() if k != "per_item_in"},
        "note": getattr(args, "note", "") or "",
    }

    summary = report.write_all(outdir, ctx, records, item_rows, dim_rows,
                               quant, calib, runtime, plan.get("skipped") or [],
                               agg=agg, cost_stats=cost_stats, health=health)

    # 行为信号单独落盘：report.write_all 的四件产物是固定的，
    # 信号还在观察阶段（实施计划 1.x），不进正式报告免得被当成结论。
    with open(os.path.join(outdir, "signals.json"), "w", encoding="utf-8") as f:
        json.dump({"aggregate": signals.aggregate(records),
                   "efficiency": signals.efficiency(records, item_rows)},
                  f, ensure_ascii=False, indent=2)

    prog.finish(cost=(cost_stats or {}).get("total_cost"))

    if not getattr(args, "no_store", False):
        con = db.connect(paths.DB)
        db.save_run(con, summary, kind=kind, outdir=outdir)
        con.close()

    return summary, outdir, est


# --------------------------------------------------------------------------
# eval
# --------------------------------------------------------------------------

def cmd_eval(args):
    cfg = _endpoint(args, need_key=not args.dry_run)
    plan = _plan(args, cfg)
    summary, outdir, est = _execute(args, cfg, plan, kind="eval")
    if summary is None:                       # --dry-run
        return _emit(args, {"dry_run": True, "estimate": est,
                            "plan": {"tier": plan["tier"],
                                     "n_items": plan["n_items"],
                                     "requests": plan["requests"]}},
                     [])

    mins = (summary["finished_at"] - summary["started_at"]) / 60.0
    L = ["", "耗时 %.1f 分钟 ｜ 报告 %s"
         % (mins, os.path.join(outdir, "report.md")), ""]
    L += report.render_card(summary["card"])
    code = UNUSABLE if (summary.get("health") or {}).get(
        "verdict") == "unusable" else OK
    _emit(args, summary, L)
    return code


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------

def _notify_cfg():
    p = os.path.join(paths.CONFIG, "notify.json")
    if not os.path.isfile(p):
        return dict(notify.DEFAULT)
    with open(p, "r", encoding="utf-8-sig") as f:
        return dict(notify.DEFAULT, **json.load(f))


def cmd_check(args):
    cfg = _endpoint(args, need_key=not args.dry_run)
    # 判定只能用通过监控准入的题
    plan = _plan(args, cfg, monitor_default=True)
    summary, outdir, est = _execute(args, cfg, plan, kind="check")
    if summary is None:
        return _emit(args, {"dry_run": True, "estimate": est}, [])

    score = summary.get("conservative")
    if score is None:
        score = summary.get("quant_score")

    con = db.connect(paths.DB)
    key = (summary["model_key"], summary["endpoint_sha"],
           summary["bank_rev"], summary["tier"])
    bl = db.active_baseline(con, *key)
    hints = []
    if not bl:
        stale = db.stale_baselines(con, summary["model_key"],
                                   summary["endpoint_sha"],
                                   summary["bank_rev"], summary["tier"])
        if stale:
            hints.append("这条序列没有生效基线，但同一个模型下有别的基线："
                         + "；".join("题库%s/端点%s/%s档（%d轮）"
                                     % (s["bank_rev"], s["endpoint_sha"],
                                        s["tier"], s["rounds"])
                                     for s in stale[:4])
                         + "。题库或端点变了就要重建基线，不能沿用。")
        else:
            hints.append("还没有基线。先跑 %d 轮 eval，再执行 "
                         "`mprobe baseline --model %s --tier %s --build`。"
                         % (plan.get("baseline_rounds") or 5,
                            summary["model_key"], summary["tier"]))

    prev = db.recent_checks(con, *key, limit=10) if bl else []
    n_scored = (summary.get("health") or {}).get("scored")
    state, msg, detail = judgemod.judge(bl, score, prev, n_trials=n_scored)

    if bl:
        db.save_check(con, {
            "run_id": summary["run_id"], "baseline_id": bl["id"],
            "model_key": summary["model_key"],
            "endpoint_sha": summary["endpoint_sha"],
            "bank_rev": summary["bank_rev"], "tier": summary["tier"],
            "score": score, "delta": detail.get("delta"),
            "z": detail.get("z"), "state": state,
            "consecutive": detail.get("consecutive", 0),
            "reason": msg, "created_at": time.time()})
    con.close()

    card = judgemod.render(state, msg, detail, score=score, baseline=bl)
    sent, why = (False, "已禁用推送")
    if not getattr(args, "no_notify", False):
        # 推送**绝不能**让一次已经完成的判定失败。这一轮已经花了钱、
        # 已经写了库、已经得出结论；因为一个 webhook 出错而把这些丢掉
        # 是本末倒置。notify 内部已经保证不抛，这里再兜一层。
        try:
            sent, why = notify.maybe_send(
                _notify_cfg(),
                {"model_key": summary["model_key"],
                 "endpoint_sha": summary["endpoint_sha"],
                 "bank_rev": summary["bank_rev"], "tier": summary["tier"]},
                state, msg, detail, judgemod.triage_steps())
        except Exception as e:
            sent, why = False, "推送异常（已忽略，不影响判定）：%s: %s" % (
                type(e).__name__, e)

    L = ["", "== 判定 == %s %s"
         % (judgemod.STATE_ICON.get(state, "?"),
            judgemod.STATE_LABEL.get(state, state)), ""]
    L += ["- " + x for x in card["fact"]]
    L += [""] + ["- " + x for x in card["read"]]
    if card["allow"]:
        L += ["", "可以说："] + ["- " + x for x in card["allow"]]
    if card["deny"]:
        L += ["", "不能说："] + ["- " + x for x in card["deny"]]
    if state == judgemod.ALERT:
        L += ["", "排查顺序："] + ["%d. %s" % (i + 1, s)
                                   for i, s in enumerate(judgemod.triage_steps())]
    for h in hints:
        L += ["", "提示：" + h]
    L += ["", "推送：%s" % ("已发送" if sent else why),
          "报告 %s" % os.path.join(outdir, "report.md")]

    data = {"run_id": summary["run_id"], "state": state, "score": score,
            "message": msg, "detail": detail, "baseline": bl,
            "card": card, "hints": hints, "notified": sent,
            "outdir": outdir, "health": summary.get("health")}
    _emit(args, data, L)
    if state == judgemod.ALERT:
        return ALERT_CODE
    if (summary.get("health") or {}).get("verdict") == "unusable":
        return UNUSABLE
    return OK


# --------------------------------------------------------------------------
# baseline
# --------------------------------------------------------------------------

def cmd_baseline(args):
    cfg = _endpoint(args, need_key=False)
    tier = args.tier
    con = db.connect(paths.DB) if args.build else _readonly_db(args)
    if con is None:
        return _no_db(args, "基线")
    key = (cfg["key"], cfg["endpoint_sha"], BANK_REV, tier)

    if not args.build:
        bl = db.active_baseline(con, *key)
        con.close()
        if not bl:
            return _emit(args, {"baseline": None},
                         ["这条序列还没有基线（%s / %s / %s / %s档）。"
                          % key, "先跑几轮 eval，再加 --build。"])
        return _emit(args, {"baseline": bl},
                     ["== 基线 =="] + blmod.explain(bl)
                     + ["建自 run：" + "、".join(bl["run_ids"])])

    rows = [r for r in db.series(con, cfg["key"], cfg["endpoint_sha"],
                                 BANK_REV, tier=tier)
            if r.get("conservative") is not None
            and r.get("health") != "unusable"]
    if args.rounds:
        rows = rows[-int(args.rounds):]
    if not rows:
        con.close()
        _die("没有可用于建基线的轮次。要求：同模型 / 同端点指纹 / 同题库 / "
             "同档位，且请求健康度不是 unusable。",
             as_json=getattr(args, "json", False))

    rounds = []
    for r in rows:
        full = db.get_run(con, r["run_id"]) or {}
        s = json.loads(full.get("summary") or "{}")
        # σ 的样本量必须是**计分请求数**，不是总请求数。
        #
        # 冒烟维和观测题不进总分，拿它们充样本量会让 n 偏大 → 二项 σ 偏小
        # → 阈值贴着均值 → **更容易误报**。而本项目的取向是
        # 「允许漏报、不允许频繁误报」，所以这个方向的错最要命。
        # 实测 monitor 档：42 个请求里只有 36 个计分，
        # σ 7.104 vs 7.673，阈值 55.29 vs 54.15。
        n_scored = ((s.get("card") or {}).get("scored_requests")
                    or r["requests"])
        rounds.append({"run_id": r["run_id"], "score": r["conservative"],
                       "requests": n_scored,
                       "dims": {k: v.get("score") for k, v in
                                (s.get("dims") or {}).items()}})
    try:
        bl = blmod.build(cfg["key"], cfg["endpoint_sha"], BANK_REV, tier, rounds)
    except blmod.BaselineError as e:
        con.close()
        _die(e, as_json=getattr(args, "json", False))
    bl["id"] = db.save_baseline(con, bl)
    con.close()
    return _emit(args, {"baseline": bl},
                 ["== 已建立基线 =="] + blmod.explain(bl)
                 + ["用了 %d 轮：%s" % (len(rounds),
                                        "、".join(r["run_id"] for r in rounds))])


# --------------------------------------------------------------------------
# status（只读）
# --------------------------------------------------------------------------

def _run_status(args):
    outdir = paths.run_dir(args.run)
    pp = os.path.join(outdir, "progress.json")
    prog = progmod.read(pp)
    sp = os.path.join(outdir, "summary.json")
    summary = None
    if os.path.isfile(sp):
        with open(sp, "r", encoding="utf-8") as f:
            summary = json.load(f)
    if prog is None and summary is None:
        _die("找不到 run %s（%s）" % (args.run, outdir),
             as_json=getattr(args, "json", False))

    L = []
    if prog:
        L += ["== %s ==" % args.run, progmod.render_bar(prog),
              "状态 %s ｜ 已用 %s ｜ 预计剩余 %s ｜ 当前 %s"
              % (prog["state"], progmod.hms(prog.get("elapsed_sec") or 0),
                 progmod.hms(prog.get("eta_sec") or 0) if prog.get("eta_sec")
                 else "—", prog.get("label") or "—")]
        if prog.get("error"):
            L.append("错误：%s" % prog["error"])
    if summary:
        L += [""] + report.render_card(summary["card"])
    return _emit(args, {"progress": prog, "summary": summary}, L)


def cmd_status(args):
    if args.run:
        return _run_status(args)

    con = _readonly_db(args)
    if con is None:
        return _no_db(args)
    runs = db.list_runs(con, model_key=args.model, limit=args.limit)
    data = {"runs": runs, "baselines": [], "checks": []}
    L = ["== 最近 %d 轮 ==" % len(runs),
         "| run_id | 类型 | 模型 | 档 | 分数 | 请求 | 失败 | 健康 |",
         "|---|---|---|---|---:|---:|---:|---|"]
    for r in runs:
        L.append("| %s | %s | %s | %s | %s | %d | %d | %s |" % (
            r["run_id"], r["kind"], r["model_key"], r["tier"] or "-",
            ("%.1f" % r["score"]) if r["score"] is not None else "—",
            r["requests"], r["failed"], r["health"] or "-"))

    if args.model:
        cfg = _endpoint(args, need_key=False)
        for t in tiers.ORDER:
            bl = db.active_baseline(con, cfg["key"], cfg["endpoint_sha"],
                                    BANK_REV, t)
            if bl:
                data["baselines"].append(bl)
                L += ["", "== 基线 · %s 档 ==" % t] + blmod.explain(bl)
                ck = db.recent_checks(con, cfg["key"], cfg["endpoint_sha"],
                                      BANK_REV, t, limit=5)
                data["checks"] += ck
                for c in ck:
                    L.append("  %s %s %.1f 分（%+.1f）· %s"
                             % (judgemod.STATE_ICON.get(c["state"], "?"),
                                time.strftime("%m-%d %H:%M",
                                              time.localtime(c["created_at"])),
                                c["score"] or 0, c["delta"] or 0,
                                judgemod.STATE_LABEL.get(c["state"], c["state"])))
    con.close()
    return _emit(args, data, L)


# --------------------------------------------------------------------------
# compare（只读）
# --------------------------------------------------------------------------

def cmd_compare(args):
    con = _readonly_db(args)
    if con is None:
        # 这里**不能**走 _no_db。_no_db 回「还没有任何记录」+ 退出码 0，
        # 那是列表类命令的正确答案；而 compare 是用户点名了两个对象，
        # 「库都不存在」意味着这两个 run 一定不存在，是错误而非空结果。
        # 与 db.compare_runs 的报错保持同一措辞，使有库无库行为一致。
        _die("run 不存在：%s、%s（%s 不存在，还没有跑过任何一轮）"
             % (args.run_a, args.run_b, paths.DB),
             as_json=getattr(args, "json", False))
    try:
        a, b = db.compare_runs(con, args.run_a, args.run_b)
    except db.StoreError as e:
        con.close()
        _die(e, as_json=getattr(args, "json", False))
    con.close()

    sa = json.loads(a["summary"] or "{}")
    sb = json.loads(b["summary"] or "{}")
    md = tiers.min_detectable(min(a["requests"], b["requests"]))
    d = (b["score"] or 0) - (a["score"] or 0)
    verdict = ("差异 %+.1f 分，**小于最小可检出量 %.1f 分**——"
               "这两轮在统计上没有区别。" % (d, md)) if abs(d) < md else \
              ("差异 %+.1f 分，超过最小可检出量 %.1f 分。" % (d, md))

    L = ["== 对比 ==",
         "A %s  %.1f 分" % (a["run_id"], a["score"] or 0),
         "B %s  %.1f 分" % (b["run_id"], b["score"] or 0),
         "题库 %s ｜ 端点 %s" % (a["bank_rev"], a["endpoint_sha"]),
         "", verdict, "",
         "| 维度 | A | B | 差 | 阈值 ±T |", "|---|---:|---:|---:|---:|"]
    rows = []
    for dim in sorted(set(sa.get("dims") or {}) | set(sb.get("dims") or {})):
        va = (sa.get("dims") or {}).get(dim) or {}
        vb = (sb.get("dims") or {}).get(dim) or {}
        if va.get("score") is None or vb.get("score") is None:
            continue
        m = min(va.get("n_items") or 0, vb.get("n_items") or 0)
        show, _style, _why = tiers.dim_display(m)
        t = tiers.dim_threshold(m) if m else 0
        diff = 100 * (vb["score"] - va["score"])
        rows.append({"dim": dim, "a": 100 * va["score"], "b": 100 * vb["score"],
                     "diff": diff, "threshold": t, "significant": abs(diff) > t,
                     "displayable": show})
        if show:
            L.append("| %s | %.1f | %.1f | %+.1f | %.1f%s |"
                     % (dims.label(dim), 100 * va["score"], 100 * vb["score"],
                        diff, t, " ←超阈值" if abs(diff) > t else ""))
    return _emit(args, {"a": a["run_id"], "b": b["run_id"], "delta": d,
                        "min_detectable": md, "dims": rows}, L)


# --------------------------------------------------------------------------
# bank
# --------------------------------------------------------------------------

def cmd_bank(args):
    if args.action == "freeze":
        rev = args.rev or BANK_REV
        if rev != BANK_REV:
            _die("题库版本必须等于工具版本 %s。改题库就要发新版本——"
                 "否则历史分数会被静默地和新题混在一起。" % BANK_REV,
                 as_json=getattr(args, "json", False))
        if not args.yes and not _confirm(
                "重新冻结会改写 MANIFEST.json，此后旧报告的 sha256 不再匹配。继续？[y/N] "):
            _die("已取消", as_json=getattr(args, "json", False))
        mf = bankmod.freeze(paths.BANKS, rev, notes=args.note)
        return _emit(args, mf,
                     ["已写入 %s" % os.path.join(paths.BANKS, "MANIFEST.json"),
                      "题库 %s ｜ %d 题 ｜ %d 份素材"
                      % (mf["bank_rev"], sum(f["items"] for f in mf["files"].values()),
                         len(mf.get("assets") or {}))])

    try:
        mf = bankmod.load_manifest(paths.BANKS)
        assets = bankmod.load_assets(paths.BANKS, mf)
        items, skipped = bankmod.load(paths.BANKS, sorted(mf["files"]),
                                      assets=assets, manifest=mf)
    except bankmod.BankError as e:
        _die(e, as_json=getattr(args, "json", False))

    st = bankmod.stats(items)
    mon = [i for i in items if (mf["items"].get(i["id"]) or {}).get("monitor_ok")]
    L = ["== 题库 %s ==" % mf["bank_rev"],
         "冻结于 %s ｜ %d 题 ｜ 可用于监控 %d 题"
         % (mf.get("created_at"), st["total"], len(mon)),
         "", "| 维度 | 题数 | 可展示 |", "|---|---:|---|"]
    for d, n in sorted(st["by_dim"].items()):
        show, style, why = tiers.dim_display(n)
        L.append("| %s | %d | %s |" % (dims.label(d), n,
                                       {"solid": "是", "dashed": "仅趋势",
                                        "none": "否（题太少）"}.get(style, style)))
    L += ["", "判分器分布：" + "、".join("%s×%d" % kv
                                        for kv in sorted(st["by_checker"].items())),
          "稳健性：" + "、".join(
              "%s×%d" % (k, sum(1 for i in items if bankmod.robustness(i) == k))
              for k in ("white", "grey", "black")),
          "多轮题 %d 道 ｜ 素材 %d 份" % (st["multi_turn"], len(assets))]
    if skipped:
        L.append("跳过：" + "；".join("%s(%s)" % s for s in skipped))
    if args.dim:
        keep = {x.strip().upper() for x in args.dim.split(",")}
        L += ["", "| 题号 | 维 | 判分器 | 稳健 | 监控 | 标题 |",
              "|---|---|---|---|---|---|"]
        for i in items:
            if i["dim"] not in keep:
                continue
            m = mf["items"].get(i["id"]) or {}
            L.append("| %s | %s | %s | %s | %s | %s |"
                     % (i["id"], i["dim"], i["check"]["type"],
                        bankmod.robustness(i),
                        "✓" if m.get("monitor_ok") else "—",
                        i.get("title", "")))
    return _emit(args, {"manifest": mf, "stats": st,
                        "monitor_items": [i["id"] for i in mon],
                        "skipped": skipped}, L)


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def cmd_config(args):
    if args.action == "list":
        eps = endpoint.list_all()
        L = ["== 端点 ==",
             "| key | 模型 | 指纹 | 密钥 | 默认 |", "|---|---|---|---|---|"]
        for e in eps:
            ks = e["key_status"]
            L.append("| %s | %s | %s | %s | %s |"
                     % (e["key"], e["model"], e["endpoint_sha"],
                        ("%s（%s）" % (ks["masked"], ks["source"]))
                        if ks["set"] else "未设置：%s" % e["key_env"],
                        "✓" if e["default"] else ""))
        L += ["", "密钥永远不写进 config/models/*.json——那些文件要进版本管理。",
              "本地文件 %s %s" % (paths.SECRETS,
                                  "存在" if secrets.file_exists() else "不存在")]
        return _emit(args, {"endpoints": eps}, L)

    if args.action == "key":
        cfg = _endpoint(args, need_key=False)
        env = cfg["model"]["api_key_env"]
        if args.clear:
            secrets.clear(env)
            return _emit(args, {"cleared": env}, ["已清除 %s（会话与本地文件）" % env])
        val = args.value
        if not val:
            if not sys.stdin or not sys.stdin.isatty():
                _die("非交互环境请用 --value，或直接设环境变量 %s" % env,
                     as_json=getattr(args, "json", False))
            try:
                import getpass
                val = getpass.getpass("输入 %s（不回显）：" % env)
            except Exception:
                _die("读取失败", as_json=getattr(args, "json", False))
        if not val.strip():
            _die("空密钥", as_json=getattr(args, "json", False))
        secrets.set_key(env, val.strip(), store=args.store)
        st = secrets.status_of(env)
        return _emit(args, {"env": env, "status": st},
                     ["已保存 %s = %s（%s）" % (env, st["masked"],
                                               secrets.SOURCE_LABEL.get(
                                                   st["source"], st["source"])),
                      "会话内存只在当前进程有效；计划任务要用用户级环境变量。"])

    # test
    cfg = _endpoint(args)
    if not args.json:
        _p("向 %s 发 1 条探针请求（成本可忽略）…" % cfg["model"]["base_url"])
    r = _probe(cfg, as_json=getattr(args, "json", False))
    return _emit(args, {"ok": True, "endpoint": endpoint.redact(cfg), **r},
                 ["渠道通。延迟 %s ms，回了：%s"
                  % (r.get("latency_ms"), (r.get("sample") or "").strip())])


# --------------------------------------------------------------------------
# schedule
# --------------------------------------------------------------------------

def cmd_schedule(args):
    if args.action == "list":
        rows = schedule.list_all()
        L = ["== 计划任务 =="]
        L += (["%s ｜ %s ｜ 下次 %s" % (r.get("name"), r.get("status"),
                                       r.get("next_run")) for r in rows]
              or ["（没有 mprobe 的计划任务）"])
        return _emit(args, {"tasks": rows}, L)

    cfg = _endpoint(args, need_key=False)
    if args.action == "status":
        st = schedule.status(schedule.task_name(cfg["key"], args.tier))
        return _emit(args, st,
                     ["%s：%s" % (schedule.task_name(cfg["key"], args.tier),
                                  "存在" if st["exists"] else "不存在")] +
                     ["  %s = %s" % (k, v) for k, v in st.items()
                      if k != "exists"])

    if args.action == "remove":
        try:
            name, note = schedule.uninstall(cfg["key"], args.tier)
        except schedule.ScheduleError as e:
            _die(e, as_json=getattr(args, "json", False))
        return _emit(args, {"removed": name}, [note])

    try:
        name, note = schedule.install(cfg["key"], args.tier, args.cadence,
                                      at=args.at, force=args.force)
    except schedule.ScheduleError as e:
        _die(e, as_json=getattr(args, "json", False))
    return _emit(args, {"installed": name},
                 [note, "",
                  "把密钥设成用户级（当前终端看不到，要重开一个）：",
                  '  [Environment]::SetEnvironmentVariable("%s","你的key","User")'
                  % cfg["model"]["api_key_env"]])


# --------------------------------------------------------------------------
# tiers（只读）
# --------------------------------------------------------------------------

def cmd_web(args):
    from .web import server as websrv
    return websrv.serve(port=args.port,
                        open_browser=not getattr(args, "no_open", False))


def cmd_tiers(args):
    rows = []
    L = ["== 档位 ==",
         "| 档 | 题 | 次 | 请求 | σ | 最小可检出 | 95%区间 | 用途 |",
         "|---|---:|---:|---:|---:|---:|---:|---|"]
    for t in tiers.table():
        rows.append(t)
        L.append("| %s | %d | %d | %d | %.2f | %.1f 分 | ±%.1f | %s |"
                 % (t["tier"], t["items"], t["trials"], t["requests"],
                    t["sigma"], t["min_detectable"], t["ci_half"], t["purpose"]))
    L.append("")
    for p in profiles.list_all():
        try:
            pl = profiles.resolve(p["tier"])
        except Exception as e:
            L.append("%s 档案读取失败：%s" % (p["tier"], e))
            continue
        rows.append({"profile": p["tier"], "n_items": pl["n_items"],
                     "requests": pl["requests"],
                     "min_detectable": pl["min_detectable"],
                     "dim_counts": pl["dim_counts"]})
        L.append("档案 %s：实选 %d 题 x %d 次 = %d 请求，最小可检出 %.1f 分%s"
                 % (p["tier"], pl["n_items"], pl["trials"], pl["requests"],
                    pl["min_detectable"],
                    "（仅监控准入题）" if p.get("monitor_only") else ""))
        for w in pl.get("warnings", []):
            L.append("  ⚠ " + w)
    return _emit(args, {"tiers": rows}, L)


# --------------------------------------------------------------------------
# 参数
# --------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(
        prog="mprobe",
        description="模型能力测评与降智监控（题库版本 %s）" % BANK_REV,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="退出码：0 正常 ｜ 1 告警 ｜ 2 用法/配置错误 ｜ 3 结果不可用")
    ap.add_argument("--version", action="version",
                    version="mprobe %s（题库 %s）" % (__version__, BANK_REV))
    sub = ap.add_subparsers(dest="cmd", metavar="命令")

    def common(p, money=False):
        p.add_argument("--model", help="端点 key（config/models/*.json 的文件名）")
        p.add_argument("--json", action="store_true", help="输出 JSON")
        if money:
            p.add_argument("--tier", default="small",
                           help="档位：small/medium/large（默认 small）")
            p.add_argument("--trials", type=int, help="覆盖每题采样次数")
            p.add_argument("--concurrency", type=int, help="覆盖并发数")
            p.add_argument("--items", help="只跑这些题号，逗号分隔")
            p.add_argument("--dim", help="只跑这些维度，逗号分隔")
            p.add_argument("--monitor-only", dest="monitor_only",
                           action="store_true", default=None,
                           help="强制只用监控准入题")
            p.add_argument("--all-items", dest="monitor_only",
                           action="store_false",
                           help="强制用全部题（含未通过监控准入的）")
            p.add_argument("--run-id", dest="run_id",
                           help="指定 run_id（给 MCP 层用，避免猜时间戳的竞态）")
            p.add_argument("--out-tokens", type=int,
                           help="估算用的每次输出 token 数（推理模型要调大）")
            p.add_argument("--confirm-above", type=float,
                           help="覆盖成本确认线")
            p.add_argument("--yes", "-y", action="store_true", help="跳过确认")
            p.add_argument("--dry-run", action="store_true",
                           help="只算账不发请求")
            p.add_argument("--no-store", action="store_true", help="不写数据库")
            p.add_argument("--note", default="", help="备注，写进 summary")
        return p

    common(sub.add_parser("eval", help="跑一轮测评并出报告"), money=True)
    c = common(sub.add_parser("check", help="跑一轮并对照基线判定"), money=True)
    c.add_argument("--no-notify", action="store_true", help="不推送")

    b = common(sub.add_parser("baseline", help="查看或建立基线"))
    b.add_argument("--tier", default="small")
    b.add_argument("--build", action="store_true", help="用历史轮次建立基线")
    b.add_argument("--rounds", type=int, help="只用最近 N 轮")

    s = common(sub.add_parser("status", help="进度 / 历史（只读）"))
    s.add_argument("--run", help="看某一轮的实时进度")
    s.add_argument("--tier", default="small")
    s.add_argument("--limit", type=int, default=15)

    cp = common(sub.add_parser("compare", help="对比两轮（只读）"))
    cp.add_argument("run_a")
    cp.add_argument("run_b")

    bk = common(sub.add_parser("bank", help="题库信息 / 冻结"))
    bk.add_argument("action", choices=["info", "freeze"], nargs="?",
                    default="info")
    bk.add_argument("--dim", help="列出这些维度的逐题清单")
    bk.add_argument("--rev", help="冻结用的版本号（必须等于工具版本）")
    bk.add_argument("--note", help="写进清单的说明")
    bk.add_argument("--yes", "-y", action="store_true")

    cf = common(sub.add_parser("config", help="端点与密钥"))
    cf.add_argument("action", choices=["list", "key", "test"], nargs="?",
                    default="list")
    cf.add_argument("--value", help="密钥值（不给则交互输入，不回显）")
    cf.add_argument("--store", choices=["session", "file"], default="session",
                    help="session=只在本进程；file=写 config/secrets.local.json")
    cf.add_argument("--clear", action="store_true")

    sc = common(sub.add_parser("schedule", help="Windows 计划任务"))
    sc.add_argument("action",
                    choices=["install", "remove", "status", "list"],
                    nargs="?", default="list")
    sc.add_argument("--tier", default="small")
    sc.add_argument("--cadence", default="daily",
                    choices=sorted(schedule.CADENCES))
    sc.add_argument("--at", default="09:30", help="执行时间 HH:MM")
    sc.add_argument("--force", action="store_true")

    common(sub.add_parser("tiers", help="档位与档案对照表（只读）"))

    w = common(sub.add_parser("web", help="本地只读界面（只绑 127.0.0.1）"))
    w.add_argument("--port", type=int, default=8790)
    # **故意不提供 --host。** 这个服务能读全部测评历史和端点配置，
    # 绑到 0.0.0.0 等于把它交给整个网段。要远程看就 ssh -L 端口转发。
    # 把安全做成默认值，用户随手就能改掉；做成**没有那个参数**才是真关掉了。
    w.add_argument("--no-open", dest="no_open", action="store_true",
                   help="不自动打开浏览器")
    return ap


HANDLERS = {"eval": cmd_eval, "check": cmd_check, "baseline": cmd_baseline,
            "status": cmd_status, "compare": cmd_compare, "bank": cmd_bank,
            "config": cmd_config, "schedule": cmd_schedule,
            "tiers": cmd_tiers, "web": cmd_web}


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return OK
    if args.cmd in ("eval", "check"):
        paths.ensure_data()
    try:
        rc = HANDLERS[args.cmd](args)
    except KeyboardInterrupt:
        sys.stderr.write("\n已中断\n")
        return USAGE
    return OK if rc is None or not isinstance(rc, int) else rc
