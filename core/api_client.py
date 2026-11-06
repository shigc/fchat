"""REST 数据层：封装 rocketchat_API 库。

职责：登录、拉取会话列表、历史消息、发送文本/文件、头像 URL 等所有
同步 HTTP 操作。实时推送由 realtime.py（DDP/WebSocket）负责。

对外主要异常：
    ApiAuthError    认证失败（用户名/密码/服务器错误）
    ApiConnectionError  网络/连接失败
    ApiError        其他业务错误
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import time as _time
import uuid
import requests
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import List, Optional

from rocketchat_API.rocketchat import RocketChat
from rocketchat_API.APIExceptions.RocketExceptions import (
    RocketAuthenticationException,
    RocketBadStatusCodeException,
    RocketConnectionException,
    RocketApiException,
)

from .models import (
    Conversation,
    DirectoryEntry,
    Message,
    User,
    json_ts_to_epoch,
)
from .messages import parse_message, preview_with_sender
from .e2e import E2EManager
from .ssl_prewarm import ConcurrencyAdapter

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[_a-z0-9-]+(\.[_a-z0-9-]+)*@[a-z0-9-]+(\.[a-z0-9-]+)*(\.[a-z]{2,4})$", re.I)

# 房间元数据一次拉取的条数上限（只为补显示名，不做全量同步）
_ROOM_PAGE = 100
# topic 一般是长描述，短到这个长度以内才适合当会话名
_TOPIC_AS_NAME_MAX = 20

# REST 超时分级（(connect, read)，requests 支持元组、rocketchat_API 原样
# 透传给每次请求）。库默认是单值 30 秒：实测这台机器到服务器的冷连接会抖
# （同一脚本两次 login 分别 28.1 秒和 1.8 秒），点开私聊时 30 秒白屏对用户
# 等于「程序死了」，还会吃掉「加载失败 + 重试」的反馈窗口。交互类调用
# 宁可快速失败让 UI 报错，也不要长时间静默。
_INTERACTIVE_TIMEOUT = (5, 20)
# 上传（rooms.media 传文件）单独放宽：read 里花在真实传输上的时间不是网络
# 抖动，20 秒会误杀正常的大文件上传。
_UPLOAD_TIMEOUT = (10, 120)

# requests 默认连接池只有 10 条，而启动瞬间历史/成员/回执/用户状态会多路
# 并发；真机日志出现 "Connection pool is full ... pool size: 10"，
# 排队建连把请求拖到了 20s 读超时。32 远高于实测峰值并发。
_SESSION_POOL_SIZE = 32


def _tune_rest_session(session) -> None:
    """把库自建的 requests.Session 连接池调到启动并发之上，并挂并发上限。

    并发上限用 `core.ssl_prewarm.ConcurrencyAdapter`：打包版崩溃定位时发现
    启动瞬间多线程同时做 HTTPS、OpenSSL 并发加载 CA 证书会触发 access
    violation（见 core/ssl_prewarm.py docstring），这里把在途请求数收紧，
    不给证书加载压到临界点。
    """
    adapter = ConcurrencyAdapter(pool_connections=_SESSION_POOL_SIZE,
                                 pool_maxsize=_SESSION_POOL_SIZE)
    session.mount("http://", adapter)
    session.mount("https://", adapter)


class ApiError(Exception):
    pass


class ApiAuthError(ApiError):
    pass


class ApiConnectionError(ApiError):
    pass


class ReceiptsUnsupportedError(ApiError):
    """服务器明确拒绝已读回执：端点不存在或功能未开启。

    与网络抖动（5xx/连接失败，抛普通 ApiError）区分开，调用方据此
    决定是否进入降级模式（本会话内不再尝试拉取）。"""


class UploadCancelled(Exception):
    """用户在上传途中取消（流式 body 读到取消标志时抛出）。

    与 ApiError 分开：取消不是失败，UI 据此静默关窗、不弹错误提示。"""


# 流式上传时每次从文件读取的块大小；够大以减少 read 次数，够小以让进度
# 回调及时（urllib3 实际按 ~16KB 块 read，这里再切细只为进度粒度）。
_UPLOAD_CHUNK = 64 * 1024


class _MultipartBody:
    """把 multipart/form-data 的「前缀 + 文件 + 后缀」拼成可增量 read 的流。

    requests 对带 ``.len`` 属性的 file-like body 会自动设 Content-Length 并
    流式发送（urllib3 按块 read），因此进度回调≈真实上传进度，且不走
    chunked（反代可能不接受）。线格式与 requests 用 ``files=`` 时逐字节一致：
    中文文件名按原始 UTF-8 放进 ``filename="..."``（实测 requests/urllib3
    同款，非 RFC 2231）。

    read() 里检查取消标志，置位即抛 UploadCancelled——异常会干净地穿出
    requests.post（实测不被 urllib3 包裹），调用方据此中止上传。
    """

    def __init__(self, fileobj, prefix: bytes, suffix: bytes, file_total: int,
                 progress_cb=None, cancel=None):
        self._f = fileobj
        self._prefix = prefix
        self._suffix = suffix
        self._file_total = file_total
        self._cb = progress_cb
        self._cancel = cancel
        self.len = len(prefix) + file_total + len(suffix)
        self._stage = 0          # 0=前缀 1=文件 2=后缀 3=结束
        self._off = 0            # 当前段内偏移
        self._sent = 0           # 已读出的文件字节（用于进度）
        self._produced = 0       # 已交出的总字节（用于 read(-1) 兜底）

    def read(self, amt: int = -1) -> bytes:
        if amt is None or amt < 0:
            amt = self.len - self._produced
        out = bytearray()
        while len(out) < amt and self._stage < 3:
            need = amt - len(out)
            if self._stage == 0:
                chunk = self._prefix[self._off:self._off + need]
                self._off += len(chunk)
                if self._off >= len(self._prefix):
                    self._stage, self._off = 1, 0
            elif self._stage == 1:
                if self._cancel is not None and self._cancel.is_set():
                    raise UploadCancelled("已取消")
                chunk = self._f.read(min(need, _UPLOAD_CHUNK))
                if not chunk:
                    self._stage, self._off = 2, 0
                    continue
                self._sent += len(chunk)
                if self._cb is not None:
                    self._cb(self._sent, self._file_total)
            else:
                chunk = self._suffix[self._off:self._off + need]
                self._off += len(chunk)
                if self._off >= len(self._suffix):
                    self._stage = 3
            out += chunk
        self._produced += len(out)
        return bytes(out)


class ApiClient:
    def __init__(self, server_url: str, ssl_verify: bool = True,
                 upload_service_url: str = "", upload_service_token: str = ""):
        self.server_url = server_url.rstrip("/")
        self.ssl_verify = ssl_verify
        # 图文混排的图片上传服务：配置了则经该服务拿内联 URL；空串表示未配置，
        # 回落为 rooms.media 上传 + markdown 文件链接。token 来自该服务端
        # 静态配置的 IMAGE_TOKEN，经 Authorization: Bearer <token> 传递。
        self.upload_service_url = (upload_service_url or "").strip()
        self.upload_service_token = (upload_service_token or "").strip()
        self._rc: Optional[RocketChat] = None
        self.user_id: str = ""
        self.auth_token: str = ""
        self.me: Optional[User] = None
        self.e2e: Optional[E2EManager] = None
        # 用户名称缓存：username → 中文名（显示名）
        self._user_names: dict = {}

    # ------------------------------------------------------------------ #
    # 连接与登录
    # ------------------------------------------------------------------ #
    @property
    def connected(self) -> bool:
        return self._rc is not None and bool(self.auth_token)

    def fetch_department(self, username: str) -> str:
        """取部门信息。me() 不返回部门字段，只有 users_info 的
        user.department 有；失败返回空，不阻断登录。

        不在 login() 里串行调用：部门只用于账号页展示，登录关键路径
        每省一拍是一拍（调用方登录成功后异步补齐）。
        """
        if not self._rc or not username:
            return ""
        try:
            info = self._rc.users_info(username=username) or {}
            return info.get("user", {}).get("department", "") or ""
        except Exception as e:  # noqa: BLE001
            log.debug("取部门信息失败（非致命）：%s", e)
            return ""

    def login(self, username: str, password: str) -> User:
        """登录服务器。成功返回当前登录用户信息，失败抛 ApiAuthError / ApiConnectionError。"""
        try:
            self._rc = RocketChat(
                username,
                password,
                server_url=self.server_url,
                ssl_verify=self.ssl_verify,
                timeout=_INTERACTIVE_TIMEOUT,
            )
            _tune_rest_session(self._rc.session)
            self.auth_token = self._rc.headers.get("X-Auth-Token", "")
            self.user_id = self._rc.headers.get("X-User-Id", "")
            me = self._rc.me()
            data = me.get("data", {})
            uname = data.get("username", username)
            # avatar_url 必须在这里拼好：发送路径的乐观回显直接拿 self.me 当
            # sender，缺了它委托会画成首字占位且永不重试（本地 G 头像的根因）
            self.me = User(
                id=data.get("_id", self.user_id),
                username=uname,
                name=data.get("name", ""),
                avatar_url=f"{self.server_url}/avatar/{uname}" if uname else "",
                department="",
            )
            return self.me
        except RocketAuthenticationException:
            raise ApiAuthError("登录失败：用户名或密码错误")
        except RocketConnectionException:
            raise ApiConnectionError(
                f"无法连接到服务器：{self.server_url}，请检查地址与网络"
            )
        except Exception as e:  # noqa: BLE001
            raise ApiConnectionError(f"连接服务器失败：{e}")

    def login_with_token(self, user_id: str, auth_token: str) -> User:
        """用已保存的 token 直接恢复会话（用于 DDP 复用 / 断线重连）。"""
        try:
            self._rc = RocketChat(
                auth_token=auth_token,
                user_id=user_id,
                server_url=self.server_url,
                ssl_verify=self.ssl_verify,
                timeout=_INTERACTIVE_TIMEOUT,
            )
            _tune_rest_session(self._rc.session)
            self.user_id = user_id
            self.auth_token = auth_token
            me = self._rc.me()
            data = me.get("data", {})
            uname = data.get("username", "")
            self.me = User(id=data.get("_id", user_id),
                           username=uname,
                           name=data.get("name", ""),
                           avatar_url=(f"{self.server_url}/avatar/{uname}"
                                       if uname else ""),
                           department="")
            return self.me
        except Exception as e:  # noqa: BLE001
            raise ApiConnectionError(f"恢复会话失败：{e}")

    def create_e2e(self, e2e_password: str) -> bool:
        """构造 E2EManager 并挂到 self.e2e。纯本地、不联网。

        密钥初始化（init_keys，走 REST）由调用方在登录成功后异步执行：
        把它挡在登录关键路径上会白吃一次串行网络往返（实测 ~1.1s）。
        """
        if not e2e_password or not self.connected:
            return False
        self.e2e = E2EManager(
            self.server_url, self.user_id, self.auth_token,
            e2e_password, ssl_verify=self.ssl_verify)
        return True

    # ------------------------------------------------------------------ #
    # 登出与状态
    # ------------------------------------------------------------------ #
    def logout(self) -> None:
        """登出当前会话。失败只记日志，不抛异常（本地清理照常进行）。"""
        if not self._rc:
            self._rc = None
            return
        try:
            self._rc.logout()
        except Exception:  # noqa: BLE001
            log.debug("logout 调用失败（非致命）")
        finally:
            self._rc = None
            self.auth_token = ""
            self.user_id = ""
            self.me = None

    def set_status(self, status: str) -> None:
        """设置在线状态（online/away/busy/offline）。失败只记日志。"""
        if not self._rc or not status:
            return
        try:
            self._rc.users_set_status(status=status, message="")
        except Exception:  # noqa: BLE001
            log.debug("setStatus 调用失败（非致命）：%s", status)

    # ------------------------------------------------------------------ #
    # 会话列表
    # ------------------------------------------------------------------ #
    def get_conversations(self) -> List[Conversation]:
        """拉取当前用户订阅的所有会话，按最后消息时间倒序。

        subscriptions.get 与 rooms.get 在同一个主会话上串行发：热连接下
        各只要几十毫秒，串行总耗时反而低于"并行 + rooms 新建连接的 TLS
        握手"（经代理实测约 1.3s），也避免两个线程共用 Session 的安全问题。
        """
        if not self._rc:
            raise ApiError("尚未登录")
        try:
            _t0 = _time.perf_counter()
            _ts = _time.perf_counter()
            subs = self._rc.subscriptions_get().get("update", [])
            log.info("[计时] subscriptions.get 耗时 %.0f ms",
                     (_time.perf_counter() - _ts) * 1000)

            rooms_err: Optional[Exception] = None
            rooms: list = []
            try:
                _tr = _time.perf_counter()
                rooms = (self._rc.rooms_get() or {}).get("update", []) or []
                log.info("[计时] rooms.get 耗时 %.0f ms",
                         (_time.perf_counter() - _tr) * 1000)
            except Exception as e:  # noqa: BLE001
                rooms_err = e
            rooms_by_id = {r.get("_id"): r for r in rooms if r.get("_id")}
            log.info("[计时] subs %d 条 / rooms %d 条，串行耗时 %.0f ms",
                     len(subs), len(rooms_by_id),
                     (_time.perf_counter() - _t0) * 1000)

            if rooms_err:
                # 订阅列表里没有 lastMessage（真机实测 97 条全无），
                # 最后消息时间/预览只能从 rooms.get 的 lm / lastMessage 拿；
                # 拿不到就退回订阅顺序显示
                log.debug("rooms.get 失败，会话退回订阅顺序显示：%s", rooms_err)
            conversations = []
            for s in subs:
                conv = self._conversation_from_subscription(
                    s, rooms_by_id.get(s.get("rid", "") or ""))
                if conv:
                    conversations.append(conv)
            # 按最后消息时间倒序
            conversations.sort(key=lambda c: c.last_message_time, reverse=True)
            return conversations
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"拉取会话失败：{e}")

    def fetch_user_names(self, usernames: list) -> dict:
        """批量获取用户显示名（中文名），返回 {username: name} 并缓存。"""
        if not self._rc or not usernames:
            return {}
        result = {}
        for uname in usernames:
            if uname in self._user_names:
                result[uname] = self._user_names[uname]
                continue
            try:
                info = self._rc.users_info(username=uname)
                data = info.get("user", {}) or {}
                name = data.get("name", "") or ""
                if name:
                    self._user_names[uname] = name
                    result[uname] = name
            except Exception:
                pass
        return result

    def fetch_room_display_names(self) -> dict:
        """取频道/私有群在服务器上配置的显示名，返回 {room_id: 显示名}。

        订阅记录里频道只有 ASCII slug，中文名在房间的 fname 里；
        没配 fname 时退而用够短的 topic。
        """
        if not self._rc:
            return {}
        result = {}
        # 这两个方法被 @paginated 装饰成逐条产出房间的生成器，
        # 用 max_count 兜住总量，避免在大服务器上拉完整表
        for lister in (self._rc.channels_list_joined, self._rc.groups_list):
            try:
                rooms = list(lister(count=_ROOM_PAGE, max_count=_ROOM_PAGE))
            except Exception as e:  # noqa: BLE001
                log.debug("取房间显示名失败（非致命）：%s", e)
                continue
            for r in rooms:
                rid = r.get("_id", "")
                disp = self._room_display_name(r)
                if rid and disp:
                    result[rid] = disp
        return result

    @staticmethod
    def _room_display_name(r: dict) -> str:
        disp = (r.get("fname") or "").strip()
        if disp:
            return disp
        topic = (r.get("topic") or "").strip()
        # topic 通常是一句长描述，只有短到像名字的才拿来当显示名
        return topic if len(topic) <= _TOPIC_AS_NAME_MAX else ""

    def fetch_user_info(self, username: str) -> dict:
        """取单个用户资料（头像弹卡片用）。失败返回空 dict。"""
        if not self._rc or not username:
            return {}
        try:
            data = (self._rc.users_info(username=username) or {}).get("user") or {}
        except Exception as e:  # noqa: BLE001
            log.debug("取用户资料失败 %s：%s", username, e)
            return {}
        emails = data.get("emails") or []
        # 部门信息可能在 customFields 或顶层字段
        department = ""
        cf = data.get("customFields") or {}
        if isinstance(cf, dict):
            department = cf.get("department", "") or ""
        if not department:
            department = data.get("department", "") or ""
        info = {
            "id": data.get("_id", ""),
            "username": data.get("username", "") or username,
            "name": data.get("name", "") or "",
            "status": data.get("status", "") or "",
            "status_text": data.get("statusText", "") or "",
            # 停用账号标记；离职判定只认 department 含「停用」
            # （active=false 里混有临时/外包/公用账户，不都对应离职）
            "active": bool(data.get("active", True)),
            "roles": data.get("roles") or [],
            "email": emails[0].get("address", "") if emails else "",
            "department": department,
        }
        if info["name"]:
            self._user_names[info["username"]] = info["name"]
        return info

    def _conversation_from_subscription(self, s: dict,
                                        room: Optional[dict] = None) -> Optional[Conversation]:
        room_type = s.get("t", "c")
        # 频道这里给的是 ASCII slug（general…），私聊给的是对方 username
        slug = s.get("name", "") or ""
        fname = s.get("fname", "") or ""
        conv = Conversation(
            id=s.get("rid", "") or s.get("_id", ""),
            room_type=room_type,
            unread=int(s.get("unread", 0) or 0),
            # 收藏状态来自订阅的 f 字段（服务器同步）；rooms.get 项兜底
            is_favorite=bool(s.get("f") or (room and room.get("f"))),
            # 免打扰与 @ 计数：订阅字段优先，整表重拉时服务端值覆盖本地
            muted=bool(s.get("disableNotifications")
                       or (room and room.get("disableNotifications"))),
            mention_count=int(s.get("userMentions", 0) or 0),
            # 讨论串有父房间 id `prid`（订阅或房间记录上），据此标注
            is_discussion=bool(s.get("prid") or (room and room.get("prid"))),
            # 团队主房间带 teamMain/teamId（订阅记录也带，rooms.get 项兜底）。
            # 团队的 t 仍是 c/p，UI 需要它区分「频道/团队」标签。
            is_team=bool(s.get("teamMain") or (room and room.get("teamMain"))),
            # 加密态只在房间记录上有（订阅只有 E2EKey 字符串，判不了开关）
            encrypted=bool(room and room.get("encrypted")),
        )
        # 是否本人创建：房间元数据的 u 是创建者，与登录用户比对。
        # 自己创建的房间才能删除；别人创建的只能退出。
        if room:
            creator = room.get("u") or {}
            conv.i_created = bool(creator.get("_id")
                                  and creator["_id"] == self.user_id)
        # 中文显示名在 fname，必须优先于 slug
        if room_type == "d":
            conv.username = slug
            conv.name = fname or self._user_names.get(slug, "") or slug or "私聊"
            # 多人私聊（真机：t 仍是 d，房间 usernames 含自己、usersCount>2；
            # 订阅 name 是对方 username 逗号拼接）。rooms.get 失败时退回
            # 订阅 name 里的逗号判断
            members = (room or {}).get("usernames") or []
            count = int((room or {}).get("usersCount", 0) or 0)
            conv.is_multi_dm = len(members) > 2 or count > 2 or "," in slug
        else:
            conv.name = fname or slug or "未命名"
        # permalink 用的房间名：私聊 slug 即对方 username，频道/群是 ASCII slug
        conv.slug = slug
        # 头像 URL：DM 用对方 username；频道/群用房间 slug
        try:
            if room_type == "d":
                if conv.username:
                    conv.avatar_url = f"{self.server_url}/avatar/{conv.username}"
            elif slug:
                conv.avatar_url = f"{self.server_url}/avatar/{slug}"
        except Exception:  # noqa: BLE001
            pass
        # 最后一条消息：优先订阅自带 lastMessage，其次房间里的
        lm = s.get("lastMessage") or (room.get("lastMessage") if room else None) or {}
        # 时间取房间的 lm 字段（最后消息时刻）；空房间没有 lm，保持 0.0 沉底。
        # 不能用订阅自身的 ts —— 那是订阅创建时间，不是最后消息时间
        if room and room.get("lm"):
            conv.last_message_time = json_ts_to_epoch(room.get("lm"))
        elif lm:
            conv.last_message_time = json_ts_to_epoch(lm.get("ts"))
        if lm:
            conv.last_message = self._message_from_json(lm, conv.id)
            conv.last_message_text = preview_with_sender(
                conv.last_message,
                conv.room_type == "d" and not conv.is_multi_dm)
        return conv

    def mark_room_read(self, room_id: str) -> bool:
        """向服务端上报已读（subscriptions.read，传房间 rid）。

        不上报的话服务端未读计数永远不减，客户端重启拉会话列表时
        角标会原样回来。注意必须传 rid：传订阅 _id 会回
        error-invalid-subscription（实测）。
        """
        if not self._rc:
            raise ApiError("尚未登录")
        try:
            resp = self._rc.call_api_post("subscriptions.read",
                                          body={"rid": room_id})
            return bool(resp.get("success", False))
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"标记已读失败：{e}")

    def mark_room_unread(self, room_id: str) -> bool:
        """标记房间未读（subscriptions.unread，服务端同步）。
        非 stock 服务端若不支持该端点会抛 ApiError，由调用方降级为本地未读。
        """
        if not self._rc:
            raise ApiError("尚未登录")
        try:
            resp = self._rc.subscriptions_unread(room_id)
            return bool(resp.get("success", False))
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"标记未读失败：{e}")

    def set_room_favorite(self, room_id: str, favorite: bool) -> bool:
        """收藏/取消收藏房间（rooms.favorite，服务器同步）。

        走库的 rooms_favorite 包装，最终 POST /api/v1/rooms.favorite；
        失败按 ApiError 抛出，由调用方回滚乐观的本地状态。
        """
        if not self._rc:
            raise ApiError("尚未登录")
        try:
            resp = self._rc.rooms_favorite(room_id, favorite=favorite)
            return bool(resp.get("success", False))
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"设置收藏失败：{e}")

    def set_room_muted(self, room_id: str, muted: bool) -> bool:
        """房间免打扰（rooms.saveNotification）。

        只抑制桌面/移动推送，不影响未读计数；取消时回 default 而不是 all，
        避免覆盖用户账号级的通知偏好。非 stock 服务端若不支持该端点，
        由调用方回滚乐观状态并提示。
        """
        if not self._rc:
            raise ApiError("尚未登录")
        value = "nothing" if muted else "default"
        try:
            resp = self._rc.call_api_post(
                "rooms.saveNotification",
                body={
                    "roomId": room_id,
                    "notifications": {
                        "desktopNotifications": value,
                        "mobilePushNotifications": value,
                        "emailNotifications": "nothing",
                        "unreadAlert": "default",
                    },
                })
            return bool(resp.get("success", False))
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"设置免打扰失败：{e}")

    def set_room_e2e(self, room_id: str, encrypted: bool) -> bool:
        """开启/关闭房间端到端加密（rooms.saveRoomSettings）。

        真机实测：参数必须叫 `rid`（`roomId` 报
        error-invalid-room）；且仅私有群/私聊可加密，公开频道（t=c）
        服务端拒绝 error-action-not-allowed（调用方负责置灰）。
        失败按 ApiError 抛出，由调用方回滚乐观的本地状态。
        """
        if not self._rc:
            raise ApiError("尚未登录")
        try:
            resp = self._rc.call_api_post(
                "rooms.saveRoomSettings",
                body={"rid": room_id, "encrypted": bool(encrypted)})
            return bool(resp.get("success", False))
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"设置房间加密失败：{e}")

    def get_read_receipts(self, message_id: str) -> List[dict]:
        """取单条消息的已读回执：[{messageId, userId, ts}]。

        服务端是魔改版，此端点是否可用未知，靠异常分类原地探测：
        服务端 JSON 错误（success:false）或 404/405 视为「不支持」，
        5xx/连接失败视为瞬时抖动抛普通 ApiError，不能误降级。
        """
        if not self._rc:
            raise ApiError("尚未登录")
        try:
            resp = self._rc.call_api_get(
                "chat.getMessageReadReceipts", messageId=message_id)
        except RocketApiException as e:
            raise ReceiptsUnsupportedError(f"服务器拒绝已读回执：{e.error}")
        except RocketBadStatusCodeException as e:
            if e.status_code in (404, 405):
                raise ReceiptsUnsupportedError(
                    f"端点不存在（HTTP {e.status_code}）")
            raise ApiError(f"拉取已读回执失败：HTTP {e.status_code}")
        except RocketConnectionException as e:
            raise ApiConnectionError(f"连接失败：{e}")
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"拉取已读回执失败：{e}")
        if not isinstance(resp, dict) or not resp.get("success"):
            raise ReceiptsUnsupportedError("响应无 success 标记")
        return list(resp.get("receipts") or [])

    def fetch_room_members(self, room_id: str,
                           room_type: str = "c") -> List[dict]:
        """取房间成员列表 [{_id, username, name, ...}]（计算未读人数用）。

        "d" 走 im.members：多人和某人的私聊 type 仍是 "d" 但成员多于
        两人，未读数分母需要真实成员列表。沿用 get_history 的做法绕开
        @paginated 生成器直连原生端点；大房间 count=0 可能被截断，
        按 total 补拉一页。
        """
        if not self._rc:
            raise ApiError("尚未登录")
        endpoint = {"c": "channels.members", "p": "groups.members",
                    "d": "im.members"}.get(room_type)
        if not endpoint:
            raise ApiError(f"房间类型 {room_type} 无成员端点")
        try:
            data = self._rc.call_api_get(endpoint, roomId=room_id, count=0)
            members = list(data.get("members") or [])
            total = int(data.get("total") or 0)
            if total > len(members):
                data = self._rc.call_api_get(
                    endpoint, roomId=room_id, count=total)
                members = list(data.get("members") or [])
            return members
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"拉取群成员失败：{e}")

    # ------------------------------------------------------------------ #
    # 房间文件
    # ------------------------------------------------------------------ #
    def fetch_room_files(self, room_id: str, room_type: str = "c",
                         offset: int = 0, count: int = 50) -> dict:
        """分页拉取房间内的文件列表。

        Rocket.Chat 文件列表端点：channels.files / groups.files / dm.files。
        与 get_history 同款直连原生端点（绕开 @paginated 生成器），返回
        {"files": [...], "total": N}。每条 file 含 _id/name/size/type/url/
        userId/uploadedAt；url 相对地址已用 _absolute_link 补成绝对地址，
        供下载打开直接使用。失败抛 ApiError。
        """
        if not self._rc:
            raise ApiError("尚未登录")
        endpoint = {"c": "channels.files", "p": "groups.files",
                    "d": "dm.files"}.get(room_type)
        if not endpoint:
            raise ApiError(f"房间类型 {room_type} 无文件端点")
        try:
            data = self._rc.call_api_get(
                endpoint, roomId=room_id, count=count, offset=offset)
            files = list((data or {}).get("files") or [])
            for f in files:
                url = f.get("url") or ""
                if url and not url.startswith("http"):
                    f["url"] = self._absolute_link(url)
            return {"files": files, "total": int((data or {}).get("total") or 0)}
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"拉取房间文件失败：{e}")

    # ------------------------------------------------------------------ #
    # 历史消息
    # ------------------------------------------------------------------ #
    def get_history(self, room_id: str, count: int = 50, offset: int = 0,
                    room_type: str = "c") -> List[Message]:
        """分页拉取历史消息（从新到旧）。"""
        if not self._rc:
            raise ApiError("尚未登录")
        try:
            # 注意：channels/groups/dm_history 被 @paginated() 装饰为生成器，
            # 直接调用会拿到 generator 且会不按 count 自动翻页。这里用
            # call_api_get 直连原生端点，返回确定的一页 dict，保持分页语义。
            endpoint = {
                "d": "dm.history",
                "p": "groups.history",
                "c": "channels.history",
            }[room_type]
            data = self._rc.call_api_get(
                endpoint, roomId=room_id, count=count, offset=offset
            )
            raw = data.get("messages", [])
            # 历史消息是时间倒序（新的在前），这里保持原序返回，由 UI 决定展示顺序
            return [self._message_from_json(m, room_id) for m in raw]
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"拉取历史消息失败：{e}")

    def get_message(self, msg_id: str, room_id: str = "") -> Optional[Message]:
        """按 id 取单条消息（引用块补抓原消息用）。取不到返回 None。"""
        if not self._rc:
            raise ApiError("尚未登录")
        try:
            resp = self._rc.chat_get_message(msg_id=msg_id)
            msg = resp.get("message")
            if not msg:
                return None
            rid = msg.get("rid") or room_id
            return self._message_from_json(msg, rid)
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"获取消息详情失败：{e}")

    def search_messages(self, room_id: str, text: str, limit: int = 20) -> List[Message]:
        """在指定房间内搜索聊天消息。失败抛 ApiError。

        chat_search 被 @paginated 装饰成逐条产出的生成器，若不限制
        max_count 会在大房间里自动翻完整表，这里用 max_count 兜底一页。
        """
        if not self._rc:
            raise ApiError("尚未登录")
        try:
            msgs = list(self._rc.chat_search(room_id, text, max_count=limit))
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"搜索消息失败：{e}")
        return [self._message_from_json(m, room_id) for m in msgs]

    def fetch_history_around(self, room_id: str, ts: float, count: int = 30,
                             room_type: str = "c") -> List[Message]:
        """按消息时间戳拉"附近一页"历史（搜索结果定位跳转用）。

        用 history 端点的 latest 参数（ISO 8601 UTC 含毫秒）锚定目标
        时刻、多加 1 秒容差，使目标消息成为返回页里最新的一条；返回
        顺序与 get_history 一致（新→旧）。目标不在页内（时钟偏差/
        服务端对 latest 支持异常）由调用方走逐页翻页回退。
        """
        if not self._rc:
            raise ApiError("尚未登录")
        endpoint = {"d": "dm.history", "p": "groups.history",
                    "c": "channels.history"}.get(room_type)
        if not endpoint:
            raise ApiError(f"房间类型 {room_type} 无历史端点")
        dt = datetime.fromtimestamp(ts + 1.0, tz=timezone.utc)
        latest = dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
        try:
            data = self._rc.call_api_get(endpoint, roomId=room_id,
                                         count=count, latest=latest)
            raw = (data or {}).get("messages") or []
            return [self._message_from_json(m, room_id) for m in raw]
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"拉取附近历史失败：{e}")

    def fetch_messages_since(self, room_id: str, ts: float) -> List[Message]:
        """拉时间戳之后的新消息（时间正序；锚定窗口向下续载用）。

        history 端点固定按时间倒序返回，带 oldest 也只给区间里最新的一页
        （真机实测），没法正向翻页；chat.syncMessages 按 _updatedAt 升序
        返回游标之后的全部更新，正好补这个方向。返回值也含对更早消息的
        编辑（ts 早于游标），这里只保留真正的更新消息。
        """
        if not self._rc:
            raise ApiError("尚未登录")
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        last_update = (dt.strftime("%Y-%m-%dT%H:%M:%S.")
                       + f"{dt.microsecond // 1000:03d}Z")
        try:
            data = self._rc.call_api_get("chat.syncMessages",
                                         roomId=room_id,
                                         lastUpdate=last_update)
            updated = ((data or {}).get("result") or {}).get("updated") or []
            msgs = [self._message_from_json(m, room_id) for m in updated]
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"拉取更新消息失败：{e}")
        newer = [m for m in msgs if m.timestamp > ts]
        newer.sort(key=lambda m: m.timestamp)
        return newer

    # ------------------------------------------------------------------ #
    # 发送
    # ------------------------------------------------------------------ #
    def _absolute_link(self, link: str) -> str:
        """把相对 permalink 补成绝对 URL（服务端识别引用原文需要完整地址）。

        link 已是 http(s):// 开头时原样返回；否则用 self.server_url 拼接。
        兼容 server_url 本身带子路径（如 https://host/chat）的情况。
        """
        if link.startswith("http://") or link.startswith("https://"):
            return link
        base = self.server_url.rstrip("/")
        if not link.startswith("/"):
            link = "/" + link
        return base + link

    def send_text(self, room_id: str, text: str,
                  quote_sender: str = "", quote_text: str = "",
                  quote_msg_id: str = "", quote_link: str = "") -> Optional[Message]:
        """发送文本消息。可选带引用回复。

        引用回复走网页版规范：正文首行放一个空标签的绝对 permalink
        ``[ ](链接)``，换行后接回复内容，由**服务器**据此把原消息展开成引用
        附件（自动补作者/头像/原文/时间）。手工往 attachments 塞引用卡片
        服务器不认——不会持久化成引用，网页版与本应用重载后都会退化成普通
        消息（真机实测）。拿不到 permalink（缺 quote_link）时才退回附件卡片，
        至少本应用当前会话能显示引用块。
        """
        if not self._rc:
            raise ApiError("尚未登录")
        try:
            attachments = None
            body_text = text
            if quote_link:
                # 服务端要求 permalink 是绝对 URL，相对地址不认；用登录地址补全
                body_text = f"[ ]({self._absolute_link(quote_link)})\n{text}"
            elif quote_msg_id or quote_sender:
                attachments = [{
                    "author_name": quote_sender,
                    "text": quote_text or text,
                }]
            resp = self._rc.chat_post_message(
                body_text, room_id=room_id, attachments=attachments)
            msg = resp.get("message", {})
            return self._message_from_json(msg, room_id)
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"发送消息失败：{e}")

    def forward_message(self, room_id: str, text: str,
                        attachments=None) -> Optional[Message]:
        """转发消息到目标会话。附件沿用原始地址，不做文件重传。"""
        if not self._rc:
            raise ApiError("尚未登录")
        try:
            resp = self._rc.chat_post_message(
                text, room_id=room_id,
                attachments=list(attachments) if attachments else None)
            msg = resp.get("message", {})
            return self._message_from_json(msg, room_id)
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"转发消息失败：{e}")

    def delete_message(self, room_id: str, msg_id: str) -> bool:
        """撤回（删除）消息。调用 chat.delete。"""
        if not self._rc:
            raise ApiError("尚未登录")
        try:
            resp = self._rc.call_api_post("chat.delete", body={
                "roomId": room_id, "msgId": msg_id,
            })
            return resp.get("success", False)
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"撤回消息失败：{e}")

    def react_message(self, message_id: str, emoji_key: str,
                      should_react: bool = True) -> bool:
        """添加/移除表情回应（chat.react）。

        emoji_key 用服务端的短码形式（如 :thumbsup:），与网页版一致；
        失败抛 ApiError，由 UI 回滚本地 reactions。
        """
        if not self._rc:
            raise ApiError("尚未登录")
        try:
            resp = self._rc.call_api_post(
                "chat.react",
                body={"messageId": message_id, "emoji": emoji_key,
                      "shouldReact": bool(should_react)})
            if not resp.get("success", False):
                raise ApiError("服务器拒绝表情回应")
            return True
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"表情回应失败：{e}")

    def update_message(self, room_id: str, msg_id: str,
                       text: str) -> Optional[Message]:
        """编辑消息正文（chat.update）；返回服务端生成的新消息（可能为空）。"""
        if not self._rc:
            raise ApiError("尚未登录")
        try:
            resp = self._rc.call_api_post(
                "chat.update",
                body={"roomId": room_id, "msgId": msg_id, "text": text})
            if not resp.get("success", False):
                raise ApiError("服务器拒绝编辑消息")
            raw = resp.get("message")
            if isinstance(raw, dict):
                return self._message_from_json(raw, room_id)
            return None
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"编辑消息失败：{e}")

    def pin_message(self, msg_id: str, pinned: bool) -> bool:
        """置顶/取消置顶（chat.pinMessage / chat.unPinMessage）。"""
        if not self._rc:
            raise ApiError("尚未登录")
        method = "chat.pinMessage" if pinned else "chat.unPinMessage"
        try:
            resp = self._rc.call_api_post(method, body={"messageId": msg_id})
            if not resp.get("success", False):
                raise ApiError("服务器拒绝置顶操作")
            return True
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"置顶消息失败：{e}")

    def star_message(self, msg_id: str, starred: bool) -> bool:
        """星标/取消星标（chat.starMessage / chat.unStarMessage）。"""
        if not self._rc:
            raise ApiError("尚未登录")
        method = "chat.starMessage" if starred else "chat.unStarMessage"
        try:
            resp = self._rc.call_api_post(method, body={"messageId": msg_id})
            if not resp.get("success", False):
                raise ApiError("服务器拒绝星标操作")
            return True
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"星标消息失败：{e}")

    def get_pinned_messages(self, room_id: str,
                            count: int = 50) -> List[Message]:
        """房间置顶消息（最多 count 条，避免库分页生成器无上限翻页）。"""
        return self._get_messages_endpoint(
            "chat.getPinnedMessages", room_id, count)

    def get_starred_messages(self, room_id: str,
                             count: int = 50) -> List[Message]:
        """当前用户在该房间的星标消息（最多 count 条）。"""
        return self._get_messages_endpoint(
            "chat.getStarredMessages", room_id, count)

    def _get_messages_endpoint(self, method: str, room_id: str,
                               count: int) -> List[Message]:
        if not self._rc:
            raise ApiError("尚未登录")
        try:
            resp = self._rc.call_api_get(
                method, roomId=room_id, count=int(count), offset=0)
            if not resp.get("success", False):
                raise ApiError("服务器拒绝消息列表请求")
            return [self._message_from_json(m, room_id)
                    for m in (resp.get("messages") or [])
                    if isinstance(m, dict)]
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"拉取消息列表失败：{e}")

    def leave_room(self, room_id: str, room_type: str = "c",
                   is_team: bool = False) -> bool:
        """退出/离开房间。仅 c/p 有退出语义，d 私聊走 delete_room 真删。

        团队主房间（teamMain）必须走 teams.leave：channels/groups.leave
        对它报 error-room-not-found（实测），而 teams.leave 只收
        teamId/teamName，room_id 是 roomId，先 rooms.info 换 teamId。
        """
        if not self._rc:
            raise ApiError("尚未登录")
        if is_team:
            try:
                rinfo = self._rc.call_api_get("rooms.info", roomId=room_id)
                tid = ((rinfo.get("room") or {}).get("teamId") or "")
                if not tid:
                    raise ApiError("取团队 id 失败")
                resp = self._rc.call_api_post("teams.leave",
                                              body={"teamId": tid})
                return resp.get("success", False)
            except ApiError:
                raise
            except Exception as e:  # noqa: BLE001
                raise ApiError(f"退出团队失败：{e}")
        endpoint = {
            "c": "channels.leave",
            "p": "groups.leave",
        }.get(room_type)
        if not endpoint:
            raise ApiError(f"不支持的房间类型：{room_type}")
        try:
            resp = self._rc.call_api_post(endpoint, body={
                "roomId": room_id,
            })
            return resp.get("success", False)
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"退出房间失败：{e}")

    def add_room_member(self, room_id: str, username: str,
                        room_type: str = "c") -> bool:
        """把用户加进 c/p 房间：users_info 取 userId → channels/groups.invite。

        d 私聊无"加成员"语义，调用方（UI）不应传 "d"。服务端业务错误
        （如 error-user-already-in-room）随 RocketApiException 进异常串，
        UI 原样透出。
        """
        if not self._rc:
            raise ApiError("尚未登录")
        endpoint = {"c": "channels.invite", "p": "groups.invite"}.get(room_type)
        if not endpoint:
            raise ApiError(f"房间类型 {room_type} 不支持添加成员")
        try:
            info = self._rc.users_info(username=username) or {}
            user_id = (info.get("user") or {}).get("_id", "")
            if not user_id:
                raise ApiError(f"用户不存在：{username}")
            resp = self._rc.call_api_post(endpoint, body={
                "roomId": room_id, "userId": user_id,
            })
            return resp.get("success", False)
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"添加成员失败：{e}")

    def delete_room(self, room_id: str, room_type: str = "c",
                    is_team: bool = False) -> bool:
        """删除房间（c/p 仅限本人创建；d 私聊也真删，双方记录一并清空）。

        DM 用 rooms.delete（需要服务端 delete-room 权限，普通用户无权限时
        服务端报错并透出到 UI）；im.close 只是把会话藏起来，对方再来消息
        又会重开，达不到「真删」效果。团队主房间走 teams.delete——
        channels/groups.delete 会报 error-cannot-delete-team-channel
        （实测），且参数是 teamId/teamName 二选一。
        """
        if not self._rc:
            raise ApiError("尚未登录")
        if is_team:
            try:
                rinfo = self._rc.call_api_get("rooms.info", roomId=room_id)
                tid = ((rinfo.get("room") or {}).get("teamId") or "")
                if not tid:
                    raise ApiError("取团队 id 失败")
                resp = self._rc.call_api_post("teams.delete",
                                              body={"teamId": tid})
                return resp.get("success", False)
            except ApiError:
                raise
            except Exception as e:  # noqa: BLE001
                raise ApiError(f"删除团队失败：{e}")
        endpoint = {
            "c": "channels.delete",
            "p": "groups.delete",
            "d": "rooms.delete",
        }.get(room_type)
        if not endpoint:
            raise ApiError(f"不支持的房间类型：{room_type}")
        try:
            resp = self._rc.call_api_post(endpoint, body={
                "roomId": room_id,
            })
            return resp.get("success", False)
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"删除房间失败：{e}")

    def create_dm(self, username: str) -> str:
        """创建或查找与指定用户的 DM 房间，返回 room_id。"""
        if not self._rc:
            raise ApiError("尚未登录")
        try:
            resp = self._rc.call_api_post("im.create", body={
                "username": username,
            })
            room = resp.get("room") or {}
            return room.get("_id", "")
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"创建私聊失败：{e}")

    def search_users(self, query: str, limit: int = 15):
        """按关键字搜索用户（REST spotlight，服务端同时匹配用户名和姓名）。

        返回 User 列表；按用户名去重（大小写不敏感），避免同一用户因匹配到
        用户名与姓名等多个字段、或被服务端重复返回而出现两条相同记录。
        """
        if not self._rc or not query.strip():
            return []
        try:
            resp = self._rc.spotlight(query=query)
            users = resp.get("users") or []
            out = []
            seen = set()
            for u in users:
                username = u.get("username", "")
                key = username.casefold()
                if not username or key in seen:
                    continue
                seen.add(key)
                out.append(User(
                    id=u.get("_id", ""),
                    username=username,
                    name=u.get("name", ""),
                    avatar_url=u.get("avatarUrl", "") or u.get("avatar_url", ""),
                ))
                if len(out) >= limit:
                    break
            return out
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"搜索用户失败：{e}")

    # ------------------------------------------------------------------ #
    # 目录（频道/用户/团队列表）
    # ------------------------------------------------------------------ #
    def _directory_channel_entry(self, raw: dict, kind: str) -> Optional[DirectoryEntry]:
        """channels.list / groups.list 单条 → DirectoryEntry。

        lm/fname 可能缺失：last_ts=0.0（UI 显"—"）、显示名回落 slug。
        带 teamMain 的是团队主房间，由团队页签负责展示，这里返回 None
        去重，避免同一房间在两个页签重复出现。
        """
        if raw.get("teamMain"):
            return None
        ts = raw.get("ts")
        lm = raw.get("lm")
        return DirectoryEntry(
            kind=kind,
            rid=raw.get("_id", ""),
            name=raw.get("fname") or raw.get("name") or "",
            users_count=int(raw.get("usersCount") or 0),
            created_ts=json_ts_to_epoch(ts) if ts else 0.0,
            last_ts=json_ts_to_epoch(lm) if lm else 0.0,
            room_type="p" if kind == "group" else "c",
            is_private=(kind == "group"),
        )

    def fetch_directory_channels(self, offset: int = 0, count: int = 50) -> dict:
        """分页拉取权限可见的公开频道，返回 {"entries": [...], "total": N}。"""
        return self._fetch_directory_page("channels.list", "channels",
                                          "channel", offset, count)

    def fetch_directory_groups(self, offset: int = 0, count: int = 50) -> dict:
        """分页拉取自己已加入的私有群组（groups.list 只返回已加入的）。"""
        return self._fetch_directory_page("groups.list", "groups",
                                          "group", offset, count)

    def _fetch_directory_page(self, endpoint: str, key: str, kind: str,
                              offset: int, count: int) -> dict:
        if not self._rc:
            raise ApiError("尚未登录")
        try:
            # list 端点被 @paginated 装饰成生成器，call_api_get 直连拿确定的一页
            data = self._rc.call_api_get(endpoint, count=count, offset=offset)
            raw = list((data or {}).get(key) or [])
            entries = [e for e in
                       (self._directory_channel_entry(r, kind) for r in raw)
                       if e is not None]
            return {"entries": entries,
                    "total": int((data or {}).get("total") or 0)}
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"拉取目录失败：{e}")

    def _directory_user_entry(self, u: dict) -> DirectoryEntry:
        """users.list 单条 → DirectoryEntry（部门与 fetch_user_info 同源）。"""
        cf = u.get("customFields") or {}
        department = (cf.get("department", "") or ""
                      if isinstance(cf, dict) else "")
        if not department:
            department = u.get("department", "") or ""
        created = u.get("createdAt")
        return DirectoryEntry(
            kind="user",
            rid="",
            name=u.get("name", "") or u.get("username", "") or "",
            username=u.get("username", "") or "",
            department=department,
            status=u.get("status", "") or "",
            created_ts=json_ts_to_epoch(created) if created else 0.0,
        )

    def fetch_directory_users(self, offset: int = 0, count: int = 50) -> dict:
        """分页拉取用户列表（users.list）。部门与 fetch_user_info 同源。"""
        if not self._rc:
            raise ApiError("尚未登录")
        try:
            data = self._rc.call_api_get("users.list", count=count, offset=offset)
            raw = list((data or {}).get("users") or [])
            entries = []
            for u in raw:
                if u.get("_id", "") == self.user_id:
                    continue
                entries.append(self._directory_user_entry(u))
            return {"entries": entries,
                    "total": int((data or {}).get("total") or 0)}
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"拉取用户列表失败：{e}")

    def _directory_call(self, typ: str, offset: int, count: int,
                        text: str = "") -> tuple:
        """directory 端点统一入口，返回 (result 列表, total)。

        text/type/workspace 必须是**平级 URL 参数**（真机抓包网页目录页
        同款）；塞进一个 JSON `query` 参数会被本服务器整体忽略（四种
        text/type 组合返回同一份房间列表）。响应可能被 {"data": {...}}
        包裹，双形态兜底。
        """
        data = self._rc.call_api_get(
            "directory", count=count, offset=offset, text=text, type=typ,
            workspace="local", sort=json.dumps({"name": 1})) or {}
        body = data.get("data") if isinstance(data.get("data"), dict) else data
        return list(body.get("result") or []), int(body.get("total") or 0)

    def search_directory_users(self, query: str, count: int = 100) -> dict:
        """服务端搜索用户：directory type=users（网页目录页同款端点，
        实测毫秒级、支持中文、可分页），字段带 department 和 createdAt，
        比 spotlight 全。

        不能用 users.list+query：真机实测本服务器忽略 query 参数
        （4.4 万用户原样返回首页），Mongo 正则下推无效。
        """
        if not self._rc:
            raise ApiError("尚未登录")
        q = (query or "").strip()
        if not q:
            return {"entries": [], "total": 0}
        try:
            raw, total = self._directory_call("users", 0, count, text=q)
            entries = []
            for u in raw:
                if u.get("_id", "") == self.user_id:
                    continue
                created = u.get("createdAt")
                entries.append(DirectoryEntry(
                    kind="user", rid="",
                    name=u.get("name") or "",
                    username=u.get("username") or "",
                    department=u.get("department") or "",
                    status=u.get("status") or "",
                    created_ts=json_ts_to_epoch(created) if created else 0.0,
                ))
            return {"entries": entries, "total": total}
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"搜索用户失败：{e}")

    def search_directory_rooms(self, query: str, count: int = 100) -> dict:
        """服务端搜索频道：directory type=channels（网页目录页同款端点，
        支持中文、分页；未加入的公开频道也可见）。

        团队页搜索走 search_directory_teams（type=teams 直接命中团队，
        与网页团队页签一致）。讨论串（带 prid）不在 directory 收录
        范围——网页同样搜不到，非缺陷。
        """
        if not self._rc:
            raise ApiError("尚未登录")
        q = (query or "").strip()
        if not q:
            return {"entries": [], "total": 0}
        try:
            raw, total = self._directory_call("channels", 0, count, text=q)
            entries = []
            for r in raw:
                lm = (r.get("lastMessage") or {}).get("ts") \
                    if isinstance(r.get("lastMessage"), dict) else None
                entries.append(DirectoryEntry(
                    kind="channel",
                    rid=r.get("_id", "") or "",
                    name=r.get("fname") or r.get("name") or "",
                    room_type=r.get("t") or "c",
                    is_private=(r.get("t") == "p"),
                    users_count=int(r.get("usersCount") or 0),
                    created_ts=json_ts_to_epoch(r["ts"]) if r.get("ts") else 0.0,
                    last_ts=json_ts_to_epoch(lm) if lm else 0.0,
                ))
            return {"entries": entries, "total": total}
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"搜索频道失败：{e}")

    def search_directory_teams(self, query: str, count: int = 100) -> dict:
        """服务端搜索团队：directory type=teams + text（网页团队页签
        搜索同款，实测中文/拼音名都能命中；此前借道 type=channels 会
        把频道混进团队页——网页团队页签只搜团队）。"""
        if not self._rc:
            raise ApiError("尚未登录")
        q = (query or "").strip()
        if not q:
            return {"entries": [], "total": 0}
        try:
            raw, total = self._directory_call("teams", 0, count, text=q)
            return {"entries": [self._team_entry(t) for t in raw],
                    "total": total}
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"搜索团队失败：{e}")

    def _team_entry(self, raw: dict) -> DirectoryEntry:
        """directory type=teams 单条 → DirectoryEntry。

        _id 是团队主房间 id，teamId 才是团队 id（teams.join 用）；
        t='p' 为私有团队（与 teams.list 数字方言 1=私有 一致）；
        roomsCount 本服务器恒为 0（网页同款显示 0）。
        """
        ts = raw.get("ts")
        rooms_count = raw.get("roomsCount")
        return DirectoryEntry(
            kind="team",
            rid=raw.get("_id", "") or "",
            name=raw.get("fname") or raw.get("name") or "",
            room_type=raw.get("t") or "c",
            is_private=(raw.get("t") == "p"),
            users_count=int(raw.get("usersCount") or 0),
            rooms_count=int(rooms_count) if rooms_count is not None else -1,
            created_ts=json_ts_to_epoch(ts) if ts else 0.0,
            team_id=raw.get("teamId", "") or "",
        )

    def fetch_directory_teams(self, offset: int = 0, count: int = 50) -> dict:
        """浏览团队：directory type=teams 分页（公开 + 已加入私有团队
        全量可见，取代 teams.list——后者只回已加入，是「团队列表少了
        公开团队」的根因；teams.listAll 又要管理员权限）。"""
        if not self._rc:
            raise ApiError("尚未登录")
        try:
            raw, total = self._directory_call("teams", offset, count)
            return {"entries": [self._team_entry(t) for t in raw],
                    "total": total}
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"拉取团队列表失败：{e}")

    def fetch_room_info(self, room_id: str) -> dict:
        """取房间元数据（预览打开前校准 t/fname/teamMain）。失败返回空 dict。"""
        if not self._rc or not room_id:
            return {}
        try:
            data = self._rc.call_api_get("rooms.info", roomId=room_id) or {}
            return data.get("room") or {}
        except Exception as e:  # noqa: BLE001
            log.debug("取房间信息失败 %s：%s", room_id, e)
            return {}

    def join_channel(self, room_id: str) -> bool:
        """加入公开频道（channels.join）。"""
        if not self._rc:
            raise ApiError("尚未登录")
        try:
            resp = self._rc.call_api_post("channels.join", body={
                "roomId": room_id, "joinCode": "",
            })
            return resp.get("success", False)
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"加入频道失败：{e}")

    def join_team(self, room_id: str, team_id: str = "",
                  is_public: bool = True) -> bool:
        """加入团队。

        teams.join 端点存在性未经真机确认（库也无包装），先试它；公开
        团队主房间 t=c、成员关系挂在主房间上，回退 channels.join（端点
        确定存在）。两连发在同一工作线程内串行——rocketchat_API 共享
        Session 非线程安全。私有团队调用方不应发起（需受邀）。
        """
        if not self._rc:
            raise ApiError("尚未登录")
        errors = []
        if team_id:
            try:
                resp = self._rc.call_api_post("teams.join",
                                              body={"teamId": team_id})
                if resp.get("success", False):
                    return True
            except Exception as e:  # noqa: BLE001
                errors.append(f"teams.join：{e}")
        if is_public:
            try:
                resp = self._rc.call_api_post("channels.join", body={
                    "roomId": room_id, "joinCode": "",
                })
                if resp.get("success", False):
                    return True
            except Exception as e:  # noqa: BLE001
                errors.append(f"channels.join：{e}")
        detail = "；".join(errors)
        raise ApiError(f"加入团队失败{('：' + detail) if detail else ''}")

    def create_channel(self, name: str, topic: str = "", members=None) -> str:
        """创建新的公开频道，返回 room_id；members 为可选用户名列表（同时加入）。"""
        if not self._rc:
            raise ApiError("尚未登录")
        try:
            if members:
                resp = self._rc.channels_create(name, topic=topic, members=members)
            else:
                resp = self._rc.channels_create(name, topic=topic)
            channel = resp.get("channel") or {}
            return channel.get("_id", "")
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"创建频道失败：{e}")

    def create_group(self, name: str, topic: str = "", members=None) -> str:
        """创建新的私有频道（group），返回 room_id；members 为可选用户名列表（同时加入）。"""
        if not self._rc:
            raise ApiError("尚未登录")
        try:
            if members:
                resp = self._rc.groups_create(name, topic=topic, members=members)
            else:
                resp = self._rc.groups_create(name, topic=topic)
            group = resp.get("group") or {}
            return group.get("_id", "")
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"创建私有频道失败：{e}")

    def create_team(self, name: str, topic: str = "", members=None,
                    private: bool = True) -> str:
        """创建团队（teams.create），返回主房间 room_id。

        本服务器的 type 是数字：1=私有、0=公开，字符串/布尔全被拒
        （实测）。私有团队主房间 t=p，公开团队 t=c。
        响应里没有 room 键，主房间 id 取 team.roomId。room={"topic":...}
        参数会被服务端静默丢弃，主题需创建后对主房间单独 setTopic；
        setTopic 失败只降级，不让整个创建报错。
        """
        if not self._rc:
            raise ApiError("尚未登录")
        try:
            resp = self._rc.call_api_post("teams.create", body={
                "name": name,
                "type": 1 if private else 0,
                "members": list(members or []),
            })
            team = resp.get("team") or {}
            room_id = team.get("roomId", "")
            if topic and room_id:
                try:
                    t_ep = "groups.setTopic" if private else "channels.setTopic"
                    self._rc.call_api_post(t_ep, body={
                        "roomId": room_id, "topic": topic})
                except Exception as e:  # noqa: BLE001
                    log.warning("设置团队主题失败（忽略）：%s", e)
            return room_id
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"创建团队失败：{e}")

    def create_discussion(self, parent_id: str, name: str) -> str:
        """在指定父房间下创建讨论串（discussion），返回 room_id。"""
        if not self._rc:
            raise ApiError("尚未登录")
        try:
            resp = self._rc.rooms_create_discussion(prid=parent_id, t_name=name)
            discussion = resp.get("discussion") or {}
            return discussion.get("_id", "")
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"创建讨论失败：{e}")

    @contextmanager
    def _rc_timeout(self, value):
        """临时把库的超时换成 value（上传路径用），退出恢复原值。

        self._rc.timeout 是实例级共享状态：并发窗口里别的线程读到的会是
        「更大的超时」——只会晚点失败，不会提前失败，无害。因此不加锁
        （为一个放宽窗口的读竞争上锁属过度设计）。
        """
        rc = self._rc
        if rc is None:
            yield
            return
        previous = rc.timeout
        rc.timeout = value
        try:
            yield
        finally:
            rc.timeout = previous

    def _upload_via_service(self, file_path: str) -> str:
        """把图片经配置的上传服务上传，返回可直接内联的绝对 URL。

        上传服务（自建的图片/文件图床）契约：
        - 方法/路径：POST {upload_service_url}，请求体 multipart 的 file 字段；
        - 鉴权：Authorization: Bearer <IMAGE_TOKEN>（token 缺失则 401）；
        - 成功：HTTP 200，响应为纯文本 URL（Content-Type: text/plain）；
        - 失败：HTTP 401/400/413，响应 JSON {"error": "..."}。

        该 URL 不像 rooms.media 那样带 Content-Disposition: attachment，
        因此 markdown ![..](url) 能在官方客户端里真正内联显示。
        """
        if not os.path.exists(file_path):
            raise ApiError(f"文件不存在：{file_path}")
        headers = {}
        if self.upload_service_token:
            headers["Authorization"] = f"Bearer {self.upload_service_token}"
        try:
            with open(file_path, "rb") as f:
                resp = requests.post(
                    self.upload_service_url,
                    files={"file": (os.path.basename(file_path), f)},
                    headers=headers,
                    timeout=30,
                    verify=self.ssl_verify,
                )
            resp.raise_for_status()
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"上传服务调用失败：{e}")
        url = (resp.text or "").strip()
        if not url:
            raise ApiError("上传服务未返回图片URL")
        return self._absolute_link(url)

    def _rooms_media_stream(self, room_id: str, file_path: str,
                            progress_cb=None, cancel=None) -> tuple:
        """流式上传到 rooms.media/{rid}，返回 (fid, url)。

        与库 rooms_media 同款端点/鉴权/multipart 线格式，区别只在 body 经
        _MultipartBody 增量喂出——好让 progress_cb 报真实进度、cancel 能中止
        （置位后 read 抛 UploadCancelled，干净穿出 session.post）。
        """
        name = os.path.basename(file_path)
        mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        size = os.path.getsize(file_path)
        boundary = uuid.uuid4().hex
        # 去掉会破坏 multipart 头的字符（引号/换行）；中文按原始 UTF-8 保留，
        # 与 requests files= 的编码逐字节一致（实测，非 RFC 2231）。
        safe = name.replace('"', "").replace("\r", "").replace("\n", "")
        prefix = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{safe}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
        suffix = f"\r\n--{boundary}--\r\n".encode("utf-8")
        url = self._rc.server_url + self._rc.api_path + "rooms.media/" + room_id
        headers = dict(self._rc.headers)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        f = open(file_path, "rb")
        try:
            body = _MultipartBody(f, prefix, suffix, size, progress_cb, cancel)
            resp = self._rc.session.post(
                url, data=body, headers=headers,
                verify=self._rc.ssl_verify, cert=self._rc.cert,
                proxies=self._rc.proxies, timeout=self._rc.timeout)
        finally:
            f.close()
        if resp.status_code >= 400:
            raise ApiError(f"上传失败（HTTP {resp.status_code}）")
        data = resp.json()
        if not data.get("success", False):
            raise ApiError("上传失败")
        file_info = data.get("file") or {}
        fid = file_info.get("_id", "")
        if not fid:
            raise ApiError("上传成功但服务端未返回文件ID")
        return fid, file_info.get("url", "")

    def _upload_media(self, room_id: str, file_path: str) -> str:
        """经库上传到 rooms.media/{rid}，返回文件 _id（吃放宽超时窗口）。"""
        if not os.path.exists(file_path):
            raise ApiError(f"文件不存在：{file_path}")
        with self._rc_timeout(_UPLOAD_TIMEOUT):
            resp = self._rc.rooms_media(room_id, file_path)
        fid = (resp.get("file") or {}).get("_id", "")
        if not fid:
            raise ApiError("上传成功但服务端未返回文件ID")
        return fid

    def _upload_confirm(self, room_id: str, fid: str, msg: str = "") -> dict:
        """确认已上传的文件（rooms.mediaConfirm/{rid}/{fid}），返回服务端生成的消息。

        未确认的 rooms.media 上传记录带 ``expiresAt``（上传时间 + 24 小时），
        到期被服务端清理，消息里引用的文件 URL 全部 404——这是「fchat 发出的
        文件/图片隔天打不开」的根因。官方客户端的流程就是上传后立即
        mediaConfirm：确认后记录无 expiresAt、永久保留，且文件/图片消息由
        **服务端**按官方格式生成（含预览、尺寸等 attachment 全部字段），
        客户端不再自组 chat.sendMessage。msg 参数成为消息正文（说明文字）。

        注意路由名是 rooms.mediaConfirm（camelCase）——曾误探测
        rooms.media/confirm 得到 404 而误判「confirm 端点不可用」，勿再改回。
        """
        params = {"msg": msg} if msg else {}
        try:
            # 同样吃放宽窗口：确认时服务端要落库并生成预览/尺寸等字段
            with self._rc_timeout(_UPLOAD_TIMEOUT):
                resp = self._rc.rooms_media_confirm(room_id, fid, **params)
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"确认上传失败：{e}")
        return resp.get("message") or {}

    def send_file(self, room_id: str, file_path: str,
                  progress_cb=None, cancel=None) -> bool:
        """上传文件/图片到会话（rooms.media 上传 + mediaConfirm 确认）。返回是否成功。

        与官方客户端同款两步流程：先上传拿到文件 _id，再 mediaConfirm 确认。
        确认后上传永久保留（未确认的约 24 小时后被服务端清理，历史消息里的
        URL 全部 404，见 _upload_confirm），消息由服务端按官方格式生成。

        progress_cb(done, total) 与 cancel(threading.Event) 可选：给了就走
        流式上传（_rooms_media_stream），实时报进度、可中途取消（抛
        UploadCancelled）；不给则走库的 rooms_media，行为与原来完全一致。
        """
        if not self._rc:
            raise ApiError("尚未登录")
        try:
            if progress_cb is not None or cancel is not None:
                with self._rc_timeout(_UPLOAD_TIMEOUT):
                    fid, _url = self._rooms_media_stream(
                        room_id, file_path, progress_cb, cancel)
            else:
                fid = self._upload_media(room_id, file_path)
            self._upload_confirm(room_id, fid)
            return True
        except UploadCancelled:
            raise
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"发送文件失败：{e}")

    @staticmethod
    def _merge_trailing_text(parts: list) -> list:
        """把最后一个媒体之后的尾随文本段并到该媒体之前。

        循环按「媒体吃掉之前累计的全部文本作 caption」取说明，所以只需把
        尾随文本搬到最后一个媒体前面；各段文本用换行连接。"""
        merged = list(parts)
        tail: List[str] = []
        while merged and merged[-1][0] == "text":
            tail.insert(0, merged.pop()[1])
        tail_text = "\n".join(t for t in tail if t.strip()).strip()
        if not merged or not tail_text:
            return parts
        merged.insert(len(merged) - 1, ("text", tail_text))
        return merged

    def send_mixed(self, room_id: str, parts: list) -> Optional[Message]:
        """发送「文本 + 内联图片/文件」混合内容，返回首条媒体消息。

        parts 为有序列表，每项 ('text', str)、('image', 本地路径) 或
        ('file', 本地路径)。媒体一律走上传 + mediaConfirm（官方客户端同款
        流程）：服务端为每个媒体生成一条原生文件/图片消息，该媒体之前最近
        一段文本作为这条消息的正文（caption）——收端看到的是顺序正确的
        原生消息组合。旧实现把 rooms.media URL 拼进 markdown 正文发给
        chat.postMessage：上传记录未确认，约 24 小时后被服务端清理，历史
        消息里的图片/链接全部 404（此前定位）。

        例外：消息里没有文件、且配置了上传服务（upload_service_url）时，
        图片仍走图床并内联拼成一条 markdown 消息（图床 URL 无
        Content-Disposition，官方客户端可直接内联显示；图床记录持久，
        无 24 小时清理问题）——保持该开关既有的「截图/图片内联」行为。

        最后一个媒体之后的尾随文本并入该媒体的 caption（_merge_trailing_text），
        图文混排始终合并发送、不拆条；完全没有媒体时退化为纯文本发送。
        任一步失败即抛错，不再继续发送后续内容。
        """
        if not self._rc:
            raise ApiError("尚未登录")
        try:
            has_file = any(k == "file" for k, _ in parts)
            # 图床内联路径：纯「文本 + 图片」且配置了上传服务（旧行为）
            if self.upload_service_url and not has_file:
                body = []
                for kind, payload in parts:
                    if kind == "text":
                        if payload:
                            body.append(payload)
                    elif kind == "image":
                        url = self._upload_via_service(payload)
                        # 内联图片的描述统一用固定 "picture"，不用文件名
                        body.append(f"\n![picture]({url})\n")
                text = "\n".join(p for p in body if p)
                resp = self._rc.chat_post_message(text, room_id=room_id)
                return self._message_from_json(
                    resp.get("message", {}), room_id)

            # 原生消息路径：媒体逐个上传 + confirm，前一段文本作 caption。
            # 最后一个媒体之后的尾随文本并入该媒体的 caption（图文混排必须
            # 合并为一条消息，与官方客户端的「图片 + 说明」格式一致），否则
            # 会拆成图片 + 纯文本两条
            parts = self._merge_trailing_text(parts)
            first: Optional[Message] = None
            buf: List[str] = []
            for kind, payload in parts:
                if kind == "text":
                    if payload:
                        buf.append(payload)
                    continue
                caption = "\n".join(s for s in buf if s.strip()).strip()
                buf = []
                fid = self._upload_media(room_id, payload)
                msg = self._upload_confirm(room_id, fid, caption)
                if first is None and msg:
                    first = self._message_from_json(msg, room_id)
            text = "\n".join(s for s in buf if s.strip()).strip()
            if text:
                resp = self._rc.chat_post_message(text, room_id=room_id)
                if first is None:
                    first = self._message_from_json(
                        resp.get("message", {}), room_id)
            return first
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"发送图文消息失败：{e}")

    # ------------------------------------------------------------------ #
    # 头像 / 用户
    # ------------------------------------------------------------------ #
    def set_avatar(self, file_path: str) -> bool:
        """上传头像到服务器（users.setAvatar）。服务端拒绝时抛 ApiError。

        不用 rocketchat_API 库的 users_set_avatar：它 open 后不关文件句柄，
        且走共享 Session——本方法由 UI 经 run_async 工作线程调用，Session
        非线程安全（见本文件 get_conversations 处说明）。
        """
        if not self._rc:
            raise ApiError("尚未登录")
        if not os.path.exists(file_path):
            raise ApiError(f"文件不存在：{file_path}")
        mime = mimetypes.guess_type(file_path)[0] or "image/png"
        try:
            with open(file_path, "rb") as f:
                resp = requests.post(
                    f"{self.server_url}/api/v1/users.setAvatar",
                    headers={
                        "X-Auth-Token": self.auth_token,
                        "X-User-Id": self.user_id,
                    },
                    files={"image": (os.path.basename(file_path), f, mime)},
                    verify=self.ssl_verify, timeout=30)
            data = resp.json()
            if not data.get("success"):
                raise ApiError(
                    f"头像上传失败：{data.get('error', '服务端拒绝')}")
            return True
        # ApiError 原样抛出，否则会被下面的兜底再包一层、消息里前缀重复
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"头像上传失败：{e}")

    def get_avatar_url(self, user_id: str = None, username: str = None,
                       room_id: str = None, room_type: str = "c") -> str:
        """构造头像 URL。优先用户头像；无则返回空（UI 画默认圆形头像）。"""
        try:
            if user_id or username:
                return f"{self.server_url}/avatar/{username}" if username else ""
            return ""
        except Exception:
            return ""

    # ------------------------------------------------------------------ #
    # 消息 JSON 解析
    # ------------------------------------------------------------------ #
    def _decrypt_raw(self, m: dict, room_id: str) -> dict:
        """E2E 消息解密入口：非加密消息原样返回。"""
        if m.get("t") != "e2e" or self.e2e is None:
            return m
        return self.e2e.decrypt_to_dict(m, room_id)

    def _message_from_json(self, m: dict, room_id: str) -> Message:
        # 解析逻辑统一收口到 core/messages.py（与 realtime 共用）
        return parse_message(
            self._decrypt_raw(m, room_id),
            room_id, my_user_id=self.user_id, server_url=self.server_url)
