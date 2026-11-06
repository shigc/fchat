"""会话列表与私聊标题栏的状态显示。

- ConversationList.apply_status：刷新对应私聊行；`_render` 重建行后
  状态从 _status_cache 回填保持；群聊行不受影响
- ChatArea.set_dm_status / clear_dm_status / refresh_status_icon

运行：python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication  # noqa: E402

from core.models import Conversation  # noqa: E402
from ui.conversation_list import ConversationList  # noqa: E402
from ui.main_window import ChatArea  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _conv(rid, name, room_type="d", username=""):
    return Conversation(id=rid, name=name, room_type=room_type,
                        username=username)


class ConversationStatusTest(unittest.TestCase):

    def setUp(self):
        self.lst = ConversationList()
        self.lst.set_conversations([
            _conv("r1", "陈一", "d", "chenyi"),
            _conv("r2", "研发群", "c"),
        ])

    def _row(self, rid):
        return self.lst.itemWidget(self.lst._conv_by_id[rid])

    def test_apply_status_sets_dm_row(self):
        self.lst.apply_status("chenyi", {"username": "chenyi",
                                         "status_key": "departed",
                                         "status_text": ""})
        self.assertEqual(self._row("r1").avatar._status_key, "departed")

    def test_group_row_untouched(self):
        self.lst.apply_status("chenyi", {"username": "chenyi",
                                         "status_key": "online",
                                         "status_text": ""})
        self.assertIsNone(self._row("r2").avatar._status_key)

    def test_status_survives_rerender(self):
        """_render 整体重建行（排序变化/搜索过滤）后状态不丢。"""
        self.lst.apply_status("chenyi", {"username": "chenyi",
                                         "status_key": "online",
                                         "status_text": "出差中"})
        self.lst.set_conversations([_conv("r1", "陈一", "d", "chenyi")])
        self.assertEqual(self._row("r1").avatar._status_key, "online")

    def test_unknown_username_ignored(self):
        self.lst.apply_status("nobody", {"username": "nobody",
                                         "status_key": "online",
                                         "status_text": ""})
        self.assertIsNone(self._row("r1").avatar._status_key)

    def test_clear_conversations_drops_cache(self):
        self.lst.apply_status("chenyi", {"username": "chenyi",
                                         "status_key": "online",
                                         "status_text": ""})
        self.lst.clear_conversations()
        self.assertEqual(self.lst._status_cache, {})


class ChatAreaDmStatusTest(unittest.TestCase):

    def test_set_and_clear(self):
        area = ChatArea()
        self.assertTrue(area.status_icon.isHidden())
        area.set_dm_status("online", "会议中")
        self.assertFalse(area.status_icon.isHidden())
        self.assertEqual(area.status_text.text(), "会议中")
        self.assertEqual(area._dm_status_key, "online")
        area.clear_dm_status()
        self.assertTrue(area.status_icon.isHidden())
        self.assertIsNone(area._dm_status_key)

    def test_departed_label_fixed(self):
        area = ChatArea()
        area.set_dm_status("departed", "会议中")
        self.assertEqual(area.status_text.text(), "已停用")

    def test_refresh_icon_renders(self):
        area = ChatArea()
        area.set_dm_status("busy", "")
        area.refresh_status_icon()  # 主题切换后的重画路径，不应崩
        area.clear_dm_status()
        area.refresh_status_icon()  # 无状态时是空操作


if __name__ == "__main__":
    unittest.main()
