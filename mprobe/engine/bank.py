# -*- coding: utf-8 -*-
"""题库加载、校验、冻结。

题库为什么必须冻结
------------------
测评分数只在**同一套题**上可比。题库改一道题，此前所有基线和趋势线
就全部作废——而作废是**静默**的：图还在画，线还在连，只是那条线
不再意味着任何东西。

所以这里的规矩是：

  · 题库随工具版本发布，用户端**只读**
  · MANIFEST.json 记 bank_rev + 每个文件的 sha256
  · 校验失败**拒绝运行**，不是打印警告
  · 想改题？改上游仓库，升版本号，重新建基线。没有第二条路

判分全自动
----------
check.type 是必填项，且不允许 manual。原因见 MEASUREMENT.md 第四章：
人工判分不可复现、不能进定时任务，而监控要的恰恰是「无人值守下的可比性」。
构造成加载期报错，是为了让「悄悄混进一道人工题」这件事不可表达。
"""

import hashlib
import json
import os
import time

from . import checkers, dims


class BankError(Exception):
    pass


VALID_TOP = {"id", "dim", "tier", "title", "prompt", "turns", "check",
             "weight", "requires", "note", "observe", "source"}

REQUIRED = ("id", "dim", "check")


# --------------------------------------------------------------------------
# 清单
# --------------------------------------------------------------------------

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(banks_dir, expect_rev=None, verify=True):
    """读 MANIFEST.json 并校验每个题库文件的 sha256。

    verify=False 只在制作清单本身时用（mprobe bank freeze）。
    正常运行路径上**永远**是 True。
    """
    path = os.path.join(banks_dir, "MANIFEST.json")
    if not os.path.isfile(path):
        raise BankError(
            "找不到 %s。题库必须有清单才能运行——没有清单就没有 bank_rev，"
            "没有 bank_rev 就无法判断两次结果能不能比。" % path)
    with open(path, "r", encoding="utf-8-sig") as f:
        mf = json.load(f)

    rev = mf.get("bank_rev")
    if not rev:
        raise BankError("MANIFEST.json 缺少 bank_rev")
    if expect_rev and rev != expect_rev:
        raise BankError(
            "题库版本 %s 与工具版本 %s 不一致。两者必须同步升级——"
            "否则会出现「同一个 bank_rev 对应两套题」的情况，那比没有版本号更糟。"
            % (rev, expect_rev))

    if verify:
        bad = []
        targets = dict(mf.get("files") or {})
        for name, meta in (mf.get("assets") or {}).items():
            targets[os.path.join(ASSET_DIR, meta["file"])] = meta
        for fn, meta in sorted(targets.items()):
            p = os.path.join(banks_dir, fn)
            if not os.path.isfile(p):
                bad.append("%s 不存在" % fn)
                continue
            got = sha256_file(p)
            if got != meta.get("sha256"):
                bad.append("%s 内容已被改动（清单 %s… / 实际 %s…）"
                           % (fn, meta.get("sha256", "")[:12], got[:12]))
        if bad:
            raise BankError(
                "题库校验失败，拒绝运行：\n  " + "\n  ".join(bad) +
                "\n题库是冻结的。要改题请改上游仓库并升版本号，"
                "在本机改一个字节就会让这台机器的分数和别人的不可比。")
    return mf


# --------------------------------------------------------------------------
# 加载
# --------------------------------------------------------------------------

def load_file(path, assets=None):
    """读单个 jsonl 题库，逐题校验。返回 (items, skipped)。"""
    items, seen = [], set()
    with open(path, "r", encoding="utf-8-sig") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                it = json.loads(line)
            except json.JSONDecodeError as e:
                raise BankError("%s 第 %d 行不是合法 JSON: %s"
                                % (os.path.basename(path), ln, e))
            _validate(it, os.path.basename(path), ln)
            if it["id"] in seen:
                raise BankError("%s 第 %d 行题号重复: %s"
                                % (os.path.basename(path), ln, it["id"]))
            seen.add(it["id"])
            items.append(it)
    if not items:
        raise BankError("题库为空: %s" % path)

    out, skipped = [], []
    assets = assets or {}
    for it in items:
        need = it.get("requires")
        if need and need not in assets:
            skipped.append((it["id"], "缺少素材 %s" % need))
            continue
        out.append(_materialize(it, assets))
    return out, skipped


ASSET_DIR = "assets"


