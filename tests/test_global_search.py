"""跨房间全局搜索：Ctrl+K 固定入口、面板扫描链与跨房定位接线。

offscreen；get_history / client 全 mock，不联网。
运行：python -m unittest tests.test_global_search -v
"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from core.models import Conversation, Message, User  # noqa: E402
from ui.global_search_panel import GlobalSearchPanel  # noqa: E402
from ui.quick_switcher import QuickSwitcher  # noqa: E402
import ui.main_window as mw  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _mk_msg(mid, text, rid="r1"):
    sender = User(id="u1", username="alice", name="爱丽丝", avatar_url="")
    return Message(id=mid, room_id=rid, sender=sender,
                   timestamp=1700000000.0, msg_type="text", text=text)


def _mk_conv(cid, name, lm_time):
    return Conversation(id=cid, name=name, room_type="c",
                        last_message_time=lm_time)


class QuickSwitcherGlobalEntryTest(unittest.TestCase):
    """Ctrl+K 结果底部的「在聊天记录中搜索」固定项。"""

    def setUp(self):
        self.dlg = QuickSwitcher([_mk_conv("r1", "频道甲", 100)],
                                 client=None)
        self.addCleanup(self.dlg.deleteLater)

    def _last_data(self):
        item = self.dlg._list.item(self.dlg._list.count() - 1)
        return item.data(Qt.UserRole) if item is not None else None

    def test_query_adds_global_row_at_bottom(self):
        self.dlg._populate("关键词")
        data = self._last_data()
        self.assertEqual(data["kind"], "global")
        self.assertEqual(data["query"], "关键词")

    def test_no_query_no_global_row(self):
        self.dlg._populate("")
        for i in range(self.dlg._list.count()):
            data = self.dlg._list.item(i).data(Qt.UserRole) or {}
            self.assertNotEqual(data.get("kind"), "global")

    def test_activate_global_emits_keyword(self):
        self.dlg._populate("预算")
        got = []
        self.dlg.global_search_requested.connect(got.append)
        self.dlg._activate_item(self.dlg._list.item(
            self.dlg._list.count() - 1))
        self.assertEqual(got, ["预算"])
        self.assertFalse(self.dlg.isVisible())

    def test_directory_results_keep_global_row_at_bottom(self):
        self.dlg._populate("预算")
        self.dlg._append_directory([], [], "预算")
        self.assertEqual(self._last_data()["kind"], "global")
        kinds = [self.dlg._list.item(i).data(Qt.UserRole).get("kind")
                 for i in range(self.dlg._list.count())]
        self.assertEqual(kinds.count("global"), 1)


class GlobalScanTest(unittest.TestCase):
    """扫描链：逐房分页、房内续页、结果行与取消。"""

    def setUp(self):
        self.panel = GlobalSearchPanel()
        self.addCleanup(self.panel.deleteLater)
        self.convs = [_mk_conv("r2", "频道乙", 200),
                      _mk_conv("r1", "频道甲", 300)]
        self.client = mock.Mock()
        # run_async 抽平成同步执行 + 转零事件循环，让续页链立即走完
        self._sync = mock.patch(
            "ui.global_search_panel.run_async",
            side_effect=lambda fn, on_done, on_fail, parent=None:
            on_done(fn()) if True else None)
        self._sync.start()
        self.addCleanup(self._sync.stop)

    def _drain(self):
        for _ in range(50):
            QApplication.processEvents()

    def _get_history(self, rid, count, offset, room_type=None):
        """每房 1.5 页：第一页满、第二页一条命中即短页收束。"""
        if offset == 0:
            return [_mk_msg(f"{rid}-{i}", f"普通内容{i}", rid=rid)
                    for i in range(count)]
        if offset == count:
            return [_mk_msg(f"{rid}-hit", "这里有关键词预算", rid=rid)]
        return []

    def test_scan_collects_hits_across_rooms(self):
        self.client.get_history.side_effect = self._get_history
        self.panel.start_scan("预算", self.convs, self.client)
        self._drain()
        # 两个房间各扫 2 页（第 2 页短即止）
        self.assertEqual(self.client.get_history.call_count, 4)
        self.assertEqual(self.panel._hits, 2)
        self.panel._flush_rows()
        self.assertEqual(self.panel.list.count(), 2)
        self.assertIn("已扫描全部", self.panel.status.text())

    def test_room_cap_limits_pages(self):
        """单房扫满 200 条（2 页）就换下一房。"""
        self.client.get_history.side_effect = (
            lambda rid, count, offset, room_type=None:
            [_mk_msg(f"{rid}-{offset + i}", "x", rid=rid)
             for i in range(count)])
        self.panel.start_scan("x", self.convs, self.client)
        self._drain()
        self.assertEqual(self.client.get_history.call_count, 4)

    def test_result_row_click_emits_locate(self):
        self.client.get_history.side_effect = self._get_history
        got = []
        self.panel.locate_requested.connect(
            lambda rid, m: got.append((rid, m)))
        self.panel.start_scan("预算", self.convs, self.client)
        msg = _mk_msg("r1-hit", "这里有关键词预算", rid="r1")
        self.panel._on_row("r1", msg)
        self.assertEqual(got, [("r1", msg)])
        self.assertFalse(self.panel.isVisible())

    def test_stop_invalidates_token(self):
        self.client.get_history.side_effect = self._get_history
        self.panel.start_scan("预算", self.convs, self.client)
        t = self.panel._token
        self.panel._stop()
        self.assertGreater(self.panel._token, t)
        self.assertIn("已停止", self.panel.status.text())

    def test_close_invalidates_token(self):
        self.client.get_history.side_effect = self._get_history
        self.panel.show()
        self.panel.start_scan("预算", self.convs, self.client)
        t = self.panel._token
        self.panel.close()
        self.assertGreater(self.panel._token, t)


class GlobalLocateWiringTest(unittest.TestCase):
    """主窗口接线：同房直接定位、跨房挂起等渲染完成后续接。"""

    def _stub_win(self, active_rid, convs):
        w = mock.Mock()
        w.client = object()
        w._conversations = convs
        w._active_room_id = active_rid
        w._locate_message = mock.Mock()
        w._open_conversation = mock.Mock()
        w.statusBar.return_value = mock.Mock()
        return w

    def test_same_room_locates_directly(self):
        conv = _mk_conv("r1", "频道甲", 100)
        w = self._stub_win("r1", {"r1": conv})
        msg = _mk_msg("m1", "hi", rid="r1")
        mw.MainWindow._on_global_search_locate(w, "r1", msg)
        w._locate_message.assert_called_once_with(msg)
        w._open_conversation.assert_not_called()

    def test_other_room_opens_then_locates_after_render(self):
        conv = _mk_conv("r2", "频道乙", 100)
        w = self._stub_win("r1", {"r2": conv})
        msg = _mk_msg("m1", "hi", rid="r2")
        mw.MainWindow._on_global_search_locate(w, "r2", msg)
        w._open_conversation.assert_called_once_with(conv)
        self.assertEqual(w._pending_locate, ("r2", msg))
        mw.MainWindow._maybe_locate_pending(w, "r2")
        w._locate_message.assert_called_once_with(msg)
        self.assertIsNone(w._pending_locate)

    def test_pending_mismatch_is_ignored(self):
        w = self._stub_win("r1", {})
        w._pending_locate = ("r2", _mk_msg("m1", "x", rid="r2"))
        mw.MainWindow._maybe_locate_pending(w, "r3")
        w._locate_message.assert_not_called()
        self.assertEqual(w._pending_locate[0], "r2")

    def test_missing_conv_shows_hint(self):
        w = self._stub_win("r1", {})
        mw.MainWindow._on_global_search_locate(
            w, "r9", _mk_msg("m1", "hi", rid="r9"))
        w._locate_message.assert_not_called()
        w._open_conversation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
