"""已读回执 core 层：REST 异常分类（探测降级）+ 群成员拉取 + @范围识别。

服务器是魔改版，chat.getMessageReadReceipts 是否可用未知——
异常分类的正确性直接决定会不会误降级（把网络抖动当"不支持"），
这里锁死分类矩阵。

运行：python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rocketchat_API.APIExceptions.RocketExceptions import (  # noqa: E402
    RocketApiException, RocketBadStatusCodeException,
    RocketConnectionException,
)

from core.api_client import (  # noqa: E402
    ApiClient, ApiConnectionError, ApiError, ReceiptsUnsupportedError,
)
from core.messages import mention_scope  # noqa: E402


def _client():
    c = ApiClient("http://s")
    c._rc = mock.Mock()
    return c


class GetReadReceiptsTest(unittest.TestCase):

    def test_success_returns_receipt_list(self):
        c = _client()
        receipts = [{"messageId": "m1", "userId": "u1",
                     "ts": {"$date": 1700000000000}}]
        c._rc.call_api_get.return_value = {"success": True,
                                           "receipts": receipts}
        self.assertEqual(c.get_read_receipts("m1"), receipts)

    def test_success_missing_receipts_key_returns_empty(self):
        c = _client()
        c._rc.call_api_get.return_value = {"success": True}
        self.assertEqual(c.get_read_receipts("m1"), [])

    def test_200_with_success_false_is_unsupported(self):
        c = _client()
        c._rc.call_api_get.return_value = {"success": False}
        with self.assertRaises(ReceiptsUnsupportedError):
            c.get_read_receipts("m1")

    def test_api_exception_is_unsupported(self):
        """服务端 JSON 错误（含 Read Receipts 功能关闭）→ 明确不支持。"""
        c = _client()
        c._rc.call_api_get.side_effect = RocketApiException(
            400, "err", {"success": False, "error": "not enabled"})
        with self.assertRaises(ReceiptsUnsupportedError):
            c.get_read_receipts("m1")

    def test_404_is_unsupported(self):
        """反代挡掉未知端点返回非 JSON 404 → 明确不支持。"""
        c = _client()
        c._rc.call_api_get.side_effect = RocketBadStatusCodeException(
            404, "<html>404</html>")
        with self.assertRaises(ReceiptsUnsupportedError):
            c.get_read_receipts("m1")

    def test_405_is_unsupported(self):
        c = _client()
        c._rc.call_api_get.side_effect = RocketBadStatusCodeException(
            405, "method not allowed")
        with self.assertRaises(ReceiptsUnsupportedError):
            c.get_read_receipts("m1")

    def test_5xx_is_transient_not_unsupported(self):
        """网关抖动不能误降级，必须抛普通 ApiError。"""
        c = _client()
        c._rc.call_api_get.side_effect = RocketBadStatusCodeException(
            502, "bad gateway")
        with self.assertRaises(ApiError) as ctx:
            c.get_read_receipts("m1")
        self.assertNotIsInstance(ctx.exception, ReceiptsUnsupportedError)

    def test_connection_error_is_transient(self):
        c = _client()
        c._rc.call_api_get.side_effect = RocketConnectionException("down")
        with self.assertRaises(ApiConnectionError):
            c.get_read_receipts("m1")

    def test_not_logged_in(self):
        c = ApiClient("http://s")
        with self.assertRaises(ApiError):
            c.get_read_receipts("m1")


class FetchRoomMembersTest(unittest.TestCase):

    def test_channel_endpoint(self):
        c = _client()
        members = [{"_id": "u1", "username": "a"}]
        c._rc.call_api_get.return_value = {"success": True,
                                           "members": members, "total": 1}
        self.assertEqual(c.fetch_room_members("r1", "c"), members)
        args, kwargs = c._rc.call_api_get.call_args
        self.assertEqual(args[0], "channels.members")
        self.assertEqual(kwargs["roomId"], "r1")

    def test_group_endpoint(self):
        c = _client()
        c._rc.call_api_get.return_value = {"success": True, "members": [],
                                           "total": 0}
        c.fetch_room_members("r1", "p")
        self.assertEqual(c._rc.call_api_get.call_args[0][0],
                         "groups.members")

    def test_total_truncation_refetches(self):
        """count=0 被服务端截断（total 大于返回数）时按 total 补拉。"""
        c = _client()
        full = [{"_id": f"u{i}"} for i in range(3)]
        c._rc.call_api_get.side_effect = [
            {"success": True, "members": [full[0]], "total": 3},
            {"success": True, "members": full, "total": 3},
        ]
        self.assertEqual(len(c.fetch_room_members("r1", "c")), 3)
        second = c._rc.call_api_get.call_args_list[1]
        self.assertEqual(second[0][0], "channels.members")
        self.assertEqual(second[1].get("count"), 3)

    def test_dm_uses_im_members(self):
        """多人和某人的私聊 type 仍是 "d"，成员列表走 im.members。"""
        c = _client()
        members = [{"_id": "u1"}, {"_id": "u2"}]
        c._rc.call_api_get.return_value = {"success": True,
                                           "members": members, "total": 2}
        self.assertEqual(c.fetch_room_members("r1", "d"), members)
        args, kwargs = c._rc.call_api_get.call_args
        self.assertEqual(args[0], "im.members")
        self.assertEqual(kwargs["roomId"], "r1")

    def test_not_logged_in(self):
        c = ApiClient("http://s")
        with self.assertRaises(ApiError):
            c.fetch_room_members("r1", "c")


class MentionScopeTest(unittest.TestCase):

    def test_empty(self):
        self.assertEqual(mention_scope([]), "")
        self.assertEqual(mention_scope(None), "")

    def test_user_mention(self):
        self.assertEqual(mention_scope(
            [{"id": "u1", "username": "alice", "name": "张三"}]), "user")

    def test_all_by_id(self):
        self.assertEqual(mention_scope(
            [{"id": "all", "username": "all", "name": ""}]), "all")

    def test_here_by_username(self):
        self.assertEqual(mention_scope(
            [{"id": "xxx", "username": "here", "name": ""}]), "all")

    def test_user_and_all_mixed(self):
        self.assertEqual(mention_scope(
            [{"id": "u1", "username": "alice", "name": ""},
             {"id": "all", "username": "all", "name": ""}]), "all")


if __name__ == "__main__":
    unittest.main()
