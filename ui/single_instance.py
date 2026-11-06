"""单实例守卫：同一配置目录只允许一个 fchat 进程。

后启动的实例通过 QLocalSocket（Windows 命名管道）给已运行的实例发
activate，随后自己直接退出；已有实例收到后把主窗口唤到前台。

实例名按 CONFIG_DIR 哈希：不同配置目录（如开发/测试临时目录）互不干扰，
同一用户同一配置只会有一个进程。
"""
from __future__ import annotations

import hashlib
import logging
import sys
import time

from PyQt5.QtCore import QObject
from PyQt5.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger(__name__)

_ACTIVATE = b"activate\n"
_ACK = b"ok\n"


def instance_key(prefix: str = "fchat") -> str:
    import config
    raw = f"{prefix}|{config.CONFIG_DIR}"
    return f"{prefix}-{hashlib.md5(raw.encode('utf-8')).hexdigest()[:16]}"


class SingleInstance(QObject):
    """先 notify_existing()，没有已有实例再 start() 监听后续启动。"""

    def __init__(self, key: str, parent=None):
        super().__init__(parent)
        self._name = key
        self._server: QLocalServer | None = None
        self._handler = None
        self._pending = False
        self._notify_socket = None

    # ------------------------------------------------------------------ #
    def notify_existing(self, connect_timeout_ms: int = 2000,
                        ack_timeout_ms: int = 2000) -> bool:
        """已有实例则发激活消息并返回 True。

        连接带重试：首实例还在初始化（尚未 listen）时短暂等待。
        发送后必须等 ack —— 本进程退出时 QLocalSocket 析构会硬关管道，
        早于对端处理就会丢消息（实测命名管道会连连接一起丢）。
        """
        if sys.platform == "win32":
            # Windows 前台抢占限制：让已有实例有权把窗口提到最前
            try:
                import ctypes
                ctypes.windll.user32.AllowSetForegroundWindow(-1)
            except Exception:  # noqa: BLE001
                pass
        deadline = time.monotonic() + connect_timeout_ms / 1000.0
        sock = QLocalSocket()
        while True:
            sock.connectToServer(self._name)
            if sock.waitForConnected(200):
                break
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)
        # 保活：进程退出前的析构不能在 ack 到达前把连接掐掉
        self._notify_socket = sock
        sock.write(_ACTIVATE)
        sock.flush()
        sock.waitForBytesWritten(500)
        sock.waitForReadyRead(ack_timeout_ms)
        sock.disconnectFromServer()
        return True

    def start(self) -> bool:
        """监听实例名。失败只记日志：宁可多开也不能挡住主程序。"""
        if self._server is not None:
            return True
        QLocalServer.removeServer(self._name)
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.UserAccessOption)
        if not server.listen(self._name):
            log.warning("单实例监听失败：%s", server.errorString())
            return False
        server.newConnection.connect(self._on_new_connection)
        self._server = server
        return True

    def set_activation_handler(self, handler) -> None:
        """窗口创建后注册；此前收到的激活请求补发一次。"""
        self._handler = handler
        if self._pending:
            self._pending = False
            handler()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        self._server.deleteLater()
        self._server = None

    # ------------------------------------------------------------------ #
    def _on_new_connection(self):
        while self._server is not None and self._server.hasPendingConnections():
            conn = self._server.nextPendingConnection()
            conn.readyRead.connect(lambda c=conn: self._read(c))
            conn.disconnected.connect(conn.deleteLater)
            # 连接可能早于信号连接就已带上数据，先读一次兜底
            if conn.bytesAvailable():
                self._read(conn)

    def _read(self, conn):
        data = getattr(conn, "_fchat_buf", b"") + bytes(conn.readAll())
        conn._fchat_buf = data
        if _ACTIVATE.strip() in data:
            conn._fchat_buf = b""
            conn.write(_ACK)
            conn.flush()
            self._fire()

    def _fire(self):
        if self._handler is not None:
            self._handler()
        else:
            self._pending = True
