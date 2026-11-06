"""实时连接横幅四态：ready / connected / reconnecting / disconnected。"""
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from ui.main_window import ChatArea  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class ConnectionBannerTest(unittest.TestCase):

    def setUp(self):
        self.area = ChatArea()
        self.area.resize(700, 500)

    def tearDown(self):
        self.area.deleteLater()

    def test_first_connect_before_ready_does_not_flash(self):
        self.area.set_connection_state("connected")
        self.assertFalse(self.area.conn_banner.isVisibleTo(self.area))

    def test_disconnected_shows_actionable_banner(self):
        self.area.set_connection_state("disconnected")
        self.assertTrue(self.area.conn_banner.isVisibleTo(self.area))
        self.assertTrue(self.area.conn_btn.isEnabled())
        self.assertIn("断开", self.area.conn_label.text())

    def test_ready_hides_and_marks_was_ready(self):
        self.area.set_connection_state("disconnected")
        self.area.set_connection_state("ready")
        self.assertFalse(self.area.conn_banner.isVisibleTo(self.area))
        self.assertTrue(self.area._realtime_was_ready)

    def test_reconnecting_after_ready_shows_progress(self):
        self.area.set_connection_state("ready")
        self.area.set_connection_state("reconnecting")
        self.assertTrue(self.area.conn_banner.isVisibleTo(self.area))
        self.assertTrue(self.area.conn_btn.isEnabled())
        self.assertIn("重新连接", self.area.conn_label.text())

    def test_connected_after_ready_syncs_with_disabled_button(self):
        self.area.set_connection_state("ready")
        self.area.set_connection_state("disconnected")
        self.area.set_connection_state("connected")
        self.assertTrue(self.area.conn_banner.isVisibleTo(self.area))
        self.assertFalse(self.area.conn_btn.isEnabled())
        self.assertIn("同步", self.area.conn_label.text())

    def test_reconnect_button_emits(self):
        got = []
        self.area.reconnect_clicked.connect(lambda: got.append(True))
        self.area.set_connection_state("disconnected")
        # 必须 show 后 click 才走真实事件；offscreen 下 click() 直接触发
        QTest.mouseClick(self.area.conn_btn, Qt.LeftButton)
        self.assertEqual(got, [True])

    def test_reset_clears_was_ready(self):
        self.area.set_connection_state("ready")
        self.area.reset_connection_state()
        self.assertFalse(self.area._realtime_was_ready)
        # 复位后首次 connected 不弹横幅
        self.area.set_connection_state("connected")
        self.assertFalse(self.area.conn_banner.isVisibleTo(self.area))


if __name__ == "__main__":
    unittest.main()
