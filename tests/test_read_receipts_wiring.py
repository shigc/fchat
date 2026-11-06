"""已读状态装配与编排：点击信号、拉取在途去重、探测降级、浮窗分组。

run_async 走一次性 QThread，测试用 processEvents 轮询等回调落地；
所有网络均用 stub，不联网。

运行：python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QEvent, QPoint, Qt  # noqa: E402
from PyQt5.QtGui import QMouseEvent  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402
from core.api_client import ReceiptsUnsupportedError  # noqa: E402
from core.models import Message, User  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.read_receipts_popup import ReadReceiptsPopup  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class _NoLogin(MainWindow):

    def _try_auto_login(self):
        self.auto_login_skipped = True


def _mine(mid, ts=1700000000.0):
    return Message(id=mid, room_id="r", is_mine=True,
                   sender=User(id="me", username="me", name="我"),
                   timestamp=ts, text="正文", status="sent")


class WiringTest(unittest.TestCase):

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
        self.view = self.win.chat_area.chat_view
        self.view.set_room("r")
        self.win._active_room_id = "r"
        self.win._room_type = "c"
        self.popups = []

    def tearDown(self):
        for p in self.popups:
            try:
                p.close()
            except RuntimeError:
                pass
        self.win.realtime and self.win.realtime.stop()
        self.win.deleteLater()
        (config.CONFIG_DIR, config.CONFIG_FILE,
         config.KEY_FILE, config.LEGACY_INI_FILE) = self._orig
        self._tmp.cleanup()

    def _wait(self, cond, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            QApplication.processEvents()
            if cond():
                return True
            time.sleep(0.01)
        return False

    def _add_unread_message(self, member_count=3):
        self.view.model.set_room_context("c", member_count)
        self.view.model.append_message(_mine("m1"))
        self.view.model.set_read_receipts("m1", [{"userId": "u1", "ts": 0}])

    # ---- 点击命中 ---- #
    def test_click_on_unread_chunk_emits_signal(self):
        self._add_unread_message()
        row = self.view.model.find_message_row("m1")
        idx = self.view.model.index(row, 0)
        srect = self.view._delegate.status_rect(idx, self.view.visualRect(idx))
        self.assertFalse(srect.isNull(), "群聊未读文本块应可命中")
        got = []
        self.view.read_receipts_requested.connect(
            lambda m, p: got.append(m))
        ev = QMouseEvent(QEvent.MouseButtonPress, srect.center(),
                         Qt.LeftButton, Qt.NoButton, Qt.NoModifier)
        self.view.mousePressEvent(ev)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].id, "m1")

    def test_click_on_other_sender_unread_chunk_emits_signal(self):
        """别人消息的未读块同样可点（未读是房间级状态，名单一样有意义）。"""
        other = Message(id="m2", room_id="r", is_mine=False,
                        sender=User(id="u9", username="u9", name="对方"),
                        timestamp=1700000060.0, text="对方消息", status="sent")
        self.view.model.set_room_context("c", 3)
        self.view.model.append_message(other)
        self.view.model.set_read_receipts("m2", [{"userId": "u1", "ts": 0}])
        row = self.view.model.find_message_row("m2")
        idx = self.view.model.index(row, 0)
        srect = self.view._delegate.status_rect(idx, self.view.visualRect(idx))
        self.assertFalse(srect.isNull(), "别人消息的未读块应可命中")
        got = []
        self.view.read_receipts_requested.connect(
            lambda m, p: got.append(m))
        ev = QMouseEvent(QEvent.MouseButtonPress, srect.center(),
                         Qt.LeftButton, Qt.NoButton, Qt.NoModifier)
        self.view.mousePressEvent(ev)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].id, "m2")

    # ---- 拉取编排 ---- #
    def test_fetch_receipts_updates_model_cache(self):
        self.win.client = mock.Mock()
        self.win.client.user_id = "me"
        self.win.client.get_read_receipts.return_value = [
            {"messageId": "m1", "userId": "u1", "ts": 0}]
        self.view.model.append_message(_mine("m1"))
        self.win._fetch_receipts_for_active_room()
        self.assertTrue(self._wait(
            lambda: "m1" in self.view.model._receipts_by_msg))
        self.assertEqual(self.view.model._receipts_by_msg["m1"],
                         [{"messageId": "m1", "userId": "u1", "ts": 0}])
        self.assertEqual(self.win._receipts["r"]["m1"],
                         self.view.model._receipts_by_msg["m1"])

    def test_fetch_receipts_drops_own_receipt(self):
        """服务端会把发消息人自己也计入已读名单（多人私聊实测），剔除掉，
        否则未读数 = 成员数 − 回执数会少算一人。"""
        self.win.client = mock.Mock()
        self.win.client.user_id = "me"
        self.win.client.get_read_receipts.return_value = [
            {"messageId": "m1", "userId": "me", "ts": 0},
            {"messageId": "m1", "userId": "u1", "ts": 1},
        ]
        self.view.model.append_message(_mine("m1"))
        self.win._fetch_receipts_for_active_room()
        self.assertTrue(self._wait(
            lambda: "m1" in self.view.model._receipts_by_msg))
        self.assertEqual(self.view.model._receipts_by_msg["m1"],
                         [{"messageId": "m1", "userId": "u1", "ts": 1}])

    def test_fetch_dedups_concurrent_calls(self):
        self.win.client = mock.Mock()
        self.win.client.user_id = "me"
        gate = threading.Event()

        def slow_receipts(_mid):
            gate.wait(2)
            return []

        self.win.client.get_read_receipts.side_effect = slow_receipts
        self.view.model.append_message(_mine("m1"))
        self.win._fetch_receipts_for_active_room()
        self.win._fetch_receipts_for_active_room()  # 在途，应被去重
        gate.set()
        self.assertTrue(self._wait(
            lambda: "m1" in self.view.model._receipts_by_msg))
        self.assertEqual(self.win.client.get_read_receipts.call_count, 1)

    def test_empty_receipts_walks_back_to_earlier_message(self):
        """最后一条回执为空（还没人读）时补拉更早一条：空名单对更早的
        消息不含单调信息，不补拉会把已读旧消息全误判成未读（李肖房间
        实测场景）。"""
        self.win.client = mock.Mock()
        self.win.client.user_id = "me"
        self.view.model.append_message(_mine("m1", ts=1700000000.0))
        self.view.model.append_message(_mine("m2", ts=1700000060.0))
        data = {"m1": [{"messageId": "m1", "userId": "u1", "ts": 0}],
                "m2": []}
        self.win.client.get_read_receipts.side_effect = \
            lambda mid: list(data[mid])
        self.win._fetch_receipts_for_active_room()
        self.assertTrue(self._wait(
            lambda: self.view.model._receipts_by_msg.get("m1") is not None))
        self.assertEqual(self.win.client.get_read_receipts.call_count, 2)
        self.assertEqual(self.view.model._receipts_by_msg["m2"], [])
        self.assertEqual(self.view.model._receipts_by_msg["m1"],
                         [{"messageId": "m1", "userId": "u1", "ts": 0}])

    def test_nonempty_latest_skips_walkback(self):
        """最后一条名单非空时不往前补拉，常规场景仍是一次请求。"""
        self.win.client = mock.Mock()
        self.win.client.user_id = "me"
        self.view.model.append_message(_mine("m1", ts=1700000000.0))
        self.view.model.append_message(_mine("m2", ts=1700000060.0))
        self.win.client.get_read_receipts.return_value = [
            {"messageId": "m2", "userId": "u1", "ts": 0}]
        self.win._fetch_receipts_for_active_room()
        self.assertTrue(self._wait(
            lambda: "m2" in self.view.model._receipts_by_msg))
        QApplication.processEvents()
        self.assertEqual(self.win.client.get_read_receipts.call_count, 1)
        self.assertNotIn("m1", self.view.model._receipts_by_msg)

    def test_unsupported_degrades(self):
        self.win.client = mock.Mock()
        self.win.client.user_id = "me"
        self.win.client.get_read_receipts.side_effect = \
            ReceiptsUnsupportedError("功能未开启")
        self.view.model.append_message(_mine("m1"))
        self.win._fetch_receipts_for_active_room()
        self.assertTrue(self._wait(lambda: self.win._receipts_degraded))
        self.assertFalse(self.view.model._receipts_supported)

    def test_degraded_skips_fetch(self):
        self.win._receipts_degraded = True
        self.win.client = mock.Mock()
        self.view.model.append_message(_mine("m1"))
        self.win._fetch_receipts_for_active_room()
        QApplication.processEvents()
        self.win.client.get_read_receipts.assert_not_called()

    def test_huge_group_skips_fetch(self):
        """超大群不显示未读数，也不发回执请求（省流量）。"""
        self.win.client = mock.Mock()
        self.view.model.set_room_context("c", 10 ** 6)
        self.view.model.append_message(_mine("m1"))
        self.win._fetch_receipts_for_active_room()
        QApplication.processEvents()
        self.win.client.get_read_receipts.assert_not_called()

    def test_local_pending_message_not_fetched(self):
        """乐观回显（local- 假 id）尚未落地，不发起回执请求。"""
        self.win.client = mock.Mock()
        self.view.model.append_message(_mine("local-abc123"))
        self.win._fetch_receipts_for_active_room()
        QApplication.processEvents()
        self.win.client.get_read_receipts.assert_not_called()

    # ---- 浮窗处理 ---- #
    def test_receipts_popup_groups_read_and_unread(self):
        msg = _mine("m1")
        msg.sender.id = "me"
        popup = ReadReceiptsPopup(msg, "http://s", my_user_id="me")
        self.popups.append(popup)
        members = [
            {"_id": "u1", "username": "alice", "name": "张三"},
            {"_id": "u2", "username": "bob", "name": "李四"},
            {"_id": "me", "username": "me", "name": "我"},
        ]
        popup.populate(
            [{"messageId": "m1", "userId": "u1", "ts": {"$date": 1700000000000}}],
            members)
        self.assertIn("已读 1 人", popup.title_label.text())
        self.assertIn("未读 1 人", popup.title_label.text())  # 自己不算未读

    def test_popup_excludes_login_user_not_sender(self):
        """点开别人消息的名单时，「自己」是登录者而不是消息发送者：
        发送者已读要计入已读名单，不能被当成自己排除。"""
        msg = Message(id="m9", room_id="r", is_mine=False,
                      sender=User(id="u1", username="alice", name="张三"),
                      timestamp=1700000000.0, text="正文", status="sent")
        popup = ReadReceiptsPopup(msg, "http://s", my_user_id="me")
        self.popups.append(popup)
        members = [
            {"_id": "u1", "username": "alice", "name": "张三"},
            {"_id": "u2", "username": "bob", "name": "李四"},
            {"_id": "me", "username": "me", "name": "我"},
        ]
        popup.populate(
            [{"messageId": "m9", "userId": "u1", "ts": {"$date": 1700000000000}},
             {"messageId": "m9", "userId": "me", "ts": {"$date": 1700000001000}}],
            members)
        # 回执含自己（服务端行为，上层已剔除则不会有）时也不能算进已读
        self.assertIn("已读 1 人", popup.title_label.text())
        self.assertIn("未读 1 人", popup.title_label.text())

    def test_popup_all_badge(self):
        msg = _mine("m1")
        msg.mentions = [{"id": "all", "username": "all", "name": ""}]
        popup = ReadReceiptsPopup(msg, "http://s", my_user_id="me")
        self.popups.append(popup)
        popup.show_loading()
        self.assertIn("读取中", popup.title_label.text())

    def test_handler_feeds_exact_receipts_back(self):
        """点开浮窗的精确回执要反哺行内数字（纠正单调外推的估计值）。"""
        self.win.client = mock.Mock()
        self.win.client.user_id = "me"
        self.win.client.server_url = "http://s"
        self.win.client.get_read_receipts.return_value = [
            {"messageId": "m1", "userId": "u1", "ts": 0}]
        self.win.client.fetch_room_members.return_value = [
            {"_id": "u1", "username": "alice", "name": "张三"},
            {"_id": "u2", "username": "bob", "name": "李四"},
            {"_id": "me", "username": "me", "name": "我"},
        ]
        self._add_unread_message()
        msg = self.view.model.get_message(
            self.view.model.find_message_row("m1"))
        self.win._on_read_receipts_requested(msg, QPoint(100, 100))
        self.assertTrue(self._wait(lambda: self.win._receipts.get("r", {})
                                   .get("m1") is not None))
        self.assertEqual(
            len(self.win._receipts["r"]["m1"]), 1)
        self.assertEqual(self.view.model._receipts_by_msg["m1"],
                         self.win._receipts["r"]["m1"])

    def test_handler_degrades_when_unsupported(self):
        self.win.client = mock.Mock()
        self.win.client.user_id = "me"
        self.win.client.server_url = "http://s"
        self.win.client.get_read_receipts.side_effect = \
            ReceiptsUnsupportedError("no")
        self._add_unread_message()
        msg = self.view.model.get_message(
            self.view.model.find_message_row("m1"))
        self.win._on_read_receipts_requested(msg, QPoint(100, 100))
        self.assertTrue(self._wait(lambda: self.win._receipts_degraded))
        self.assertFalse(self.view.model._receipts_supported)


if __name__ == "__main__":
    unittest.main()
