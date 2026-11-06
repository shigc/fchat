"""第一批 UI 优化：双击消息（自己编辑 / 他人引用）测试。

GUI 测试，offscreen 渲染，不联网。
运行：python -m unittest tests.test_double_click -v
"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PyQt5.QtGui import QMouseEvent  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from core.models import Message, User  # noqa: E402
from ui.chat_view import ChatView  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _sender(i=0):
    return User(id=f"u{i}", username=f"user{i}", name=f"用户{i}", avatar_url="")


def _msg(n, text="这是一条测试消息内容", is_mine=False):
    return Message(id=f"m{n}", room_id="r", sender=_sender(),
                   timestamp=1700000000.0 + n * 60, msg_type="text",
                   text=text, is_mine=is_mine)


def _dblclick(view, pos):
    ev = QMouseEvent(QEvent.MouseButtonDblClick, QPointF(pos), Qt.LeftButton,
                     Qt.NoButton, Qt.NoModifier)
    QApplication.sendEvent(view.viewport(), ev)
    QApplication.sendEvent(view, ev)


class TestDoubleClick(unittest.TestCase):
    def setUp(self):
        self.view = ChatView()
        self.view.resize(640, 420)
        self.view.set_room("r")
        self.view.set_my_username("user0")
        # 消息 1 标记为自己的（is_mine），其余为他人消息
        msgs = [_msg(i) for i in range(4)]
        msgs[1] = _msg(1, is_mine=True)
        self.view.model.prepend_messages_immediate(msgs)
        self.model = self.view.model
        self.edits = []
        self.quotes = []
        self.view.edit_requested.connect(self.edits.append)
        self.view.quote_requested.connect(self.quotes.append)

    def tearDown(self):
        self.view.deleteLater()

    def _bubble_center(self, row=1):
        idx = self.model.index(row, 0)
        bubble, avatar, image = self.view._delegate.layout_rects(
            idx, self.view.visualRect(idx))
        return bubble, bubble.center()

    def test_double_click_own_text_emits_edit(self):
        # 自己的消息（sender 是 user0）→ 编辑
        bubble, center = self._bubble_center(1)
        _dblclick(self.view, center)
        self.assertEqual(len(self.edits), 1)
        self.assertEqual(len(self.quotes), 0)
        self.assertEqual(self.edits[0].id, self.model.get_message(1).id)

    def test_double_click_other_text_emits_quote(self):
        # 他人消息（sender 是 user1）→ 引用
        bubble, center = self._bubble_center(2)
        _dblclick(self.view, center)
        self.assertEqual(len(self.quotes), 1)
        self.assertEqual(len(self.edits), 0)
        self.assertEqual(self.quotes[0].id, self.model.get_message(2).id)

    def test_double_click_outside_bubble_no_signal(self):
        # 点击气泡外（列表底部空白）不触发
        _dblclick(self.view, QPoint(320, 400))
        self.assertEqual(len(self.edits), 0)
        self.assertEqual(len(self.quotes), 0)

    def test_double_click_merge_message_no_signal(self):
        # 合并消息不触发编辑/引用
        from tests.test_text_selection import _merge_msg
        self.view.model.clear()
        self.view.model.prepend_messages_immediate([_merge_msg(0)])
        idx = self.model.index(0, 0)
        bubble, avatar, image = self.view._delegate.layout_rects(
            idx, self.view.visualRect(idx))
        _dblclick(self.view, bubble.center())
        self.assertEqual(len(self.edits), 0)
        self.assertEqual(len(self.quotes), 0)
