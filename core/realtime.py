"""Realtime 层：Rocket.Chat 的 DDP 客户端。

负责订阅 stream-room-messages，实时接收新消息，并把事件交给回调。
设计为纯 Python、无 Qt 依赖：在独立线程里跑收发循环，通过回调把消息抛给
上层（UI 层会把它桥接到 Qt 信号）。支持断线自动重连。收帧在 ddp-client
线程，发帧在独立的 ddp-sender 线程——长轮询下发送是一次阻塞 HTTP，
不能占用调用方（Qt 主线程）。

两种可互换的传输：
  * WebSocketTransport：直连 /websocket，服务端配置正确时优先使用。
  * PollingTransport：SockJS 的 xhr-polling。不少部署（nginx 没转发
    Upgrade 头）会把手动 WebSocket 握手挡成 400，此时官方网页版也是靠
    这条长轮询通道工作的，所以照同样方式回落，避免实时推送彻底不可用。
"""
from __future__ import annotations

import json
import logging
import random
import string
import threading
import time
import uuid
from typing import Callable

import requests
import websocket  # websocket-client

from .models import Message, User, json_ts_to_epoch
from .messages import parse_message

log = logging.getLogger(__name__)

# 长轮询单次等待上限（秒）。服务端持有请求直到有数据或心跳
_POLL_TIMEOUT = 45
_WS_TIMEOUT = 15
_KEEPALIVE = 20.0
# 已投递消息的指纹缓存上限（实测服务端会把同一条消息重复推送两次）
_SEEN_LIMIT = 512
# 参数被拒的错误码：新版服务端拿到单个 rid 时按 Meteor 惯例抛 Match error，
# 本项目的服务端则抛 not-allowed，两者都说明该换订阅方言
_DIALECT_ERRORS = ("not-allowed", "match error", "not an array")
# 一次 xhr_send 最多带多少条 DDP 帧（SockJS 允许一条消息装多个帧）。
# 必须批量：长轮询下每帧都是一次阻塞 POST（实测经代理约 1.2 秒/次），而
# 服务端在最后一次 xhr 轮询后约 60 秒就作废会话。逐帧发完 96 个房间订阅要
# 上百秒，中间没人轮询，于是会话被回收、剩下的帧全部 404。24 帧/批把重订阅
# 压到 4 次 POST（约 5 秒），实测服务端一次收下 96 帧不触发限流。
_SEND_CHUNK = 24
# stream-notify-user 事件名（相对 user_id 拼 "<uid>/<event>"）
_NOTIFY_EVENTS = ("user-status", "subscriptions-changed")


