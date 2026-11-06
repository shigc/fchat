"""会话列表 [草稿] 标识。

- ConversationItemWidget.set_has_draft：预览前加 [草稿] 前缀，去掉后恢复
- ConversationList.set_draft_rooms：注入有草稿的房间集合，刷新已建行
- main_window._refresh_draft_rooms：登录后遍历会话初始化草稿集合
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication  # noqa: E402

from core.models import Conversation  # noqa: E402
from ui.conversation_list import ConversationList  # noqa: E402
from core import drafts  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _conv(rid, name, room_type="d", username=""):
    return Conversation(id=rid, name=name, room_type=room_type,
                        username=username)


class ItemDraftFlagTest(unittest.TestCase):
    def setUp(self):
        self.lst = ConversationList()
        self.lst.set_conversations([_conv("r1", "陈一", "d", "chenyi")])
        w = self.lst.itemWidget(self.lst.item(0))
        self.w = w
        self.base = w.preview_label.text()

    def test_preview_gets_draft_prefix(self):
        self.w.set_has_draft(True)
        self.assertEqual(self.w.preview_label.text(), "[草稿] " + self.base)

    def test_clearing_draft_removes_prefix(self):
        self.w.set_has_draft(True)
        self.w.set_has_draft(False)
        self.assertEqual(self.w.preview_label.text(), self.base)

    def test_same_value_no_rewrite(self):
        self.w.set_has_draft(False)
        self.assertEqual(self.w.preview_label.text(), self.base)


class SetDraftRoomsTest(unittest.TestCase):
    def setUp(self):
        self.lst = ConversationList()
        self.lst.set_conversations([
            _conv("r1", "陈一", "d", "chenyi"),
            _conv("r2", "李四", "d", "lisi"),
        ])

    def _preview(self, index):
        w = self.lst.itemWidget(self.lst.item(index))
        return w.preview_label.text()

    def test_injects_and_refreshes_rows(self):
        self.lst.set_draft_rooms({"r1"})
        self.assertTrue(self._preview(0).startswith("[草稿] "))
        self.assertFalse(self._preview(1).startswith("[草稿] "))

    def test_clearing_all_removes_all_prefixes(self):
        self.lst.set_draft_rooms({"r1", "r2"})
        self.assertTrue(self._preview(0).startswith("[草稿] "))
        self.assertTrue(self._preview(1).startswith("[草稿] "))
        self.lst.set_draft_rooms(set())
        self.assertFalse(self._preview(0).startswith("[草稿] "))
        self.assertFalse(self._preview(1).startswith("[草稿] "))


class RefreshDraftRoomsTest(unittest.TestCase):
    """main_window._refresh_draft_rooms 遍历会话初始化草稿集合。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(drafts, "CONFIG_DIR", self._tmp.name)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_builds_set_from_drafts(self):
        from ui.main_window import MainWindow
        win = MainWindow.__new__(MainWindow)
        ident = ("https://chat.example.com", "me")
        drafts.set_draft(ident[0], ident[1], "r1", "草稿内容", None)
        drafts.set_draft(ident[0], ident[1], "r2", "", None)
        win._conversations = {
            "r1": Conversation(id="r1", name="a"),
            "r2": Conversation(id="r2", name="b"),
            "r3": Conversation(id="r3", name="c"),
        }
        win._draft_rooms = set()
        win.conversation_list = ConversationList()
        win._draft_identity = lambda: ident
        win._refresh_draft_rooms()
        self.assertEqual(win._draft_rooms, {"r1"})
