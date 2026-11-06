"""自定义表情包存储：storage_root()/stickers/ 目录，纯文件操作不碰 Qt。

图片消息「收藏为表情」与表情包页签「+ 导入」共用 save_sticker：
按内容 md5 命名保扩展名（动图必须保 .gif 才能在接收端保持动画），
重复收藏同一张图天然去重。clear_cache 只清 cache/，表情不受影响。

使用频率记在同目录 usage.json（basename -> {count, last}），发送一次
计数一次，list_stickers 按使用次数倒序、同频按最近使用/收藏时间排。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time

_VALID_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
_USAGE_FILE = "usage.json"


def stickers_dir() -> str:
    from .paths import storage_root
    d = os.path.join(storage_root(), "stickers")
    os.makedirs(d, exist_ok=True)
    return d


def _sniff_ext(path: str) -> str:
    """扩展名缺失/异常时按文件魔数归类；认不出按 .png 兜底。"""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:
        return ".png"
    if head.startswith(b"\x89PNG"):
        return ".png"
    if head.startswith(b"GIF8"):
        return ".gif"
    if head.startswith(b"\xff\xd8"):
        return ".jpg"
    if head.startswith(b"BM"):
        return ".bmp"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return ".webp"
    return ".png"


def save_sticker(src: str, report_new: bool = False):
    """复制图片进表情包目录，返回存储路径。

    按内容 md5 命名去重：完全相同的内容（哪怕文件名不同）返回既有文件。
    report_new=True 时返回 (路径, 是否新收藏)，供批量导入统计跳过数。
    """
    ext = os.path.splitext(src)[1].lower()
    if ext not in _VALID_EXTS:
        ext = _sniff_ext(src)
    with open(src, "rb") as f:
        data = f.read()
    name = hashlib.md5(data).hexdigest() + ext
    dst = os.path.join(stickers_dir(), name)
    created = False
    if not os.path.exists(dst):
        # .part 原子改名，避免中途失败留下半个文件
        tmp = dst + ".part"
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
        created = True
    return (dst, created) if report_new else dst


def _usage_path() -> str:
    return os.path.join(stickers_dir(), _USAGE_FILE)


def _load_usage() -> dict:
    try:
        with open(_usage_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_usage(data: dict):
    tmp = _usage_path() + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, _usage_path())


def record_sticker_use(path: str):
    """使用一次表情包：计数 +1 并记最近使用时间（频率排序依据）。"""
    name = os.path.basename(path)
    data = _load_usage()
    entry = data.get(name) or {}
    entry["count"] = int(entry.get("count", 0)) + 1
    entry["last"] = time.time()
    data[name] = entry
    _save_usage(data)


def usage_count(path: str) -> int:
    entry = _load_usage().get(os.path.basename(path)) or {}
    return int(entry.get("count", 0))


def delete_sticker(path: str) -> bool:
    """删除表情文件与使用记录；文件本就不存在时返回 False。

    计数文件清理失败只记为忽略（不影响删除本体）。"""
    name = os.path.basename(path)
    removed = False
    try:
        os.remove(path)
        removed = True
    except FileNotFoundError:
        pass
    data = _load_usage()
    if data.pop(name, None) is not None:
        try:
            _save_usage(data)
        except OSError:  # noqa: BLE001
            pass
    return removed


def list_stickers() -> list:
    """已收藏表情的绝对路径列表：使用次数倒序 > 最近使用时间 >
    收藏时间（新近的在前）。"""
    d = stickers_dir()
    usage = _load_usage()
    names = [f for f in os.listdir(d)
             if os.path.splitext(f)[1].lower() in _VALID_EXTS]

    def _key(f):
        entry = usage.get(f) or {}
        return (-int(entry.get("count", 0)),
                -float(entry.get("last", 0.0)),
                -os.path.getmtime(os.path.join(d, f)))

    names.sort(key=_key)
    return [os.path.join(d, f) for f in names]