class DDPClient:
    def __init__(self, server_url: str, user_id: str, auth_token: str,
                 on_message=None, on_status=None, ssl_verify: bool = True,
                 e2e_manager=None, on_user_status=None, on_subscription=None):
        """
        server_url:  https://host  或 http://host
        on_message:  callable(message: Message)
        on_status:   callable(state: str)  connected/disconnected/reconnecting/connecting
        on_user_status:  callable(username: str, status: str)  他人状态推送（status 为服务器原串，归一化在 UI 层）
        on_subscription: callable(action: str, sub: dict)      订阅记录变更（inserted/updated/removed）
        """
        self.server_url = server_url.rstrip("/")
        self.user_id = user_id
        self.auth_token = auth_token
        self.on_message = on_message
        self.on_status = on_status
        self.on_user_status = on_user_status
        self.on_subscription = on_subscription
        self.ssl_verify = ssl_verify
        self.e2e = e2e_manager

        scheme = "wss" if self.server_url.startswith("https") else "ws"
        host = self.server_url.split("://", 1)[-1]
        self.ws_url = f"{scheme}://{host}/websocket"
        self.http_url = f"{'https' if scheme == 'wss' else 'http'}://{host}"
        self.sockjs_url = f"{self.http_url}/sockjs"

        self._transport = None
        self._prefer_polling = False
        # stream-room-messages 有两种服务端方言：新版收 rid 数组，旧版只认单个
        # rid。先按新版发，被拒后整体切过去（见 _handle_nosub）。
        self._rid_style = "array"
        self._sub_by_id = {}
        self._notify_by_id = {}
        # 已投递消息的指纹，用于吃掉服务端的重复推送（见 _handle_stream_message）
        self._seen_msgs = {}
        self._started = False
        self._stop = threading.Event()
        # 手动重连催促：打断 _run_loop 的退避等待（stop 也复用它）
        self._wake = threading.Event()
        self._thread = None
        self._subscribed_rooms = set()
        self._lock = threading.Lock()
        self._ws_connected = False
        # 已发出但还没等到 DDP ready 帧的订阅 id。全部就绪后向上抛 "ready"
        # 状态，主窗口据此区分「传输已连接」与「订阅已就绪」（服务端对
        # ready 之前的消息不会补发，过早宣称实时会漏听）。
        self._pending_ready = set()
        # 待发送帧的出口队列 + 独立发送线程：长轮询下一次 xhr_send 就是一整
        # 趟 TCP+TLS 握手（实测约 1 秒），不能让调用方（Qt 主线程）等它，
        # 所以 _send_many 只入队，真正发送在 _run_sender 线程里做。
        self._outbox = []
        self._outbox_lock = threading.Lock()
        self._outbox_wakeup = threading.Event()
        self._sender = None

    # ------------------------------------------------------------------ #
    # 线程生命周期
    # ------------------------------------------------------------------ #
    def start(self):
        if self._started:
            return
        self._started = True
        self._stop.clear()
        # 发送线程和主循环线程一起起来：connected 帧一到就有消费者，
        # 订阅帧不会在队列里多等一个轮询周期
        self._ensure_sender()
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name="ddp-client")
        self._thread.start()

    def stop(self):
        self._stop.set()
        # 主循环可能正卡在退避 wait 里，一并唤醒立即退出
        self._wake.set()
        # 发送线程可能正卡在 event.wait(0.5) 里，不唤醒它就要等满一个
        # 轮询周期才看到 stop（在途的那次 POST 只能等它自己结束，线程是
        # daemon，不会挡住退出）
        self._outbox_wakeup.set()
        transport = self._transport
        if transport:
            try:
                transport.close()
            except Exception:  # noqa: BLE001
                pass

    def join(self, timeout=None):
        """等两个后台线程收尾；timeout 是总预算，不是每个线程各等一遍。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        for thread in (self._thread, self._sender):
            if not thread:
                continue
            if deadline is None:
                thread.join()
            else:
                thread.join(max(0.0, deadline - time.monotonic()))

    def _emit_status(self, state: str):
        if self.on_status:
            try:
                self.on_status(state)
            except Exception:  # noqa: BLE001
                log.exception("status callback failed")

    # ------------------------------------------------------------------ #
    # 主循环：选传输 → 握手（connect + login + 重订阅）→ 收帧，断线重连
    # ------------------------------------------------------------------ #
    def _run_loop(self):
        delay = 0.5
        while not self._stop.is_set():
            transport = None
            try:
                self._emit_status("connecting")
                transport = self._open_transport()
                self._transport = transport
                # 连上过就把退避重新计起，避免偶发掉线后一直等退避上限
                delay = 0.5
                transport.run(self._on_transport_open, self._handle_frame,
                              self._stop)
            except Exception as e:  # noqa: BLE001
                log.warning("DDP 连接异常：%s", e)
            finally:
                self._ws_connected = False
                self._transport = None
                # 丢掉还没发出去的帧：旧连接的 sub id 随断线一起作废，而下方
                # connected 分支已经按 _subscribed_rooms 全量重生成帧，队列里
                # 的陈旧帧只会让同一个房间被订阅两次
                self._clear_outbox()
                if transport:
                    try:
                        transport.close()
                    except Exception:  # noqa: BLE001
                        pass
                self._emit_status("disconnected")
            if self._stop.is_set():
                break
            self._emit_status("reconnecting")
            self._wake.wait(delay)
            if self._wake.is_set():
                # 手动催促或 stop：立刻醒，并把退避重置回最小值；
                # stop 时下一轮循环开头的判断会直接退出
                self._wake.clear()
                delay = 0.5
            else:
                delay = min(delay * 2, 10)

    def reconnect_now(self):
        """尽力立刻重连（主线程可调用）。

        唤醒退避等待并掐断当前传输：WebSocket 的 run 循环在 1 秒内看到
        broken 退出；长轮询无法中断在途请求，最多等到本次长轮询返回
        （服务端心跳通常远小于 45 秒超时）。
        """
        if not self._started or self._stop.is_set():
            return
        self._wake.set()
        transport = self._transport
        if transport is not None:
            try:
                transport.broken = True
                transport.close()
            except Exception:  # noqa: BLE001
                pass

    def _open_transport(self):
        """优先直连 WebSocket；握手被反代挡掉时回落到 SockJS 长轮询并记住选择。"""
        if not self._prefer_polling:
            ws = WebSocketTransport(self.ws_url, self.ssl_verify)
            try:
                ws.connect()
                return ws
            except Exception as e:  # noqa: BLE001
                self._prefer_polling = True
                log.info("WebSocket 握手失败（%s），回落到 SockJS 长轮询", e)
        polling = PollingTransport(self.sockjs_url, self.ssl_verify)
        polling.connect()
        return polling

    # ---- 传输层事件（运行于 _run_loop 所在的线程） ---- #
    def _on_transport_open(self, send: Callable[[str], None]):
        """传输就绪：发送 DDP connect。"""
        send(json.dumps({
            "msg": "connect", "version": "1", "support": ["1", "pre2", "pre1"],
        }))

    def _handle_frame(self, raw):
        try:
            msg = json.loads(raw)
        except Exception:  # noqa: BLE001
            return
        mtype = msg.get("msg")
        if mtype == "ping":
            self._send_raw({"msg": "pong"})
        elif mtype == "connected":
            # 握手成功 → resume token 登录，进入已连接状态
            self._ws_connected = True
            log.info("DDP 已就绪：%s session=%s", self._transport.name,
                     msg.get("session", ""))
            self._emit_status("connected")
            # 重订阅已记录的房间；旧连接的 sub id 已随断线作废，
            # 不清掉的话 _sub_frame 的幂等判断会把重订阅全挡掉
            with self._lock:
                self._sub_by_id.clear()
                self._notify_by_id.clear()
                to_sub = list(self._subscribed_rooms)
            frames = [{"msg": "method", "method": "login",
                       "params": [{"resume": self.auth_token}],
                       "id": f"m-{uuid.uuid4().hex}"}]
            sub_frames = [f for f in map(self._sub_frame, to_sub) if f]
            notify_frames = [f for f in map(self._notify_frame, _NOTIFY_EVENTS)
                             if f]
            frames += sub_frames + notify_frames
            with self._lock:
                # 记录本轮待等 ready 的子订阅；全部就绪后向上抛 "ready"
                self._pending_ready.update(
                    f["id"] for f in sub_frames + notify_frames
                    if f.get("msg") == "sub")
            # 合成一批发出：逐帧 POST 会让接收循环上百秒不轮询，会话随即被作废
            self._send_many(frames)
        elif mtype == "ready":
            # 服务端确认这些 sub 已就绪——ready 之前的消息服务端不会补发，
            # 等全部就绪后再宣称"实时已就绪"，避免漏听假象
            self._mark_ready(msg.get("subs") or [])
        elif mtype == "changed" and msg.get("collection") == "stream-room-messages":
            self._handle_stream_message(msg)
        elif mtype == "changed" and msg.get("collection") == "stream-notify-user":
            self._handle_notify_message(msg)
        elif mtype == "nosub":
            self._handle_nosub(msg)
        elif mtype == "result":
            pass
        # "added"/"updated"/"removed" 忽略

    # ------------------------------------------------------------------ #
    # 订阅
    # ------------------------------------------------------------------ #
    def subscribe_room(self, room_id: str):
        """订阅聊天室接收新消息（幂等）。可在任意线程调用。"""
        with self._lock:
            new_room = room_id not in self._subscribed_rooms
            self._subscribed_rooms.add(room_id)
        if self._ws_connected and new_room:
            self._subscribe_room(room_id)

    def subscribe_rooms(self, room_ids):
        """批量订阅（会话列表到货时用）。

        帧只入队、调用方立即返回（见 _send_many），所以这里不再被阻塞 HTTP 拖住；
        但仍要一次给出全部房间：发送线程会把一批帧合并成一次 xhr_send，逐帧
        发既多跑几趟握手，又会因为迟迟不轮询让服务端在约 60 秒后作废会话。
        """
        with self._lock:
            new_rooms = [r for r in dict.fromkeys(room_ids)
                         if r not in self._subscribed_rooms]
            self._subscribed_rooms.update(new_rooms)
        if self._ws_connected and new_rooms:
            self._send_many([f for f in map(self._sub_frame, new_rooms) if f])

    def _subscribe_room(self, room_id: str):
        frame = self._sub_frame(room_id)
        if frame:
            self._send_raw(frame)

    def _notify_frame(self, event: str):
        """登记一个 stream-notify-user 订阅（"<uid>/<event>"），返回待发帧。

        事件名方言与 stream-room-messages 一致（同一套 streamer 实现）：
        新版要 [event] 数组，老版要裸字符串，按已探测的 _rid_style 走；
        被拒后 _handle_nosub 会整体切换并连同房间一起重发。
        """
        if not self._transport:
            return None
        sub_id = "sub-" + uuid.uuid4().hex
        name = f"{self.user_id}/{event}"
        params = [[name], False] if self._rid_style == "array" else [name, False]
        with self._lock:
            if event in self._notify_by_id.values():
                return None
            self._notify_by_id[sub_id] = event
        return {
            "msg": "sub",
            "id": sub_id,
            "name": "stream-notify-user",
            "params": params,
        }

    def _sub_frame(self, room_id: str):
        """登记一个房间订阅，返回待发的 sub 帧；已在订阅中就返回 None。

        幂等：主线程的 subscribe_room 与 "connected" 后的重订阅可能交错，
        不加判断会给同一个房间注册两个订阅。
        """
        if not self._transport:
            return None
        sub_id = "sub-" + uuid.uuid4().hex
        params = [[room_id], False] if self._rid_style == "array" else [room_id, False]
        with self._lock:
            if room_id in self._sub_by_id.values():
                return None
            self._sub_by_id[sub_id] = room_id
        return {
            "msg": "sub",
            "id": sub_id,
            "name": "stream-room-messages",
            "params": params,
        }

    def _mark_ready(self, sub_ids):
        """把已就绪的 sub 从待等集合移除；全部就绪后向上抛 "ready" 状态。"""
        with self._lock:
            if not sub_ids:
                return
            self._pending_ready.difference_update(sub_ids)
            empty = not self._pending_ready
        if empty:
            self._emit_status("ready")

    def _handle_nosub(self, msg: dict):
        """订阅被拒：若还在用数组方言，判定服务端是"单 rid"方言，整体换一次。"""
        sub_id = str(msg.get("id", ""))
        error = str((msg.get("error") or {}).get("error", ""))
        with self._lock:
            room_id = self._sub_by_id.get(sub_id)
            notify_event = self._notify_by_id.pop(sub_id, "")
            self._pending_ready.discard(sub_id)
        if (self._rid_style == "array"
                and any(k in error.lower() for k in _DIALECT_ERRORS)):
            self._rid_style = "single"
            log.info("服务端不认 rid 数组（%s），改用单个 rid 订阅 stream-room-messages",
                     error)
            with self._lock:
                rooms = list(self._subscribed_rooms)
                self._sub_by_id.clear()
                self._notify_by_id.clear()
            re_frames = [f for f in map(self._sub_frame, rooms) if f]
            re_frames += [f for f in map(self._notify_frame, _NOTIFY_EVENTS)
                          if f]
            with self._lock:
                # 换方言重订阅同样要等 ready；上次被拒的子订阅已从
                # _pending_ready 移除，这里补回，保持"全部就绪"语义
                self._pending_ready.update(
                    f["id"] for f in re_frames if f.get("msg") == "sub")
            self._send_many(re_frames)
        elif room_id:
            log.debug("订阅被拒（rid=%s）：%s", room_id, error)
        elif notify_event:
            # 非方言问题（如服务端整体不开放该 stream）：只记日志，拉取
            # 逻辑照旧兜底，状态/订阅同步退化为"无推送"
            log.warning("stream-notify-user 订阅被拒（event=%s）：%s",
                        notify_event, error)
        # 无论哪种情况，被拒的 sub 都不会再来 ready 帧，若待等集合因此清空，
        # 补一个已就绪判定，避免 pending 被拒帧卡住"未就绪"状态
        with self._lock:
            empty = not self._pending_ready
        if empty:
            self._emit_status("ready")

    # ------------------------------------------------------------------ #
    # 底层收发
    # ------------------------------------------------------------------ #
    def _send_raw(self, obj: dict):
        self._send_many([obj])

    def _send_many(self, frames: list):
        """把帧塞进待发送队列后立即返回（绝不在这条线程上做 HTTP）。

        为什么要异步：这个部署的反向代理不转发 Upgrade 头，实时通道只能走
        SockJS xhr-polling，于是每一次 xhr_send 都是一整趟 TCP+TLS 握手
        （同机实测约 1 秒/次，代理一慢就顶到 20 秒超时）。调用方是 Qt 主线程
        （点私聊订阅单个房间、登录时几十个房间整批订阅），同步发送会把窗口
        冻成「未响应」。真正的发送在 _run_sender 线程里做，批量合并与判死
        语义都在那边保持不变。
        """
        if not frames:
            return
        with self._outbox_lock:
            self._outbox.extend(frames)
        self._outbox_wakeup.set()
        # 兜底：未经 start() 的调用（单测、脚本直连）也要有人消费队列
        self._ensure_sender()

    def _ensure_sender(self):
        """确保发送线程在跑（幂等，可在任意线程调用）。"""
        with self._outbox_lock:
            if self._stop.is_set():
                return
            if self._sender and self._sender.is_alive():
                return
            self._sender = threading.Thread(target=self._run_sender,
                                            daemon=True, name="ddp-sender")
            self._sender.start()

    def _clear_outbox(self):
        """丢弃队列里尚未发出的帧。"""
        with self._outbox_lock:
            self._outbox = []

    def _run_sender(self):
        """发送线程：把 outbox 整批交给 transport，一次 xhr_send 装多帧。

        分块仍由 PollingTransport.send_many 按 _SEND_CHUNK 负责，这里不能拆成
        逐帧发送——逐帧 POST 会让接收循环上百秒不轮询，服务端约 60 秒就作废
        会话（见 _SEND_CHUNK 注释）。
        """
        while not self._stop.is_set():
            # 0.5 秒是兜底轮询：正常路径由 _send_many 的 set() 立刻唤醒
            self._outbox_wakeup.wait(0.5)
            self._outbox_wakeup.clear()
            if self._stop.is_set():
                return
            with self._outbox_lock:
                frames, self._outbox = self._outbox, []
            if not frames:
                continue
            transport = self._transport
            if not transport or transport.broken or not self._ws_connected:
                # 直接丢弃这批是安全的：断线/判死后 connected 分支会从
                # _subscribed_rooms 全量重订阅，留着陈旧帧只会重复订阅；
                # pong 之类的一次性帧丢了也不影响正确性
                continue
            try:
                transport.send_many([json.dumps(f) for f in frames])
            except Exception as e:  # noqa: BLE001
                # 发不出去基本等于会话已被服务端回收，接着逐帧抛栈只会把日志刷满
                # （实测一天 1900 条），而收件方向的 xhr 轮询还会继续造成"连接正常"
                # 的假象。标记失效让接收循环退出，重连后的 connected 会重发订阅。
                log.warning("DDP 发送通道失效（%s），断开重连", e)
                transport.broken = True
                self._ws_connected = False

    # ------------------------------------------------------------------ #
    # 消息解析
    # ------------------------------------------------------------------ #
    def _handle_stream_message(self, msg: dict):
        fields = msg.get("fields", {}) or {}
        args = fields.get("args") or []
        if not args:
            return
        m = args[0]
        if not isinstance(m, dict):
            return
        if self._is_replay(m):
            return
        # 帧上的 id 在实测服务端是占位值（字面量 "id"），房间以消息体为准
        room_id = m.get("rid") or fields.get("eventName") or msg.get("id")
        message = self._to_message(m, room_id)
        if self.on_message:
            try:
                self.on_message(message)
            except Exception:  # noqa: BLE001
                log.exception("message callback failed")

    def _handle_notify_message(self, msg: dict):
        """stream-notify-user 事件分发：user-status / subscriptions-changed。

        payload 形状未经真机全量验证（本服务器方言多），按官方文档常见
        形态防御式解析：user-status 的 args[0] 可能是 {username/_id,
        status} 字典或 [username, status] 双元素列表；subscriptions-changed
        固定 [action, 订阅记录 dict]。解析不出就丢弃并打 debug，不影响
        消息流；解析出但无回调注册同样直接返回。
        """
        fields = msg.get("fields", {}) or {}
        event = str(fields.get("eventName") or "")
        args = fields.get("args") or []
        if not args:
            return
        if event.endswith("/user-status"):
            # 两种已知形态：args=[{username/status 字典}]，或 args 整体
            # 就是 [username, status]；其余（纯字符串单个等）丢弃
            first = args[0]
            if isinstance(first, str) and len(args) >= 2:
                username, status = str(first or ""), str(args[1] or "")
            else:
                username, status = self._parse_user_status(first)
            if not username:
                log.debug("user-status 帧解析不出用户名：%r", args)
                return
            if self.on_user_status:
                try:
                    self.on_user_status(username, status)
                except Exception:  # noqa: BLE001
                    log.exception("user status callback failed")
        elif event.endswith("/subscriptions-changed"):
            if len(args) < 2 or not isinstance(args[1], dict):
                log.debug("subscriptions-changed 帧形状异常：%r", args[:1])
                return
            if self.on_subscription:
                try:
                    self.on_subscription(str(args[0] or ""), args[1])
                except Exception:  # noqa: BLE001
                    log.exception("subscription callback failed")

    @staticmethod
    def _parse_user_status(arg) -> tuple:
        if isinstance(arg, dict):
            username = str(arg.get("username") or arg.get("_id") or "")
            status = str(arg.get("status") or "")
            return username, status
        if isinstance(arg, (list, tuple)) and len(arg) >= 2:
            return str(arg[0] or ""), str(arg[1] or "")
        return "", ""

    def _is_replay(self, m: dict) -> bool:
        """判断这条帧是不是服务端重复推送。

        实测同一条消息会以约 1 秒的间隔推两次，重复帧内容完全一致；用
        "消息 id + 最后更新时间" 作指纹，既能吃掉重复，又不会挡掉编辑。
        指纹取自密文之外的字段，所以放在 E2E 解密之前。
        """
        stamp = m.get("_updatedAt") or m.get("ts") or ""
        if isinstance(stamp, dict):
            stamp = stamp.get("$date", "")
        key = (str(m.get("_id")), str(stamp))
        if key in self._seen_msgs:
            return True
        self._seen_msgs[key] = True
        while len(self._seen_msgs) > _SEEN_LIMIT:
            self._seen_msgs.pop(next(iter(self._seen_msgs)))
        return False

    def _to_message(self, m: dict, room_id: str) -> Message:
        # 解析逻辑统一收口到 core/messages.py（与 REST 共用）；
        # E2E 消息先解密还原为普通 dict（失败原样，由 parse_message 兑底）
        if self.e2e is not None:
            m = self.e2e.decrypt_to_dict(m, room_id or m.get("rid", ""))
        return parse_message(
            m, room_id or m.get("rid", ""),
            my_user_id=self.user_id, server_url=self.server_url)


# ---------------------------------------------------------------------- #
# 传输：两者接口一致，DDPClient 按需选用
# ---------------------------------------------------------------------- #
class WebSocketTransport:
    """直连 /websocket。握手失败会抛出，交由 DDPClient 回落。"""

    name = "websocket"

    def __init__(self, url: str, ssl_verify: bool = True):
        self.url = url
        self.ssl_verify = ssl_verify
        self._ws = None
        # 发送失败时置位：run 循环据此退出，由 DDPClient 重连
        self.broken = False

    def connect(self):
        sslopt = None
        if not self.ssl_verify and self.url.startswith("wss"):
            sslopt = {"cert_reqs": 0}
        self._ws = websocket.create_connection(
            self.url, subprotocol="meteor", timeout=_WS_TIMEOUT,
            sslopt=sslopt, enable_multithread=True)

    def run(self, on_open, on_frame, stop: threading.Event):
        """阻塞收帧，直到连接关闭或被 stop 打断。"""
        self._ws.settimeout(1.0)
        on_open(self.send)
        next_ping = time.monotonic() + _KEEPALIVE
        while not stop.is_set() and not self.broken and self._ws:
            try:
                raw = self._ws.recv()
            except websocket.WebSocketTimeoutException:
                # 空闲时发 ping，免得中间代理把连接掐掉
                if time.monotonic() >= next_ping:
                    next_ping = time.monotonic() + _KEEPALIVE
                    try:
                        self._ws.ping()
                    except Exception:  # noqa: BLE001
                        return
                continue
            except (websocket.WebSocketConnectionClosedException, OSError):
                return
            if raw:
                on_frame(raw)

    def send(self, text: str):
        if self._ws is None:
            raise ConnectionError("WebSocket 已关闭")
        self._ws.send(text)

    def send_many(self, texts):
        # WebSocket 帧开销可忽略，不必合并
        for text in texts:
            self.send(text)

    def close(self):
        ws, self._ws = self._ws, None
        if ws:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass


class PollingTransport:
    """SockJS 的 xhr-polling 通道，只走普通 HTTP，因此不受 Upgrade 限制。

    报文按 SockJS 协议成帧：发送是 JSON 字符串数组（一次 xhr_send 可以带
    多帧，见 send_many），接收按行给出操作符（o 握手 / h 心跳 / a 数据 /
    c 关闭）。/sockjs/info 声明 cookie_needed 为 false，所以无会话状态，
    直接用模块级 requests 保持线程安全。
    """

    name = "xhr-polling"

    def __init__(self, sockjs_url: str, ssl_verify: bool = True):
        server_id = random.randint(100, 999)
        session_id = "".join(
            random.choices(string.ascii_lowercase + string.digits, k=8))
        self.base_url = f"{sockjs_url}/{server_id}/{session_id}"
        self.ssl_verify = ssl_verify
        # 发送失败（会话被服务端回收）时置位，run 循环据此退出触发重连
        self.broken = False

    def _leg(self, name: str) -> str:
        return f"{self.base_url}/{name}?t={int(time.time() * 1000)}"

    def connect(self):
        resp = requests.post(self._leg("xhr"), timeout=20,
                             verify=self.ssl_verify)
        if resp.status_code != 200 or not resp.text.startswith("o"):
            raise ConnectionError(
                f"SockJS 握手失败：{resp.status_code} {resp.text[:80]!r}")

    def run(self, on_open, on_frame, stop: threading.Event):
        """阻塞收帧：每次 xhr 长轮询由服务端持有到有心跳或数据为止。"""
        on_open(self.send)
        while not stop.is_set() and not self.broken:
            try:
                resp = requests.post(self._leg("xhr"), timeout=_POLL_TIMEOUT,
                                     verify=self.ssl_verify)
            except requests.RequestException as e:
                log.debug("长轮询中断：%s", e)
                return
            if resp.status_code != 200:
                log.debug("长轮询异常状态：%s", resp.status_code)
                return
            for op, payload in _parse_sockjs_frames(resp.text):
                if op == "c":
                    return
                for raw in payload:
                    on_frame(raw)

    def send(self, text: str):
        self.send_many([text])

    def send_many(self, texts):
        """按 _SEND_CHUNK 分批发：一次 xhr_send 带多帧，减少阻塞 POST 次数。

        长轮询的发送和接收共用一个调用线程，POST 期间没人轮询；实测服务端
        在最后一次轮询后约 60 秒销毁会话，届时再发就只拿到 404。
        """
        for i in range(0, len(texts), _SEND_CHUNK):
            chunk = texts[i:i + _SEND_CHUNK]
            resp = requests.post(
                self._leg("xhr_send"), data=json.dumps(chunk),
                headers={"Content-Type": "application/json"},
                timeout=20, verify=self.ssl_verify)
            if resp.status_code not in (200, 204):
                raise ConnectionError(f"xhr_send 失败：{resp.status_code}")

    def close(self):
        # 长轮询无法从别的线程掐断；在途请求最多再等一个超时，
        # run 的循环条件随后就会看到 stop 并退出。
        pass


def _parse_sockjs_frames(body: str):
    """把 SockJS 响应正文拆成 [(操作符, 消息列表)]；o/h 只用于保活，不产出帧。"""
    frames = []
    for line in body.split("\n"):
        line = line.strip()
        if len(line) < 2 or line[0] not in "ac":
            continue
        try:
            payload = json.loads(line[1:])
        except ValueError:
            continue
        frames.append((line[0], payload if line[0] == "a" else []))
    return frames
