"""未读分隔线与「N 条新消息」提示条的几何/交互验证。

分隔线通过行首额外高度实现：模型行高、委托绘制与 layout_rects 命中
必须同时使用 _UNREAD_DIVIDER_H，否则分隔线会压住第一条未读消息。
"""
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QRect  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

import ui.message_model as mm  # noqa: E402
from core.models import Message, User  # noqa: E402
from ui.chat_view import ChatView  # noqa: E402
from ui.main_window import ChatArea  # noqa: E402
from ui.message_model import RoleMessage  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _sender(i):
    return User(id=f"u{i}", username=f"user{i}", name=f"用户{i}")


def _msg(n, text=None):
    return Message(
        id=f"m{n}", room_id="r", is_mine=False, sender=_sender(n % 2),
        timestamp=1700000000.0 + n * 60, msg_type="text",
        text=text or f"正文 {n}")


class UnreadDividerTest(unittest.TestCase):

    def setUp(self):
        self.view = ChatView()
        self.view.resize(640, 420)
        self.view.set_room("r")
        self.view.model.prepend_messages_immediate([_msg(i) for i in range(6)])
        self.model = self.view.model
        self.delegate = self.view._delegate

    def _height(self, row):
        return self.model.index(row, 0).data(mm.RoleRowHeight)

    def test_divider_adds_height_only_to_target_row(self):
        before = [self._height(i) for i in range(self.model.rowCount())]
        target = self.model.index(2, 0).data(RoleMessage)
        self.model.set_unread_divider(target.id)
        after = [self._height(i) for i in range(self.model.rowCount())]
        self.assertEqual(after[2], before[2] + mm._UNREAD_DIVIDER_H)
        self.assertEqual(after[1], before[1])
        self.assertEqual(after[3], before[3])

    def test_layout_rects_shift_with_divider(self):
        idx = self.model.index(2, 0)
        msg = idx.data(RoleMessage)
        bubble_before = self.delegate.layout_rects(
            idx, QRect(0, 0, 640, self._height(2)))[0]
        self.model.set_unread_divider(msg.id)
        bubble_after = self.delegate.layout_rects(
            idx, QRect(0, 0, 640, self._height(2)))[0]
        self.assertEqual(bubble_after.top(),
                         bubble_before.top() + mm._UNREAD_DIVIDER_H)
        self.assertEqual(bubble_after.height(), bubble_before.height())

    def test_clear_restores_height(self):
        msg = self.model.index(2, 0).data(RoleMessage)
        h0 = self._height(2)
        self.model.set_unread_divider(msg.id)
        self.model.set_unread_divider("")
        self.assertEqual(self._height(2), h0)

    def test_clear_resets_divider_id(self):
        msg = self.model.index(2, 0).data(RoleMessage)
        self.view.set_unread_divider(msg.id)
        self.view.clear_unread_divider()
        self.assertEqual(self.model._unread_divider_id, "")

    def test_jump_without_divider_returns_false(self):
        self.assertFalse(self.view.jump_to_first_unread())

    def test_jump_to_existing_row(self):
        msg = self.model.index(2, 0).data(RoleMessage)
        self.view.set_unread_divider(msg.id)
        self.assertTrue(self.view.jump_to_first_unread())

    def test_jump_missing_row_returns_false(self):
        self.view.set_unread_divider("not-in-model")
        self.assertFalse(self.view.jump_to_first_unread())


class NewMessagesBarTest(unittest.TestCase):

    def setUp(self):
        self.area = ChatArea()

    def test_starts_hidden(self):
        self.assertTrue(self.area.new_msg_bar.isHidden())

    def test_show_accumulates_count(self):
        self.area.show_new_messages(3)
        self.assertFalse(self.area.new_msg_bar.isHidden())
        self.assertIn("3 条新消息", self.area.new_msg_btn.text())
        self.area.show_new_messages(2)
        self.assertIn("5 条新消息", self.area.new_msg_btn.text())

    def test_non_positive_is_ignored(self):
        self.area.show_new_messages(0)
        self.assertTrue(self.area.new_msg_bar.isHidden())

    def test_clear_resets(self):
        self.area.show_new_messages(4)
        self.area.clear_new_messages()
        self.assertTrue(self.area.new_msg_bar.isHidden())
        self.assertEqual(self.area._new_msg_count, 0)

    def test_button_emits_clicked(self):
        got = []
        self.area.new_messages_clicked.connect(lambda: got.append(1))
        self.area.new_msg_btn.click()
        self.assertEqual(got, [1])


if __name__ == "__main__":
    unittest.main()
