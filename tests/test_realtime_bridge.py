"""RealtimeBridge 生命周期测试：DDP 线程的残余回调不得炸到已销毁的 Qt 对象。

场景：登出/关窗口时 stop() 只设停止标志，守护线程收尾期间仍可能补发
status 回调；此时 RealtimeBridge 的 C++ 对象可能已销毁，emit 会抛
RuntimeError（被 core 捕获记 ERROR 日志，噪音）。
运行：python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication  # noqa: E402

from core.realtime import DDPClient  # noqa: E402
from ui.realtime_bridge import RealtimeBridge  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class RealtimeBridgeLifecycleTest(unittest.TestCase):
    def test_stop_detaches_callbacks(self):
        bridge = RealtimeBridge("http://s.example.com", "u", "t")
        bridge.stop()
        self.assertIsNone(bridge._client.on_status)
        self.assertIsNone(bridge._client.on_message)
        self.assertIsNone(bridge._client.on_user_status)
        self.assertIsNone(bridge._client.on_subscription)

    def test_callback_after_cpp_delete_is_silent(self):
        from PyQt5 import sip
        bridge = RealtimeBridge("http://s.example.com", "u", "t")
        sip.delete(bridge)  # 强制销毁 C++ 对象（模拟关窗口/登出后的时序）
        # 回调必须静默返回而不是抛 RuntimeError
        bridge._on_status("connected")
        bridge._on_message(None)
        bridge._on_user_status("alice", "online")
        bridge._on_subscription("updated", {"rid": "R1"})

    def test_push_callbacks_forward_to_signals(self):
        statuses = []
        subs = []
        bridge = RealtimeBridge("http://s.example.com", "u", "t")
        try:
            bridge.user_status_changed.connect(
                lambda u, s: statuses.append((u, s)))
            bridge.subscription_changed.connect(
                lambda a, s: subs.append((a, s)))
            bridge._on_user_status("alice", "busy")
            bridge._on_subscription("removed", {"rid": "R1"})
            self.assertEqual(statuses, [("alice", "busy")])
            self.assertEqual(subs, [("removed", {"rid": "R1"})])
        finally:
            bridge.stop()


class DDPReconnectTest(unittest.TestCase):
    """手动重连：未启动/已停止不动作；运行中掐断传输并唤醒退避等待。"""

    def test_reconnect_noop_before_start(self):
        client = DDPClient("http://s.example.com", "u", "t")
        client.reconnect_now()
        self.assertFalse(client._wake.is_set())

    def test_reconnect_noop_after_stop(self):
        client = DDPClient("http://s.example.com", "u", "t")
        client._started = True
        client._stop.set()
        client.reconnect_now()
        self.assertFalse(client._wake.is_set())

    def test_reconnect_breaks_and_closes_transport(self):
        client = DDPClient("http://s.example.com", "u", "t")
        client._started = True
        transport = mock.Mock()
        transport.broken = False
        client._transport = transport
        client.reconnect_now()
        self.assertTrue(client._wake.is_set())
        self.assertTrue(transport.broken)
        transport.close.assert_called_once()

    def test_reconnect_without_transport_only_wakes(self):
        client = DDPClient("http://s.example.com", "u", "t")
        client._started = True
        client.reconnect_now()
        self.assertTrue(client._wake.is_set())

    def test_bridge_reconnect_passthrough(self):
        bridge = RealtimeBridge.__new__(RealtimeBridge)
        bridge._client = mock.Mock()
        bridge.reconnect_now()
        bridge._client.reconnect_now.assert_called_once()


if __name__ == "__main__":
    unittest.main()
