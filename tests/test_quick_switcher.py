"""Ctrl+K 快速切换：模糊打分与本地会话过滤。

目录搜索需要网络，测试只验证本地部分（服务端结果由真机验证）。
"""
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from core.models import Conversation  # noqa: E402
from ui.quick_switcher import (QuickSwitcher, conversation_score,  # noqa: E402
                               fuzzy_score)

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _conv(rid, name, **kw):
    return Conversation(id=rid, name=name, **kw)


class FuzzyScoreTest(unittest.TestCase):

    def test_substring_matches(self):
        self.assertGreater(fuzzy_score("产品", "产品研发"), 0)

    def test_earlier_substring_scores_higher(self):
        self.assertGreater(fuzzy_score("ab", "abxx"),
                           fuzzy_score("ab", "xxab"))

    def test_subsequence_matches(self):
        self.assertGreater(fuzzy_score("abc", "a-b-c"), 0)

    def test_unrelated_returns_none(self):
        self.assertIsNone(fuzzy_score("xyz", "abc"))

    def test_empty_query_scores_zero(self):
        self.assertEqual(fuzzy_score("", "anything"), 0)

    def test_name_beats_preview(self):
        by_name = _conv("1", "Alpha", last_message_text="zzz")
        by_preview = _conv("2", "zzz", last_message_text="alpha")
        self.assertGreater(conversation_score("alpha", by_name),
                           conversation_score("alpha", by_preview))


class LocalSearchTest(unittest.TestCase):

    def setUp(self):
        self.convs = [
            _conv("r1", "设计评审", room_type="c", username="design",
                  last_message_text="下午三点开会"),
            _conv("r2", "Alice", room_type="d", username="alice",
                  last_message_text="hello"),
            _conv("r3", "产品研发组", room_type="c",
                  last_message_text="构建通过"),
        ]
        self.dlg = QuickSwitcher(self.convs, client=None, theme=None)

    def tearDown(self):
        self.dlg.deleteLater()

    def test_empty_query_lists_all(self):
        self.assertEqual(self.dlg._list.count(), 3)

    def test_filters_by_name(self):
        self.dlg._populate("alice")
        # 结果行 + 底部固定的「在聊天记录中搜索」行
        self.assertEqual(self.dlg._list.count(), 2)
        item = self.dlg._list.item(0)
        self.assertEqual(item.data(Qt.UserRole)["obj"].id, "r2")

    def test_filters_by_preview(self):
        self.dlg._populate("构建")
        self.assertEqual(self.dlg._list.count(), 2)
        self.assertEqual(
            self.dlg._list.item(0).data(Qt.UserRole)["obj"].id, "r3")

    def test_no_match_shows_global_search_row(self):
        self.dlg._populate("zzzz-not-found")
        # 本地无匹配仍留全局搜索入口
        self.assertEqual(self.dlg._list.count(), 1)
        self.assertEqual(
            self.dlg._list.item(0).data(Qt.UserRole)["kind"], "global")

    def test_activate_emits_open_requested(self):
        self.dlg._populate("alice")
        got = []
        self.dlg.open_requested.connect(got.append)
        self.dlg._activate_current()
        self.assertEqual([c.id for c in got], ["r2"])

    def test_move_current_clamped(self):
        self.dlg._populate("")
        self.dlg._move_current(-1)
        self.assertEqual(self.dlg._list.currentRow(), 0)
        self.dlg._move_current(1)
        self.assertEqual(self.dlg._list.currentRow(), 1)

    def test_no_client_does_not_start_directory_search(self):
        self.dlg._on_text_changed("alice")
        self.assertFalse(self.dlg._search_timer.isActive())


if __name__ == "__main__":
    unittest.main()
