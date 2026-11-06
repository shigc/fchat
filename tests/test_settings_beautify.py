"""设置页美化相关新增功能的验证。

覆盖：config 状态/快捷键持久化、ApiClient.logout/set_status、圆角开关
SwitchButton、设置页左侧导航结构、主窗口登出/状态/主题重绘接线。

运行：python -m unittest discover -s tests -v
不连真实服务器、不触碰真实用户配置目录。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import Qt, QPoint, QEvent  # noqa: E402
from PyQt5.QtGui import QMouseEvent  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402
from tests.helpers import install_keyring_mock  # noqa: E402
from core.api_client import ApiClient  # noqa: E402
from ui.settings_page import SettingsPage, SwitchButton  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.theme import ThemeManager  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class ConfigStatusShortcutsTest(unittest.TestCase):
    """状态与快捷键的配置持久化。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = self._tmp.name
        self._orig = (config.CONFIG_DIR, config.CONFIG_FILE,
                      config.KEY_FILE, config.LEGACY_INI_FILE)
        config.CONFIG_DIR = os.path.join(tmp, "cfgdir")
        config.CONFIG_FILE = os.path.join(config.CONFIG_DIR, "config.yaml")
        config.KEY_FILE = os.path.join(config.CONFIG_DIR, "secret.key")
        config.LEGACY_INI_FILE = os.path.join(tmp, "config.ini")
        install_keyring_mock(self)

    def tearDown(self):
        (config.CONFIG_DIR, config.CONFIG_FILE,
         config.KEY_FILE, config.LEGACY_INI_FILE) = self._orig
        self._tmp.cleanup()

    def test_status_defaults_online(self):
        self.assertEqual(config.load_config()["status"], "online")

    def test_save_status_roundtrip(self):
        config.save_status("busy")
        self.assertEqual(config.load_config()["status"], "busy")

    def test_save_shortcuts_roundtrip(self):
        config.save_shortcuts({"open_settings": "Ctrl+Shift+P"})
        cfg = config.load_config()
        self.assertEqual(cfg["shortcuts"]["open_settings"], "Ctrl+Shift+P")

    def test_status_and_shortcuts_do_not_wipe_other_fields(self):
        config.save_login("https://s", "u", "pw", remember_password=True)
        config.save_appearance(theme="dark")
        config.save_status("away")
        config.save_shortcuts({"open_settings": "Ctrl+,"})
        cfg = config.load_config()
        self.assertEqual(cfg["server_url"], "https://s")
        self.assertEqual(cfg["theme"], "dark")
        self.assertEqual(cfg["status"], "away")
        self.assertEqual(cfg["shortcuts"]["open_settings"], "Ctrl+,")


class ApiClientLogoutStatusTest(unittest.TestCase):
    """登出与状态切换的客户端层（mock 掉底层 RocketChat）。"""

    def _client(self):
        c = ApiClient("https://s.example.com")
        c._rc = mock.Mock()
        c.user_id = "u1"
        c.auth_token = "tok"
        c.me = mock.Mock()
        return c

    def test_logout_clears_session(self):
        c = self._client()
        rc = c._rc
        c.logout()
        rc.logout.assert_called_once()
        self.assertIsNone(c._rc)
        self.assertEqual(c.auth_token, "")
        self.assertEqual(c.user_id, "")
        self.assertIsNone(c.me)

    def test_logout_no_rc_is_safe(self):
        c = ApiClient("https://s.example.com")
        c.logout()  # 不应抛异常
        self.assertIsNone(c._rc)

    def test_set_status_calls_users_set_status(self):
        c = self._client()
        c.set_status("dnd")
        c._rc.users_set_status.assert_called_once_with(status="dnd", message="")

    def test_set_status_no_rc_is_safe(self):
        c = ApiClient("https://s.example.com")
        c.set_status("away")  # 不应抛异常

    def test_logout_rc_failure_still_clears(self):
        c = self._client()
        rc = c._rc
        rc.logout.side_effect = Exception("boom")
        c.logout()
        self.assertIsNone(c._rc)
        self.assertEqual(c.user_id, "")
        self.assertEqual(c.auth_token, "")
        self.assertIsNone(c.me)


