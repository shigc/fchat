"""系统托盘：行聚合、预览卡片、闪烁/悬停/菜单控制器。

全部 offscreen 运行：QSystemTrayIcon 不可用，这里用 fake tray 注入。
"""
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QObject, QPoint, QRect, Qt, pyqtSignal  # noqa: E402
from PyQt5.QtGui import QColor, QIcon  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import (  # noqa: E402
    QApplication, QMenu, QSystemTrayIcon,
)

from ui.theme import ThemeManager  # noqa: E402
from ui.tray import (  # noqa: E402
    TrayController, TrayPreviewCard, TrayRow, build_tray_rows,
)

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class _Conv:
    def __init__(self, rid, name="", unread=0, text="", ts=0.0, muted=False):
        self.id = rid
        self.name = name
        self.unread = unread
        self.last_message_text = text
        self.last_message_time = ts
        self.muted = muted


def _alert(rid, msg_id, ts, text="", room="", sender=""):
    return {"rid": rid, "msg_id": msg_id, "room": room or rid,
            "sender": sender, "text": text or msg_id, "ts": ts}


class BuildTrayRowsTest(unittest.TestCase):

    def test_empty(self):
        self.assertEqual(build_tray_rows([], []), ([], 0))

    def test_newest_first_and_dedup_by_msg_id(self):
        alerts = [
            _alert("a", "m1", 100),
            _alert("b", "m2", 300),
            _alert("a", "m1", 100),     # 同 id 回推 -> 去重
        ]
        rows, overflow = build_tray_rows(alerts, [])
        self.assertEqual([r.msg_id for r in rows], ["m2", "m1"])
        self.assertEqual(overflow, 0)

    def test_active_room_excluded(self):
        alerts = [_alert("a", "m1", 100), _alert("cur", "m2", 200)]
        convs = [_Conv("cur", "当前", unread=4)]
        rows, overflow = build_tray_rows(alerts, convs, active_rid="cur")
        self.assertEqual([r.rid for r in rows], ["a"])
        self.assertEqual(overflow, 0)

    def test_fallback_summary_for_uncovered_unread_rooms(self):
        convs = [
            _Conv("a", "A", unread=2, text="hi", ts=50),
            _Conv("b", "B", unread=3, text="yo", ts=90),
        ]
        rows, overflow = build_tray_rows([_alert("a", "m1", 100)], convs)
        self.assertEqual([(r.rid, r.msg_id) for r in rows],
                         [("a", "m1"), ("b", "")])
        # a 的会话未读 2 与消息行 1 取大，b 汇总 3；显示 1+3
        self.assertEqual(overflow, 1)

    def test_limit_and_overflow(self):
        alerts = [_alert("a", f"m{i}", i) for i in range(10)]
        rows, overflow = build_tray_rows(
            alerts, [_Conv("a", "A", unread=10)], limit=6)
        self.assertEqual(len(rows), 6)
        self.assertEqual(overflow, 4)

    def test_alert_only_room_counts(self):
        rows, overflow = build_tray_rows([_alert("new", "m1", 1)], [])
        self.assertEqual([r.rid for r in rows], ["new"])
        self.assertEqual(overflow, 0)

    def test_muted_room_alerts_and_unread_excluded(self):
        alerts = [_alert("m", "m1", 100), _alert("a", "m2", 200)]
        convs = [_Conv("m", "静音", unread=5, muted=True),
                 _Conv("a", "普通", unread=2)]
        rows, overflow = build_tray_rows(alerts, convs)
        self.assertEqual([r.rid for r in rows], ["a"])
        # 静音房间的 5 条未读不计入合计
        self.assertEqual(overflow, 1)

    def test_muted_room_fallback_row_excluded(self):
        convs = [_Conv("m", "静音", unread=9, text="hi", ts=100, muted=True)]
        rows, overflow = build_tray_rows([], convs)
        self.assertEqual(rows, [])
        self.assertEqual(overflow, 0)


