#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""冻结题库，生成 `banks/MANIFEST.json`。**零请求。**

把两件事合到一份台账里：
  · 判分器稳健性（`bank.robustness_detail`，含按长度／项数的黑名单）
  · 信噪比实测（实施计划第 1.1 项 的 `banks/snr_core41.json`）

每道题都要能回答两个问题：**它凭什么在库里**、**它凭什么还进不了监控**。

用法：
    python tools/freeze_bank.py --rev 0.2.0            # 只看差异
    python tools/freeze_bank.py --rev 0.2.0 --write    # 写 MANIFEST.json
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from mprobe.engine import bank, dims                  # noqa: E402

BANKS = os.path.join(ROOT, "banks")
SNR = os.path.join(BANKS, "snr_core41.json")
PROBE = os.path.join(BANKS, "probe_3models.json")
FILES = ["core.jsonl", "public_v1.jsonl"]

NOTES = (
    "core.jsonl = 自建行为题与冒烟题；public_v1.jsonl = 公开基准衍生的能力题。"
    "\n"
    "【1.0.0 这个版本号保证什么】题集与题面已冻结，sha256 校验生效，"
    "每道题都有一份台账写明它的判分器稳健性、跨模型实测、以及为什么进不了监控。"
    "同一 bank_rev 下的分数可比。"
    "\n"
    "【它不保证什么】**不保证每道题都通过了测评准入三条判据。**"
    "实际全过者为少数。限制来自采样次数而非题目质量："
    "trials=3 时两比例 z 检验只放行「一方 3/3、另一方 0/3」这种完美分裂，"
    "极差 0.667（3/3 对 1/3）的 z 只有 1.732，达不到 1.96。"
    "需 n 达到数十量级才能通过该检验。"
    "所以 eval_gates 逐条记录在案，读的人自己决定用哪一档证据，"
    "而不是被一个「已定版」的版本号误导。"
    "\n"
    "【监控仍不可用】monitor_ok 要求判分器不黑 + 信噪比（跨模型极差 / 同模型轮间SD）"
    "实测 >= 3。轮间 SD 需要同一模型跑多轮，新并入的 176 道题每题只跑了一轮，"
    "所以算不出分母。可进监控的仍只有 1.1 从历史存档验证过的 2 道。"
    "snr_state = untested 的题**不进监控**——「没测过」不能当「合格」用。"
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rev", required=True)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    ledger = {}
    if os.path.exists(SNR):
        ledger = json.load(open(SNR, encoding="utf-8"))["items"]
        print("信噪比台账：%d 道题有实测" % len(ledger))
    else:
        print("没有信噪比台账，全部题目将标 untested")

    probe = {}
    if os.path.exists(PROBE):
        pj = json.load(open(PROBE, encoding="utf-8"))
        probe = pj["items"]
        print("跨模型台账：%d 道题有实测（模型 %s）"
              % (len(probe), "、".join(pj.get("models") or [])))

    old = {}
    mpath = os.path.join(BANKS, "MANIFEST.json")
    if os.path.exists(mpath):
        o = json.load(open(mpath, encoding="utf-8"))
        old = o.get("items") or {}
        print("旧清单：bank_rev %s，%d 道题" % (o.get("bank_rev"), len(old)))

    if not args.write:
        # 先算一遍但不落盘，把差异摆出来
        import tempfile
        tmp = tempfile.mkdtemp()
        for fn in FILES + ["assets"]:
            src = os.path.join(BANKS, fn)
            dst = os.path.join(tmp, fn)
            if os.path.isdir(src):
                import shutil
                shutil.copytree(src, dst)
            else:
                import shutil
                shutil.copy2(src, dst)
        mf, _ = bank.freeze(tmp, args.rev, FILES, NOTES, ledger, probe)
    else:
        mf, path = bank.freeze(BANKS, args.rev, FILES, NOTES, ledger, probe)

    items = mf["items"]
    print("\nbank_rev %s，共 %d 道题" % (mf["bank_rev"], len(items)))
    print("文件：%s"
          % "、".join("%s(%d 道)" % (k, v["items"])
                      for k, v in sorted(mf["files"].items())))

    rob = Counter(v["robustness"] for v in items.values())
    print("稳健性：白 %d ／ 灰 %d ／ 黑 %d"
          % (rob["white"], rob["grey"], rob["black"]))

    st = Counter(v.get("snr_state") for v in items.values())
    print("信噪比状态：已验证 %d ／ 实测不达标 %d ／ 饱和 %d ／ 未测 %d"
          % (st["verified"], st["weak"], st["saturated"], st["untested"]))

    pg = Counter(v.get("probe_grade") for v in items.values())
    print("跨模型定级：live %d ／ 天花板 %d ／ 地板 %d ／ 截断 %d ／ 未测 %d"
          % (pg["live"], pg["ceiling"], pg["floor"], pg["truncated"],
             pg["unmeasured"]))
    ev = sum(1 for v in items.values() if v.get("eval_ok"))
    print("**测评准入三条全过 %d 道**（非饱和 + 极差>=0.15 + z>=1.96）" % ev)

    ok = [k for k, v in items.items() if v["monitor_ok"]]
    print("**可进监控 %d 道**：%s" % (len(ok), sorted(ok)))
    blk = Counter(v.get("monitor_block") for v in items.values()
                  if not v["monitor_ok"])
    for why, n in blk.most_common():
        print("   挡在外面 %3d 道：%s" % (n, why))

    # 逐维盘点：题数、黑题、可监控
    print("\n%-5s %-9s %5s %5s %5s %5s   %s"
          % ("维", "名称", "题数", "黑", "可监控", "命名空间", "档位含义"))
    per = defaultdict(lambda: Counter())
    for k, v in items.items():
        c = per[v["dim"]]
        c["n"] += 1
        if v["robustness"] == "black":
            c["black"] += 1
        if v["monitor_ok"]:
            c["ok"] += 1
    for d in sorted(per, key=lambda x: (-per[x]["n"], x)):
        c = per[d]
        n = c["n"]
        cap = ("可判定" if n >= 12 else "看趋势" if n >= 6 else "不显示")
        print("%-5s %-9s %5d %5d %5d %5s   %s"
              % (d, dims.name(d), n, c["black"], c["ok"],
                 dims.NS_LABEL[dims.namespace(d)], cap))
    print("档位含义按 MEASUREMENT 2.4：m>=12 可判定，6<=m<12 只看趋势，m<6 不显示")

    if old:
        chg = [k for k in set(old) & set(items)
               if old[k].get("robustness") != items[k].get("robustness")]
        print("\n与旧清单相比：新增 %d 道、移除 %d 道、分级变动 %d 道"
              % (len(set(items) - set(old)), len(set(old) - set(items)),
                 len(chg)))
        for k in sorted(chg):
            print("   %-8s %s -> %s"
                  % (k, old[k].get("robustness"), items[k].get("robustness")))

    if args.write:
        print("\n已写 %s" % os.path.relpath(path, ROOT))
    else:
        print("\n（未写盘。加 --write 才落 MANIFEST.json）")


if __name__ == "__main__":
    main()