class SwitchButtonTest(unittest.TestCase):
    def test_default_off(self):
        sw = SwitchButton()
        self.assertFalse(sw.isChecked())

    def test_set_checked_emits_toggled(self):
        sw = SwitchButton()
        seen = []
        sw.toggled.connect(seen.append)
        sw.setChecked(True)
        self.assertTrue(sw.isChecked())
        self.assertEqual(seen, [True])

    def test_click_toggles(self):
        sw = SwitchButton()
        ev = QMouseEvent(QEvent.MouseButtonRelease, QPoint(10, 10),
                         Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        sw.mouseReleaseEvent(ev)
        self.assertTrue(sw.isChecked())


class SettingsPageStructureTest(unittest.TestCase):
    def setUp(self):
        self.page = SettingsPage()

    def tearDown(self):
        self.page.deleteLater()

    def test_left_nav_eight_buttons(self):
        self.assertEqual(len(self.page._nav_buttons), 8)
        texts = [b.text().strip() for b in self.page._nav_buttons]
        self.assertEqual(
            texts, ["账号", "通用", "外观", "字体", "快捷键", "存储", "AI", "关于"])

    def test_eight_content_pages(self):
        self.assertEqual(self.page._stack.count(), 8)

    def test_nav_switches_page(self):
        self.page._nav_buttons[3].setChecked(True)
        self.page._nav_buttons[3].click()
        self.assertEqual(self.page._stack.currentIndex(), 3)

    @staticmethod
    def _accent_ink(btn):
        """统计图标 pixmap 里强调绿（accent）实心像素数。"""
        from PyQt5.QtGui import QImage
        img = btn.icon().pixmap(32, 32).toImage() \
            .convertToFormat(QImage.Format_ARGB32)
        return sum(
            1 for y in range(img.height()) for x in range(img.width())
            if img.pixelColor(x, y).alpha() > 200
            and img.pixelColor(x, y).green() > img.pixelColor(x, y).red() + 50)

    def test_nav_checked_icon_follows_accent(self):
        """选中项文字变强调色，图标也得跟着换，否则深图标配绿字不协调。"""
        btn = self.page._nav_buttons[0]   # 构造期默认选中「账号」
        self.assertGreater(self._accent_ink(btn), 0,
                           "选中项图标应为强调色")
        self.assertEqual(self._accent_ink(self.page._nav_buttons[1]), 0,
                         "未选中项图标应为全局主题色")

    def test_nav_click_swaps_icon_color(self):
        """切换导航后旧选中项图标回深色、新选中项变强调色。"""
        self.page._nav_buttons[2].setChecked(True)
        self.page._nav_buttons[2].click()
        self.assertGreater(self._accent_ink(self.page._nav_buttons[2]), 0)
        self.assertEqual(self._accent_ink(self.page._nav_buttons[0]), 0)

    def test_account_show_account(self):
        self.page.show_account("alice", "Alice Smith", "")
        self.assertEqual(self.page._name_label.text(), "Alice Smith")
        self.assertEqual(self.page._username_label.text(), "@alice")

    def test_general_page_has_server_input_no_account_label(self):
        # 需求：通用页删除账号显示（与「账号」页重复），改为可配置服务器地址
        from ui.widgets import WxLineEdit
        self.assertIsInstance(self.page._server_input, WxLineEdit)
        self.assertFalse(hasattr(self.page, "account_label"),
                         "通用页不应再有「当前账号」标签")
        self.assertFalse(hasattr(self.page, "server_label"))

    def test_set_server_url_writes_input_without_signal(self):
        got = []
        self.page.server_saved.connect(got.append)
        self.page.set_server_url("https://chat.example.com")
        self.assertEqual(self.page._server_input.text(), "https://chat.example.com")
        self.assertEqual(got, [], "回写配置不应触发 server_saved")

    def test_server_save_button_emits_server_saved(self):
        got = []
        self.page.server_saved.connect(got.append)
        self.page._server_input.setText("chat.example.com")
        self.page._on_server_saved()
        self.assertEqual(got, ["chat.example.com"])

    def test_status_combo_values(self):
        self.assertEqual(self.page.status_combo.count(), 5)

    def test_wallpaper_swatches_cover_default_and_presets(self):
        from ui.chat_background import PRESETS
        self.assertEqual(len(self.page._wallpaper_swatches), 1 + len(PRESETS))
        self.assertEqual(self.page._wallpaper_swatches[0].value, "")
        self.assertTrue(self.page._wallpaper_swatches[0].isChecked())

    def test_set_wallpaper_selects_single_swatch(self):
        self.page.set_wallpaper("preset:grad_dawn")
        checked = [s.value for s in self.page._wallpaper_swatches
                   if s.isChecked()]
        self.assertEqual(checked, ["preset:grad_dawn"])
        self.assertFalse(self.page.wallpaper_preview.pixmap().isNull())

    def test_set_wallpaper_custom_path_unchecks_all(self):
        self.page.set_wallpaper(os.path.join(tempfile.gettempdir(),
                                             "no-such-bg.png"))
        self.assertFalse(any(s.isChecked()
                             for s in self.page._wallpaper_swatches))
        # QLabel 对空 pixmap 返回 None
        self.assertIsNone(self.page.wallpaper_preview.pixmap())
        self.assertEqual(self.page.wallpaper_preview.text(), "默认")

    def test_swatch_click_emits_changed(self):
        got = []
        self.page.wallpaper_changed.connect(got.append)
        swatch = self.page._wallpaper_swatches[1]
        swatch.click()
        self.assertEqual(got, [swatch.value])
        self.assertTrue(swatch.isChecked())

    def test_swatch_pixmap_follows_theme(self):
        """亮/暗各一套配色：换主题后缩略图要重渲染成暗色版本。"""
        class _Light:
            theme = "light"

            def color(self, key, default=""):
                return default

        class _Dark:
            theme = "dark"

            def color(self, key, default=""):
                return default

        swatch = self.page._wallpaper_swatches[1]
        swatch.set_theme(_Light())
        light = swatch._preview_pixmap().toImage()
        swatch.set_theme(_Dark())
        dark = swatch._preview_pixmap().toImage()
        self.assertNotEqual(light, dark)

    def test_font_size_is_combo_with_values(self):
        # 需求6：字号设置改为下拉框（WxComboBox），选项按数值存 itemData
        from ui.widgets import WxComboBox
        self.assertIsInstance(self.page.font_size_combo, WxComboBox)
        self.assertEqual(self.page.font_size_combo.count(), 15)
        data = [self.page.font_size_combo.itemData(i)
                for i in range(self.page.font_size_combo.count())]
        self.assertEqual(data, list(range(10, 25)))
        self.assertEqual(self.page.font_size_combo.currentData(), 13)

    def test_font_size_combo_emits_change(self):
        got = []
        self.page.font_size_changed.connect(got.append)
        self.page.font_size_combo.setCurrentIndex(
            self.page.font_size_combo.findData(18))
        self.assertEqual(got, [18])

    def test_set_font_size_writes_combo_without_signal(self):
        got = []
        self.page.font_size_changed.connect(got.append)
        self.page.set_font_size(18)
        self.assertEqual(self.page.font_size_combo.currentData(), 18)
        self.assertEqual(got, [], "回写字号不应触发 font_size_changed")

    def test_default_shortcut_values(self):
        self.assertEqual(self.page.get_shortcuts()["open_settings"], "Ctrl+,")
        self.assertEqual(self.page.get_shortcuts()["close_settings"], "Esc")

    def test_set_shortcuts_writes_inputs_without_signal(self):
        got = []
        self.page.shortcuts_changed.connect(got.append)
        self.page.set_shortcuts({"open_settings": "Ctrl+Alt+O"})
        self.assertEqual(
            self.page._shortcut_edits["open_settings"].text(), "Ctrl+Alt+O")
        self.assertEqual(got, [], "回写配置不应触发 shortcuts_changed")

    def test_default_shortcut_includes_quick_switch(self):
        self.assertEqual(self.page.get_shortcuts()["quick_switch"], "Ctrl+K")

    def test_account_show_department(self):
        self.page.show_account("alice", "Alice Smith", "", "研发部")
        self.assertFalse(self.page._department_label.isHidden())
        self.assertEqual(self.page._department_label.text(), "研发部")

    def test_account_hides_department_when_empty(self):
        self.page.show_account("alice", "Alice Smith", "", "")
        self.assertTrue(self.page._department_label.isHidden())

    def test_theme_injection_syncs_combo_and_styles(self):
        # 需求4：注入 ThemeManager，主题模式下拉框应与取色器一致
        tm = ThemeManager()
        self.page.set_theme(tm)
        self.assertEqual(self.page.theme_combo.currentIndex(), 0)
        tm.theme = "dark"
        self.page.set_theme(tm)
        self.assertEqual(self.page.theme_combo.currentIndex(), 1)
        self.assertIs(self.page._theme, tm)

    def test_shortcut_edit_captures_combo(self):
        # 需求5：点击输入框后按组合键应自动写入
        from PyQt5.QtGui import QKeyEvent
        edit = self.page._shortcut_edits["open_settings"]
        ev = QKeyEvent(QEvent.KeyPress, Qt.Key_G, Qt.ControlModifier)
        edit.keyPressEvent(ev)
        self.assertEqual(edit.text(), "Ctrl+G")

    def test_shortcut_edit_escape_restores_default(self):
        from PyQt5.QtGui import QKeyEvent
        edit = self.page._shortcut_edits["close_settings"]
        ev = QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier)
        edit.keyPressEvent(ev)
        self.assertEqual(edit.text(), "Esc")


