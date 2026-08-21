# -*- coding: utf-8 -*-
"""API 密钥管理。

三级来源，优先级从高到低：

    1. 会话内存   进程内，重启即失。最安全，适合临时试一个 key
    2. 本地文件   config/secrets.local.json，已加入 .gitignore
    3. 环境变量   原有方式，CI / 定时任务里最合适

设计上的两条硬规矩：

  · **完整密钥永不回传给浏览器，也永不出现在任何报错里。** 界面只拿到
    掩码（sk-…a1b2）和来源标记。否则任何能打开这个页面的人都能把 key 抄走。
  · **secrets 文件默认不存在。** 只有你在界面上明确选择「存到本地文件」
    才会创建，且创建时会尽力收紧文件权限。

要在版本管理里共享配置又不泄露密钥，就保持用环境变量——配置文件里存的
始终只是变量名。
"""

import json
import os
import stat
import threading

from .. import paths

CONFIG_DIR = paths.CONFIG
SECRETS_FILE = paths.SECRETS

_SESSION = {}
_LOCK = threading.Lock()

SOURCE_LABEL = {"session": "会话内存", "file": "本地文件", "env": "环境变量"}


def mask(key):
    """掩码。短 key 一律全掩，避免把有效信息露出来。"""
    if not key:
        return ""
    if len(key) < 12:
        return "*" * len(key)
    return "%s…%s" % (key[:3], key[-4:])


def _read_file():
    if not os.path.isfile(SECRETS_FILE):
        return {}
    try:
        with open(SECRETS_FILE, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_file(d):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(SECRETS_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    try:                                    # 尽力收紧权限（Windows 上作用有限）
        os.chmod(SECRETS_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


def resolve(name):
    """返回 (密钥, 来源)。都找不到时返回 (None, None)。"""
    if not name:
        return None, None
    with _LOCK:
        if name in _SESSION and _SESSION[name]:
            return _SESSION[name], "session"
    v = _read_file().get(name)
    if v:
        return v, "file"
    v = os.environ.get(name)
    if v:
        return v, "env"
    return None, None


def set_key(name, key, store="file"):
    """默认写 config/secrets.local.json。

    session 只在当前进程有效，作为默认值会让人以为已经存好了，
    下一条命令又说密钥缺失。
    """
    if not name:
        raise ValueError("必须指定变量名")
    key = (key or "").strip()
    if not key:
        raise ValueError("密钥不能为空")
    if store == "session":
        with _LOCK:
            _SESSION[name] = key
    elif store == "file":
        d = _read_file()
        d[name] = key
        _write_file(d)
    else:
        raise ValueError("store 只支持 session / file")
    return status_of(name)


def clear(name, store="all"):
    if store in ("all", "session"):
        with _LOCK:
            _SESSION.pop(name, None)
    if store in ("all", "file"):
        d = _read_file()
        if name in d:
            d.pop(name)
            _write_file(d)
    return status_of(name)


def status_of(name):
    key, src = resolve(name)
    with _LOCK:
        in_session = bool(_SESSION.get(name))
    return {
        "name": name,
        "set": bool(key),
        "source": src,
        "source_label": SOURCE_LABEL.get(src, "未设置"),
        "masked": mask(key),
        "in_session": in_session,
        "in_file": bool(_read_file().get(name)),
        "in_env": bool(os.environ.get(name)),
    }


def status_all(names):
    return [status_of(n) for n in dict.fromkeys(n for n in names if n)]


def file_exists():
    return os.path.isfile(SECRETS_FILE)