class TrayPreviewCardTest(unittest.TestCase):

    def setUp(self):
        self.theme = ThemeManager()
        self.card = TrayPreviewCard()
        self.card.set_theme(self.theme)
        self.rows = [
            TrayRow(rid="a", msg_id="m1", room="房间A", sender="张三",
                    text="你好", ts=100),
            TrayRow(rid="b", msg_id="m2", room="房间B", sender="李四",
                    text="世界", ts=200),
        ]

    def tearDown(self):
        self.card.hide()
        self.card.deleteLater()

    def test_empty_rows_hides(self):
        self.card.set_rows(self.rows, 0)
        self.card.show()
        self.assertTrue(self.card.isVisible())
        self.card.set_rows([], 0)
        self.assertFalse(self.card.isVisible())

    def test_rows_and_footer(self):
        self.card.set_rows(self.rows, 3)
        self.assertEqual(len(self.card._row_widgets), 2)
        self.assertFalse(self.card._footer.isHidden())
        self.assertIn("3", self.card._footer.text())
        self.card.set_rows(self.rows, 0)
        self.assertTrue(self.card._footer.isHidden())

    def test_click_emits_row_activated(self):
        self.card.set_rows(self.rows, 0)
        got = []
        self.card.row_activated.connect(
            lambda rid, mid: got.append((rid, mid)))
        QTest.mouseClick(self.card._row_widgets[0], Qt.LeftButton)
        self.assertEqual(got, [("a", "m1")])
        self.assertFalse(self.card.isVisible())     # 点完立即收起卡片

    def test_message_text_is_plain_text(self):
        # ts=0 让时间留空：offscreen 字体度量下标题容易被挤成省略号，
        # 这里要断言的是文本没被当富文本解析，不是省略行为
        row = TrayRow(rid="a", msg_id="m1", room="<b>房</b>",
                      sender="", text="<b>加粗</b>", ts=0)
        self.card.set_rows([row], 0)
        widget = self.card._row_widgets[0]
        for lb in (widget._title, widget._preview):
            self.assertEqual(lb.textFormat(), Qt.PlainText)
        self.assertEqual(widget._title.text(), "<b>房</b>")
        self.assertEqual(widget._preview.text(), "<b>加粗</b>")

    def test_show_near_flips_above_and_clamps(self):
        self.card.set_rows(self.rows, 0)
        screen = QApplication.primaryScreen().availableGeometry()
        # 屏幕底部：翻到图标上方
        bottom = QRect(screen.center().x(), screen.bottom() - 2, 24, 24)
        self.card.show_near(bottom)
        self.assertLessEqual(self.card.geometry().bottom(), bottom.top())
        # 贴右缘：钳回屏内
        right = QRect(screen.right() - 4, screen.center().y(), 4, 24)
        self.card.show_near(right)
        self.assertLessEqual(self.card.geometry().right(), screen.right())
        self.assertGreaterEqual(self.card.geometry().left(), screen.left())

    def test_window_flags(self):
        self.assertTrue(bool(self.card.windowFlags() & Qt.Tool))
        self.assertTrue(self.card.testAttribute(Qt.WA_ShowWithoutActivating))


class _FakeTray(QObject):
    activated = pyqtSignal(int)

    def __init__(self, geometry=None):
        super().__init__()
        self._geometry = geometry or QRect(100, 100, 24, 24)
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


