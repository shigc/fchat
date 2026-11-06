"""无边框主窗口验证：窗口标志、三按钮接线、拖动带、缩放热区、最大化。

项目无系统标题栏后，拖动/缩放/最大化都是自绘逻辑，几何与信号接线必须
逐项验；圆角描边等观感 offscreen 验不了，需真机确认。

运行：python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QEvent, QPoint, Qt  # noqa: E402
from PyQt5.QtGui import QMouseEvent  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.window_chrome import WindowControls  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class _NoLogin(MainWindow):
    """跳过登录：_show_login 里是阻塞的 exec_()，测试不能进。"""

    def _try_auto_login(self):
        self.auto_login_skipped = True


class WindowControlsTest(unittest.TestCase):
    """三按钮控件本体：信号、图标状态切换、主题刷新。"""

    def setUp(self):
        self.wc = WindowControls()

    def test_three_buttons_exist(self):
        for btn in (self.wc._min_btn, self.wc._max_btn, self.wc._close_btn):
            self.assertEqual(btn.parent(), self.wc)

    def test_click_emits_zero_arg_signals(self):
        got = {"min": 0, "max": 0, "close": 0}
        self.wc.minimize_clicked.connect(lambda: got.__setitem__("min", 1))
        self.wc.maximize_clicked.connect(lambda: got.__setitem__("max", 1))
        self.wc.close_clicked.connect(lambda: got.__setitem__("close", 1))
        self.wc._min_btn.click()
        self.wc._max_btn.click()
        self.wc._close_btn.click()
        self.assertEqual(got, {"min": 1, "max": 1, "close": 1})

    def test_set_maximized_switches_state(self):
        self.assertFalse(self.wc._maximized)
        self.wc.set_maximized(True)
        self.assertTrue(self.wc._maximized)
        self.assertEqual(self.wc._max_btn.toolTip(), "还原")
        self.wc.set_maximized(False)
        self.assertFalse(self.wc._maximized)
        self.assertEqual(self.wc._max_btn.toolTip(), "最大化")

    def test_set_theme_no_crash(self):
        self.wc.set_theme(None)
        from ui.theme import ThemeManager
        self.wc.set_theme(ThemeManager())


class MainWindowChromeTest(unittest.TestCase):
    """主窗口无边框行为：标志、接线、热区、拖动带、最大化。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = self._tmp.name
        self._orig = (config.CONFIG_DIR, config.CONFIG_FILE,
                      config.KEY_FILE, config.LEGACY_INI_FILE)
        config.CONFIG_DIR = os.path.join(tmp, "cfgdir")
        config.CONFIG_FILE = os.path.join(config.CONFIG_DIR, "config.yaml")
        config.KEY_FILE = os.path.join(config.CONFIG_DIR, "secret.key")
        config.LEGACY_INI_FILE = os.path.join(tmp, "config.ini")
        self.win = _NoLogin()
        # 先 show 一轮让布局生效（否则子部件还是默认几何，mapTo 全错），
        # 再 hide 回到不显示状态；isVisibleTo 判定不受影响
        self.win.show()
        _APP.processEvents()
        self.win.resize(1000, 680)
        _APP.processEvents()
        self.win.hide()

    def tearDown(self):
        self.win.realtime and self.win.realtime.stop()
        self.win.deleteLater()
        (config.CONFIG_DIR, config.CONFIG_FILE,
         config.KEY_FILE, config.LEGACY_INI_FILE) = self._orig
        self._tmp.cleanup()

    # ---- 窗口标志与装配 ---- #
    def test_frameless_and_translucent(self):
        self.assertTrue(self.win.windowFlags() & Qt.FramelessWindowHint)
        self.assertTrue(self.win.testAttribute(Qt.WA_TranslucentBackground))

    def test_window_controls_wired(self):
        for page in (self.win.chat_area, self.win.settings_page):
            wc = page.window_controls
            self.assertIn(wc, self.win._window_controls)
            # 接线断言用 receivers：connect 时绑定的是原方法对象，
            # patch 实例属性拦不到已建立的连接
            self.assertGreaterEqual(wc.receivers(wc.minimize_clicked), 1)
            self.assertGreaterEqual(wc.receivers(wc.maximize_clicked), 1)
            self.assertGreaterEqual(wc.receivers(wc.close_clicked), 1)

    def test_settings_controls_wired_same(self):
        wc = self.win.settings_page.window_controls
        self.assertGreaterEqual(wc.receivers(wc.minimize_clicked), 1)
        self.assertGreaterEqual(wc.receivers(wc.maximize_clicked), 1)
        self.assertGreaterEqual(wc.receivers(wc.close_clicked), 1)

    def test_statusbar_visible_only_with_message(self):
        """空 QStatusBar 常驻底部会在窗口下沿横出一条空白带（真机反馈），
        所以只在有消息时出现；无边框窗口也不需要原生 size grip。"""
        sb = self.win.statusBar()
        self.assertFalse(sb.isVisibleTo(self.win))
        self.assertFalse(sb.isSizeGripEnabled())
        sb.showMessage("提示一条")
        self.assertTrue(sb.isVisibleTo(self.win))
        sb.clearMessage()
        self.assertFalse(sb.isVisibleTo(self.win))

    def test_icon_color_survives_theme_roundtrip(self):
        """亮→暗→亮一圈后各处图标必须回到深色。

        _on_theme_changed 里图标重挂曾被安排在延迟一轮的 apply 之前，
        用旧全局色渲染：切回亮色后图标停在暗色主题的 #e0e0e0，白底上
        几乎隐形（真机复现）。修复后重挂排在 apply 之后。覆盖三类
        曾漏挂的控件：+ 号新建按钮、输入框工具栏、跳到底悬浮钮。
        """
        from PyQt5.QtGui import QImage

        def dark_ink(icon_obj, size):
            img = icon_obj.pixmap(size * 4, size * 4).toImage() \
                .convertToFormat(QImage.Format_ARGB32)
            return sum(
                1 for y in range(img.height()) for x in range(img.width())
                if img.pixelColor(x, y).alpha() > 200
                and img.pixelColor(x, y).lightness() < 128)

        self.win.chat_area.chat_view._ensure_jump_btn()
        self.win._on_theme_changed("dark")
        _APP.processEvents()
        self.win._on_theme_changed("light")
        _APP.processEvents()
        targets = {
            "新建+": (self.win.new_room_btn.icon(), 16),
            "目录": (self.win.directory_btn.icon(), 16),
            "输入框表情": (self.win.chat_area.input.emoji_btn.icon(), 24),
            "跳底": (self.win.chat_area.chat_view._jump_btn.icon(), 16),
            "成员": (self.win.chat_area.panel_buttons["members"].icon(), 18),
        }
        for name, (ic, size) in targets.items():
            self.assertGreater(
                dark_ink(ic, size), 5, f"{name}图标切回亮色后仍是浅色")

    def test_panels_follow_saved_dark_theme_on_startup(self):
        """配置里存了暗色时，启动必须把主题刷到浮层面板。

        _load_and_apply_theme 曾漏掉 _room_panels：面板在构造期按默认
        亮色 manager 注入（此时配置还没加载），配置读出暗色后其余部件
        都刷新了、唯独面板停在亮色——暗色应用里挂白底面板，且 AI 面板
        正文 _render 从 manager 实时取色，烤出暗色文字配白底（真机踩过）。
        """
        from ui.theme import ThemeManager
        mgr = ThemeManager()
        mgr.theme = "dark"
        conv_bg = mgr.color("conv_bg")
        input_bg = mgr.color("input_bg")
        config.save_appearance(theme="dark")
        win = _NoLogin()
        try:
            ai = win._room_panels.ai
            self.assertIn(conv_bg, ai.view.styleSheet(),
                          "AI 面板正文底色未随启动主题变暗")
            self.assertIn(input_bg, ai.range_combo.styleSheet(),
                          "AI 面板下拉框未随启动主题变暗")
        finally:
            win.realtime and win.realtime.stop()
            win.deleteLater()

    # ---- 缩放热区 ---- #
    def test_edge_at_values(self):
        w, h = self.win.width(), self.win.height()
        self.assertEqual(self.win._edge_at(QPoint(w // 2, h // 2)), 0)
        self.assertEqual(
            self.win._edge_at(QPoint(2, h // 2)) & int(Qt.LeftEdge),
            int(Qt.LeftEdge))
        self.assertEqual(
            self.win._edge_at(QPoint(w - 2, h // 2)) & int(Qt.RightEdge),
            int(Qt.RightEdge))
        self.assertEqual(
            self.win._edge_at(QPoint(w // 2, 2)) & int(Qt.TopEdge),
            int(Qt.TopEdge))
        self.assertEqual(
            self.win._edge_at(QPoint(w // 2, h - 2)) & int(Qt.BottomEdge),
            int(Qt.BottomEdge))
        self.assertEqual(
            self.win._edge_at(QPoint(2, 2)),
            int(Qt.LeftEdge | Qt.TopEdge))

    def test_edge_at_zero_when_maximized(self):
        self.win.setWindowState(Qt.WindowMaximized)
        _APP.processEvents()
        try:
            self.assertTrue(self.win.isMaximized())
            self.assertEqual(self.win._edge_at(QPoint(2, 2)), 0)
        finally:
            self.win.setWindowState(Qt.WindowNoState)
            _APP.processEvents()

    def test_event_filter_intercepts_edge_press(self):
        """边沿按下被子部件吞掉前要被过滤器拦成缩放。"""
        evt = QMouseEvent(QEvent.MouseButtonPress, QPoint(2, 300),
                          QPoint(2, 300), Qt.LeftButton, Qt.LeftButton,
                          Qt.NoModifier)
        self.assertTrue(self.win.eventFilter(self.win.sidebar, evt))
        self.assertEqual(self.win._resize_edges, int(Qt.LeftEdge))
        self.win._resize_edges = 0

    def test_event_filter_passes_inner_press(self):
        evt = QMouseEvent(QEvent.MouseButtonPress, QPoint(300, 300),
                          QPoint(300, 300), Qt.LeftButton, Qt.LeftButton,
                          Qt.NoModifier)
        self.assertFalse(self.win.eventFilter(self.win.sidebar, evt))

    # ---- 拖动带 ---- #
    def test_drag_bands_hit(self):
        pt = self.win.conversation_header.mapTo(
            self.win, QPoint(10, 10))
        self.assertTrue(self.win._in_drag_band(pt))
        pt = self.win.chat_area.title.mapTo(self.win, QPoint(10, 10))
        self.assertTrue(self.win._in_drag_band(pt))
        # 聊天页激活时设置页整页隐藏，其导航不参与拖动带
        self.assertFalse(self.win._in_drag_band(
            self.win.settings_page.nav.mapTo(self.win, QPoint(10, 10))))
        self.assertFalse(self.win._in_drag_band(
            QPoint(self.win.width() // 2, self.win.height() // 2)))

    def test_settings_nav_band_when_settings_shown(self):
        self.win._show_settings_tab()
        try:
            self.assertTrue(self.win._in_drag_band(
                self.win.settings_page.nav.mapTo(self.win, QPoint(10, 10))))
        finally:
            self.win._show_chat_tab()

    def test_settings_top_band_hit_only_when_settings_shown(self):
        """设置页右内容区顶部横带（三按钮周围空白）可拖动；聊天页不误判。"""
        pt = QPoint(self.win.width() - 100, 20)
        self.assertFalse(self.win._in_drag_band(pt))
        self.win._show_settings_tab()
        try:
            self.assertTrue(self.win._in_drag_band(pt))
            # nav 左列以内仍是聊天内容坐标，横带只覆盖右侧
            self.assertFalse(self.win._in_drag_band(QPoint(20, 20)))
        finally:
            self.win._show_chat_tab()

    def test_press_settings_top_band_starts_drag(self):
        pt = QPoint(self.win.width() - 100, 20)
        self.win._show_settings_tab()
        try:
            press = QMouseEvent(QEvent.MouseButtonPress, pt,
                                pt + QPoint(200, 200), Qt.LeftButton,
                                Qt.LeftButton, Qt.NoModifier)
            self.win.mousePressEvent(press)
            self.assertIsNotNone(self.win._drag_offset)
        finally:
            self.win._drag_offset = None
            self.win._show_chat_tab()

    def test_drag_bands_hidden_page_skipped(self):
        """设置页时 conversation_panel 隐藏，其头部不再是拖动带。"""
        self.win._show_settings_tab()
        try:
            self.assertFalse(self.win.conversation_header.isVisible())
            self.assertTrue(self.win._in_drag_band(
                self.win.settings_page.nav.mapTo(self.win, QPoint(10, 10))))
        finally:
            self.win._show_chat_tab()

    def test_double_click_band_toggles_maximize(self):
        pt = self.win.conversation_header.mapTo(self.win, QPoint(10, 10))
        evt = QMouseEvent(QEvent.MouseButtonDblClick, pt, QPoint(600, 60),
                          Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        with mock.patch.object(self.win, "showMaximized") as smax:
            self.win.mouseDoubleClickEvent(evt)
            smax.assert_called_once()

    def test_press_band_starts_manual_drag(self):
        """拖动带按下即进入手动拖动。PyQt5 未包装 startSystemMove
        （真机 AttributeError 崩溃），这里守住"不许再用系统调用"。"""
        pt = self.win.conversation_header.mapTo(self.win, QPoint(10, 10))
        press = QMouseEvent(QEvent.MouseButtonPress, pt,
                            pt + QPoint(200, 200), Qt.LeftButton,
                            Qt.LeftButton, Qt.NoModifier)
        self.win.mousePressEvent(press)
        self.assertIsNotNone(self.win._drag_offset)
        move = QMouseEvent(QEvent.MouseMove, pt + QPoint(30, 20),
                           pt + QPoint(230, 220), Qt.LeftButton,
                           Qt.LeftButton, Qt.NoModifier)
        before = self.win.pos()
        self.win.mouseMoveEvent(move)
        self.assertEqual(self.win.pos(), before + QPoint(30, 20))
        release = QMouseEvent(QEvent.MouseButtonRelease,
                              pt + QPoint(30, 20), pt + QPoint(230, 220),
                              Qt.LeftButton, Qt.NoButton, Qt.NoModifier)
        self.win.mouseReleaseEvent(release)
        self.assertIsNone(self.win._drag_offset)

    def test_double_click_outside_band_ignored(self):
        evt = QMouseEvent(QEvent.MouseButtonDblClick,
                          QPoint(self.win.width() // 2,
                                 self.win.height() // 2),
                          QPoint(500, 340), Qt.LeftButton, Qt.LeftButton,
                          Qt.NoModifier)
        with mock.patch.object(self.win, "showMaximized") as smax:
            self.win.mouseDoubleClickEvent(evt)
            smax.assert_not_called()

    # ---- 最大化 ---- #
    def test_toggle_maximized_calls_show_maximized(self):
        self.assertFalse(self.win.isMaximized())
        with mock.patch.object(self.win, "showMaximized") as smax:
            self.win._toggle_maximized()
            smax.assert_called_once()

    def test_toggle_restore_calls_show_normal(self):
        with mock.patch.object(self.win, "isMaximized", return_value=True), \
                mock.patch.object(self.win, "showNormal") as sn:
            self.win._toggle_maximized()
            sn.assert_called_once()

    def test_state_change_syncs_controls_icons(self):
        self.win.setWindowState(Qt.WindowMaximized)
        _APP.processEvents()
        try:
            for wc in self.win._window_controls:
                self.assertTrue(wc._maximized)
        finally:
            self.win.setWindowState(Qt.WindowNoState)
            _APP.processEvents()
        for wc in self.win._window_controls:
            self.assertFalse(wc._maximized)

    def test_maximized_geometry_within_available(self):
        """frameless 最大化不能盖住任务栏（可用区钳制）。"""
        self.win.setWindowState(Qt.WindowMaximized)
        _APP.processEvents()
        try:
            avail = self.win.screen().availableGeometry()
            self.assertTrue(avail.contains(self.win.frameGeometry()))
        finally:
            self.win.setWindowState(Qt.WindowNoState)
            _APP.processEvents()

    # ---- 圆角与遮罩 ---- #
    def test_mask_updated_on_show(self):
        self.win.show()
        _APP.processEvents()
        try:
            self.assertFalse(self.win.mask().isEmpty())
        finally:
            self.win.hide()

    def test_paint_event_runs(self):
        self.win.show()
        _APP.processEvents()
        try:
            pm = self.win.grab()
            self.assertFalse(pm.isNull())
        finally:
            self.win.hide()

    def test_sidebar_paints_qss_background(self):
        """侧栏 QFrame 子类必须开 WA_StyledBackground，否则 #Sidebar 的
        深色背景画不出来（真机曾透出窗口白底、浅色图标几乎看不见）。"""
        from ui.sidebar import Sidebar
        sb = Sidebar()
        sb.resize(56, 400)
        # 局部样式表等价于主题里的 #Sidebar 规则，且不污染全局 QSS
        sb.setStyleSheet("#Sidebar{background:#2e2e2e;}")
        sb.show()
        _APP.processEvents()
        try:
            c = sb.grab().toImage().pixelColor(28, 200)
            self.assertLess(c.lightness(), 100,
                            f"侧栏底色应是深色，实际 {c.name()}")
        finally:
            sb.hide()

    def test_sidebar_uses_drawn_icons(self):
        """导航按钮用现画图标（非 emoji 文本）。"""
        for nav_id in ("chats", "settings"):
            btn = self.win.sidebar._buttons[nav_id]
            self.assertFalse(btn.icon().isNull(), f"{nav_id} 图标为空")

    def test_sidebar_icon_color_states(self):
        """未选中浅灰（配深底）、选中白（配 accent 深底），切主题不崩。"""
        from ui.sidebar import Sidebar

        def _lightness(img):
            vals = [img.pixelColor(x, y).lightness()
                    for y in range(img.height())
                    for x in range(img.width())
                    if img.pixelColor(x, y).alpha() > 90]
            return vals

        sb = Sidebar()
        checked = sb._buttons["chats"]          # 默认选中
        unchecked = sb._buttons["settings"]
        vals = _lightness(checked.icon().pixmap(22, 22).toImage())
        self.assertTrue(vals)
        self.assertGreater(min(vals), 200,
                           f"选中态图标应是白色，实际 {min(vals)}")
        vals = _lightness(unchecked.icon().pixmap(22, 22).toImage())
        self.assertTrue(vals)
        self.assertLess(max(vals), 200,
                        f"未选中图标应是浅灰，实际 {max(vals)}")
        sb.set_theme(None)  # 不崩即可（走 fallback 色）
        self.assertFalse(sb._buttons["chats"].icon().isNull())


if __name__ == "__main__":
    unittest.main()