def load_assets(banks_dir, manifest=None):
    """读 banks/assets/ 下的全部素材。返回 {名称: 文本}。

    名称取文件名去掉扩展名。素材和题库文件一样受 sha256 约束——
    题目内容包含素材，素材变了题就变了。
    """
    d = os.path.join(banks_dir, ASSET_DIR)
    out = {}
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if fn.startswith("."):
            continue
        p = os.path.join(d, fn)
        if not os.path.isfile(p):
            continue
        with open(p, "r", encoding="utf-8-sig") as f:
            out[os.path.splitext(fn)[0]] = f.read()
    if manifest:
        want = set((manifest.get("assets") or {}).keys())
        have = set(out)
        if want - have:
            raise BankError("清单里有素材但文件不在：%s"
                            % "、".join(sorted(want - have)))
    return out


def load(banks_dir, files, ids=None, dim_filter=None, assets=None,
         monitor_only=False, manifest=None):
    """加载若干题库文件并合并。

    monitor_only=True 时只保留清单里 monitor_ok 为真的题
    ——测评是监控的超集，两者共用一份物理题库，在**加载时**分流。
    """
    items, skipped, seen = [], [], {}
    for fn in files:
        got, sk = load_file(os.path.join(banks_dir, fn), assets)
        for it in got:
            if it["id"] in seen:
                raise BankError("题号 %s 同时出现在 %s 与 %s。"
                                "沉默去重会让人以为跑了却没跑。"
                                % (it["id"], seen[it["id"]], fn))
            seen[it["id"]] = fn
            it["_bank_file"] = fn
        items.extend(got)
        skipped.extend(sk)

    ledger = (manifest or {}).get("items") or {}
    out = []
    for it in items:
        if ids and it["id"] not in ids:
            continue
        if dim_filter and it["dim"] not in dim_filter:
            continue
        if monitor_only:
            entry = ledger.get(it["id"])
            if entry is None:
                raise BankError(
                    "题 %s 不在清单台账里，无法判断它能不能进监控。"
                    "请重新冻结题库（mprobe bank freeze）。" % it["id"])
            if not entry.get("monitor_ok"):
                skipped.append((it["id"], "监控稳健性过滤：%s"
                                % (entry.get("reason") or "未标注原因")))
                continue
        out.append(it)
    if not out:
        raise BankError("按当前条件筛选后一道题都不剩，不会发起任何请求。")
    return out, skipped


def _validate(it, fname, ln):
    where = "%s 第 %d 行" % (fname, ln)
    unknown = set(it) - VALID_TOP
    if unknown:
        raise BankError("%s 含未知字段: %s" % (where, ", ".join(sorted(unknown))))
    for k in REQUIRED:
        if k not in it:
            raise BankError("%s 缺少字段 %s" % (where, k))

    try:
        dims.resolve(it["dim"])
    except dims.DimError as e:
        raise BankError("%s（题 %s）: %s" % (where, it.get("id"), e))

    if not it.get("prompt") and not it.get("turns"):
        raise BankError("%s 必须有 prompt 或 turns" % where)
    if it.get("prompt") and it.get("turns"):
        raise BankError("%s prompt 与 turns 只能有一个" % where)

    ck = it.get("check") or {}
    kind = ck.get("type")
    if not kind:
        raise BankError("%s（题 %s）check.type 未填。本工具全自动判分，"
                        "每道题都必须有可程序化执行的判据。" % (where, it["id"]))
    if kind == "manual":
        raise BankError(
            "%s（题 %s）check.type = manual。**本工具不支持人工判分**：\n"
            "  · 人工分不可复现，同一份输出两个人给不同分\n"
            "  · 定时任务里没有人，监控会永远停在「待评分」\n"
            "把它改造成可程序化的判据，或移到 banks/retired/。"
            % (where, it["id"]))
    if kind not in checkers.available():
        raise BankError("%s 判分类型 %r 不存在。可用: %s"
                        % (where, kind, ", ".join(checkers.available())))

    w = it.get("weight", 1.0)
    if not isinstance(w, (int, float)) or w <= 0:
        raise BankError("%s weight 必须是正数，当前 %r" % (where, w))


def _materialize(it, assets):
    """把 {{asset}} 占位符替换成实际素材。"""
    out = dict(it)
    need = it.get("requires")
    if need:
        content = assets[need]
        if out.get("prompt"):
            out["prompt"] = out["prompt"].replace("{{asset}}", content)
        if out.get("turns"):
            out["turns"] = [dict(t, content=t["content"].replace("{{asset}}", content))
                            for t in out["turns"]]
    return out


