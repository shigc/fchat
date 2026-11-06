"""设置页 E2E 密码即时生效流程：保存→重新解私钥→成功刷新加密房占位。

回归动因：桌面端保存的 E2E 密码在网页端改密后失效，而设置页只落盘不
重新初始化，导致加密消息一直显示占位且没有任何入口能修复。改密后必须
即时重跑 init_keys，失败就地红字提示，成功自动重拉含占位消息的房间。

运行：python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402
from tests.helpers import install_keyring_mock  # noqa: E402
from core.messages import E2E_PLACEHOLDER_TEXT  # noqa: E402
from core.models import Conversation, Message, User  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _sync_run_async(fn, on_done=None, on_fail=None, parent=None):
    """run_async 同步替身：当前线程直接执行并回调。"""
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001
        if on_fail:
            on_fail(e)
        return
    if on_done:
        on_done(result)


class _StubE2E:
    """E2EManager 替身：只复刻主窗口改密流程碰到的字段。"""

    def __init__(self, ok=True):
        self.password = "old"
        self.enabled = True
        self._priv_key = object()
        self._room_keys = {"r": ("kid", b"k")}
        self._lock = threading.RLock()
        self._ok = ok
        self.init_calls = []

    def init_keys(self):
        self.init_calls.append(self.password)
        self.enabled = self._ok
        return self._ok


class _StubClient:
    connected = True

    def __init__(self, e2e=None):
        self.e2e = e2e
        self.created = []
        self.history_calls = []
        self.history_result = []

    def create_e2e(self, pw):
        self.created.append(pw)
        self.e2e = _StubE2E()
        self.e2e.password = pw
        return True

    def get_history(self, rid, count=50, offset=0, room_type="c"):
        self.history_calls.append((rid, count, offset, room_type))
        return self.history_result


class _NoLogin(MainWindow):

    def _try_auto_login(self):
        self.auto_login_skipped = True


class E2EPasswordFlowTest(unittest.TestCase):

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
        self.win.resize(1000, 680)

    def tearDown(self):
        self.win.realtime and self.win.realtime.stop()
        self.win.deleteLater()
        (config.CONFIG_DIR, config.CONFIG_FILE,
         config.KEY_FILE, config.LEGACY_INI_FILE) = self._orig
        self._tmp.cleanup()

    def _edit(self, pw):
        self.win.settings_page.e2e_input.setText(pw)
        with mock.patch("ui.main_window.run_async", _sync_run_async):
            self.win._on_e2e_password_edited()

    # ---- 未登录：只落盘 ---- #
    def test_save_before_login_only_persists(self):
        self.win.client = None
        self._edit("newpw")
        self.assertEqual(config.load_config()["e2e_password"], "newpw")
        self.assertEqual(self.win.settings_page.e2e_hint.text(), "已保存，登录后生效")

    # ---- 未配置过 e2e：现场创建 ---- #
    def test_first_time_password_creates_manager_and_enables(self):
        client = _StubClient(e2e=None)
        self.win.client = client
        self._edit("newpw")
        self.assertEqual(client.created, ["newpw"])
        self.assertEqual(client.e2e.init_calls, ["newpw"])
        self.assertEqual(self.win.settings_page.e2e_hint.text(), "解密已启用")
        self.assertEqual(config.load_config()["e2e_password"], "newpw")

    # ---- 已有 manager：重置并重跑，缓存作废 ---- #
    def test_existing_manager_reset_and_reinit(self):
        stub = _StubE2E()
        stub.password = "old"
        client = _StubClient(e2e=stub)
        self.win.client = client
        self._edit("newpw")
        self.assertIs(client.e2e, stub)
        self.assertEqual(stub.password, "newpw")
        self.assertTrue(stub.enabled)  # init_keys 成功后重新置位
        self.assertIsNone(stub._priv_key)  # 重置时已作废（stub 不重建）
        self.assertEqual(stub._room_keys, {})
        self.assertEqual(stub.init_calls, ["newpw"])
        self.assertEqual(self.win.settings_page.e2e_hint.text(), "解密已启用")

    # ---- 密码没变：不重复验证 ---- #
    def test_unchanged_password_skips_reinit(self):
        stub = _StubE2E()
        stub.password = "same"
        client = _StubClient(e2e=stub)
        self.win.client = client
        self._edit("same")
        self.assertEqual(stub.init_calls, [])
        self.assertEqual(self.win.settings_page.e2e_hint.text(), "")

    # ---- 密码错误：红字提示 ---- #
    def test_wrong_password_shows_error_hint(self):
        stub = _StubE2E(ok=False)
        stub.password = "old"
        client = _StubClient(e2e=stub)
        self.win.client = client
        self._edit("wrong")
        hint = self.win.settings_page
        self.assertIn("不正确", hint.e2e_hint.text())
        self.assertEqual(hint.e2e_hint._variant, "danger")
        # 配置仍按用户输入落盘（下次登录还会试）
        self.assertEqual(config.load_config()["e2e_password"], "wrong")

    # ---- 清空：停用 E2E ---- #
    def test_clear_password_disables_e2e(self):
        stub = _StubE2E()
        client = _StubClient(e2e=stub)
        self.win.client = client
        self._edit("")
        self.assertIsNone(client.e2e)
        self.assertIn("已清除", self.win.settings_page.e2e_hint.text())
        self.assertEqual(config.load_config()["e2e_password"], "")

    # ---- 成功后自动刷新占位房间 ---- #
    def test_success_refreshes_placeholder_rooms(self):
        client = _StubClient()
        client.history_result = [
            Message(id="m2", room_id="r1", text="明文了",
                    sender=User(username="u", name="张三"),
                    timestamp=2.0, msg_type="text"),
        ]
        self.win.client = client
        placeholder = Message(id="m1", room_id="r1",
                              text=E2E_PLACEHOLDER_TEXT,
                              sender=User(username="u", name="张三"),
                              timestamp=1.0, msg_type="system")
        normal = Message(id="m3", room_id="r2", text="普通",
                         sender=User(username="u", name="张三"),
                         timestamp=1.0, msg_type="text")
        self.win._room_messages = {"r1": [placeholder], "r2": [normal]}
        self.win._conversations = {"r1": Conversation(id="r1", room_type="c",
                                                      name="g")}
        with mock.patch("ui.main_window.run_async", _sync_run_async):
            self.win._refresh_encrypted_rooms()
        self.assertEqual(client.history_calls,
                         [("r1", self.win._page_size, 0, "c")])
        self.assertEqual([m.text for m in self.win._room_messages["r1"]],
                         ["明文了"])
        self.assertEqual(self.win._history_offset["r1"], 1)
        self.assertEqual(self.win._history_more["r1"], False)

    def test_refresh_skips_rooms_without_conversation_entry(self):
        client = _StubClient()
        self.win.client = client
        placeholder = Message(id="m1", room_id="rx",
                              text=E2E_PLACEHOLDER_TEXT,
                              sender=User(username="u", name="张三"),
                              timestamp=1.0, msg_type="system")
        self.win._room_messages = {"rx": [placeholder]}
        self.win._conversations = {}
        self.win._refresh_encrypted_rooms()
        self.assertEqual(client.history_calls, [])


if __name__ == "__main__":
    unittest.main()
