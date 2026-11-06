"""主窗口与托盘的接线：关闭到托盘、退出分流、未读提醒、点击跳转。

托盘控制器注入 fake tray/card/menu，全程 offscreen、不联网。
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QEvent, QObject, QRect, pyqtSignal  # noqa: E402
from PyQt5.QtGui import QIcon  # noqa: E402
from PyQt5.QtWidgets import QApplication, QMenu  # noqa: E402

import config  # noqa: E402
from core.models import Conversation, Message, User  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.tray import TrayController  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class _FakeTray(QObject):
    activated = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self._geometry = QRect()
        self.icons = []
        self.tooltips = []
        self.context_menu = None
        self.visible = False

    def geometry(self):
        return self._geometry

    def setIcon(self, icon):
        self.icons.append(icon)

    def icon(self):
        return self.icons[-1] if self.icons else QIcon()

    def setToolTip(self, text):
        self.tooltips.append(text)

    def setContextMenu(self, menu):
        self.context_menu = menu

    def show(self):
        self.visible = True

    def hide(self):
        self.visible = False


class _FakeCard(QObject):
    row_activated = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self.rows = []
        self.overflow = 0
        self.visible = False
        self.theme = None

    def set_theme(self, theme):
        self.theme = theme

    def set_rows(self, rows, overflow=0):
        self.rows = list(rows)
        self.overflow = overflow

    def show_near(self, rect):
        self.visible = True

    def hide(self):
        self.visible = False

    def isVisible(self):
        return self.visible

    def frameGeometry(self):
        return QRect()


class _TrayWindow(MainWindow):
    """跳过登录，并强制启用托盘 + 注入 fake 托盘组件。"""

    def _try_auto_login(self):
        self.auto_login_skipped = True

    def _tray_available(self):
        return True

    def _create_tray_controller(self):
        self.fake_tray = _FakeTray()
        self.fake_card = _FakeCard()
        self.fake_menu = QMenu()
        self.tray_ctl = TrayController(
            self.theme_manager, self, tray_icon=self.fake_tray,
            card=self.fake_card, menu=self.fake_menu)
        return self.tray_ctl


class TrayWiringTest(unittest.TestCase):

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
        self.win = _TrayWindow()
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

    def _msg(self, rid="r1", mid="m1", mine=False, mtype="text", text="hi"):
        return Message(
            id=mid, room_id=rid, text=text,
            sender=User(username="u", name="用户"),
            timestamp=time.time(), msg_type=mtype, is_mine=mine)

    # ---- 生命周期 ---- #
    def test_setup_tray_created(self):
        self.assertIsNotNone(self.win._tray)
        self.assertTrue(self.win.fake_tray.visible)
        self.assertIs(self.win.fake_menu, self.win.fake_tray.context_menu)
        self.assertFalse(self.app.quitOnLastWindowClosed())

    def test_close_hides_to_tray_when_logged_in(self):
        self.win.client = mock.Mock()
        self.win.realtime = mock.Mock()
        with mock.patch.object(self.win, "hide") as hidden:
            ev = mock.Mock()
            self.win.closeEvent(ev)
        ev.ignore.assert_called_once()
        ev.accept.assert_not_called()
        hidden.assert_called_once()
        self.win.realtime.stop.assert_not_called()
        self.assertFalse(self.win._closing)

    def test_close_quits_when_not_logged_in(self):
        self.win.client = None
        with mock.patch.object(self.win, "_request_quit") as req:
            ev = mock.Mock()
            self.win.closeEvent(ev)
        ev.accept.assert_called_once()
        ev.ignore.assert_not_called()
        req.assert_called_once()
        self.assertTrue(self.win._closing)
        self.assertFalse(self.win.fake_tray.visible)

    def test_force_quit_does_cleanup(self):
        self.win.client = mock.Mock()
        self.win.realtime = mock.Mock()
        self.win._force_quit = True
        with mock.patch.object(self.win, "_request_quit") as req:
            ev = mock.Mock()
            self.win.closeEvent(ev)
        ev.accept.assert_called_once()
        ev.ignore.assert_not_called()
        self.win.realtime.stop.assert_called_once()
        req.assert_called_once()
        self.assertFalse(self.win.fake_tray.visible)

    def test_second_close_accepts_and_quits(self):
        self.win._closing = True
        with mock.patch.object(self.win, "_request_quit") as req:
            ev = mock.Mock()
            self.win.closeEvent(ev)
        ev.accept.assert_called_once()
        ev.ignore.assert_not_called()
        req.assert_called_once()

    # ---- 未读提醒 ---- #
    def test_add_alert_filters(self):
        self.assertFalse(self.win._add_tray_alert(
            self._msg(mid="a", mine=True), "R"))
        self.assertFalse(self.win._add_tray_alert(
            self._msg(mid="b", mtype="system"), "R"))
        self.assertFalse(self.win._add_tray_alert(
            self._msg(mid="c", text=""), "R"))
        self.assertEqual(len(self.win._tray_alerts), 0)

    def test_add_alert_skips_foreground_current_room(self):
        self.win._active_room_id = "r1"
        with mock.patch.object(self.win, "_is_chat_foreground",
                               return_value=True):
            self.assertFalse(self.win._add_tray_alert(self._msg(), "R"))
        with mock.patch.object(self.win, "_is_chat_foreground",
                               return_value=False):
            self.assertTrue(self.win._add_tray_alert(self._msg(), "R"))

    def test_add_alert_dedupes_by_message_id(self):
        self.assertTrue(self.win._add_tray_alert(self._msg(), "R"))
        self.assertFalse(self.win._add_tray_alert(self._msg(), "R"))
        self.assertEqual(len(self.win._tray_alerts), 1)

    def test_realtime_message_unknown_room_queues_alert(self):
        self.win.client = mock.Mock()
        self.win._load_conversations = mock.Mock()
        self.win._on_realtime_message(self._msg(rid="new", mid="m9"))
        self.win._load_conversations.assert_called_once()
        self.assertEqual(len(self.win._tray_alerts), 1)
        self.assertEqual(len(self.win.fake_card.rows), 1)
        self.assertEqual(self.win.fake_card.rows[0].room, "")

    def test_realtime_message_known_room_updates_unread_and_card(self):
        self.win.client = mock.Mock()
        conv = Conversation(id="r1", name="房间一")
        self.win._conversations = {"r1": conv}
        self.win._active_room_id = "other"
        # 列表为空时 upsert 会重建并自动选中首行（真实场景列表非空，
        # 那句选中会触发 _open_conversation 把未读清零）
        self.win.conversation_list.blockSignals(True)
        try:
            self.win._on_realtime_message(self._msg())
        finally:
            self.win.conversation_list.blockSignals(False)
        self.assertEqual(conv.unread, 1)
        self.assertEqual(self.win.fake_card.rows[0].room, "房间一")
        self.assertEqual(self.win.fake_card.rows[0].msg_id, "m1")

    def test_subscription_zero_unread_drops_alerts(self):
        conv = Conversation(id="a", name="A", unread=5)
        self.win._conversations = {"a": conv}
        self.win._active_room_id = "other"
        self.assertTrue(self.win._add_tray_alert(self._msg("a", "m1"), "A"))
        self.win._on_pushed_subscription(
            "updated", {"rid": "a", "unread": 0})
        self.assertEqual(len(self.win._tray_alerts), 0)

    def test_subscription_current_room_keeps_alerts(self):
        conv = Conversation(id="a", name="A", unread=5)
        self.win._conversations = {"a": conv}
        self.win._active_room_id = "a"
        with mock.patch.object(self.win, "_is_chat_foreground",
                               return_value=False):
            self.assertTrue(self.win._add_tray_alert(
                self._msg("a", "m1"), "A"))
        self.win._on_pushed_subscription(
            "updated", {"rid": "a", "unread": 0})
        # 当前会话被强制 unread=0，不能据此删提醒
        self.assertEqual(len(self.win._tray_alerts), 1)

    # ---- 点击跳转 ---- #
    def test_activated_known_conversation_opens_and_flashes(self):
        conv = Conversation(id="r1", name="R")
        self.win._conversations = {"r1": conv}
        self.assertTrue(self.win._add_tray_alert(self._msg(), "R"))
        with mock.patch.object(self.win, "_restore_from_tray") as restore, \
                mock.patch.object(self.win, "_show_chat_tab") as tab, \
                mock.patch.object(self.win, "_open_conversation") as opened, \
                mock.patch.object(self.win, "_flash_when_ready") as flash, \
                mock.patch.object(self.win, "_refresh_tray_state") as refresh:
            self.win._on_tray_message_activated("r1", "m1")
        restore.assert_called_once()
        tab.assert_called_once()
        opened.assert_called_once_with(conv)
        flash.assert_called_once_with("r1", "m1")
        refresh.assert_called_once()
        self.assertEqual(len(self.win._tray_alerts), 0)

    def test_activated_unknown_conversation_reloads(self):
        self.win._conversations = {}
        with mock.patch.object(self.win, "_restore_from_tray"), \
                mock.patch.object(self.win, "_show_chat_tab"), \
                mock.patch.object(self.win, "_reload_and_open") as reload_, \
                mock.patch.object(self.win, "_refresh_tray_state"):
            self.win._on_tray_message_activated("new", "m9")
        reload_.assert_called_once_with("new", flash_msg_id="m9")

    def test_flash_when_ready_hits_model(self):
        self.win._active_room_id = "r1"
        view = self.win.chat_area.chat_view
        with mock.patch.object(view.model, "find_message_row",
                               return_value=3), \
                mock.patch.object(view, "flash_row") as flash:
            self.win._flash_when_ready("r1", "m1")
        flash.assert_called_once_with("m1")

    # ---- 登出 / 主题 / 激活 ---- #
    def test_logout_pending_login_keeps_running(self):
        self.win.client = mock.Mock()
        self.win._show_login = lambda *a, **k: None
        self.win._login_pending = True
        self.assertTrue(self.win._add_tray_alert(self._msg(), "R"))
        with mock.patch.object(self.win, "close") as closed:
            self.win._on_logout_requested()
        closed.assert_not_called()
        self.assertEqual(len(self.win._tray_alerts), 0)
        self.assertEqual(self.win.fake_card.rows, [])

    def test_logout_without_relogin_quits(self):
        self.win.client = mock.Mock()
        self.win._show_login = lambda *a, **k: None
        self.win._login_pending = False
        with mock.patch.object(self.win, "_quit_app") as quit_:
            self.win._on_logout_requested()
        quit_.assert_called_once()

    def test_theme_change_updates_tray(self):
        with mock.patch.object(self.win._tray, "set_theme") as set_theme:
            self.win._on_theme_changed("dark")
        set_theme.assert_called_once_with(self.win.theme_manager)

    def test_activation_drops_current_room_alerts(self):
        self.win._active_room_id = "r1"
        with mock.patch.object(self.win, "_is_chat_foreground",
                               return_value=False):
            self.assertTrue(self.win._add_tray_alert(self._msg(), "R"))
        self.win._tray.start_alert()
        with mock.patch.object(self.win, "isVisible", return_value=True), \
                mock.patch.object(self.win, "isMinimized", return_value=False), \
                mock.patch.object(self.win, "isActiveWindow",
                                  return_value=True):
            self.win.changeEvent(QEvent(QEvent.ActivationChange))
        self.assertFalse(self.win._tray._alerting)
        self.assertEqual(len(self.win._tray_alerts), 0)

    def test_tray_open_main_switches_to_chat_tab(self):
        # 「打开主程序」和点托盘图标都应落到聊天页，而不是停在设置页
        self.win._show_settings_tab()
        self.win.tray_ctl.show_main_requested.emit()
        self.assertEqual(self.win.content_stack.currentIndex(), 0)
        self.assertFalse(self.win.conversation_panel.isHidden())
        self.assertTrue(self.win.sidebar._buttons["chats"].isChecked())

    def test_programmatic_tab_switch_syncs_sidebar(self):
        # 托盘「打开配置」不走左侧按钮点击，两侧高亮必须手动同步
        self.win._show_settings_tab()
        self.assertTrue(self.win.sidebar._buttons["settings"].isChecked())
        self.assertFalse(self.win.sidebar._buttons["chats"].isChecked())
        self.win._show_chat_tab()
        self.assertTrue(self.win.sidebar._buttons["chats"].isChecked())
        self.assertFalse(self.win.sidebar._buttons["settings"].isChecked())

    def test_show_chat_tab_drops_current_room_alerts(self):
        self.win._active_room_id = "r1"
        with mock.patch.object(self.win, "_is_chat_foreground",
                               return_value=False):
            self.assertTrue(self.win._add_tray_alert(self._msg(), "R"))
        self.win._show_chat_tab()
        self.assertEqual(len(self.win._tray_alerts), 0)


if __name__ == "__main__":
    unittest.main()
