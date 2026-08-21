# -*- coding: utf-8 -*-
"""SQLite 存储。四张表，一个文件，零依赖。

为什么不是「每次跑完写个 json 就完了」
------------------------------------
run 目录已经有 summary.json 了，那是给人看的现场记录。
数据库解决的是**跨轮**的问题：查某个模型三个月的分数曲线、
找上一次基线、算连续几轮低于阈值。这些查询要是靠遍历目录，
每加一个模型就慢一点，而且没有任何东西能阻止你把两套不同题库的
分数画进同一条折线。

三条约束写死在这里
------------------
1. **bank_rev 是每张表的必填列。** 不是元数据，是序列身份的一部分。
2. **跨 bank_rev 的比较抛异常，不是返回空。** 返回空会被上层
   当成「没数据」静默跳过，然后报告里出现一条断掉的曲线；
   抛异常会当场停下来告诉你为什么不能比。
3. **endpoint_sha 一起存。** 同一个模型名换了 base_url 或
   temperature 就是另一条序列了，指纹不同不给连线。
"""

import json
import os
import sqlite3

SCHEMA_VERSION = 1


class StoreError(Exception):
    pass


class BankRevMismatch(StoreError):
    """跨题库版本比较。这是使用错误，不是数据缺失。"""


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 一次测评 / 一次监控采样 = 一行
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,          -- eval | check | baseline
    model_key     TEXT NOT NULL,
    endpoint_sha  TEXT NOT NULL,
    bank_rev      TEXT NOT NULL,
    tier          TEXT,
    n_items       INTEGER NOT NULL,
    trials        INTEGER NOT NULL,
    requests      INTEGER NOT NULL,
    failed        INTEGER NOT NULL DEFAULT 0,
    score         REAL,
    conservative  REAL,
    cost          REAL,
    currency      TEXT,
    started_at    REAL NOT NULL,
    finished_at   REAL,
    health        TEXT,
    outdir        TEXT,
    summary       TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_series
    ON runs(model_key, endpoint_sha, bank_rev, started_at);

