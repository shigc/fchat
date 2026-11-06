"""第一批 UI 优化：通知设置 + 发送方式配置 测试。

覆盖：config 层 save_notify/save_send_mode 往返、settings_page 新控件回写、
chat_input 的 set_send_mode 快捷键切换。GUI 测试 offscreen 渲染，不联网。
运行：python -m unittest tests.test_notify_sendmode -v
"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtGui import QKeySequence  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402
from tests.helpers import KeyringIsolatedTestCase  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class TestNotifyConfig(KeyringIsolatedTestCase):
    """config 层通知与发送方式配置往返。"""

    def test_defaults(self):
        cfg = config.load_config()
        self.assertTrue(cfg["notify_enabled"])
        self.assertFalse(cfg["notify_mentions_only"])
        self.assertEqual(cfg["send_mode"], "enter")

    def test_save_notify_roundtrip(self):
        config.save_notify(enabled=False, mentions_only=True)
        cfg = config.load_config()
        self.assertFalse(cfg["notify_enabled"])
        self.assertTrue(cfg["notify_mentions_only"])

    def test_save_notify_partial(self):
        # 只改 mentions_only，enabled 保持默认
        config.save_notify(mentions_only=True)
        cfg = config.load_config()
        self.assertTrue(cfg["notify_enabled"])
        self.assertTrue(cfg["notify_mentions_only"])

    def test_save_send_mode_roundtrip(self):
        config.save_send_mode("ctrlenter")
        cfg = config.load_config()
        self.assertEqual(cfg["send_mode"], "ctrlenter")

    def test_notify_persisted_in_yaml(self):
        config.save_notify(enabled=False, mentions_only=True)
        config.save_send_mode("ctrlenter")
        with open(config.CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = f.read()
        self.assertIn("enabled:false", raw.replace(" ", ""))
        self.assertIn("mentions_only:true", raw.replace(" ", ""))
        self.assertIn("mode:ctrlenter", raw.replace(" ", ""))


class TestChatInputSendMode(unittest.TestCase):
    """chat_input 的 set_send_mode 切换回车发送/换行行为。

    回车发送/换行的判定在 ChatInputEdit.keyPressEvent 里按 _send_mode 做：
    QTextEdit 对 Return 键有内建换行处理且优先级高于 QShortcut，不能用全局
    快捷键拦截（真机实测），必须在按键事件层拦截。这里用 QTest.keyClick
    模拟真实按键，验证 send_requested 信号与换行行为。
    """

    def setUp(self):
        from ui.chat_input import ChatInput
        self.input = ChatInput()
        # 直接观察 ChatInputEdit 的发送信号（_send 会触发发送链路，测试
        # 只关心 keyPressEvent 是否正确判定「发送」）
        self.sent = []
        self.input.input.send_requested.connect(
            lambda: self.sent.append(True))

    def tearDown(self):
        self.input.deleteLater()

    def _key(self, key, modifier=Qt.NoModifier):
        QTest.keyClick(self.input.input, key, modifier)

    def test_default_enter_sends_on_return(self):
        self.assertEqual(self.input.input._send_mode, "enter")
        self._key(Qt.Key_Return)
        self.assertEqual(self.sent, [True], "enter 模式回车应发送")

    def test_enter_shift_return_newline(self):
        self._key(Qt.Key_Return, Qt.ShiftModifier)
        self.assertEqual(self.sent, [], "Shift+回车应换行而非发送")
        self.assertIn("\n", self.input.input.toPlainText())

    def test_switch_to_ctrlenter(self):
        self.input.set_send_mode("ctrlenter")
        self.assertEqual(self.input.input._send_mode, "ctrlenter")
        # Ctrl+回车发送
        self._key(Qt.Key_Return, Qt.ControlModifier)
        self.assertEqual(self.sent, [True], "ctrlenter 模式 Ctrl+回车应发送")
        # 普通回车换行
        self._key(Qt.Key_Return)
        self.assertEqual(len(self.sent), 1, "ctrlenter 模式普通回车应换行")
        self.assertIn("\n", self.input.input.toPlainText())

    def test_switch_back_to_enter(self):
        self.input.set_send_mode("ctrlenter")
        self.input.set_send_mode("enter")
        self.assertEqual(self.input.input._send_mode, "enter")
        self._key(Qt.Key_Return)
        self.assertEqual(self.sent, [True], "切回 enter 后回车应发送")


class TestSettingsPageNotifySendMode(unittest.TestCase):
    """settings_page 的通知开关与发送方式下拉框回写。"""

    def setUp(self):
        from ui.settings_page import SettingsPage
        self.page = SettingsPage()

    def tearDown(self):
        self.page.deleteLater()

    def test_set_notify(self):
        self.page.set_notify(True)
        self.assertTrue(self.page.notify_switch.isChecked())
        self.page.set_notify(False)
        self.assertFalse(self.page.notify_switch.isChecked())

    def test_set_notify_mentions(self):
        self.page.set_notify_mentions(True)
        self.assertTrue(self.page.notify_mentions_switch.isChecked())
        self.page.set_notify_mentions(False)
        self.assertFalse(self.page.notify_mentions_switch.isChecked())

    def test_set_send_mode(self):
        self.page.set_send_mode("ctrlenter")
        self.assertEqual(
            self.page.send_mode_combo.itemData(
                self.page.send_mode_combo.currentIndex()), "ctrlenter")
        self.page.set_send_mode("enter")
        self.assertEqual(
            self.page.send_mode_combo.itemData(
                self.page.send_mode_combo.currentIndex()), "enter")

    def test_send_mode_changed_signal(self):
        captured = []
        self.page.send_mode_changed.connect(captured.append)
        self.page.set_send_mode("ctrlenter")
        self.page.send_mode_combo.setCurrentIndex(
            self.page.send_mode_combo.findData("enter"))
        self.assertEqual(captured, ["enter"])
