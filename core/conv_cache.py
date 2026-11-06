"""会话列表快照缓存：登录后先把上次的结果渲染出来，网络刷新后覆盖。

快照存 cache/conv_cache/<md5(server|user_id)>.json，只存列表渲染需要的
展示字段（不含 last_message 详情）。写入用临时文件原子改名，写失败只
记日志——快照永远是"锦上添花"，任何异常都不能影响正常网络加载流程。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os

from .models import Conversation
from .paths import cache_root

log = logging.getLogger(__name__)

# 快照里只保留列表渲染用得到的字段（Conversation 的标量展示字段）
_FIELDS = ("id", "name", "room_type", "unread", "avatar_url",
           "is_favorite", "username", "is_multi_dm",
           "last_message_time", "last_message_text",
           "i_created", "is_discussion", "is_team", "encrypted",
           "muted", "mention_count")


def _snapshot_path(server_url: str, username: str) -> str:
    # key 用 username 而非 user_id：登录前就能从 config 拿到 username，
    # 快照才可能在登录完成前渲染出来（微信式首屏）
    key = hashlib.md5(f"{server_url}|{username}".encode("utf-8")).hexdigest()
    return os.path.join(cache_root(), "conv_cache", f"{key}.json")


def save_conversations_snapshot(server_url: str, username: str, convs: list):
    """保存会话快照；失败只记日志，不抛异常。"""
    if not server_url or not username:
        return
    try:
        data = [{k: getattr(c, k) for k in _FIELDS} for c in convs]
        path = _snapshot_path(server_url, username)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("会话快照写入失败（忽略）：%s", e)


def load_conversations_snapshot(server_url: str, username: str) -> list:
    """读取上次保存的会话快照；无/损坏返回空列表，不抛异常。"""
    if not server_url or not username:
        return []
    try:
        with open(_snapshot_path(server_url, username), "r",
                  encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        convs = []
        for item in data:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            kwargs = {k: item.get(k, "") if k not in ("unread", "mention_count")
                      else int(item.get(k, 0))
                      for k in _FIELDS
                      if k not in ("is_favorite", "is_discussion", "is_team",
                                   "encrypted", "muted", "is_multi_dm")}
            kwargs["is_favorite"] = bool(item.get("is_favorite"))
            kwargs["is_discussion"] = bool(item.get("is_discussion"))
            kwargs["is_team"] = bool(item.get("is_team"))
            # 旧快照无 encrypted 键：item.get 缺省 None，bool() 兜底 False
            kwargs["encrypted"] = bool(item.get("encrypted"))
            kwargs["muted"] = bool(item.get("muted"))
            kwargs["is_multi_dm"] = bool(item.get("is_multi_dm"))
            convs.append(Conversation(**kwargs))
        return convs
    except (OSError, ValueError):
        return []