def to_messages(item, system=""):
    """转成一次对话的消息列表。**一题一会话，绝不复用历史。**"""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    if item.get("turns"):
        msgs.extend(item["turns"])
    else:
        msgs.append({"role": "user", "content": item["prompt"]})
    return msgs


def stats(items):
    from collections import Counter
    by_dim = Counter(i["dim"] for i in items)
    return {
        "total": len(items),
        "by_dim": dict(sorted(by_dim.items())),
        "by_namespace": dict(sorted(Counter(dims.namespace(i["dim"])
                                            for i in items).items())),
        "by_checker": dict(sorted(Counter(i["check"]["type"] for i in items).items())),
        "multi_turn": sum(1 for i in items if i.get("turns")),
        "scored_dims": sorted(d for d in by_dim if dims.in_score(d)),
    }


# --------------------------------------------------------------------------
# 冻结（制作清单）
# --------------------------------------------------------------------------

#: 判分器稳健性分级。这是**监控**的准入闸门之一，测评不受它约束。
#:
#: 分级依据只有一条：同一份回答，换个说法重说一遍，判分结果会不会变。
#: 会变的判分器放进监控就是在测量自己的抖动。
ROBUSTNESS = {
    # 白名单：判据是字面的、可复现的，模型换个措辞不影响结果
    "exact": "white",
    "number": "white",
    "mcq": "white",
    "span_answer": "white",
    "json_strict": "white",
    "tree": "white",
    "cn_count": "white",
    "length": "white",
    "lines_spec": "white",
    "math_equal": "white",
    "self_ref": "white",
    "banned_and_length": "white",
    "bulk": "white",
    "chain_compound": "white",
    "regex": "white",
    # 灰名单：关键词命中。同义改写会漏判，但只要词表够宽，抖动可控
    "contains_all": "grey",
    "contains_any": "grey",
    "contains_none": "grey",
    "checkpoints": "grey",
    "ifeval": "grey",
    "calibration": "grey",
    # 黑名单：不进监控
    "exec_python": "black",     # 依赖本机 Python 环境，跨机器不可比
}

ROBUSTNESS_WHY = {
    "white": "字面判据，可复现",
    "grey": "关键词命中，同义改写可能漏判，词表需覆盖常见说法",
    "black": "结果依赖本机环境或不可复现，不进监控",
}

#: 监控准入：同模型轮间 SD 的上限。超过就会产生假告警。
#:
#: 取 0.15 的依据是 1.1 的实测分布：41 道历史实测题里
#: SD <= 0.10 的 32 道、<= 0.15 的 35 道、<= 0.20 的 38 道。
#: 0.15 处有个自然的台阶 —— 再放宽就要收进 `contains_any`
#: 那批实测 SD 0.25–0.40 的题，那些是真的会乱报。
MONITOR_SD_MAX = 0.15

#: 监控准入：通过率的饱和边界。与 MEASUREMENT.md 3.3 的非饱和判据同值。
SAT_LO, SAT_HI = 0.20, 0.80

#: 期望串字符数上限。超过就判黑。
#:
#: 依据 MEASUREMENT.md 4.3：一条一百多字符的正则，本质是在要求模型
#: 逐字复现一个特定串。模型换个说法、多一个词、顺序变一下就 0 分 ——
#: 而那**不是能力变化**。这种题进监控就是假告警发生器。
#: 公开基准题源实测：regex 65 道，期望串均值 102.7 字符、最长 267。
EXPECT_CHARS_MAX = 40

#: 关键词列表项数上限（`contains_all`）。超过就判黑。
#: 要求同时命中 8 个词，任一漏判就 0 分，漏判概率随项数累积。
EXPECT_ITEMS_MAX = 3


def _expect_chars(ck):
    """期望串的字符数。取不到返回 None。"""
    for key in ("pattern", "expect"):
        v = ck.get(key)
        if isinstance(v, str):
            return len(v)
    return None


def _expect_items(ck):
    """期望列表的项数。取不到返回 None。"""
    for key in ("expect", "all", "any", "none", "words", "banned"):
        v = ck.get(key)
        if isinstance(v, list):
            return len(v)
    return None


