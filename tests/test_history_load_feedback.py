"""慢网络下的两条体验修复：REST 超时分级 + 历史加载状态条。

背景（真机日志）：点开私聊后白屏几十秒——库默认 30 秒超时吃满了冷连接抖动，
而加载期间界面没有任何反馈、失败只写状态栏，重复点击又被在途去重静默挡掉。

覆盖两层：
- core/api_client：登录/恢复会话传 (5, 20)，上传路径临时放宽到 (10, 120) 并恢复；
- ui/main_window：视图空白时显示「正在加载」，失败显示「加载失败 + 重试」，
  重试重新发起加载，成功/切房间清掉状态条。

offscreen 只能验证可见性/文案/接线，验不了文字渲染与观感。
Qt 相关：在 import Qt 之前设 offscreen；run_async 用同步替身；全程不联网。

运行：python -m unittest discover -s tests -v
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
from core import api_client as ac  # noqa: E402
from core.api_client import ApiClient, ApiError  # noqa: E402
from core.models import Conversation, Message, User  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def sync_run_async(fn, on_done=None, on_fail=None, parent=None):
    """run_async 同步替身：当前线程直接执行并回调。"""
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001
        if on_fail:
            on_fail(e)
        return
    if on_done:
        on_done(result)


# --------------------------------------------------------------------- #
# 改动 A：超时分级（纯 core，离线）
# --------------------------------------------------------------------- #
def _rc_stub():
    """能撑起 login/login_with_token 的假 RocketChat。"""
    rc = mock.Mock()
    rc.headers = {"X-Auth-Token": "tok", "X-User-Id": "u1"}
    rc.timeout = 30          # 库默认的构造后值
    rc.me.return_value = {"data": {"_id": "u1", "username": "alice",
                                   "name": "Alice"}}
    return rc


class TimeoutTest(unittest.TestCase):

    def _client(self):
        c = ApiClient("https://s.example.com")
        c._rc = _rc_stub()
        return c

    def test_login_passes_interactive_timeout(self):
        c = self._client()
        with mock.patch("core.api_client.RocketChat",
                        return_value=c._rc) as rc:
            c.login("alice", "pwd-not-printed")
        self.assertEqual(rc.call_args.kwargs.get("timeout"), (5, 20))

    def test_login_with_token_passes_interactive_timeout(self):
        c = self._client()
        with mock.patch("core.api_client.RocketChat",
                        return_value=c._rc) as rc:
            c.login_with_token("u1", "tok")
        self.assertEqual(rc.call_args.kwargs.get("timeout"), (5, 20))

    def test_constants_are_connect_read_pairs(self):
        """元组语义：(连接超时, 读超时)——requests 与库都原样透传。"""
        self.assertEqual(ac._INTERACTIVE_TIMEOUT, (5, 20))
        self.assertEqual(ac._UPLOAD_TIMEOUT, (10, 120))

    def test_login_paths_size_connection_pool(self):
        """启动突发并发会超过 requests 默认的 10 条连接池（真机日志
        "Connection pool is full ... pool size: 10"），排队建连曾把
        im.members 拖到 20s 读超时；两条登录路径都要把池调大。"""
        import requests
        for method, args in (("login", ("alice", "pwd-not-printed")),
                             ("login_with_token", ("u1", "tok"))):
            c = ApiClient("https://s.example.com")
            session = requests.Session()
            c._rc = _rc_stub()
            c._rc.session = session
            with mock.patch("core.api_client.RocketChat",
                            return_value=c._rc):
                getattr(c, method)(*args)
            adapter = session.get_adapter("https://s.example.com")
            self.assertEqual(adapter._pool_maxsize, ac._SESSION_POOL_SIZE)
            self.assertEqual(adapter._pool_connections,
                             ac._SESSION_POOL_SIZE)

    def test_rc_timeout_restores_previous_value(self):
        c = self._client()
        with c._rc_timeout(ac._UPLOAD_TIMEOUT):
            self.assertEqual(c._rc.timeout, (10, 120))
        self.assertEqual(c._rc.timeout, 30)

    def test_rc_timeout_restores_after_exception(self):
        c = self._client()
        with self.assertRaises(ZeroDivisionError):
            with c._rc_timeout(ac._UPLOAD_TIMEOUT):
                1 / 0
        self.assertEqual(c._rc.timeout, 30)

    def test_rc_timeout_without_client_is_noop(self):
        c = ApiClient("https://s.example.com")
        with c._rc_timeout(ac._UPLOAD_TIMEOUT):   # 未登录不能炸
            pass

    def _tmp_file(self, suffix=".txt"):
        f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        f.write(b"hello")
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_upload_media_widens_and_restores(self):
        c = self._client()
        seen = []

        def media(rid, path):
            seen.append(c._rc.timeout)
            return {"success": True, "file": {"_id": "f1",
                                              "url": "/files/f1"}}

        c._rc.rooms_media.side_effect = media
        fid = c._upload_media("r1", self._tmp_file())
        self.assertEqual(seen, [(10, 120)])
        self.assertEqual(c._rc.timeout, 30)
        self.assertEqual(fid, "f1")

    def test_upload_confirm_widens_and_restores(self):
        c = self._client()
        seen = []

        def confirm(rid, fid, **kwargs):
            seen.append(c._rc.timeout)
            return {"message": {"_id": "m1"}}

        c._rc.rooms_media_confirm.side_effect = confirm
        c._upload_confirm("r1", "f1")
        self.assertEqual(seen, [(10, 120)])
        self.assertEqual(c._rc.timeout, 30)

    def test_send_file_keeps_widened_window_through_confirm(self):
        """rooms.media 与紧随的 mediaConfirm 都在放宽窗口内。"""
        c = self._client()
        seen = []

        def media(rid, path):
            seen.append(("media", c._rc.timeout))
            return {"success": True, "file": {"_id": "f1",
                                              "url": "/files/f1"}}

        def confirm(rid, fid, **kwargs):
            seen.append(("confirm", c._rc.timeout))
            return {"message": {"_id": "m1"}}

        c._rc.rooms_media.side_effect = media
        c._rc.rooms_media_confirm.side_effect = confirm
        self.assertTrue(c.send_file("r1", self._tmp_file()))
        self.assertEqual(seen, [("media", (10, 120)),
                                ("confirm", (10, 120))])
        self.assertEqual(c._rc.timeout, 30)

    def test_send_file_failure_restores_timeout(self):
        c = self._client()
        c._rc.rooms_media.side_effect = RuntimeError("网络超时")
        with self.assertRaises(ApiError):
            c.send_file("r1", self._tmp_file())
        self.assertEqual(c._rc.timeout, 30)

    def test_mixed_upload_reuses_widened_window(self):
        """send_mixed 的原生路径（上传 + confirm）同样吃放宽窗口且不残留。"""
        c = self._client()
        seen = []

        def media(rid, path):
            seen.append(("media", c._rc.timeout))
            return {"success": True, "file": {"_id": "f1",
                                              "url": "/files/f1"}}

        def confirm(rid, fid, **kwargs):
            seen.append(("confirm", c._rc.timeout))
            return {"message": {"_id": "m1"}}

        c._rc.rooms_media.side_effect = media
        c._rc.rooms_media_confirm.side_effect = confirm
        c._message_from_json = mock.Mock(side_effect=lambda m, rid: m)
        ret = c.send_mixed("r1", [("file", self._tmp_file())])
        self.assertEqual(seen, [("media", (10, 120)),
                                ("confirm", (10, 120))])
        self.assertEqual(c._rc.timeout, 30)
        self.assertEqual(ret["_id"], "m1")


# --------------------------------------------------------------------- #
# 改动 B：历史加载状态条（MainWindow 装配）
# --------------------------------------------------------------------- #
class _NoLogin(MainWindow):
    """跳过登录：_try_auto_login 里是阻塞的登录流程。"""

    def _try_auto_login(self):
        self.auto_login_skipped = True


def _msg(mid="m1", rid="R1", ts=1700000000.0):
    return Message(id=mid, room_id=rid, is_mine=False,
                   sender=User(id="u2", username="bob", name="Bob"),
                   timestamp=ts, text="你好")


class HistoryNoticeTest(unittest.TestCase):

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
        self.win.resize(1000, 680)
        self.area = self.win.chat_area
        self.notice = self.area.history_notice
        self.retry_btn = self.area.history_retry_btn

        self.client = mock.Mock()
        self.client.server_url = "http://s"
        self.client.user_id = "me"
        self.client.me = User(id="me", username="alice", name="Alice")
        self.client.get_history.return_value = [_msg()]
        self.client.fetch_room_members.return_value = []
        self.win.client = self.client
        self.win.realtime = mock.Mock()

        # 磁盘快照与网络都替身掉：本用例只关心状态条
        self._snaps = [
            mock.patch("core.msg_cache.load_messages_snapshot",
                       return_value=[]),
            mock.patch("core.msg_cache.save_messages_snapshot"),
            mock.patch("ui.main_window.run_async", sync_run_async),
            # 回执拉取的 600ms 定时器与本案无关，掐掉避免跨用例触发
            mock.patch.object(self.win, "_schedule_receipts_fetch"),
            # 状态拉取失败的 65s 延时重试定时器同理
            mock.patch.object(self.win, "_retry_status_later"),
        ]
        for p in self._snaps:
            p.start()

        self.view = self.area.chat_view
        self.win._active_room_id = "R1"
        self.win._room_type = "d"
        self.view.set_room("R1")

    def tearDown(self):
        for p in reversed(self._snaps):
            p.stop()
        self.win.realtime = None
        self.win.deleteLater()
        (config.CONFIG_DIR, config.CONFIG_FILE,
         config.KEY_FILE, config.LEGACY_INI_FILE) = self._orig
        self._tmp.cleanup()

    def _conv(self, rid="R1", name="Bob"):
        return Conversation(id=rid, name=name, room_type="d", username="bob")

    def _visible(self):
        return self.notice.isVisibleTo(self.win)

    # ---- 初始态 ---- #
    def test_notice_hidden_by_default(self):
        self.assertFalse(self._visible())
        self.assertFalse(self.retry_btn.isVisibleTo(self.win))

    # ---- loading ---- #
    def test_empty_view_shows_loading_while_requesting(self):
        seen = []

        def history(rid, count=0, offset=0, room_type="c"):
            seen.append((self._visible(),
                         self.retry_btn.isVisibleTo(self.win),
                         self.area.history_label.text()))
            return [_msg()]

        self.client.get_history.side_effect = history
        self.win._load_history(self._conv())
        self.assertEqual(seen, [(True, False, "正在加载历史消息…")])
        # 成功落地后横幅收掉
        self.assertFalse(self._visible())

    def test_memory_cache_suppresses_loading(self):
        self.win._room_messages["R1"] = [_msg()]
        loaded = []
        self.client.get_history.side_effect = lambda *a, **k: loaded.append(
            self._visible()) or [_msg()]
        self.win._load_history(self._conv())
        self.assertEqual(loaded, [False])

    def test_disk_snapshot_suppresses_loading(self):
        """快照已渲染进缓存：本地已有内容，不该再占一条「正在加载」。"""
        with mock.patch("core.msg_cache.load_messages_snapshot",
                        return_value=[_msg()]):
            shown = []
            self.client.get_history.side_effect = lambda *a, **k: shown.append(
                self._visible()) or [_msg()]
            self.win._load_history(self._conv())
        self.assertEqual(shown, [False])

    def test_prefetch_other_room_does_not_touch_notice(self):
        """预取别的房间不能把当前房间的横幅刷乱。"""
        self.win._load_history(self._conv(rid="OTHER"))
        self.assertFalse(self._visible())
        self.client.get_history.assert_called_once()

    # ---- error + 重试 ---- #
    def test_failure_shows_error_with_retry(self):
        self.client.get_history.side_effect = RuntimeError("读超时")
        self.win._load_history(self._conv())
        self.assertTrue(self._visible())
        self.assertTrue(self.retry_btn.isVisibleTo(self.win))
        self.assertIn("历史消息加载失败", self.area.history_label.text())
        self.assertIn("读超时", self.area.history_label.text())
        self.assertEqual(self.win._history_retry_conv.id, "R1")

    def test_error_with_content_present_is_not_shown(self):
        """已经有内容（缓存/快照）在屏上时，失败不该盖横幅。"""
        self.win._room_messages["R1"] = [_msg()]
        self.client.get_history.side_effect = RuntimeError("读超时")
        self.win._load_history(self._conv())
        self.assertFalse(self._visible())

    def test_retry_button_reissues_load_and_hides_on_success(self):
        self.client.get_history.side_effect = [RuntimeError("读超时"),
                                               [_msg()]]
        self.win._load_history(self._conv())
        self.assertTrue(self._visible())
        self.retry_btn.click()
        self.assertEqual(self.client.get_history.call_count, 2)
        self.assertFalse(self._visible())
        self.assertIsNone(self.win._history_retry_conv)

    def test_retry_ignored_when_room_switched(self):
        self.client.get_history.side_effect = RuntimeError("读超时")
        self.win._load_history(self._conv())
        self.win._active_room_id = "OTHER"
        self.retry_btn.click()
        self.assertEqual(self.client.get_history.call_count, 1)

    def test_retry_ignored_while_request_in_flight(self):
        self.client.get_history.side_effect = RuntimeError("读超时")
        self.win._load_history(self._conv())
        self.win._history_loading.add("R1")
        self.retry_btn.click()
        self.assertEqual(self.client.get_history.call_count, 1)

    def test_retry_without_client_does_nothing(self):
        self.client.get_history.side_effect = RuntimeError("读超时")
        self.win._load_history(self._conv())
        self.win.client = None
        self.retry_btn.click()
        self.assertEqual(self.client.get_history.call_count, 1)

    # ---- 切房间 ---- #
    def test_open_conversation_shows_loading_for_empty_room(self):
        conv = self._conv()
        seen = []
        self.client.get_history.side_effect = lambda *a, **k: seen.append(
            self._visible()) or [_msg()]
        self.win._open_conversation(conv)
        self.assertEqual(seen, [True])
        self.assertFalse(self._visible())

    def test_switching_room_clears_previous_notice(self):
        """残留的失败态在切房间时必须收掉（新房间要不要显示由
        _load_history 决定）。"""
        self.client.get_history.side_effect = RuntimeError("读超时")
        self.win._load_history(self._conv())
        self.assertTrue(self._visible())
        with mock.patch.object(self.win, "_load_history") as load:
            self.win._open_conversation(self._conv(rid="R2", name="Carol"))
        self.assertFalse(self._visible())
        self.assertIsNone(self.win._history_retry_conv)
        self.assertEqual(load.call_args[0][0].id, "R2")

    # ---- 主题接线（只验取色路径接上了，不验观感） ---- #
    def test_notice_theme_follows_manager(self):
        tm = self.win.theme_manager
        tm.theme = "light"
        self.area.set_preview_theme(tm)
        light = self.notice.styleSheet()
        self.assertIn("#HistoryNotice", light)
        self.assertEqual(self.area.history_label._theme, tm)
        tm.theme = "dark"
        self.area.set_preview_theme(tm)
        self.assertNotEqual(self.notice.styleSheet(), light)
        tm.theme = "light"


if __name__ == "__main__":
    unittest.main()
