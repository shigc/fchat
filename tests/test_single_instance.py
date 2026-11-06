"""单实例守卫：已有实例时二次启动只发激活消息，不再建窗口。

QLocalServer/QLocalSocket 在 offscreen 下同样可用（Windows 命名管道）。
"""
import os
import sys
import time
import unittest
import uuid
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402
from ui.single_instance import SingleInstance, instance_key  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _pump(pred, timeout=2.0):
    deadline = time.time() + timeout
    while not pred() and time.time() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    return pred()


class InstanceKeyTest(unittest.TestCase):

    def test_key_is_stable_and_config_scoped(self):
        with mock.patch.object(config, "CONFIG_DIR", "C:/tmp/fchat-a"):
            k1 = instance_key()
            k2 = instance_key()
        with mock.patch.object(config, "CONFIG_DIR", "C:/tmp/fchat-b"):
            k3 = instance_key()
        self.assertEqual(k1, k2)
        self.assertNotEqual(k1, k3)
        self.assertTrue(k1.startswith("fchat-"))


class SingleInstanceTest(unittest.TestCase):

    def setUp(self):
        self.key = f"fchat-test-{uuid.uuid4().hex}"
        self.guards = []

    def tearDown(self):
        for guard in self.guards:
            guard.stop()
            guard.deleteLater()
        QApplication.processEvents()

    def _guard(self):
        guard = SingleInstance(self.key)
        self.guards.append(guard)
        return guard

    @staticmethod
    def _notify(guard):
        return guard.notify_existing(connect_timeout_ms=100,
                                     ack_timeout_ms=100)

    def test_no_server_returns_false(self):
        self.assertFalse(self._notify(self._guard()))

    def test_second_instance_notifies_first(self):
        server = self._guard()
        self.assertTrue(server.start())
        fired = []
        server.set_activation_handler(lambda: fired.append(True))
        self.assertTrue(self._notify(self._guard()))
        self.assertTrue(_pump(lambda: bool(fired)))
        self.assertEqual(fired, [True])

    def test_activation_before_handler_is_replayed(self):
        server = self._guard()
        self.assertTrue(server.start())
        self.assertTrue(self._notify(self._guard()))
        self.assertTrue(_pump(lambda: server._pending))
        fired = []
        server.set_activation_handler(lambda: fired.append(True))
        self.assertEqual(fired, [True])
        self.assertFalse(server._pending)

    def test_stop_releases_name(self):
        server = self._guard()
        self.assertTrue(server.start())
        server.stop()
        QApplication.processEvents()
        self.assertFalse(self._notify(self._guard()))

    def test_start_is_idempotent(self):
        server = self._guard()
        self.assertTrue(server.start())
        self.assertTrue(server.start())


if __name__ == "__main__":
    unittest.main()