def robustness_detail(item):
    """返回 (等级, 原因)。

    等级不只看 `check.type` —— 同一个判分器，判据写得短还是长，
    稳健性完全不同。旧实现只查类型表，**表达不了** MEASUREMENT.md 4.3
    的两条按规模的黑名单规则，结果是清单里「黑 0」，
    看着像没有脆弱题，其实是规则没实现。

    白／灰的分界取自 1.1 的实测（`banks/snr_core41.json`）：
    同模型轮间 SD 的均值 —— contains_any 0.0917 > checkpoints 0.0415
    > exact 0.0153 > number 0.0032。所以 checkpoints 留在灰名单，
    尽管 4.3 把它列在白名单里（那与实测相悖）。

    注意：**只有黑挡监控**，白和灰都能进（灰的含义是"还需要实测信噪比"）。
    """
    ck = item.get("check") or {}
    t = ck.get("type")
    base = ROBUSTNESS.get(t)
    if base is None:
        return "black", "未知判分器 %r，按不可复现处理" % t

    nchar = _expect_chars(ck)
    nitem = _expect_items(ck)

    if t == "regex":
        # 正则匹配自由文本属于关键词类判断，**永不进白名单**
        if nchar is not None and nchar > EXPECT_CHARS_MAX:
            return "black", ("正则期望串 %d 字符 > %d，要求逐字复现特定串，"
                             "措辞一变即 0 分" % (nchar, EXPECT_CHARS_MAX))
        return "grey", ("正则期望串 %s 字符，属关键词类判断，不入白名单"
                        % ("?" if nchar is None else nchar))

    if t == "contains_all":
        if nitem is not None and nitem > EXPECT_ITEMS_MAX:
            return "black", ("要求同时命中 %d 项 > %d，漏判概率随项数累积"
                             % (nitem, EXPECT_ITEMS_MAX))
        return "grey", ROBUSTNESS_WHY["grey"]

    if t == "exact" and nchar is not None and nchar > EXPECT_CHARS_MAX:
        return "grey", ("字面相等但期望串 %d 字符 > %d，长答案的字面比对脆弱"
                        % (nchar, EXPECT_CHARS_MAX))

    return base, ROBUSTNESS_WHY[base]


def robustness(item):
    return robustness_detail(item)[0]


