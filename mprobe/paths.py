# -*- coding: utf-8 -*-
"""路径解析。全项目只有这一处知道文件在哪。

`data/` 是唯一可写的目录。`banks/` 与 `profiles/` 随版本发布，**只读**——
运行期写入题库目录，等于让每台机器的题库悄悄分叉。
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BANKS = os.path.join(ROOT, "banks")
PROFILES = os.path.join(ROOT, "profiles")
CONFIG = os.path.join(ROOT, "config")
MODELS = os.path.join(CONFIG, "models")
DATA = os.path.join(ROOT, "data")
RUNS = os.path.join(DATA, "runs")
DB = os.path.join(DATA, "mprobe.db")
SECRETS = os.path.join(CONFIG, "secrets.local.json")
PRICING = os.path.join(CONFIG, "pricing.json")
SCHEDULE = os.path.join(DATA, "schedule.json")


def ensure_data():
    os.makedirs(RUNS, exist_ok=True)
    return DATA


def run_dir(run_id, create=False):
    d = os.path.join(RUNS, run_id)
    if create:
        os.makedirs(d, exist_ok=True)
    return d


def bank(name):
    return os.path.join(BANKS, name)


def profile(tier):
    return os.path.join(PROFILES, "%s.json" % tier)


def model_config(key):
    return os.path.join(MODELS, "%s.json" % key)


def list_model_keys():
    if not os.path.isdir(MODELS):
        return []
    return sorted(f[:-5] for f in os.listdir(MODELS)
                  if f.endswith(".json") and not f.startswith("_"))
