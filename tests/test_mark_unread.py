"""标为未读功能。

- ApiClient.mark_room_unread：走 subscriptions.unread（服务端同步）
- ConversationItemWidget 右键「标为未读」emit mark_unread_requested
- ConversationList 冒泡 mark_unread_requested
- main_window._on_mark_unread：本地红点至少置 1 + 调服务端
"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication  # noqa: E402

from core.models import Conversation  # noqa: E402
from ui.conversation_list import ConversationList, ConversationItemWidget  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class ApiMarkUnreadTest(unittest.TestCase):
    """免网络：直接替换底层 rocketchat_API 桩。"""

    def _client(self, rc):
        from core.api_client import ApiClient
        class _Client(ApiClient):
            def __init__(self):
                super().__init__("https://fake")
                self._rc = rc
        return _Client()

    def test_mark_unread_calls_subscriptions_unread(self):
        rc = mock.Mock()
        rc.subscriptions_unread.return_value = {"success": True}
        c = self._client(rc)
        self.assertTrue(c.mark_room_unread("R1"))
        rc.subscriptions_unread.assert_called_once_with("R1")

    def test_mark_unread_failure_raises(self):
        rc = mock.Mock()
        rc.subscriptions_unread.side_effect = RuntimeError("boom")
        c = self._client(rc)
        from core.api_client import ApiError
        with self.assertRaises(ApiError):
            c.mark_room_unread("R1")


class ContextMenuUnreadTest(unittest.TestCase):
    def test_context_menu_unread_action_emits(self):
        conv = Conversation(id="r1", name="房间")
        widget = ConversationItemWidget(conv)
        got = []
        widget.mark_unread_requested.connect(got.append)
        with mock.patch("ui.conversation_list.QMenu") as MQ:
            pin_act, mute_act, unread_act, del_act = (
                mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock())
            MQ.return_value.addAction.side_effect = (
                [pin_act, mute_act, unread_act, del_act])
            widget.contextMenuEvent(mock.Mock())
        unread_call = MQ.return_value.addAction.call_args_list[2]
        self.assertEqual(unread_call.args[1], "标为未读")
        cb = unread_act.triggered.connect.call_args[0][0]
        cb()
        self.assertEqual(got, ["r1"])
        widget.deleteLater()

    def test_list_bubbles_unread_request(self):
        conv = Conversation(id="r1", name="房间")
        lst = ConversationList()
        lst.set_conversations([conv])
        got = []
        lst.mark_unread_requested.connect(got.append)
        widget = lst.itemWidget(lst.item(0))
        widget.mark_unread_requested.emit("r1")
        self.assertEqual(got, ["r1"])
        lst.deleteLater()
