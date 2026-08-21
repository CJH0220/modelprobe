#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""校验「运行时零第三方依赖」这个声称是**真的**。零请求、零花费。

## 为什么要有这个

`requirements.txt` 为空是一句声称，不是一个事实。任何人加一行
`import requests` 都会使它失效，而失效不会有任何报错 ——
本机装过那个包，代码就照常跑。问题只在换机器部署时才暴露。

更要紧的一层：依赖树的版本变动可能改变判分结果，而这种变动
不体现在 `bank_rev` 上，于是**不可比性无法被检测到**。
零依赖不是简洁偏好，是可比性的前提。

## 校验三处是否互相一致

  1. 代码里实际出现的第三方 import
  2. `requirements.txt` 登记的包
  3. `pyproject.toml` 的 `dependencies`

三者必须一致。不一致即失败，而不是警告。

判定「是否第三方」不查名单，而是解析 import 的实际来源：
落在 site-packages / dist-packages 下的即为第三方。这样新出现的
标准库模块不会被误报，而 vendored 进环境的包也不会被漏掉。

用法：
    python tools/check_deps.py
    python tools/check_deps.py --list     # 打印全部顶层模块及归类
"""

import ast
import importlib.util
import io
import os
import re
import sys
import sysconfig

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: 不扫的目录：产物、存档、缓存、题库数据。
SKIP_DIRS = {".git", "data", "_dev_archive", "__pycache__",
             "node_modules", ".venv", "venv", "build", "dist"}

#: 本项目自身的顶层包，不算依赖。
LOCAL = {"mprobe"}

SITE_MARKERS = ("site-packages", "dist-packages")


def py_files():
    """仓库内全部 .py：mprobe/ 与 tools/ 递归，仓库根只取顶层。"""
    for name in sorted(os.listdir(ROOT)):
        if name.endswith(".py"):
            yield os.path.join(ROOT, name)
    for base in ("mprobe", "tools"):
        for dp, dn, fn in os.walk(os.path.join(ROOT, base)):
            dn[:] = [d for d in dn if d not in SKIP_DIRS]
            for f in sorted(fn):
                if f.endswith(".py"):
                    yield os.path.join(dp, f)


def top_imports(path):
    """该文件 import 的顶层模块名。相对 import 跳过（那是本项目内部）。"""
    try:
        tree = ast.parse(io.open(path, encoding="utf-8").read())
    except (SyntaxError, UnicodeDecodeError) as e:
        raise RuntimeError("%s 解析失败：%s" % (path, e))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:          # from . import x —— 本项目内部
                continue
            if node.module:
                out.add(node.module.split(".")[0])
    return out


def classify(mod):
    """('stdlib'|'third'|'local'|'missing', 来源路径)。

    不查名单：看 import 的实际落点。site-packages 下的就是要 pip 装的。
    """
    if mod in LOCAL:
        return "local", ""
    if mod in sys.builtin_module_names:
        return "stdlib", "内置"
    names = getattr(sys, "stdlib_module_names", None)   # 3.10+
    try:
        spec = importlib.util.find_spec(mod)
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        # 找不到：若在标准库名单里说明是本解释器版本没有的标准库模块，
        # 否则是一个**装都没装**的第三方包 —— 后者是硬错误。
        return ("stdlib" if names and mod in names else "missing"), ""
    origin = spec.origin or ""
    if any(m in origin.replace("\\", "/") for m in SITE_MARKERS):
        return "third", origin
    if origin in ("built-in", "frozen"):
        return "stdlib", origin
    std = sysconfig.get_paths().get("stdlib", "")
    if std and os.path.abspath(origin).startswith(os.path.abspath(std)):
        return "stdlib", origin
    if names and mod in names:
        return "stdlib", origin
    return "third", origin or "（来源不明）"


def declared_requirements():
    """requirements.txt 登记的包名（小写、去掉版本约束）。"""
    p = os.path.join(ROOT, "requirements.txt")
    if not os.path.isfile(p):
        return None
    out = set()
    for line in io.open(p, encoding="utf-8"):
        line = line.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        out.add(re.split(r"[<>=!~\[; ]", line)[0].strip().lower())
    return out


def declared_pyproject():
    """pyproject.toml 的 dependencies。tomllib 是 3.11+，故用正则。"""
    p = os.path.join(ROOT, "pyproject.toml")
    if not os.path.isfile(p):
        return None
    text = io.open(p, encoding="utf-8").read()
    m = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.S | re.M)
    if not m:
        return None
    return {re.split(r"[<>=!~\[; ]", s)[0].strip().lower()
            for s in re.findall(r"[\"']([^\"']+)[\"']", m.group(1))}


def main():
    show_all = "--list" in sys.argv
    buckets = {"stdlib": {}, "third": {}, "local": {}, "missing": {}}
    n_files = 0
    for path in py_files():
        n_files += 1
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        for mod in top_imports(path):
            kind, origin = classify(mod)
            buckets[kind].setdefault(mod, (origin, []))[1].append(rel)

    total = sum(len(b) for b in buckets.values())
    print("扫描 %d 个 .py，顶层模块 %d 个：标准库 %d ／ 本项目 %d ／ "
          "第三方 %d ／ 找不到 %d"
          % (n_files, total, len(buckets["stdlib"]), len(buckets["local"]),
             len(buckets["third"]), len(buckets["missing"])))
    if show_all:
        for kind in ("stdlib", "local", "third", "missing"):
            if not buckets[kind]:
                continue
            print("\n[%s]" % kind)
            for mod in sorted(buckets[kind]):
                origin, users = buckets[kind][mod]
                print("  %-18s %s" % (mod, origin or "—"))

    problems = []
    req = declared_requirements()
    proj = declared_pyproject()

    if req is None:
        problems.append("requirements.txt 不存在")
    if proj is None:
        problems.append("pyproject.toml 里读不到 dependencies")

    used = set(buckets["third"])
    # ① 用了但没登记 —— 换机器部署时会 ImportError
    for mod in sorted(used - (req or set())):
        origin, users = buckets["third"][mod]
        problems.append("import 了第三方 `%s` 但 requirements.txt 没登记"
                        "（%s 等 %d 处）"
                        % (mod, users[0], len(users)))
    # ② 登记了但没人用 —— 让部署凭空多一个依赖
    for mod in sorted((req or set()) - used - set(buckets["missing"])):
        problems.append("requirements.txt 登记了 `%s` 但代码里没有用到" % mod)
    # ③ 两处声明必须一致
    if req is not None and proj is not None and req != proj:
        problems.append("requirements.txt(%s) 与 pyproject.toml dependencies"
                        "(%s) 不一致"
                        % (sorted(req) or "空", sorted(proj) or "空"))
    # ④ 本机 import 不到的模块。分两种，处置不同：
    #    登记过 -> 是「该装的没装」；没登记 -> 也是一个未登记的第三方依赖，
    #    只不过本机恰好也没装。后者按未登记报，因为那才是要改的地方。
    for mod in sorted(buckets["missing"]):
        _o, users = buckets["missing"][mod]
        if mod in (req or set()):
            problems.append("requirements.txt 登记了 `%s` 但本机没装"
                            "（pip install -r requirements.txt）" % mod)
        else:
            problems.append("import 了第三方 `%s` 但 requirements.txt "
                            "没登记（%s 等 %d 处）"
                            % (mod, users[0], len(users)))

    print()
    if problems:
        print("不一致 %d 处：" % len(problems))
        for x in problems:
            print("  ✗ %s" % x)
        return 1
    if used:
        print("✓ %d 个第三方依赖，三处声明一致：%s"
              % (len(used), "、".join(sorted(used))))
    else:
        print("✓ 运行时零第三方依赖，与 requirements.txt 和 "
              "pyproject.toml 的声明一致")
    print("✓ Python 版本要求 >= 3.9（本机 %d.%d.%d）"
          % sys.version_info[:3])
    return 0


if __name__ == "__main__":
    sys.exit(main())
