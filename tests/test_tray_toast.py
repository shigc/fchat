"""托盘 Windows 通知：showMessage 传参、点击回跳、无 API 降级、免打扰抑制。

托盘控制器注入 fake tray（带 showMessage/messageClicked），offscreen、不联网。
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QObject, QRect, pyqtSignal  # noqa: E402
from PyQt5.QtGui import QIcon  # noqa: E402
from PyQt5.QtWidgets import QApplication, QMenu, QSystemTrayIcon  # noqa: E402

import config  # noqa: E402
from core.models import Conversation, Message, User  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.tray import TrayController  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class _ToastTray(QObject):
    """带 Windows toast 接口的 fake tray。"""

    activated = pyqtSignal(int)
    messageClicked = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.messages = []
        self.icons = []
        self.tooltips = []

    def geometry(self):
        return QRect()

    def setIcon(self, icon):
        self.icons.append(icon)

    def icon(self):
        return self.icons[-1] if self.icons else QIcon()

    def setToolTip(self, text):
        self.tooltips.append(text)

    def setContextMenu(self, menu):
        pass

    def show(self):
        pass

    def hide(self):
        pass

    def showMessage(self, title, text, icon, timeout):
        self.messages.append((title, text, icon, timeout))


class _BareTray(QObject):
    """老式 fake tray：没有 showMessage / messageClicked。"""

    activated = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self.icons = []
        self.tooltips = []

    def geometry(self):
        return QRect()

    def setIcon(self, icon):
        self.icons.append(icon)

    def setToolTip(self, text):
        self.tooltips.append(text)

    def setContextMenu(self, menu):
        pass

    def show(self):
        pass

    def hide(self):
        pass


class _FakeCard(QObject):
    row_activated = pyqtSignal(str, str)

    def set_theme(self, theme):
        pass

    def set_rows(self, rows, overflow=0):
        pass

    def show_near(self, rect):
        pass

    def hide(self):
        pass

    def isVisible(self):
        return False


class TrayToastTest(unittest.TestCase):

    def _controller(self, tray):
        return TrayController(None, None, tray_icon=tray,
                              card=_FakeCard(), menu=QMenu())

    def test_show_message_toast_forwards_and_remembers_target(self):
        tray = _ToastTray()
        ctl = self._controller(tray)
        ctl.show_message_toast("房间", "小明：你好", "r1", "m1")
        self.assertEqual(len(tray.messages), 1)
        title, text, icon, timeout = tray.messages[0]
        self.assertEqual(title, "房间")
        self.assertEqual(text, "小明：你好")
        self.assertEqual(icon, QSystemTrayIcon.Information)
        self.assertGreater(timeout, 0)
        emitted = []
        ctl.message_activated.connect(lambda rid, mid: emitted.append((rid, mid)))
        tray.messageClicked.emit()
        self.assertEqual(emitted, [("r1", "m1")])

    def test_toast_click_uses_latest_target(self):
        tray = _ToastTray()
        ctl = self._controller(tray)
        ctl.show_message_toast("A", "a", "r1", "m1")
        ctl.show_message_toast("B", "b", "r2", "m2")
        emitted = []
        ctl.message_activated.connect(lambda rid, mid: emitted.append((rid, mid)))
        tray.messageClicked.emit()
        self.assertEqual(emitted, [("r2", "m2")])

    def test_fake_tray_without_toast_api_does_not_crash(self):
        tray = _BareTray()
        ctl = self._controller(tray)
        ctl.show_message_toast("房间", "正文", "r1", "m1")
        # 没有 showMessage 就静默跳过，也不能留下会误发的 target
        self.assertEqual(ctl._toast_target, ("", ""))

    def test_empty_rid_does_not_emit(self):
        tray = _ToastTray()
        ctl = self._controller(tray)
        ctl.show_message_toast("房间", "正文", "", "")
        emitted = []
        ctl.message_activated.connect(lambda rid, mid: emitted.append((rid, mid)))
        tray.messageClicked.emit()
        self.assertEqual(emitted, [])

    def test_shutdown_disables_toast(self):
        tray = _ToastTray()
        ctl = self._controller(tray)
        ctl.show_message_toast("房间", "正文", "r1", "m1")
        ctl.shutdown()
        self.assertEqual(ctl._toast_target, ("", ""))
        ctl.show_message_toast("房间2", "正文2", "r2", "m2")
        self.assertEqual(len(tray.messages), 1)


class _ToastWindow(MainWindow):
    """跳过登录，启用托盘并注入带 toast 的 fake tray。"""

    def _try_auto_login(self):
        self.auto_login_skipped = True

    def _tray_available(self):
        return True

    def _create_tray_controller(self):
        self.fake_tray = _ToastTray()
        self.tray_ctl = TrayController(
            self.theme_manager, self, tray_icon=self.fake_tray,
            card=_FakeCard(), menu=QMenu())
        return self.tray_ctl


class MainWindowToastTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = self._tmp.name
        self._orig = (config.CONFIG_DIR, config.CONFIG_FILE,
                      config.KEY_FILE, config.LEGACY_INI_FILE)
        config.CONFIG_DIR = os.path.join(tmp, "cfgdir")
        config.CONFIG_FILE = os.path.join(config.CONFIG_DIR, "config.yaml")
        config.KEY_FILE = os.path.join(config.CONFIG_DIR, "secret.key")
        config.LEGACY_INI_FILE = os.path.join(tmp, "config.ini")
        self.app = QApplication.instance()
        self._quit_on_last = self.app.quitOnLastWindowClosed()
        self.win = _ToastWindow()
        self.win.resize(1000, 680)

    def tearDown(self):
        if self.win.realtime:
            self.win.realtime.stop()
        self.win._tray and self.win._tray.shutdown()
        self.win.deleteLater()
        self.app.setQuitOnLastWindowClosed(self._quit_on_last)
        (config.CONFIG_DIR, config.CONFIG_FILE,
         config.KEY_FILE, config.LEGACY_INI_FILE) = self._orig
        self._tmp.cleanup()

    def _msg(self, rid="r1", mid="m1", mine=False, mtype="text", text="hi",
             **kwargs):
        sender = User(username="u", name="用户")
        return Message(id=mid, room_id=rid, text=text, sender=sender,
                       timestamp=time.time(), msg_type=mtype, is_mine=mine,
                       **kwargs)

    def test_alert_shows_toast(self):
        self.assertTrue(self.win._add_tray_alert(self._msg(), "房间一"))
        self.assertEqual(len(self.win.fake_tray.messages), 1)
        title, text, _icon, _timeout = self.win.fake_tray.messages[0]
        self.assertEqual(title, "房间一")
        self.assertEqual(text, "用户：hi")

    def test_mention_alert_prefixes_toast(self):
        self.assertTrue(self.win._add_tray_alert(
            self._msg(text="@我 看一下", mentions_me=True), "房间一"))
        _title, text, _icon, _timeout = self.win.fake_tray.messages[0]
        self.assertTrue(text.startswith("[@你] "))

    def test_duplicate_alert_does_not_retoast(self):
        self.assertTrue(self.win._add_tray_alert(self._msg(), "房间一"))
        self.assertFalse(self.win._add_tray_alert(self._msg(), "房间一"))
        self.assertEqual(len(self.win.fake_tray.messages), 1)

    def test_muted_room_suppresses_toast(self):
        self.win._conversations = {
            "r1": Conversation(id="r1", name="房间一", muted=True)}
        self.assertFalse(self.win._add_tray_alert(self._msg(), "房间一"))
        self.assertEqual(self.win.fake_tray.messages, [])

    def test_fake_tray_without_toast_api_still_queues_alert(self):
        # 老 fake tray 没有 toast 接口时只丢通知，卡片提醒照常
        bare = _BareTray()
        self.win._tray.shutdown()
        ctl = TrayController(None, None, tray_icon=bare,
                             card=_FakeCard(), menu=QMenu())
        self.win._tray = ctl
        self.assertTrue(self.win._add_tray_alert(self._msg(), "房间一"))
        self.assertEqual(len(self.win._tray_alerts), 1)


if __name__ == "__main__":
    unittest.main()
