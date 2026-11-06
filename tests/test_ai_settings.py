"""AI 设置页与主窗口 AI 配置/隐私门禁接线测试。

offscreen；网络调用一律 mock，配置目录指向临时目录。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402
from tests.helpers import install_keyring_mock  # noqa: E402
from ui.icons import icon  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.settings_page import SettingsPage  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class _NoLogin(MainWindow):
    def _try_auto_login(self):
        self.auto_login_skipped = True


class SettingsAIPageTest(unittest.TestCase):
    def setUp(self):
        self.page = SettingsPage()

    def tearDown(self):
        self.page.deleteLater()

    def test_ai_icon_exists(self):
        self.assertFalse(icon("ai", 16).isNull())

    def test_nav_has_ai_and_show_ai_page(self):
        texts = [b.text().strip() for b in self.page._nav_buttons]
        idx = texts.index("AI")
        self.page.show_ai_page()
        self.assertEqual(self.page._stack.currentIndex(), idx)
        self.assertTrue(self.page._nav_buttons[idx].isChecked())

    def test_default_off_and_fields_disabled(self):
        self.page.set_llm_config("https://x/v1", "m", "tk", enabled=False)
        self.assertFalse(self.page.llm_enabled_switch.isChecked())
        self.assertFalse(self.page.llm_url_input.isEnabled())
        self.assertFalse(self.page.llm_model_input.isEnabled())
        self.assertFalse(self.page.llm_token_input.isEnabled())

    def test_enabled_fields_editable(self):
        self.page.set_llm_config("https://x/v1", "m", "tk", enabled=True)
        self.assertTrue(self.page.llm_enabled_switch.isChecked())
        self.assertTrue(self.page.llm_url_input.isEnabled())
        self.assertEqual(self.page.llm_url_input.text(), "https://x/v1")
        self.assertEqual(self.page.llm_model_input.text(), "m")
        self.assertEqual(self.page.llm_token_input.text(), "tk")
        self.assertEqual(
            self.page.llm_token_input.echoMode(),
            self.page.llm_token_input.Password)

    def test_set_config_does_not_emit(self):
        enabled, changed, tested = [], [], []
        self.page.llm_enabled_toggled.connect(enabled.append)
        self.page.llm_config_changed.connect(
            lambda *a: changed.append(a))
        self.page.llm_test_requested.connect(lambda *a: tested.append(a))
        self.page.set_llm_config("https://x", "m", "t", enabled=True)
        self.assertEqual((enabled, changed, tested), ([], [], []))

    def test_switch_emits_and_toggles_fields(self):
        got = []
        self.page.llm_enabled_toggled.connect(got.append)
        self.page.llm_enabled_switch.setChecked(True)
        self.assertEqual(got, [True])
        self.assertTrue(self.page.llm_url_input.isEnabled())
        self.page.llm_enabled_switch.setChecked(False)
        self.assertEqual(got, [True, False])
        self.assertFalse(self.page.llm_url_input.isEnabled())

    def test_save_emits_trimmed_values(self):
        got = []
        self.page.llm_config_changed.connect(lambda *a: got.append(a))
        self.page.llm_url_input.setText("  https://x/v1 ")
        self.page.llm_model_input.setText(" m ")
        self.page.llm_token_input.setText(" tk ")
        self.page._on_llm_save()
        self.assertEqual(got, [("https://x/v1", "m", "tk")])

    def test_save_requires_url_and_model(self):
        got = []
        self.page.llm_config_changed.connect(lambda *a: got.append(a))
        self.page.llm_url_input.setText("")
        self.page.llm_model_input.setText("m")
        self.page._on_llm_save()
        self.assertEqual(got, [])
        self.assertIn("服务地址", self.page.llm_hint.text())

    def test_test_emits_current_fields(self):
        got = []
        self.page.llm_test_requested.connect(lambda *a: got.append(a))
        self.page.llm_url_input.setText("https://x/v1")
        self.page.llm_model_input.setText("m")
        self.page.llm_token_input.setText("tk")
        self.page._on_llm_test()
        self.assertEqual(got, [("https://x/v1", "m", "tk")])

    def test_test_busy_disables_button(self):
        self.page.set_llm_test_busy(True)
        self.assertFalse(self.page.llm_test_btn.isEnabled())
        self.assertIn("正在测试", self.page.llm_hint.text())
        self.page.set_llm_test_busy(False)
        self.assertTrue(self.page.llm_test_btn.isEnabled())

    def test_nav_click_requests_refresh(self):
        got = []
        self.page.llm_refresh_requested.connect(lambda: got.append(True))
        for btn in self.page._nav_buttons:
            if btn.text().strip() == "AI":
                btn.click()
                break
        self.assertEqual(got, [True])


class MainWindowAITest(unittest.TestCase):
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
        self.win = _NoLogin()

    def tearDown(self):
        self.win.realtime and self.win.realtime.stop()
        self.win.deleteLater()
        (config.CONFIG_DIR, config.CONFIG_FILE,
         config.KEY_FILE, config.LEGACY_INI_FILE) = self._orig
        self._tmp.cleanup()

    def test_startup_restores_llm_config(self):
        config.save_llm(url="https://x/v1", model="m", token="tk",
                        enabled=True, consent=True)
        second = _NoLogin()
        try:
            page = second.settings_page
            self.assertTrue(page.llm_enabled_switch.isChecked())
            self.assertEqual(page.llm_url_input.text(), "https://x/v1")
            self.assertEqual(page.llm_model_input.text(), "m")
            self.assertEqual(page.llm_token_input.text(), "tk")
        finally:
            second.realtime and second.realtime.stop()
            second.deleteLater()

    def test_toggle_persists_and_preserves_config(self):
        config.save_llm(url="https://x/v1", model="m", token="tk")
        self.win.settings_page.llm_enabled_switch.setChecked(True)
        cfg = config.load_config()
        self.assertTrue(cfg["llm_enabled"])
        self.assertEqual(cfg["llm_url"], "https://x/v1")
        self.assertEqual(cfg["llm_model"], "m")
        self.assertEqual(cfg["llm_token"], "tk")
        self.win.settings_page.llm_enabled_switch.setChecked(False)
        self.assertFalse(config.load_config()["llm_enabled"])

    def test_save_config_persists_and_keeps_consent_when_url_same(self):
        config.save_llm(url="https://x/v1", model="old", consent=True)
        self.win.settings_page.llm_url_input.setText("https://x/v1")
        self.win.settings_page.llm_model_input.setText("new-model")
        self.win.settings_page.llm_token_input.setText("new-token")
        self.win.settings_page._on_llm_save()
        cfg = config.load_config()
        self.assertEqual(cfg["llm_model"], "new-model")
        self.assertEqual(cfg["llm_token"], "new-token")
        self.assertTrue(cfg["llm_consent"])
        self.assertIn("已保存", self.win.settings_page.llm_hint.text())

    def test_changing_url_resets_consent(self):
        config.save_llm(url="https://old/v1", model="m", consent=True)
        self.win.settings_page.llm_url_input.setText("https://new/v1")
        self.win.settings_page.llm_model_input.setText("m")
        self.win.settings_page._on_llm_save()
        cfg = config.load_config()
        self.assertEqual(cfg["llm_url"], "https://new/v1")
        self.assertFalse(cfg["llm_consent"])
        self.assertIn("重新确认", self.win.settings_page.llm_hint.text())

    def test_test_connection_success_updates_hint(self):
        class FakeClient:
            def __init__(self, url, token, model, **kwargs):
                self.args = (url, token, model)

            def test(self):
                return "连接正常"

            def close(self):
                pass

        def run_now(fn, on_done=None, on_fail=None, parent=None):
            try:
                result = fn()
            except Exception as e:  # noqa: BLE001
                on_fail(str(e))
            else:
                on_done(result)

        with mock.patch("core.ai_client.AIClient", FakeClient), \
                mock.patch("ui.main_window.run_async", run_now):
            self.win._on_llm_test_requested("https://x/v1", "m", "tk")
        self.assertIn("连接正常", self.win.settings_page.llm_hint.text())
        self.assertTrue(self.win.settings_page.llm_test_btn.isEnabled())

    def test_test_connection_failure_updates_hint(self):
        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            def test(self):
                raise RuntimeError("401 未授权")

            def close(self):
                pass

        def run_now(fn, on_done=None, on_fail=None, parent=None):
            try:
                result = fn()
            except Exception as e:  # noqa: BLE001
                on_fail(str(e))
            else:
                on_done(result)

        with mock.patch("core.ai_client.AIClient", FakeClient), \
                mock.patch("ui.main_window.run_async", run_now):
            self.win._on_llm_test_requested("https://x/v1", "m", "bad")
        self.assertIn("连接失败", self.win.settings_page.llm_hint.text())
        self.assertIn("401", self.win.settings_page.llm_hint.text())

    # ---- 隐私门禁 ---- #
    def test_gate_disabled_notifies_and_opens_ai_page(self):
        with mock.patch("ui.main_window.notify") as m_notify, \
                mock.patch.object(self.win, "_open_settings_ai_page") as m_open:
            self.assertFalse(self.win._ensure_ai_ready())
        m_notify.assert_called_once()
        m_open.assert_called_once()

    def test_gate_missing_config_notifies(self):
        config.save_llm(enabled=True)
        with mock.patch("ui.main_window.notify") as m_notify, \
                mock.patch.object(self.win, "_open_settings_ai_page"):
            self.assertFalse(self.win._ensure_ai_ready())
        m_notify.assert_called_once()

    def test_gate_first_use_confirms_and_saves_consent(self):
        config.save_llm(url="https://x/v1", model="m", enabled=True)
        with mock.patch("ui.main_window.confirm", return_value=True) as m_confirm:
            self.assertTrue(self.win._ensure_ai_ready())
        m_confirm.assert_called_once()
        self.assertTrue(config.load_config()["llm_consent"])

    def test_gate_first_use_declined(self):
        config.save_llm(url="https://x/v1", model="m", enabled=True)
        with mock.patch("ui.main_window.confirm", return_value=False):
            self.assertFalse(self.win._ensure_ai_ready())
        self.assertFalse(config.load_config()["llm_consent"])

    def test_gate_ready_has_no_dialog(self):
        config.save_llm(url="https://x/v1", model="m", enabled=True,
                        consent=True)
        with mock.patch("ui.main_window.confirm") as m_confirm, \
                mock.patch("ui.main_window.notify") as m_notify:
            self.assertTrue(self.win._ensure_ai_ready())
        m_confirm.assert_not_called()
        m_notify.assert_not_called()

    def test_gate_e2e_requires_second_confirm(self):
        config.save_llm(url="https://x/v1", model="m", enabled=True,
                        consent=True)
        with mock.patch("ui.main_window.confirm", return_value=False) as m_confirm:
            self.assertFalse(self.win._ensure_ai_ready(encrypted=True))
        self.assertEqual(m_confirm.call_count, 1)
        self.assertTrue(m_confirm.call_args.kwargs.get("danger"))

    def test_gate_e2e_confirmed_allows(self):
        config.save_llm(url="https://x/v1", model="m", enabled=True,
                        consent=True)
        with mock.patch("ui.main_window.confirm", return_value=True):
            self.assertTrue(self.win._ensure_ai_ready(encrypted=True))

    def test_ai_client_from_config(self):
        config.save_llm(url="https://x/v1", model="m", token="tk")
        client = self.win._ai_client()
        try:
            self.assertEqual(client.endpoint,
                             "https://x/v1/chat/completions")
            self.assertEqual(client.model, "m")
            self.assertEqual(client.token, "tk")
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
