"""用户在线状态：归一化 + 按需拉取。

状态数据有三个来源，字段可用性各不相同（真机实测）：
    - members 端点：只有实时 status，无 department/active
    - users.info：status/statusText/department/active 全有（一次一人）
    - 目录端点：users.list 有 status+active 无 department；
      directory 搜索有 department 无 status
本模块把三者归一成稳定的状态 key，并提供带缓存与并发限制的
users.info 拉取器（本服务器 users.list 有 4.4 万用户，不做全量预热）。

离职判定只认 department 含「停用」：active=false 里混有临时/外包/
公用账户，不都对应离职。
"""
from __future__ import annotations

import logging
import time
from typing import Callable, List, Optional

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from core import status_cache
from .api_workers import run_async

log = logging.getLogger(__name__)

# 状态 key → 中文（ProfileCard 同款映射，迁来共用）
_STATUS_CN = {
    "online": "在线",
    "away": "离开",
    "busy": "忙碌",
    "dnd": "请勿打扰",
    "offline": "离线",
    "departed": "已停用",
}

_VALID_KEYS = ("online", "away", "busy", "dnd")
_TTL = 60.0        # 正缓存有效期（秒）
_NEG_TTL = 60.0    # 失败负缓存：短时间内不再重试同一用户
_MAX_INFLIGHT = 4  # 并发 users.info 上限，防止大列表场景打穿服务器


def normalize_status_key(status: str = "", department: str = "",
                         active=None) -> str:
    """把各端点的原始字段归一成稳定状态 key。

    department 含「停用」优先判定为 departed；active 仅入库不参与
    判定（真机实测 active=false 不都对应离职）。_mobile 后缀是
    Rocket.Chat 手机端的附加形态，归并回基本档。
    """
    if "停用" in (department or ""):
        return "departed"
    s = (status or "").strip().lower()
    if s.endswith("_mobile"):
        s = s[: -len("_mobile")]
    return s if s in _VALID_KEYS else "offline"


def status_text_cn(key: str) -> str:
    """状态 key 的中文显示名。"""
    return _STATUS_CN.get(key, key)


def status_tooltip(key: str, status_text: str = "") -> str:
    """列表悬停提示：中文状态 + 自定义状态文本（如「在线 · 会议中」）。"""
    base = status_text_cn(key)
    if key == "departed" or not status_text:
        return base
    return f"{base} · {status_text}"


def status_label(key: str, status_text: str = "") -> str:
    """私聊标题栏旁的状态文字：有自定义状态优先显示自定义。"""
    if key != "departed" and status_text:
        return status_text
    return status_text_cn(key)


def normalize_info(info: Optional[dict]) -> Optional[dict]:
    """给 users.info 结果补一个归一化后的 key 字段。"""
    if not info:
        return None
    info = dict(info)
    info["status_key"] = normalize_status_key(
        info.get("status", ""), info.get("department", ""),
        info.get("active"))
    return info