class TrayControllerTest(unittest.TestCase):

    def setUp(self):
        self.theme = ThemeManager()
        self.tray = _FakeTray()
        self.card = TrayPreviewCard()
        self.menu = QMenu()
        self.ctl = TrayController(
            self.theme, tray_icon=self.tray, card=self.card, menu=self.menu)
        self.row = TrayRow(rid="a", msg_id="m1", room="A",
                           sender="x", text="hi", ts=100)

    def tearDown(self):
        self.ctl.shutdown()
        self.menu.deleteLater()
        self.card.deleteLater()

    def _point_at_tray(self):
        self.ctl._cursor_pos = staticmethod(
            lambda: self.tray.geometry().center())

    def test_update_state_sets_icon_and_tooltip(self):
        self.ctl.update_state(True, 1, [self.row], 0)
        self.assertIs(self.tray.icons[-1], self.ctl._dot_icon)
        self.assertEqual(self.tray.tooltips[-1], "")   # geometry 有效
        self.ctl.update_state(False, 0, [], 0)
        self.assertIs(self.tray.icons[-1], self.ctl._normal_icon)

    def test_invalid_geometry_uses_tooltip_fallback(self):
        self.tray._geometry = QRect()
        self.ctl.update_state(True, 2, [self.row], 1)
        self.assertIn("2", self.tray.tooltips[-1])
        self.assertIn("A", self.tray.tooltips[-1])

    def test_hover_shows_after_three_ticks_hides_after_two(self):
        self.ctl.update_state(True, 1, [self.row], 0)
        self._point_at_tray()
        self.ctl._hover_tick()
        self.ctl._hover_tick()
        self.assertFalse(self.card.isVisible())
        self.ctl._hover_tick()
        self.assertTrue(self.card.isVisible())
        # 移到卡片内保持显示
        self.ctl._cursor_pos = staticmethod(
            lambda: self.card.frameGeometry().center())
        self.ctl._hover_tick()
        self.ctl._hover_tick()
        self.assertTrue(self.card.isVisible())
        # 移开两拍后关闭
        self.ctl._cursor_pos = staticmethod(lambda: QPoint(799, 599))
        self.ctl._hover_tick()
        self.assertTrue(self.card.isVisible())
        self.ctl._hover_tick()
        self.assertFalse(self.card.isVisible())

    def test_invalid_geometry_never_shows_card(self):
        self.tray._geometry = QRect()
        self.ctl.update_state(True, 1, [self.row], 0)
        self._point_at_tray()
        for _ in range(5):
            self.ctl._hover_tick()
        self.assertFalse(self.card.isVisible())

    def test_flash_toggles_icons(self):
        self.ctl.update_state(True, 1, [self.row], 0)
        self.ctl.start_alert()
        self.assertTrue(self.ctl._flash_timer.isActive())
        # 闪烁帧用整块红色的 alert 图标，静态未读才用小红点
        self.assertIs(self.tray.icons[-1], self.ctl._alert_icon)
        self.ctl._flash_tick()
        self.assertIs(self.tray.icons[-1], self.ctl._normal_icon)
        self.ctl._flash_tick()
        self.assertIs(self.tray.icons[-1], self.ctl._alert_icon)
        self.ctl.stop_alert()
        self.assertFalse(self.ctl._flash_timer.isActive())
        self.assertIs(self.tray.icons[-1], self.ctl._dot_icon)

    def test_menu_suppresses_hover_with_cooldown(self):
        self.ctl.update_state(True, 1, [self.row], 0)
        self._point_at_tray()
        self.ctl._hover_tick()
        self.ctl._hover_tick()
        self.ctl._hover_tick()
        self.assertTrue(self.card.isVisible())
        self.ctl._on_menu_about_to_show()
        self.assertFalse(self.card.isVisible())
        for _ in range(5):
            self.ctl._hover_tick()
        self.assertFalse(self.card.isVisible())
        self.ctl._on_menu_about_to_hide()
        self.ctl._hover_tick()
        self.assertFalse(self.card.isVisible())     # 冷却期内不弹
        self.ctl._suppress_hover_until = 0
        for _ in range(3):
            self.ctl._hover_tick()
        self.assertTrue(self.card.isVisible())

    def test_menu_actions_emit_signals(self):
        texts = [a.text() for a in self.menu.actions() if not a.isSeparator()]
        self.assertEqual(
            texts, ["打开主程序", "打开配置", "消息提醒", "关闭程序"])
        got = []
        self.ctl.show_main_requested.connect(lambda: got.append("main"))
        self.ctl.open_settings_requested.connect(
            lambda: got.append("settings"))
        self.ctl.reminders_requested.connect(
            lambda: got.append("reminders"))
        self.ctl.quit_requested.connect(lambda: got.append("quit"))
        self.ctl._act_open.trigger()
        self.ctl._act_settings.trigger()
        self.ctl._act_reminders.trigger()
        self.ctl._act_quit.trigger()
        self.assertEqual(got, ["main", "settings", "reminders", "quit"])

    def test_activation_click_is_debounced(self):
        got = []
        self.ctl.show_main_requested.connect(lambda: got.append(1))
        self.ctl._on_activated(QSystemTrayIcon.Trigger)
        self.ctl._on_activated(QSystemTrayIcon.DoubleClick)
        self.assertEqual(got, [1])

    def test_shutdown_is_idempotent(self):
        self.ctl.shutdown()
        self.assertFalse(self.tray.visible)
        self.ctl.shutdown()

    def test_dot_icon_color_follows_theme(self):
        sizes = (64,)
        for theme_name in ("light", "dark"):
            self.theme.theme = theme_name
            self.ctl.set_theme(self.theme)
            pm = self.ctl._dot_icon.pixmap(*sizes)
            self.assertFalse(pm.isNull())
            got = pm.toImage().pixelColor(47, 16).name()
            want = QColor(self.theme.color("status_failed")).name()
            self.assertEqual(got, want, theme_name)

    def test_alert_icon_is_solid_theme_red(self):
        for theme_name in ("light", "dark"):
            self.theme.theme = theme_name
            self.ctl.set_theme(self.theme)
            pm = self.ctl._alert_icon.pixmap(64, 64)
            self.assertFalse(pm.isNull())
            # 左缘中点落在圆角矩形内，应整块是主题红
            got = pm.toImage().pixelColor(8, 32).name()
            want = QColor(self.theme.color("status_failed")).name()
            self.assertEqual(got, want, theme_name)


if __name__ == "__main__":
    unittest.main()
