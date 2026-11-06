"""消息历史快照缓存：打开会话先把上次的首屏渲染出来，网络回来后整体替换。

快照存 cache/msg_cache/<md5(server|username|rid)>.json，保存网络首屏的
尾部（最新 page_size 条）。消息可能来自 E2E 房间，存的是解密后的明文
（与本地配置同目录本就存有可还原密码的密钥，未引入新攻击面）。写入用
临时文件原子改名，写失败只记日志——快照是"锦上添花"，任何异常都不能
影响正常网络加载流程。网络首屏到达前实时推送的新消息靠 _room_messages
尾部合并兜底，见 main_window._load_history._done。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict, fields

from .models import Message, User
from .paths import cache_root

log = logging.getLogger(__name__)


def _snapshot_path(server_url: str, username: str, room_id: str) -> str:
    key = hashlib.md5(
        f"{server_url}|{username}|{room_id}".encode("utf-8")).hexdigest()
    return os.path.join(cache_root(), "msg_cache", f"{key}.json")


def save_messages_snapshot(server_url: str, username: str, room_id: str,
                           msgs: list):
    """保存房间消息快照；失败只记日志，不抛异常。"""
    if not server_url or not username or not room_id or not msgs:
        return
    try:
        data = [asdict(m) for m in msgs]
        path = _snapshot_path(server_url, username, room_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError) as e:
        log.warning("消息快照写入失败（忽略）rid=%s：%s", room_id, e)


def load_messages_snapshot(server_url: str, username: str,
                           room_id: str) -> list:
    """读取房间消息快照（时间正序）；无/损坏返回空列表，不抛异常。"""
    if not server_url or not username or not room_id:
        return []
    try:
        with open(_snapshot_path(server_url, username, room_id), "r",
                  encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        known = {fl.name for fl in fields(Message)}
        msgs = []
        for item in data:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            item = {k: v for k, v in item.items() if k in known}
            sender = item.get("sender")
            if isinstance(sender, dict):
                item["sender"] = User(**{
                    k: v for k, v in sender.items()
                    if k in {fl.name for fl in fields(User)}})
            else:
                item["sender"] = None
            try:
                msgs.append(Message(**item))
            except TypeError:
                # 单条损坏跳过，不影响其余
                continue
        return msgs
    except (OSError, ValueError):
        return []
