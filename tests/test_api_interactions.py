"""Phase 0 REST 端点：表情回应 / 编辑 / 置顶 / 星标 / 免打扰。

全部用 mock 的 RocketChat 客户端，不发请求。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.api_client import ApiClient, ApiError  # noqa: E402


def _raw(mid="m1", text="hi"):
    return {"_id": mid, "rid": "r1", "msg": text,
            "ts": "2026-01-01T00:00:00.000Z",
            "u": {"_id": "u1", "username": "alice", "name": "Alice"}}


class ApiInteractionTest(unittest.TestCase):

    def setUp(self):
        self.c = ApiClient("https://fake")
        self.rc = mock.Mock()
        self.c._rc = self.rc
        self.c.user_id = "me"

    # ---- 表情回应 ---- #
    def test_react_body_and_success(self):
        self.rc.call_api_post.return_value = {"success": True}
        self.assertTrue(self.c.react_message("m1", ":thumbsup:", True))
        self.rc.call_api_post.assert_called_once_with(
            "chat.react",
            body={"messageId": "m1", "emoji": ":thumbsup:",
                  "shouldReact": True})

    def test_react_off_passes_false(self):
        self.rc.call_api_post.return_value = {"success": True}
        self.c.react_message("m1", "👍", False)
        body = self.rc.call_api_post.call_args.kwargs["body"]
        self.assertIs(body["shouldReact"], False)

    def test_react_rejected_raises(self):
        self.rc.call_api_post.return_value = {"success": False}
        with self.assertRaises(ApiError):
            self.c.react_message("m1", "👍")

    # ---- 编辑 ---- #
    def test_update_body_and_parses_message(self):
        self.rc.call_api_post.return_value = {
            "success": True, "message": _raw(text="新内容")}
        msg = self.c.update_message("r1", "m1", "新内容")
        self.rc.call_api_post.assert_called_once_with(
            "chat.update",
            body={"roomId": "r1", "msgId": "m1", "text": "新内容"})
        self.assertEqual(msg.id, "m1")
        self.assertEqual(msg.text, "新内容")

    def test_update_without_message_returns_none(self):
        self.rc.call_api_post.return_value = {"success": True}
        self.assertIsNone(self.c.update_message("r1", "m1", "x"))

    def test_update_rejected_raises(self):
        self.rc.call_api_post.return_value = {"success": False}
        with self.assertRaises(ApiError):
            self.c.update_message("r1", "m1", "x")

    # ---- 置顶 / 星标 ---- #
    def test_pin_endpoint_casing(self):
        self.rc.call_api_post.return_value = {"success": True}
        self.c.pin_message("m1", True)
        self.rc.call_api_post.assert_called_with(
            "chat.pinMessage", body={"messageId": "m1"})
        self.c.pin_message("m1", False)
        self.rc.call_api_post.assert_called_with(
            "chat.unPinMessage", body={"messageId": "m1"})

    def test_star_endpoint_casing(self):
        self.rc.call_api_post.return_value = {"success": True}
        self.c.star_message("m1", True)
        self.rc.call_api_post.assert_called_with(
            "chat.starMessage", body={"messageId": "m1"})
        self.c.star_message("m1", False)
        self.rc.call_api_post.assert_called_with(
            "chat.unStarMessage", body={"messageId": "m1"})

    def test_pin_rejected_raises(self):
        self.rc.call_api_post.return_value = {"success": False}
        with self.assertRaises(ApiError):
            self.c.pin_message("m1", True)
        with self.assertRaises(ApiError):
            self.c.star_message("m1", True)

    # ---- 置顶/星标列表 ---- #
    def test_pinned_messages_get_params_and_parse(self):
        self.rc.call_api_get.return_value = {
            "success": True, "messages": [_raw("m1"), _raw("m2")]}
        msgs = self.c.get_pinned_messages("r1", 20)
        self.rc.call_api_get.assert_called_once_with(
            "chat.getPinnedMessages", roomId="r1", count=20, offset=0)
        self.assertEqual([m.id for m in msgs], ["m1", "m2"])

    def test_starred_messages_endpoint(self):
        self.rc.call_api_get.return_value = {"success": True, "messages": []}
        self.assertEqual(self.c.get_starred_messages("r1", 50), [])
        self.rc.call_api_get.assert_called_once_with(
            "chat.getStarredMessages", roomId="r1", count=50, offset=0)

    def test_message_list_rejected_raises(self):
        self.rc.call_api_get.return_value = {"success": False}
        with self.assertRaises(ApiError):
            self.c.get_pinned_messages("r1")

    # ---- 免打扰 ---- #
    def test_mute_body(self):
        self.rc.call_api_post.return_value = {"success": True}
        self.c.set_room_muted("r1", True)
        body = self.rc.call_api_post.call_args.kwargs["body"]
        self.assertEqual(body["roomId"], "r1")
        self.assertEqual(body["notifications"]["desktopNotifications"],
                         "nothing")
        self.assertEqual(body["notifications"]["mobilePushNotifications"],
                         "nothing")
        self.c.set_room_muted("r1", False)
        body = self.rc.call_api_post.call_args.kwargs["body"]
        self.assertEqual(body["notifications"]["desktopNotifications"],
                         "default")

    def test_mute_not_logged_in_raises(self):
        self.c._rc = None
        with self.assertRaises(ApiError):
            self.c.set_room_muted("r1", True)

    # ---- 订阅记录解析 ---- #
    def test_subscription_parses_muted_and_mentions(self):
        conv = self.c._conversation_from_subscription({
            "rid": "r1", "name": "general", "fname": "综合", "t": "c",
            "unread": 3, "disableNotifications": True, "userMentions": 2})
        self.assertTrue(conv.muted)
        self.assertEqual(conv.mention_count, 2)
        self.assertEqual(conv.name, "综合")

    def test_subscription_room_record_fallback_for_mute(self):
        conv = self.c._conversation_from_subscription(
            {"rid": "r1", "name": "general", "t": "c"},
            room={"disableNotifications": True})
        self.assertTrue(conv.muted)
        self.assertEqual(conv.mention_count, 0)


if __name__ == "__main__":
    unittest.main()
