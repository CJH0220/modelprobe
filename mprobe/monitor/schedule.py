# -*- coding: utf-8 -*-
"""节奏与计划任务。

两件事，分得很开：

    due()      纯计算——按上次运行时间判断该不该跑了。不碰系统。
    install()  真正的定时——写进 Windows 计划任务。

**状态一律读 schtasks /query 的真实结果。**
配置文件里写着「已启用」不代表任务真的存在：任务可能被人删了、
被组策略禁了、被杀毒软件拦了。读配置文件会得到一个永远正确的谎言。

密钥的坑（这一条会让人卡住半小时）
--------------------------------
计划任务跑在**另一个登录会话**里，看不到你当前终端的环境变量。
API key 必须设成**用户级**环境变量：

    [Environment]::SetEnvironmentVariable("DEEPSEEK_API_KEY", "sk-xxx", "User")

设完之后，已经开着的终端仍然看不到它——那些终端启动时就把环境拷走了。
新开一个窗口才有。
"""

import subprocess
import sys
import time

CADENCES = {
    "daily": {"label": "日频", "hours": 24, "order": 1,
              "schtasks": ["/SC", "DAILY"]},
    "weekly": {"label": "周频", "hours": 24 * 7, "order": 2,
               "schtasks": ["/SC", "WEEKLY", "/D", "MON"]},
    "monthly": {"label": "月频", "hours": 24 * 30, "order": 3,
                "schtasks": ["/SC", "MONTHLY", "/D", "1"]},
}

TASK_PREFIX = "mprobe"


class ScheduleError(Exception):
    pass


def task_name(model_key, tier):
    return "%s_%s_%s" % (TASK_PREFIX, model_key, tier)


# --------------------------------------------------------------------------
# 纯计算
# --------------------------------------------------------------------------

def due(cadence, last_ts, now=None):
    """该不该跑了。返回 (是否到期, 说明, 距上次小时数)。"""
    now = now or time.time()
    if not last_ts:
        return True, "从未运行", None
    hours = (now - last_ts) / 3600.0
    c = CADENCES.get(cadence)
    if not c:
        raise ScheduleError("未知节奏：%s（可选 %s）"
                            % (cadence, "、".join(CADENCES)))
    if hours >= c["hours"]:
        return True, "距上次 %.1f 小时，已达%s周期" % (hours, c["label"]), hours
    return False, "距上次 %.1f 小时，未到 %.0f 小时周期" % (hours, c["hours"]), hours


# --------------------------------------------------------------------------
# Windows 计划任务
# --------------------------------------------------------------------------

def _schtasks(args):
    if not sys.platform.startswith("win"):
        raise ScheduleError("计划任务目前只支持 Windows。"
                            "其他平台请用 cron 调用同一条 CLI 命令。")
    try:
        p = subprocess.run(["schtasks"] + args, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise ScheduleError("找不到 schtasks.exe")
    return p.returncode, (p.stdout or ""), (p.stderr or "")


def command_line(model_key, tier, python=None):
    """计划任务实际执行的命令。用绝对路径——计划任务的工作目录不是这里。"""
    from .. import paths
    py = python or sys.executable
    return ('"%s" -m mprobe check --model %s --tier %s --quiet'
            % (py, model_key, tier)), str(paths.ROOT)


def install(model_key, tier, cadence, at="09:30", python=None, force=False):
    """装一个计划任务。返回 (name, 说明)。"""
    c = CADENCES.get(cadence)
    if not c:
        raise ScheduleError("未知节奏：%s" % cadence)
    name = task_name(model_key, tier)
    cmd, cwd = command_line(model_key, tier, python)

    if not force and status(name)["exists"]:
        raise ScheduleError("任务 %s 已存在。要替换请加 --force。" % name)

    args = ["/Create", "/TN", name, "/TR", "cmd /c cd /d \"%s\" && %s" % (cwd, cmd),
            "/ST", at, "/F"] + c["schtasks"]
    rc, out, err = _schtasks(args)
    if rc != 0:
        raise ScheduleError("创建失败（rc=%d）：%s" % (rc, (err or out).strip()))
    return name, ("已创建 %s，%s %s 执行。\n"
                  "注意：计划任务看不到当前终端的环境变量，"
                  "API key 要设成用户级（见 mprobe schedule --help）。"
                  % (name, c["label"], at))


def uninstall(model_key, tier):
    name = task_name(model_key, tier)
    rc, out, err = _schtasks(["/Delete", "/TN", name, "/F"])
    if rc != 0:
        raise ScheduleError("删除失败（rc=%d）：%s" % (rc, (err or out).strip()))
    return name, "已删除 %s" % name


def status(name):
    """任务的真实状态。**不读配置文件。**"""
    try:
        rc, out, err = _schtasks(["/Query", "/TN", name, "/FO", "LIST"])
    except ScheduleError as e:
        return {"name": name, "exists": False, "error": str(e)}
    if rc != 0:
        return {"name": name, "exists": False,
                "note": "系统里没有这个任务（哪怕配置里写着有）"}
    d = {"name": name, "exists": True}
    for line in out.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if k in ("状态", "Status"):
            d["state"] = v
        elif k in ("下次运行时间", "Next Run Time"):
            d["next_run"] = v
        elif k in ("上次运行时间", "Last Run Time"):
            d["last_run"] = v
        elif k in ("上次结果", "Last Result"):
            d["last_result"] = v
    return d


def list_all():
    """列出本工具装的全部计划任务。"""
    rc, out, err = _schtasks(["/Query", "/FO", "CSV", "/NH"])
    if rc != 0:
        return []
    names = set()
    for line in out.splitlines():
        cells = [c.strip().strip('"') for c in line.split('","')]
        if not cells:
            continue
        n = cells[0].strip('"').lstrip("\\")
        if n.startswith(TASK_PREFIX + "_"):
            names.add(n)
    return [status(n) for n in sorted(names)]