-- 逐题结果，一行一题（不是一次采样；采样明细在 raw.jsonl）
CREATE TABLE IF NOT EXISTS results (
    run_id      TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    dim         TEXT NOT NULL,
    bank_rev    TEXT NOT NULL,
    n           INTEGER NOT NULL,
    k           INTEGER NOT NULL,
    mean        REAL,
    spread      REAL,
    latency_p50 REAL,
    PRIMARY KEY (run_id, item_id),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_results_item ON results(item_id, bank_rev);

-- 基线。一个 (model, endpoint, bank_rev, tier) 只有一条生效基线
CREATE TABLE IF NOT EXISTS baselines (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    model_key     TEXT NOT NULL,
    endpoint_sha  TEXT NOT NULL,
    bank_rev      TEXT NOT NULL,
    tier          TEXT NOT NULL,
    mean          REAL NOT NULL,
    sigma         REAL NOT NULL,
    sigma_source  TEXT NOT NULL,
    threshold     REAL NOT NULL,
    rounds        INTEGER NOT NULL,
    provisional   INTEGER NOT NULL,
    run_ids       TEXT NOT NULL,
    dim_means     TEXT,
    detail        TEXT,                   -- 基线全文 JSON，读回来无损
    created_at    REAL NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_baselines_series
    ON baselines(model_key, endpoint_sha, bank_rev, tier, active);

-- 每次判定的结论。判定和采样分开存：同一个 run 可能被重新判定
CREATE TABLE IF NOT EXISTS checks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    baseline_id   INTEGER NOT NULL,
    model_key     TEXT NOT NULL,
    endpoint_sha  TEXT NOT NULL,
    bank_rev      TEXT NOT NULL,
    tier          TEXT NOT NULL,
    score         REAL NOT NULL,
    delta         REAL NOT NULL,
    z             REAL,
    state         TEXT NOT NULL,
    consecutive   INTEGER NOT NULL,
    reason        TEXT,
    created_at    REAL NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (baseline_id) REFERENCES baselines(id)
);
CREATE INDEX IF NOT EXISTS ix_checks_series
    ON checks(model_key, endpoint_sha, bank_rev, tier, created_at);
"""


def _migrate(con):
    """给已经存在的库补上后加的列。

    CREATE TABLE IF NOT EXISTS 对已存在的表什么都不做，所以新增列必须
    显式 ALTER——否则新代码读老库时会在一个完全无关的地方报 KeyError。
    """
    want = {"baselines": {"detail": "TEXT"}}
    for table, cols in want.items():
        have = {r["name"] for r in
                con.execute("PRAGMA table_info(%s)" % table).fetchall()}
        for col, typ in cols.items():
            if col not in have:
                con.execute("ALTER TABLE %s ADD COLUMN %s %s"
                            % (table, col, typ))
    con.commit()


def connect(path):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")   # 界面在读的时候 runner 还能写
    con.executescript(SCHEMA)
    _migrate(con)
    con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES(?, ?)",
                ("schema", str(SCHEMA_VERSION)))
    con.commit()
    return con


# --------------------------------------------------------------------------
# 写
# --------------------------------------------------------------------------

def save_run(con, summary, kind="eval", outdir=None):
    """把 report.write_all() 返回的 summary 落库。"""
    rt = summary.get("runtime") or {}
    health = summary.get("health") or {}
    cost = summary.get("cost") or {}
    con.execute(
        "INSERT OR REPLACE INTO runs (run_id, kind, model_key, endpoint_sha,"
        " bank_rev, tier, n_items, trials, requests, failed, score,"
        " conservative, cost, currency, started_at, finished_at, health,"
        " outdir, summary) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (summary["run_id"], kind, summary["model_key"],
         summary["endpoint_sha"], summary["bank_rev"], summary.get("tier"),
         summary["n_items"], summary["trials"], summary["requests"],
         rt.get("failed", 0), summary.get("quant_score"),
         summary.get("conservative"), cost.get("total_cost"),
         cost.get("currency"), summary.get("started_at") or 0.0,
         summary.get("finished_at"), health.get("verdict"), outdir,
         json.dumps(summary, ensure_ascii=False)))
    rows = []
    for iid, r in (summary.get("items") or {}).items():
        rows.append((summary["run_id"], iid, r.get("dim") or "?",
                     summary["bank_rev"], len(r.get("scores") or []),
                     r.get("full_pass") or 0, r.get("mean"), r.get("spread"),
                     r.get("latency_p50")))
    con.executemany(
        "INSERT OR REPLACE INTO results (run_id, item_id, dim, bank_rev,"
        " n, k, mean, spread, latency_p50) VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    return summary["run_id"]


def save_baseline(con, bl):
    """存基线，并把同序列的旧基线置为失效。"""
    con.execute(
        "UPDATE baselines SET active = 0 WHERE model_key=? AND endpoint_sha=?"
        " AND bank_rev=? AND tier=?",
        (bl["model_key"], bl["endpoint_sha"], bl["bank_rev"], bl["tier"]))
    cur = con.execute(
        "INSERT INTO baselines (model_key, endpoint_sha, bank_rev, tier,"
        " mean, sigma, sigma_source, threshold, rounds, provisional,"
        " run_ids, dim_means, detail, created_at, active)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
        (bl["model_key"], bl["endpoint_sha"], bl["bank_rev"], bl["tier"],
         bl["mean"], bl["sigma"], bl["sigma_source"], bl["threshold"],
         bl["rounds"], 1 if bl.get("provisional") else 0,
         json.dumps(bl.get("run_ids") or [], ensure_ascii=False),
         json.dumps(bl.get("dim_means") or {}, ensure_ascii=False),
         json.dumps({k: v for k, v in bl.items() if k != "id"},
                    ensure_ascii=False, default=str),
         bl["created_at"]))
    con.commit()
    return cur.lastrowid


def save_check(con, ck):
    cur = con.execute(
        "INSERT INTO checks (run_id, baseline_id, model_key, endpoint_sha,"
        " bank_rev, tier, score, delta, z, state, consecutive, reason,"
        " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ck["run_id"], ck["baseline_id"], ck["model_key"], ck["endpoint_sha"],
         ck["bank_rev"], ck["tier"], ck["score"], ck["delta"], ck.get("z"),
         ck["state"], ck["consecutive"], ck.get("reason"), ck["created_at"]))
    con.commit()
    return cur.lastrowid


# --------------------------------------------------------------------------
# 读
# --------------------------------------------------------------------------

def get_run(con, run_id):
    r = con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return dict(r) if r else None


def list_runs(con, model_key=None, kind=None, limit=30):
    # conservative 也要给出来：**监控判定用的是保守分，不是量化分**。
    # 只给 score 会让界面和判定看起来对不上（实测 91.7 vs 69.6）。
    sql = ("SELECT run_id, kind, model_key, endpoint_sha, bank_rev, tier,"
           " score, conservative, requests, failed, health, started_at,"
           " outdir FROM runs")
    where, args = [], []
    if model_key:
        where.append("model_key=?")
        args.append(model_key)
    if kind:
        where.append("kind=?")
        args.append(kind)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY started_at DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def active_baseline(con, model_key, endpoint_sha, bank_rev, tier):
    r = con.execute(
        "SELECT * FROM baselines WHERE model_key=? AND endpoint_sha=?"
        " AND bank_rev=? AND tier=? AND active=1"
        " ORDER BY created_at DESC LIMIT 1",
        (model_key, endpoint_sha, bank_rev, tier)).fetchone()
    if not r:
        return None
    d = dict(r)
    if d.get("detail"):
        # 先铺全文，再让列覆盖：列是主库，JSON 只补那些没建列的字段
        full = json.loads(d["detail"])
        full.update({k: v for k, v in d.items() if v is not None})
        d = full
    d["run_ids"] = json.loads(d["run_ids"] or "[]")
    d["dim_means"] = json.loads(d["dim_means"] or "{}")
    d["provisional"] = bool(d["provisional"])
    return d


def stale_baselines(con, model_key, endpoint_sha, bank_rev, tier):
    """本序列没有生效基线时，查是不是有别的 bank_rev / 别的端点的基线。

    这个查询存在的唯一目的是把错误信息说清楚：
    「从没建过基线」和「建过但题库换版本了」是两回事，
    后者的正确动作是重建基线并说明原因，前者是第一次建。
    """
    rows = con.execute(
        "SELECT bank_rev, endpoint_sha, tier, rounds, created_at"
        " FROM baselines WHERE model_key=? AND active=1"
        " AND (bank_rev<>? OR endpoint_sha<>? OR tier<>?)"
        " ORDER BY created_at DESC",
        (model_key, bank_rev, endpoint_sha, tier)).fetchall()
    return [dict(r) for r in rows]


def series(con, model_key, endpoint_sha, bank_rev, tier=None, limit=200):
    """一条可以连线的曲线。三元组全都要匹配——这就是不会画错的原因。"""
    sql = ("SELECT run_id, kind, tier, score, conservative, requests, failed,"
           " health, started_at FROM runs WHERE model_key=? AND endpoint_sha=?"
           " AND bank_rev=?")
    args = [model_key, endpoint_sha, bank_rev]
    if tier:
        sql += " AND tier=?"
        args.append(tier)
    sql += " ORDER BY started_at ASC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def recent_checks(con, model_key, endpoint_sha, bank_rev, tier, limit=10):
    rows = con.execute(
        "SELECT * FROM checks WHERE model_key=? AND endpoint_sha=?"
        " AND bank_rev=? AND tier=? ORDER BY created_at DESC LIMIT ?",
        (model_key, endpoint_sha, bank_rev, tier, limit)).fetchall()
    return [dict(r) for r in rows]


def compare_runs(con, run_id_a, run_id_b):
    """取两次 run 做对比。**跨 bank_rev 抛异常。**

    这是 实施计划第 0.7 项 的验收点。返回空会被上层当成没数据，
    于是两套题库的分数被默默画进同一张图——那正是这个工具要防的事。
    """
    a, b = get_run(con, run_id_a), get_run(con, run_id_b)
    missing = [x for x, r in ((run_id_a, a), (run_id_b, b)) if r is None]
    if missing:
        raise StoreError("run 不存在：%s" % "、".join(missing))
    if a["bank_rev"] != b["bank_rev"]:
        raise BankRevMismatch(
            "拒绝比较：%s 用的题库是 %s，%s 用的是 %s。\n"
            "不同版本的题库题目不同、难度不同，分差里混着换题的影响，"
            "减出来的数没有意义。要比就用同一版本各跑一轮。"
            % (run_id_a, a["bank_rev"], run_id_b, b["bank_rev"]))
    if a["endpoint_sha"] != b["endpoint_sha"]:
        raise BankRevMismatch(
            "拒绝比较：两次 run 的端点指纹不同（%s vs %s）。\n"
            "base_url / model / temperature / max_tokens 任一项变了"
            "就是另一个被测对象，不是同一条序列。"
            % (a["endpoint_sha"], b["endpoint_sha"]))
    return a, b


def item_history(con, item_id, bank_rev, model_key=None):
    """一道题在历次 run 里的表现。算轮间 SD、做稳健性筛选用（实施计划第 1.1 项）。"""
    sql = ("SELECT r.run_id, r.model_key, r.started_at, s.n, s.k, s.mean"
           " FROM results s JOIN runs r ON r.run_id = s.run_id"
           " WHERE s.item_id=? AND s.bank_rev=?")
    args = [item_id, bank_rev]
    if model_key:
        sql += " AND r.model_key=?"
        args.append(model_key)
    sql += " ORDER BY r.started_at ASC"
    return [dict(x) for x in con.execute(sql, args).fetchall()]