def freeze(banks_dir, bank_rev, files=None, notes=None, snr_ledger=None,
           probe_ledger=None):
    """生成 MANIFEST.json。

    台账里每道题都要有一行，写清楚**它凭什么在里面**，以及
    **它凭什么还进不了监控**（`monitor_block`）。

    两道闸门：
      1. 判分器稳健性 —— 由 `robustness_detail()` 判，含按长度／项数的黑名单
      2. 信噪比实测   —— 由 `snr_ledger`（实施计划第 1.1 项 的 snr_core41.json）提供

    `snr` 为 None 表示**没测过**，此时 `monitor_ok` 一律 False。
    「没测过」和「测过且合格」必须在数据结构上分得开，
    否则未验证的题会静默进基线 —— 那正是"不报错、能出结果、结论错"。
    """
    files = files or sorted(fn for fn in os.listdir(banks_dir)
                            if fn.endswith(".jsonl"))
    assets = load_assets(banks_dir)
    ledger, fmeta = {}, {}
    for fn in files:
        path = os.path.join(banks_dir, fn)
        got, _sk = load_file(path, assets)
        fmeta[fn] = {"sha256": sha256_file(path), "items": len(got)}
        for it in got:
            r, why = robustness_detail(it)
            row = {
                "file": fn,
                "dim": it["dim"],
                "checker": it["check"]["type"],
                "robustness": r,
                "snr": None,
                "snr_source": None,
                "reason": "判分器 %s：%s" % (it["check"]["type"], why),
            }
            # 信噪比台账（实施计划第 1.1 项 的产物）里有这道题就并进来。
            # 没有就留 None —— 那表示"这道题能不能分辨模型变化，还没测过"，
            # 不是"测过且合格"。
            m = (snr_ledger or {}).get(it["id"])
            if m:
                row["snr"] = m.get("snr_min")
                row["snr_source"] = "replay_round_sd.py"
                row["snr_verdict"] = m.get("verdict")
                row["snr_state"] = {"通过": "verified",
                                    "不通过": "weak",
                                    "无信号": "saturated"}.get(
                                        m.get("verdict"), "weak")
            else:
                row["snr_state"] = "untested"

            # 跨模型实测（实施计划第 1.3 项 / 1.4）。这部分是**测评准入**的证据，
            # 和上面的 snr（监控准入）是两件事：
            #   · spread / z  回答「这道题能不能分辨模型」
            #   · snr         回答「这道题的抖动会不会被误判为退化」
            pr = (probe_ledger or {}).get(it["id"])
            if pr:
                row["p_mean"] = pr.get("p_mean")
                row["spread"] = pr.get("spread")
                row["z"] = pr.get("z")
                row["probe_grade"] = pr.get("grade")
                row["models_measured"] = sorted(pr.get("per_model") or {})
                sp, z, p = pr.get("spread"), pr.get("z"), pr.get("p_mean")
                gates = {
                    "unsaturated": (None if p is None
                                    else bool(0.20 <= p <= 0.80)),
                    "spread_ge_015": None if sp is None else bool(sp >= 0.15),
                    "z_ge_196": None if z is None else bool(z >= 1.96),
                }
                row["eval_gates"] = gates
                row["eval_ok"] = all(v is True for v in gates.values())
            else:
                row["probe_grade"] = "unmeasured"
                row["eval_ok"] = False
            # 监控准入（实施计划 D1（已修订））。三条，各防一件事：
            #
            #   1. 判分器不在黑名单        —— 防判据自己抖
            #   2. 轮间 SD <= SD_MAX       —— 防**假告警**：题自己抖会被误判为退化
            #   3. 该模型上 p 非饱和       —— 防**迟钝**：p≈1 时 Δp = p(1−p)·Δθ ≈ 0，
            #                                 小退化推不动它
            #
            # 改掉的是原来那条「信噪比 = 跨模型极差 / 轮间SD >= 3」。
            # 问题在分子：**跨模型极差是测评的指标** —— 它回答「这题能不能
            # 分辨两个模型」，而监控要回答的是「这题的抖动会不会被误判为退化」。
            # 用分子卡监控，一道完全稳定的题也会被拒（只因它区分不了模型），
            # 而它根本不会产生假告警。实测：41 道历史实测题里，
            # 信噪比 >= 3 只放行 2 道，轮间 SD <= 0.15 放行 35 道。
            #
            # `MEASUREMENT.md` 3.4 论证的是「高区分度题对监控也**最优**」——
            # 那是最优性，不是准入资格。这两件事此前被合并了。
            #
            # 好处：这三条都是**建基线的副产品**（同一模型跑 5 轮，
            # 轮间 SD 和 p 都在里面），不需要额外跑第二个模型。
            sd = m.get("sd_mean_max") if m else None
            p_own = m.get("p_mean") if m else None
            row["round_sd"] = sd
            if r == "black":
                row["monitor_ok"] = False
                row["monitor_block"] = "判分器黑名单"
            elif m is None:
                row["monitor_ok"] = False
                row["monitor_block"] = "轮间 SD 未实测（需同一模型多轮数据）"
            elif sd is None or sd > MONITOR_SD_MAX:
                row["monitor_ok"] = False
                row["monitor_block"] = ("轮间 SD %.3f > %.2f，会产生假告警"
                                        % (sd or 0, MONITOR_SD_MAX))
            else:
                row["monitor_ok"] = True
                row["monitor_why"] = "轮间 SD %.3f，稳定" % sd

            # 饱和是**逐模型**的属性，不进题库级闸门。
            #
            # 一道对 opus 饱和的题，对弱模型可能正好有区分度。把逐模型属性
            # 烙进题库级的 monitor_ok，等于用一个模型的表现决定所有模型
            # 能用什么题 —— 而且题库一冻结就再也改不了。
            #
            # 所以这里只记录参考值（三个历史模型的平均通过率），
            # 真正的饱和过滤放在**建基线时**按被测模型自己的 p 做：
            # 建基线要跑 5 轮，那 5 轮里就有该模型自己的 p。
            row["p_ref"] = p_own
            row["saturated_ref"] = (
                None if p_own is None else not (SAT_LO <= p_own <= SAT_HI))
            ledger[it["id"]] = row
    ameta = {}
    adir = os.path.join(banks_dir, ASSET_DIR)
    if os.path.isdir(adir):
        for fn in sorted(os.listdir(adir)):
            p = os.path.join(adir, fn)
            if os.path.isfile(p) and not fn.startswith("."):
                ameta[os.path.splitext(fn)[0]] = {
                    "file": fn, "sha256": sha256_file(p),
                    "bytes": os.path.getsize(p)}

    mf = {
        "bank_rev": bank_rev,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "files": fmeta,
        "assets": ameta,
        "items": ledger,
        "monitor_gate": (
            "0.1 版只过了判分器稳健性闸门。信噪比闸门（跨模型极差 / "
            "同模型轮间 SD >= 3）尚未验证，要等 实施计划第 1.1 项 与 1.3。"
            "现在的 monitor_ok 只代表「判分器不会自己抖」，"
            "不代表「这道题能分辨出模型变化」。"),
        "notes": notes or "",
    }
    path = os.path.join(banks_dir, "MANIFEST.json")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(mf, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return mf, path