class AccountLayoutTest(unittest.TestCase):
    """账号页头像布局：头像与文字同排，姓名优先显示。"""

    def setUp(self):
        self.page = SettingsPage()

    def tearDown(self):
        self.page.deleteLater()

    def test_avatar_and_name_same_row(self):
        # 复现截图问题：头像与用户名必须同一行（y 坐标一致）
        self.page.show_account("alice", "Alice Smith", "")
        self.page.resize(900, 600)
        self.page.show()
        av_y = self.page._avatar.mapTo(self.page, QPoint(0, 0)).y()
        tx_y = self.page._name_label.mapTo(self.page, QPoint(0, 0)).y()
        self.assertEqual(av_y, tx_y)
        self.page.hide()

    def test_empty_name_falls_back_to_username(self):
        self.page.show_account("alice", "", "")
        self.assertEqual(self.page._name_label.text(), "alice")
        self.assertEqual(self.page._username_label.text(), "")

    def test_avatar_click_emits_signal(self):
        self.page.resize(900, 600)
        self.page.show()
        seen = []
        self.page.avatar_clicked.connect(lambda: seen.append(1))
        ev = QMouseEvent(QEvent.MouseButtonRelease, QPoint(32, 32),
                         Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        self.page._avatar.mouseReleaseEvent(ev)
        self.assertEqual(seen, [1])
        self.page.hide()

    def test_avatar_is_clickable(self):
        self.page.resize(900, 600)
        self.page.show()
        self.assertTrue(self.page._avatar._clickable)
        self.page.hide()


class MainWindowNewWiringTest(unittest.TestCase):
    """主窗口对登出/状态/主题重绘的接线。"""

    class _NoLogin(MainWindow):
        def _try_auto_login(self):
            self.auto_login_skipped = True

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = self._tmp.name
        self._orig = (config.CONFIG_DIR, config.CONFIG_FILE,
                      config.KEY_FILE, config.LEGACY_INI_FILE)
        config.CONFIG_DIR = os.path.join(tmp, "cfgdir")
        config.CONFIG_FILE = os.path.join(config.CONFIG_DIR, "config.yaml")
        config.KEY_FILE = os.path.join(config.CONFIG_DIR, "secret.key")
        config.LEGACY_INI_FILE = os.path.join(tmp, "config.ini")
        install_keyring_mock(self)
        self.win = self._NoLogin()
        self.win.resize(1000, 680)

    def tearDown(self):
        self.win.realtime and self.win.realtime.stop()
        self.win.deleteLater()
        (config.CONFIG_DIR, config.CONFIG_FILE,
         config.KEY_FILE, config.LEGACY_INI_FILE) = self._orig
        self._tmp.cleanup()

    def test_delegate_theme_is_set_on_startup(self):
        # 需求6：聊天区委托应注入 ThemeManager
        self.assertIs(
            self.win.chat_area.chat_view._delegate._theme, self.win.theme_manager)

    def test_theme_change_keeps_delegate_theme_and_redraws(self):
        # 需求6：刷新时重新注入 ThemeManager 并触发重绘
        view = self.win.chat_area.chat_view
        self.win.theme_manager.theme = "dark"
        with mock.patch.object(view.viewport(), "update") as upd:
            self.win._refresh_chat_theme()
            upd.assert_called_once()
        self.assertIs(view._delegate._theme, self.win.theme_manager)

    def test_logout_requested_clears_state_and_shows_login(self):
        client = mock.Mock()
        self.win.client = client
        shown = []
        calls = {"hide": 0, "close": 0, "show": 0}
        self.win._show_login = lambda *a, **k: shown.append(1)
        self.win.hide = lambda: calls.__setitem__("hide", calls["hide"] + 1)
        self.win.close = lambda: calls.__setitem__("close", calls["close"] + 1)
        self.win.show = lambda: calls.__setitem__("show", calls["show"] + 1)
        self.win.settings_page.logout_requested.emit()
        client.logout.assert_called_once()
        self.assertIsNone(self.win.client)
        self.assertEqual(shown, [1], "登出后应回到登录框")
        # 微信式：主界面隐藏只留登录窗；放弃登录则结束应用
        self.assertEqual(calls["hide"], 1)
        self.assertEqual(calls["close"], 1)
        self.assertEqual(calls["show"], 0)

    def test_logout_relogin_restores_main_window(self):
        # 退出后在登录框登录成功（client 重建），主界面应恢复显示而不是退出
        calls = {"close": 0, "show": 0}
        self.win.client = None
        self.win._show_login = lambda *a, **k: setattr(
            self.win, "client", mock.Mock())
        self.win.hide = lambda: None
        self.win.close = lambda: calls.__setitem__("close", calls["close"] + 1)
        self.win.show = lambda: calls.__setitem__("show", calls["show"] + 1)
        self.win.settings_page.logout_requested.emit()
        self.assertEqual(calls["close"], 0)
        self.assertEqual(calls["show"], 1)

    def test_status_changed_saves_and_calls_client(self):
        self.win.client = mock.Mock()
        self.win.statusBar = lambda: mock.Mock()
        self.win.settings_page.status_changed.emit("busy")
        self.assertEqual(config.load_config()["status"], "busy")
        self.win.client.set_status.assert_called_once_with("busy")

    def test_shortcuts_changed_applies(self):
        self.win.settings_page.shortcuts_changed.emit(
            {"open_settings": "Ctrl+Shift+O", "close_settings": "F5"})
        sc = self.win._shortcuts
        self.assertEqual(sc["open_settings"].key().toString(), "Ctrl+Shift+O")
        self.assertEqual(sc["close_settings"].key().toString(), "F5")

    def test_quick_switch_registered_by_default(self):
        self.win._apply_shortcuts({})
        self.assertIn("quick_switch", self.win._shortcuts)
        self.assertEqual(
            self.win._shortcuts["quick_switch"].key().toString(), "Ctrl+K")

    def test_startup_restores_saved_shortcut_into_edit(self):
        # 完善：已保存的快捷键在启动时应回写到设置页输入框，而不是显示默认值
        config.save_shortcuts({"open_settings": "Ctrl+Alt+O"})
        win = self._NoLogin()
        try:
            self.assertEqual(
                win.settings_page._shortcut_edits["open_settings"].text(),
                "Ctrl+Alt+O")
            # 且编辑框样式复用 WxLineEdit 主题（随注入的取色器变色）
            from ui.widgets import _color
            self.assertIn(
                _color(win.theme_manager, "input_bg", "#ffffff"),
                win.settings_page._shortcut_edits["open_settings"].styleSheet())
        finally:
            win.realtime and win.realtime.stop()
            win.deleteLater()

    def test_storage_message_goes_to_statusbar(self):
        # 连接发生在 _setup_ui，绑定的是当时真实 statusBar 的 showMessage，
        # 事后替换 self.win.statusBar 拦不到，故断言真实状态栏的 currentMessage
        self.win.settings_page.storage_message.emit("缓存已清空", 4000)
        self.assertEqual(self.win.statusBar().currentMessage(), "缓存已清空")

    def test_avatar_click_without_login_is_safe(self):
        # 未登录时点击头像只提示不做事；Qt 槽里抛异常会直接终止进程
        self.win.client = None
        bar = mock.Mock()
        self.win.statusBar = lambda: bar
        self.win.settings_page.avatar_clicked.emit()   # 不应抛异常
        bar.showMessage.assert_called_once()

    def test_server_saved_prompts_restart_and_persists(self):
        # 需求：保存新服务器地址 → 落盘 + 弹「是否重启应用」对话框
        self.win.statusBar = lambda: mock.Mock()
        with mock.patch("ui.main_window.confirm", return_value=False) as q, \
                mock.patch.object(self.win, "_restart_app") as restart:
            self.win.settings_page.server_saved.emit("chat.example.com")
        q.assert_called_once()
        restart.assert_not_called()   # 选了「否」不重启
        self.assertEqual(config.load_config()["server_url"],
                         "https://chat.example.com")

    def test_server_saved_yes_restarts(self):
        self.win.statusBar = lambda: mock.Mock()
        with mock.patch("ui.main_window.confirm", return_value=True), \
                mock.patch.object(self.win, "_restart_app") as restart:
            self.win.settings_page.server_saved.emit("chat.example.com")
        restart.assert_called_once()

    def test_server_saved_empty_is_rejected(self):
        self.win.statusBar = lambda: mock.Mock()
        with mock.patch.object(self.win, "_restart_app") as restart:
            self.win.settings_page.server_saved.emit("   ")
        restart.assert_not_called()
        self.assertEqual(config.load_config()["server_url"], "")

    def test_server_startup_fills_input_from_config(self):
        # 需求：启动时把已保存的服务器地址回写到通用页输入框
        config.save_server_url("https://s.example.com")
        win = self._NoLogin()
        try:
            self.assertEqual(
                win.settings_page._server_input.text(), "https://s.example.com")
        finally:
            win.realtime and win.realtime.stop()
            win.deleteLater()

    def test_change_avatar_uploads_purges_and_refreshes(self):
        from PyQt5.QtWidgets import QFileDialog
        _sync = lambda fn, on_done=None, on_fail=None, parent=None: on_done(fn())
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        img = os.path.join(tmp.name, "me.png")
        with open(img, "wb") as f:
            f.write(b"png")
        self.win.client = mock.Mock()
        self.win.client.connected = True
        self.win.client.me.username = "alice"
        self.win.client.me.name = "Alice"
        self.win.client.me.department = "研发部"
        self.win._avatar_url = "http://s/avatar/alice"
        bar = mock.Mock()
        self.win.statusBar = lambda: bar
        with mock.patch.object(QFileDialog, "getOpenFileName",
                               return_value=(img, "")), \
                mock.patch("ui.api_workers.run_async", side_effect=_sync), \
                mock.patch.object(self.win.avatar_loader, "purge") as purge, \
                mock.patch.object(self.win.avatar_loader, "load") as load:
            self.win.settings_page.avatar_clicked.emit()
        self.win.client.set_avatar.assert_called_once_with(img)
        purge.assert_called_once_with("http://s/avatar/alice")
        # _done 里重新注入加载器应按头像 URL 触发重载（否则界面不刷新头像）
        load.assert_called_once()
        self.assertEqual(self.win.settings_page._name_label.text(), "Alice")

    def test_change_avatar_failure_reports(self):
        from PyQt5.QtWidgets import QFileDialog
        # _sync 需复刻 run_async 的失败语义：fn 抛异常转给 on_fail，而不是向上抛
        def _sync(fn, on_done=None, on_fail=None, parent=None):
            try:
                res = fn()
            except Exception as e:
                if on_fail:
                    on_fail(str(e))
            else:
                if on_done:
                    on_done(res)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        img = os.path.join(tmp.name, "me.png")
        with open(img, "wb") as f:
            f.write(b"png")
        self.win.client = mock.Mock()
        self.win.client.connected = True
        self.win.client.set_avatar.side_effect = Exception("服务器拒绝")
        self.win._avatar_url = "http://s/avatar/alice"
        with mock.patch.object(QFileDialog, "getOpenFileName",
                               return_value=(img, "")), \
                mock.patch("ui.api_workers.run_async", side_effect=_sync), \
                mock.patch.object(self.win.avatar_loader, "load"), \
                mock.patch.object(self.win.avatar_loader, "purge"):
            self.win.settings_page.avatar_clicked.emit()
        # 断真实状态栏 currentMessage（同 test_storage_message_goes_to_statusbar）
        self.assertIn("头像上传失败", self.win.statusBar().currentMessage())

    def test_change_avatar_blocked_while_busy(self):
        self.win.client = mock.Mock()
        self.win.client.connected = True
        self.win._avatar_busy = True
        from PyQt5.QtWidgets import QFileDialog
        with mock.patch.object(QFileDialog, "getOpenFileName") as dlg:
            self.win.settings_page.avatar_clicked.emit()
        dlg.assert_not_called()


class WxComboBoxDelegateTest(unittest.TestCase):
    """下拉框选项间隔 + 分割线委托。offscreen 校验不了观感，只验接线与几何。"""

    def setUp(self):
        self.tm = ThemeManager()
        # 保证从亮色开始，隔离用例间状态
        self.tm.theme = ThemeManager.LIGHT

    def test_delegate_installed_with_padding(self):
        from ui.widgets import WxComboBox, _WxComboDelegate
        cb = WxComboBox(self.tm)
        cb.addItems(["a", "b", "c"])
        self.assertIsInstance(cb.itemDelegate(), _WxComboDelegate)
        self.assertEqual(cb.itemDelegate()._pad, 6)
        # 行距由 view.setSpacing 提供（委托不再重写 sizeHint）
        self.assertEqual(cb.view().spacing(), 6)

    def test_delegate_parented_to_popup_view(self):
        """委托必须挂弹出视图上，且不能重写 sizeHint：combo 被 GC 回收时
        Qt 会在析构期间回调 sizeHint，若进入已失效的 Python 包装会让
        PyQt 直接 qFatal/访问冲突（全量套件与真机都踩过）。"""
        from ui.widgets import WxComboBox, _WxComboDelegate
        cb = WxComboBox(self.tm)
        cb.addItems(["a", "b"])
        self.assertIs(cb._delegate.parent(), cb.view())
        self.assertNotIn("sizeHint", _WxComboDelegate.__dict__)

    def test_row_spacing_does_not_come_from_sizehint(self):
        from PyQt5.QtWidgets import QStyleOptionViewItem
        from ui.widgets import WxComboBox
        cb = WxComboBox(self.tm)
        cb.addItems(["a", "b", "c"])
        dele = cb.itemDelegate()
        idx = cb.model().index(0, 0)
        # 基类 sizeHint 不再叠加 padding；行距在视图层控制
        self.assertLess(dele.sizeHint(QStyleOptionViewItem(), idx).height(),
                        dele._pad * 2 + 20)

    def test_separator_line_follows_theme_input_border(self):
        from ui.widgets import WxComboBox
        cb = WxComboBox(self.tm)
        dele = cb.itemDelegate()
        # 亮色
        light_line = dele._line
        self.assertEqual(light_line, self.tm.color("input_border"))
        # 切暗色后重新取色
        dark = ThemeManager()
        dark.theme = ThemeManager.DARK
        cb.set_theme(dark)
        self.assertEqual(dele._line, dark.color("input_border"))
        self.assertNotEqual(light_line, dele._line)

    def test_paint_smoke_draws_separator_without_error(self):
        """真实渲染一次 paint，覆盖 sizeHint 之外的绘制路径（含 drawLine）。"""
        from PyQt5.QtCore import QRect
        from PyQt5.QtGui import QColor, QPixmap, QPainter
        from PyQt5.QtWidgets import QStyleOptionViewItem
        from ui.widgets import WxComboBox
        cb = WxComboBox(self.tm)
        cb.addItems(["a", "b", "c"])
        dele = cb.itemDelegate()
        for row in range(cb.count()):
            idx = cb.model().index(row, 0)
            opt = QStyleOptionViewItem()
            opt.rect = QRect(0, 0, 200, 40)
            opt.state = _APP.style().State_Enabled
            pm = QPixmap(200, 40)
            pm.fill(QColor(self.tm.palette.get("conv_bg", "#ffffff")))
            painter = QPainter(pm)
            try:
                dele.paint(painter, opt, idx)
            finally:
                painter.end()
        # 能跑完 paint 不抛异常即通过
        self.assertTrue(True)


class UploadCustomServerTest(unittest.TestCase):
    """「使用自定义服务器」开关 + E2E 密码回写。"""

    class _NoLogin(MainWindow):
        def _try_auto_login(self):
            self.auto_login_skipped = True

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = self._tmp.name
        self._orig = (config.CONFIG_DIR, config.CONFIG_FILE,
                      config.KEY_FILE, config.LEGACY_INI_FILE)
        config.CONFIG_DIR = os.path.join(tmp, "cfgdir")
        config.CONFIG_FILE = os.path.join(config.CONFIG_DIR, "config.yaml")
        config.KEY_FILE = os.path.join(config.CONFIG_DIR, "secret.key")
        config.LEGACY_INI_FILE = os.path.join(tmp, "config.ini")
        install_keyring_mock(self)
        self.page = SettingsPage()

    def tearDown(self):
        self.page.deleteLater()
        if hasattr(self, "win"):
            self.win.realtime and self.win.realtime.stop()
            self.win.deleteLater()
        (config.CONFIG_DIR, config.CONFIG_FILE,
         config.KEY_FILE, config.LEGACY_INI_FILE) = self._orig
        self._tmp.cleanup()

    # ---- 设置页 ---- #
    def test_switch_off_disables_inputs(self):
        self.assertFalse(self.page.upload_custom_switch.isChecked())
        self.assertFalse(self.page.upload_service_input.isEnabled())
        self.assertFalse(self.page.upload_service_token_input.isEnabled())

    def test_restore_enabled_without_signal(self):
        got = []
        self.page.upload_service_toggled.connect(got.append)
        self.page.set_upload_service_enabled(True)
        self.assertTrue(self.page.upload_service_input.isEnabled())
        self.assertTrue(self.page.upload_service_token_input.isEnabled())
        self.assertEqual(got, [], "启动回写不应触发保存信号")

    def test_toggle_emits_and_gates_inputs(self):
        got = []
        self.page.upload_service_toggled.connect(got.append)
        self.page.upload_custom_switch.setChecked(True)
        self.assertEqual(got, [True])
        self.assertTrue(self.page.upload_service_input.isEnabled())

    def test_set_e2e_password_roundtrip(self):
        self.page.set_e2e_password("e2e-secret")
        self.assertEqual(self.page.e2e_input.text(), "e2e-secret")

    # ---- 主窗口接线 ---- #
    def _make_window(self):
        self.win = self._NoLogin()
        self.win.resize(1000, 680)

    def test_toggle_on_saves_and_applies_to_client(self):
        self._make_window()
        self.win.client = mock.Mock()
        page = self.win.settings_page
        page.set_upload_service("http://10.0.0.1/up")
        page.set_upload_service_token("tk")
        page.upload_custom_switch.setChecked(True)
        self.assertEqual(config.load_config()["upload_service_enabled"], True)
        self.assertEqual(self.win.client.upload_service_url, "http://10.0.0.1/up")
        self.assertEqual(self.win.client.upload_service_token, "tk")

    def test_toggle_off_clears_client_url(self):
        config.save_config(server_url="https://s", username="u",
                           upload_service_url="http://10.0.0.1/up",
                           upload_service_token="tk",
                           upload_service_enabled=True)
        self._make_window()
        self.win.client = mock.Mock()
        page = self.win.settings_page
        self.assertEqual(page.upload_custom_switch.isChecked(), True)
        self.win._apply_upload_service_to_client("http://10.0.0.1/up", "tk")
        page.upload_custom_switch.setChecked(False)
        self.assertEqual(self.win.client.upload_service_url, "")
        self.assertEqual(self.win.client.upload_service_token, "")
        # 配置保留，重新开启即恢复
        self.assertEqual(config.load_config()["upload_service_url"],
                         "http://10.0.0.1/up")

    def test_startup_restores_e2e_password(self):
        config.save_config(server_url="https://s", username="u",
                           password="pw", remember_password=True,
                           e2e_password="e2e-saved")
        self._make_window()
        self.assertEqual(self.win.settings_page.e2e_input.text(), "e2e-saved")


if __name__ == "__main__":
    unittest.main()
