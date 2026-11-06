"""用户状态磁盘缓存：登录后先把上次的状态点显示出来，后台再刷新。

存 cache/status_cache/<md5(server|account)>.json，只存状态展示需要的
少量字段（不含 email/roles 等资料）。写入用临时文件原子改名，失败只
记日志——缓存永远是「锦上添花」，任何异常都不能影响正常拉取流程。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os

from .paths import cache_root

log = logging.getLogger(__name__)

_FIELDS = ("username", "name", "status", "status_text", "status_key",
           "department", "active")


def _cache_path(server_url: str, account: str) -> str:
    key = hashlib.md5(f"{server_url}|{account}".encode("utf-8")).hexdigest()
    return os.path.join(cache_root(), "status_cache", f"{key}.json")


def save_status_cache(server_url: str, account: str, entries: dict):
    """entries: username -> 状态字段 dict（含 _ts 保存时刻 epoch 秒）。

    失败只记日志，不抛异常。
    """
    if not server_url or not account or not entries:
        return
    try:
        data = [{k: e.get(k) for k in _FIELDS if k in e}
                | {"_ts": e.get("_ts", 0)} for e in entries.values()]
        path = _cache_path(server_url, account)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("用户状态缓存写入失败（忽略）：%s", e)


def load_status_cache(server_url: str, account: str) -> dict:
    """返回 username -> (info dict, _ts epoch 秒)；无/损坏返回空 dict。"""
    if not server_url or not account:
        return {}
    try:
        with open(_cache_path(server_url, account), "r",
                  encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return {}
        out = {}
        for item in data:
            if not isinstance(item, dict) or not item.get("username"):
                continue
            out[item["username"]] = (item, int(item.get("_ts", 0) or 0))
        return out
    except (OSError, ValueError):
        return {}
