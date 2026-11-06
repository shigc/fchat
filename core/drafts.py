"""每会话草稿：切换会话、重启后保留未发送的文本与引用。

存放在 CONFIG_DIR/drafts.json，**不放 cache/**：设置页「清空缓存」不该
清掉用户没发出去的内容，存储路径迁移也只搬 cache/logs。

内联截图是 QTextDocument 的 resource，无法序列化，草稿只保留纯文本与
引用条；恢复后截图不会回来（发送前就被清掉了）。

写入用临时文件 + os.replace 原子替换；任何读写失败只记日志——草稿是
锦上添花，不能影响收发。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Optional

from config import CONFIG_DIR

log = logging.getLogger(__name__)


def _drafts_path() -> str:
    return os.path.join(CONFIG_DIR, "drafts.json")


def _key(server_url: str, username: str, room_id: str) -> str:
    raw = f"{server_url}|{username}|{room_id}".encode("utf-8")
    return hashlib.md5(raw).hexdigest()


def _load_all() -> dict:
    try:
        with open(_drafts_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("草稿文件读取失败（忽略）：%s", e)
        return {}
    return data if isinstance(data, dict) else {}


def _save_all(data: dict) -> None:
    path = _drafts_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("草稿写入失败（忽略）：%s", e)


def get_draft(server_url: str, username: str, room_id: str) -> Optional[dict]:
    """读取草稿：{"text": str, "quote": dict | None}；无草稿返回 None。"""
    if not room_id:
        return None
    entry = _load_all().get(_key(server_url, username, room_id))
    if not isinstance(entry, dict):
        return None
    text = str(entry.get("text") or "")
    quote = entry.get("quote")
    if not text and not isinstance(quote, dict):
        return None
    return {"text": text, "quote": quote if isinstance(quote, dict) else None}


def set_draft(server_url: str, username: str, room_id: str,
              text: str, quote: Optional[dict] = None) -> None:
    """保存草稿；文本为空且无引用时删除该会话的键。"""
    if not room_id:
        return
    data = _load_all()
    key = _key(server_url, username, room_id)
    if not (text or "").strip() and not quote:
        if key in data:
            data.pop(key, None)
            _save_all(data)
        return
    data[key] = {"text": text or "", "quote": quote or None}
    _save_all(data)


def delete_draft(server_url: str, username: str, room_id: str) -> None:
    if not room_id:
        return
    data = _load_all()
    if data.pop(_key(server_url, username, room_id), None) is not None:
        _save_all(data)