class UserStatusProvider(QObject):
    """username → 用户资料（含状态）的按需拉取缓存。

    - 内存缓存之上再有一层磁盘缓存（core/status_cache）：登录注入
      session 时水合，过期条目在 get() 里先同步回调给界面显示
      （微信式先旧后新），随后照常拉新再回调一次。
    - 新鲜缓存命中时 get() 同步回调，否则经 run_async 异步拉取
      （回调已在主线程）。
    - 同一用户在途请求合并（pending 表），防止列表里同名行重复打请求。
    - 并发上限 _MAX_INFLIGHT 的 FIFO 队列。
    - 拉取失败进负缓存，避免界面反复触发重试雪崩。
    """

    # 成功拉取后防抖落盘的延时（毫秒）：批量首拉会连发几十个请求，
    # 逐条同步写盘没意义
    _SAVE_DEBOUNCE_MS = 2000

    status_pushed = pyqtSignal(str, str)  # username, status_key（实时推送落缓存后发）

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._client = None
        self._async_parent: Optional[QObject] = None
        self._cache = {}          # username -> (info, monotonic)
        self._failed_until = {}   # username -> monotonic
        self._pending = {}        # username -> [callback]
        self._queue: List[str] = []
        self._inflight = 0
        self._server = ""
        self._account = ""
        self._disk = {}           # username -> status_cache 落盘条目
        self._save_pending = False
        # PyQt5 的 singleShot 没有 (ms, QObject, fn) 上下文重载，
        # 用挂在本对象下的单发定时器：provider 销毁时定时器一并销毁
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self.flush)

    # ------------------------------------------------------------------ #
    def set_session(self, client, async_parent: Optional[QObject] = None):
        """登录成功后注入 client；先落盘上一账号的脏数据，再载入本账号
        上次保存的状态缓存（条目按磁盘时间换算年龄，多半已过期——
        界面先显示旧状态，随后批量拉新覆盖）。"""
        self.flush()
        self.clear()
        self._client = client
        self._async_parent = async_parent
        self._server = getattr(client, "server_url", "")
        me = getattr(client, "me", None)
        self._account = (getattr(me, "username", "") or "") if me else ""
        self._hydrate()

    def preload(self, server_url: str, account: str):
        """登录前预载磁盘缓存（无 client）：让自动登录路径的快照渲染
        立即显示上次的状态点，不等登录+网络往返。随后 set_session 会
        清空重水合，不冲突。"""
        self.flush()
        self.clear()
        self._server = server_url or ""
        self._account = account or ""
        self._hydrate()

    def _hydrate(self):
        now_monotonic = time.monotonic()
        now_epoch = time.time()
        hydrated = status_cache.load_status_cache(self._server, self._account)
        for uname, (item, ts) in hydrated.items():
            age = max(0.0, now_epoch - ts) if ts else 1e9
            info = dict(item)
            self._cache[uname] = (info, now_monotonic - age)
            self._disk[uname] = info
        # 水合失败（条数 0）时界面拿不到任何旧状态点，排障必须可见
        log.info("状态缓存水合 %d 条（account=%s）", len(hydrated),
                 self._account)

    def flush(self):
        """有未落盘的修改时立即写盘（换账号 / 登出前调用）。"""
        if self._save_pending:
            self._save_pending = False
            status_cache.save_status_cache(self._server, self._account,
                                           self._disk)

    def clear(self):
        self.flush()
        self._cache.clear()
        self._failed_until.clear()
        self._pending.clear()
        self._queue.clear()
        self._inflight = 0
        self._disk.clear()

    def invalidate(self, username: str):
        """外部已知数据变化（如收到该用户状态变更）时强制下次重拉。"""
        self._cache.pop(username, None)
        self._failed_until.pop(username, None)

    def apply_push(self, username: str, raw_status: str) -> Optional[dict]:
        """实时推送（stream-notify-user user-status）落缓存。

        与已有资料合并（保留 department/name 等，离职判定才能在推送后
        不丢），没有旧数据就建最小条目；返回合并后的 info 供调用方直接
        刷新界面。同时清负缓存并防抖落盘。合并后的字段过滤进 _record
        的白名单，磁盘只存 _FIELDS 内的字段。
        """
        if not username:
            return None
        ent = self._cache.get(username)
        old = dict(ent[0]) if ent else dict(self._disk.get(username) or {})
        old = {k: v for k, v in old.items()
               if v and not k.startswith("_") and k != "status_key"}
        info = {"username": username, **old}
        info["status"] = raw_status or ""
        info["status_key"] = normalize_status_key(
            raw_status, info.get("department", ""), info.get("active"))
        self._cache[username] = (info, time.monotonic())
        self._failed_until.pop(username, None)
        self._record(username, info)
        self.status_pushed.emit(username, info["status_key"])
        return info

    def peek(self, username: str) -> Optional[dict]:
        """只读缓存，不发起请求；无新鲜数据返回 None。"""
        if not username:
            return None
        ent = self._cache.get(username)
        if ent and time.monotonic() - ent[1] < _TTL:
            return ent[0]
        return None

    def get(self, username: str, callback: Callable,
            force: bool = False) -> Optional[dict]:
        """取用户资料（含归一化 status_key）。

        新鲜缓存同步回调；过期缓存也先同步回调（先旧后新），随后照常
        拉取、拿到新数据后再回调一次。返回值：新鲜缓存命中时为 info，
        否则 None。
        """
        if not username:
            callback(None)
            return None
        now = time.monotonic()
        if not force:
            ent = self._cache.get(username)
            if ent is not None:
                if now - ent[1] < _TTL:
                    callback(ent[0])
                    return ent[0]
                # 过期缓存先给界面显示，不等网络
                callback(ent[0])
                # 无 client（登录前预载）拉不了新，旧值就够用；
                # 负缓存窗口内也不重拉（上层重试机制兜底）
                if (not self._client
                        or now < self._failed_until.get(username, 0.0)):
                    return None
            elif not self._client:
                callback(None)
                return None
            elif now < self._failed_until.get(username, 0.0):
                callback(None)
                return None
        cbs = self._pending.get(username)
        if cbs is not None:
            cbs.append(callback)
            return None
        self._pending[username] = [callback]
        self._queue.append(username)
        self._pump()
        return None

    # ------------------------------------------------------------------ #
    def _pump(self):
        while self._queue and self._inflight < _MAX_INFLIGHT:
            username = self._queue.pop(0)
            if not self._pending.get(username):
                continue
            self._inflight += 1
            # parent 绝不能为 None：run_async 的返回值没人持有，无 parent
            # 的 _AsyncTask 会在回调前被 GC，QThread 随之销毁直接原生崩溃
            # （offscreen 复现 exit 127 实测）
            run_async(
                lambda u=username: self._client.fetch_user_info(u),
                on_done=lambda info, u=username: self._on_fetched(u, info),
                on_fail=lambda _err, u=username: self._on_fetched(u, None),
                parent=self._async_parent or self)

    def _on_fetched(self, username: str, info: Optional[dict]):
        self._inflight -= 1
        cbs = self._pending.pop(username, [])
        norm = normalize_info(info)
        if norm:
            self._cache[username] = (norm, time.monotonic())
            self._record(username, norm)
            log.debug("用户状态拉取成功：%s -> %s", username,
                      norm.get("status_key"))
        else:
            self._failed_until[username] = time.monotonic() + _NEG_TTL
            # 失败只进负缓存、界面不重试的话，行上的状态点会一直缺着，
            # 排障时日志里必须能看到这一步（只打用户名，无敏感数据）
            log.warning("用户状态拉取失败（%.0f 秒内不重试）：%s",
                        _NEG_TTL, username)
        for cb in cbs:
            try:
                cb(norm)
            except Exception:  # noqa: BLE001
                log.exception("用户状态回调异常（%s）", username)
        self._pump()

    def _record(self, username: str, info: dict):
        """更新落盘条目并防抖写盘；字段白名单见 core/status_cache。"""
        entry = {k: info.get(k)
                 for k in status_cache._FIELDS if k in info}
        entry["_ts"] = int(time.time())
        self._disk[username] = entry
        if not self._save_pending:
            self._save_pending = True
            self._save_timer.start(self._SAVE_DEBOUNCE_MS)
