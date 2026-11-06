"""会话列表免打扰/@徽标：行内标签、右键切换、信号冒泡。

offscreen 渲染，不联网。
"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QPoint  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from core.models import Conversation  # noqa: E402
from ui.conversation_list import ConversationItemWidget, ConversationList  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class ConversationMuteTest(unittest.TestCase):

    def test_labels_follow_conv_state(self):
        conv = Conversation(id="r1", name="房间", muted=True, mention_count=2)
        widget = ConversationItemWidget(conv)
        self.assertFalse(widget.mute_label.isHidden())
        self.assertFalse(widget.mention_badge.isHidden())
        # 构造期 setVisible 不能把标签变顶层窗口（曾是启动闪窗根因）
        widget.show()
        self.assertFalse(widget.mute_label.isWindow())
        self.assertFalse(widget.mention_badge.isWindow())
        conv.muted = False
        conv.mention_count = 0
        widget.refresh(conv)
        self.assertTrue(widget.mute_label.isHidden())
        self.assertTrue(widget.mention_badge.isHidden())
        widget.deleteLater()

    def test_context_menu_mute_action_emits(self):
        conv = Conversation(id="r1", name="房间")
        widget = ConversationItemWidget(conv)
        got = []
        widget.mute_requested.connect(got.append)
        with mock.patch("ui.conversation_list.QMenu") as MQ:
            pin_act, mute_act, unread_act, del_act = (
                mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock())
            MQ.return_value.addAction.side_effect = (
                [pin_act, mute_act, unread_act, del_act])
            widget.contextMenuEvent(mock.Mock())
        # 文案与状态绑定，动作回调切的是同一个 room id
        mute_call = MQ.return_value.addAction.call_args_list[1]
        self.assertEqual(mute_call.args[0], "消息免打扰")
        cb = mute_act.triggered.connect.call_args[0][0]
        cb(False)
        self.assertEqual(got, ["r1"])
        widget.deleteLater()

    def test_context_menu_label_when_muted(self):
        conv = Conversation(id="r1", name="房间", muted=True)
        widget = ConversationItemWidget(conv)
        with mock.patch("ui.conversation_list.QMenu") as MQ:
            pin_act, mute_act, unread_act, del_act = (
                mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock())
            MQ.return_value.addAction.side_effect = (
                [pin_act, mute_act, unread_act, del_act])
            widget.contextMenuEvent(mock.Mock())
        self.assertEqual(
            MQ.return_value.addAction.call_args_list[1].args[0], "取消免打扰")
        widget.deleteLater()

    def test_list_bubbles_mute_request(self):
        conv = Conversation(id="r1", name="房间")
        lst = ConversationList()
        lst.set_conversations([conv])
        got = []
        lst.room_mute_requested.connect(got.append)
        widget = lst.itemWidget(lst.item(0))
        widget.mute_requested.emit("r1")
        self.assertEqual(got, ["r1"])
        lst.deleteLater()


if __name__ == "__main__":
    unittest.main()
