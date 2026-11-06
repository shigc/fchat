"""Realtime 的 Qt 桥接：把 DDP 后台线程的回调安全地转为 Qt 信号。

DDPClient 会在自己的后台线程（threading.Thread）里调用 on_message /
on_status 回调。这里把这些回调安全地转成 Qt 信号，供主线程的
MainWindow 连接。DDP 线程调用时发出的信号会被 PyQt 自动转为
QueuedConnection 投递到主线程，因此线程安全，无需手动加锁。
"""
from __future__ import annotations

from PyQt5.QtCore import QObject, pyqtSignal

from core.realtime import DDPClient
from core.models import Message


class RealtimeBridge(QObject):
    message_received = pyqtSignal(object)   # Message
    status_changed = pyqtSignal(str)        # connected/disconnected/reconnecting
    user_status_changed = pyqtSignal(str, str)      # username, 服务器原状态串
    subscription_changed = pyqtSignal(str, object)  # inserted/updated/removed, 订阅记录 dict

    def __init__(self, server_url: str, user_id: str, user_token: str,
                 ssl_verify: bool = True, e2e_manager=None, parent=None):
        super().__init__(parent)
        # DDPClient 内部自行管理 daemon 线程，这里直接持有即可
        self._client = DDPClient(
            server_url, user_id, user_token,
            on_message=self._on_message,
            on_status=self._on_status,
            on_user_status=self._on_user_status,
            on_subscription=self._on_subscription,
            ssl_verify=ssl_verify,
            e2e_manager=e2e_manager,
        )

    def _on_message(self, message: Message):
        try:
            self.message_received.emit(message)
        except RuntimeError:
            # C++ 对象已销毁（登出/关窗口后 DDP 线程的残余回调），静默即可
            pass

    def _on_status(self, state: str):
        try:
            self.status_changed.emit(state)
        except RuntimeError:
            pass

    def _on_user_status(self, username: str, status: str):
        try:
            self.user_status_changed.emit(username, status)
        except RuntimeError:
            pass

    def _on_subscription(self, action: str, sub: dict):
        try:
            self.subscription_changed.emit(action, sub)
        except RuntimeError:
            pass

    def start(self):
        self._client.start()

    def subscribe(self, room_id: str):
        # 只入队就返回（内部用锁）：xhr_send 由 core 的 ddp-sender 线程执行，
        # 主线程不会再被一次约 1 秒的阻塞 POST 冻住
        self._client.subscribe_room(room_id)

    def subscribe_rooms(self, room_ids):
        # 整批房间一次给出：入队后立即返回，发送线程把它们合并成少数几次
        # xhr_send（逐帧发会让服务端约 60 秒后作废 SockJS 会话）
        self._client.subscribe_rooms(list(room_ids))

    def reconnect_now(self):
        """手动重连（横幅按钮）：立即打断退避/当前连接，尽力马上重连。"""
        self._client.reconnect_now()

    def stop(self):
        # 先摘回调再停线程：DDP 线程收尾期间仍会补发状态回调，而 Qt 侧
        # 对象可能已销毁（登出/关窗口），emit 会抛 RuntimeError
        self._client.on_message = None
        self._client.on_status = None
        self._client.on_user_status = None
        self._client.on_subscription = None
        self._client.stop()
