"""登录对话框流程验证：点登录后必须解开 loading 并把结果传出去。

回归动因：登录线程管道从对话框抽成 LoginRunner 时，漏把
runner.success / runner.failed 接回对话框，结果登录在后台其实成功了，
界面却永远停在"登录中…"（日志里能看到 E2E 解密成功，但后面没有拉会话列表）。
这里用假的 ApiClient 走完整线程链路，不联网。

运行：python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QEventLoop, QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402
from tests.helpers import install_keyring_mock  # noqa: E402
from core.api_client import ApiAuthError, ApiClient  # noqa: E402
from core.models import User  # noqa: E402
from ui.login_dialog import LoginDialog  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _wait_until(predicate, timeout=15.0):
    """跑事件循环等条件成立（登录在子线程，信号靠队列投递）。"""
    loop = QEventLoop()
    poll = QTimer()
    poll.setInterval(20)
    killer = QTimer()
    killer.setSingleShot(True)
    killer.timeout.connect(loop.quit)
    poll.timeout.connect(lambda: predicate() and loop.quit())
    killer.start(int(timeout * 1000))
    poll.start()
    loop.exec_()
    poll.stop()
    return bool(predicate())


class LoginDialogTest(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self._orig = (config.CONFIG_DIR, config.CONFIG_FILE,
                      config.KEY_FILE, config.LEGACY_INI_FILE)
        config.CONFIG_DIR = tmp
        config.CONFIG_FILE = os.path.join(tmp, "config.yaml")
        config.KEY_FILE = os.path.join(tmp, "secret.key")
        config.LEGACY_INI_FILE = os.path.join(tmp, "none.ini")
        install_keyring_mock(self)
        self._orig_login = ApiClient.login
        self._orig_e2e = ApiClient.create_e2e

    def tearDown(self):
        ApiClient.login = self._orig_login
        ApiClient.create_e2e = self._orig_e2e
        (config.CONFIG_DIR, config.CONFIG_FILE,
         config.KEY_FILE, config.LEGACY_INI_FILE) = self._orig

    def _fill(self, dialog, remember=True):
        dialog.server_input.setText("chat.example.com")
        dialog.user_input.setText("myname")
        dialog.pass_input.setText("s3cret!")
        dialog.remember_check.setChecked(remember)

    def test_success_leaves_loading_and_emits_login_ok(self):
        def fake_login(self, username, password):
            self.user_id = "uid1"
            self.auth_token = "tok1"
            self.me = User(id="uid1", username=username, name="我")
            return self.me
        ApiClient.login = fake_login

        dialog = LoginDialog()
        ok = []
        dialog.login_ok.connect(lambda client: ok.append(client))
        self._fill(dialog)
        dialog._on_login_clicked()

        # 点下去应该立刻进入 loading 态
        self.assertFalse(dialog.login_button.isEnabled())
        self.assertEqual(dialog.login_button.text(), "登录中…")

        self.assertTrue(_wait_until(lambda: len(ok) == 1),
                        "登录线程已完成，但 login_ok 没有发出（信号未接回）")
        self.assertTrue(dialog.isVisible() is False, "对话框应随登录成功关闭")
        self.assertTrue(dialog.login_button.isEnabled())
        self.assertEqual(dialog.login_button.text(), "登 录")

        cfg = config.load_config()
        self.assertEqual(cfg["server_url"], "https://chat.example.com")
        self.assertEqual(cfg["username"], "myname")
        self.assertTrue(cfg["remember_password"])
        self.assertEqual(cfg["password"], "s3cret!")   # 解密回来应一致
        dialog.deleteLater()

    def test_failure_reenables_button_and_shows_error(self):
        def boom(self, username, password):
            raise ApiAuthError("登录失败：用户名或密码错误")
        ApiClient.login = boom

        dialog = LoginDialog()
        ok = []
        dialog.login_ok.connect(lambda client: ok.append(client))
        self._fill(dialog)
        dialog._on_login_clicked()

        self.assertTrue(_wait_until(lambda: bool(dialog.error_label.text())),
                        "登录失败后错误文案没出现，界面卡在登录中")
        self.assertIn("用户名或密码错误", dialog.error_label.text())
        self.assertTrue(dialog.login_button.isEnabled(), "失败后按钮没有恢复")
        self.assertEqual(dialog.login_button.text(), "登 录")
        self.assertEqual(ok, [], "失败路径不应发出 login_ok")
        # 失败不该把密码写盘
        self.assertFalse(os.path.exists(config.CONFIG_FILE))
        dialog.deleteLater()

    def test_remember_unchecked_keeps_password_out_of_config(self):
        def fake_login(self, username, password):
            self.me = User(id="u", username=username, name="我")
            return self.me
        ApiClient.login = fake_login

        dialog = LoginDialog()
        ok = []
        dialog.login_ok.connect(lambda c: ok.append(c))
        self._fill(dialog, remember=False)
        dialog._on_login_clicked()
        self.assertTrue(_wait_until(lambda: len(ok) == 1))

        cfg = config.load_config()
        self.assertFalse(cfg["remember_password"])
        self.assertEqual(cfg["password"], "")
        # 没存密码就不可能自动登录
        self.assertFalse(cfg["remember_password"] and cfg["password"])
        dialog.deleteLater()

    def test_login_runner_reuses_one_thread_per_click(self):
        """连点两次不该留下两个在跑的线程。"""
        started = []

        def fake_login(self, username, password):
            started.append(username)
            self.me = User(id="u", username=username)
            return self.me
        ApiClient.login = fake_login

        dialog = LoginDialog()
        ok = []
        dialog.login_ok.connect(lambda c: ok.append(c))
        self._fill(dialog)
        dialog._on_login_clicked()
        self.assertTrue(_wait_until(lambda: len(ok) == 1))
        dialog._set_loading(False)
        dialog._on_login_clicked()
        self.assertTrue(_wait_until(lambda: len(ok) == 2))
        self.assertEqual([u for u in started], ["myname", "myname"])
        self.assertIsNone(dialog._runner._thread, "线程收尾后应放下引用")
        dialog.deleteLater()

    def test_e2e_password_comes_from_config_not_dialog(self):
        """E2E 密码统一在设置页配置：登录框不再有输入，登录时用已保存值。"""
        calls = []

        def fake_login(self, username, password):
            self.me = User(id="u", username=username, name="我")
            return self.me

        def fake_create_e2e(self, pw):
            calls.append(pw)
            return True

        ApiClient.login = fake_login
        ApiClient.create_e2e = fake_create_e2e
        config.save_e2e_password("pw-from-settings")

        dialog = LoginDialog()
        ok = []
        dialog.login_ok.connect(lambda c: ok.append(c))
        self._fill(dialog)
        dialog._on_login_clicked()
        self.assertTrue(_wait_until(lambda: len(ok) == 1))
        self.assertFalse(hasattr(dialog, "e2e_input"),
                         "登录框不应再有 E2E 密码输入")
        self.assertEqual(calls, ["pw-from-settings"],
                         "登录管道应使用 config 已保存的 E2E 密码")
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
