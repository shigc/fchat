"""每会话本地已读位置：供未读分隔线与「跳到首条未读」使用。

服务端只有未读数，没有「上次读到哪一条」的时间戳；打开房间即清零后
就无法再标出从哪开始是新消息。这里把打开时见过的最新消息时间记到
CONFIG_DIR/read_state.json（与 drafts.json 同思路，不放 cache/，
清缓存或迁移存储都不该丢掉已读位置）。

时间戳取服务端消息的 timestamp（不是本地时钟），避免客户端与服务器
时钟偏差导致分隔线错位。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os

from config import CONFIG_DIR

log = logging.getLogger(__name__)


def _path() -> str:
    return os.path.join(CONFIG_DIR, "read_state.json")


def _key(server_url: str, username: str, room_id: str) -> str:
    raw = f"{server_url}|{username}|{room_id}".encode("utf-8")
    return hashlib.md5(raw).hexdigest()


def _load_all() -> dict:
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("已读位置读取失败（忽略）：%s", e)
        return {}
    return data if isinstance(data, dict) else {}


def _save_all(data: dict) -> None:
    path = _path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("已读位置写入失败（忽略）：%s", e)


def get_last_read(server_url: str, username: str,
                  room_id: str) -> float:
    """上次打开房间时见过的最后一条消息时间；无记录返回 0。"""
    if not room_id:
        return 0.0
    try:
        return float(_load_all().get(_key(server_url, username, room_id), 0))
    except (TypeError, ValueError):
        return 0.0


def set_last_read(server_url: str, username: str, room_id: str,
                  ts: float) -> None:
    """记录已读位置（只前进不后退，防止迟到回调把标记改旧）。"""
    if not room_id or not ts or ts <= 0:
        return
    data = _load_all()
    key = _key(server_url, username, room_id)
    try:
        old = float(data.get(key, 0) or 0)
    except (TypeError, ValueError):
        old = 0.0
    if ts <= old:
        return
    data[key] = float(ts)
    _save_all(data)


