"""删除/退出房间的单元测试。

覆盖 ApiClient.delete_room / leave_room 对 rocketchat_API 的调用与返回解析，
以及会话列表解析时是否识别「本人创建的房间」（决定删除 vs 退出）。
offscreen 渲染，不联网。
运行：python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.api_client import ApiClient  # noqa: E402


class _Client(ApiClient):
    """免网络：直接替换底层 rocketchat_API 桩。"""

    def __init__(self, rc):
        super().__init__("https://fake")
        self._rc = rc


class TestDeleteRoom(unittest.TestCase):
    def _client(self, rc):
        return _Client(rc)

    def test_delete_channel_calls_channels_delete(self):
        rc = mock.Mock()
        rc.call_api_post.return_value = {"success": True}
        c = self._client(rc)
        self.assertTrue(c.delete_room("R1", "c"))
        rc.call_api_post.assert_called_once_with(
            "channels.delete", body={"roomId": "R1"})

    def test_delete_group_calls_groups_delete(self):
        rc = mock.Mock()
        rc.call_api_post.return_value = {"success": True}
        c = self._client(rc)
        self.assertTrue(c.delete_room("R1", "p"))
        rc.call_api_post.assert_called_once_with(
            "groups.delete", body={"roomId": "R1"})

    def test_delete_dm_calls_rooms_delete(self):
        """DM 真删走 rooms.delete（需服务端 delete-room 权限，失败透出）。"""
        rc = mock.Mock()
        rc.call_api_post.return_value = {"success": True}
        c = self._client(rc)
        self.assertTrue(c.delete_room("R1", "d"))
        rc.call_api_post.assert_called_once_with(
            "rooms.delete", body={"roomId": "R1"})

    def test_delete_failure_raises(self):
        rc = mock.Mock()
        rc.call_api_post.side_effect = RuntimeError("boom")
        c = self._client(rc)
        with self.assertRaises(Exception):
            c.delete_room("R1", "c")

    def test_delete_team_resolves_team_id_first(self):
        """团队主房间不能走 channels/groups.delete（服务端拒绝），必须
        teams.delete，且参数是 teamId——room_id 是 roomId，先 rooms.info 换。"""
        rc = mock.Mock()
        rc.call_api_get.return_value = {"room": {"_id": "R1", "teamId": "T1"}}
        rc.call_api_post.return_value = {"success": True}
        c = self._client(rc)
        self.assertTrue(c.delete_room("R1", "c", is_team=True))
        rc.call_api_post.assert_called_once_with(
            "teams.delete", body={"teamId": "T1"})

    def test_delete_team_without_team_id_raises(self):
        rc = mock.Mock()
        rc.call_api_get.return_value = {"room": {}}
        c = self._client(rc)
        with self.assertRaises(Exception):
            c.delete_room("R1", "c", is_team=True)
        rc.call_api_post.assert_not_called()

    def test_leave_channel_calls_channels_leave(self):
        """回归：退出仍走 channels.leave / groups.leave，未被删除逻辑改坏。"""
        rc = mock.Mock()
        rc.call_api_post.return_value = {"success": True}
        c = self._client(rc)
        self.assertTrue(c.leave_room("R1", "c"))
        rc.call_api_post.assert_called_once_with(
            "channels.leave", body={"roomId": "R1"})

    def test_leave_team_calls_teams_leave(self):
        """团队主房间 channels.leave 报 error-room-not-found，必须 teams.leave。"""
        rc = mock.Mock()
        rc.call_api_get.return_value = {"room": {"_id": "R1", "teamId": "T1"}}
        rc.call_api_post.return_value = {"success": True}
        c = self._client(rc)
        self.assertTrue(c.leave_room("R1", "c", is_team=True))
        rc.call_api_post.assert_called_once_with(
            "teams.leave", body={"teamId": "T1"})

    def test_leave_dm_raises(self):
        """d 私聊无退出语义：leave_room 报错，删除走 delete_room。"""
        rc = mock.Mock()
        c = self._client(rc)
        with self.assertRaises(Exception):
            c.leave_room("R1", "d")
        rc.call_api_post.assert_not_called()


class TestCreatorDetection(unittest.TestCase):
    """从订阅/房间元数据解析时识别「本人创建的房间」。"""

    def _conv(self, creator_id, me_id, t="c"):
        c = ApiClient("https://fake")
        c.user_id = me_id
        c._user_names = {}
        sub = {"rid": "R1", "t": t, "name": "g", "fname": "群"}
        room = {"_id": "R1", "u": {"_id": creator_id, "username": "x"}}
        return c._conversation_from_subscription(sub, room)

    def test_own_room_is_creator(self):
        conv = self._conv(creator_id="ME", me_id="ME")
        self.assertTrue(conv.i_created)

    def test_others_room_is_not_creator(self):
        conv = self._conv(creator_id="OTHER", me_id="ME")
        self.assertFalse(conv.i_created)

    def test_missing_room_metadata_defaults_false(self):
        c = ApiClient("https://fake")
        c.user_id = "ME"
        c._user_names = {}
        sub = {"rid": "R1", "t": "c", "name": "g"}
        conv = c._conversation_from_subscription(sub, None)
        self.assertFalse(conv.i_created)

    def test_missing_creator_field_defaults_false(self):
        conv = self._conv(creator_id="", me_id="ME")
        self.assertFalse(conv.i_created)

    def test_team_main_marks_is_team(self):
        """团队主房间带 teamMain/teamId（订阅或房间记录上），据此标 is_team。"""
        c = ApiClient("https://fake")
        c.user_id = "ME"
        c._user_names = {}
        sub = {"rid": "R1", "t": "p", "name": "team", "fname": "团队",
               "teamMain": True}
        self.assertTrue(c._conversation_from_subscription(sub, None).is_team)
        # 订阅不带、房间记录带（rooms.get 项兜底）
        sub2 = {"rid": "R2", "t": "c", "name": "team2"}
        room2 = {"_id": "R2", "teamMain": True, "u": {"_id": "OTHER"}}
        conv2 = c._conversation_from_subscription(sub2, room2)
        self.assertTrue(conv2.is_team)
        # 普通频道/群不带 teamMain
        self.assertFalse(c._conversation_from_subscription(
            {"rid": "R3", "t": "c", "name": "general"}, None).is_team)


class TestRoomEncryptedField(unittest.TestCase):
    """房间加密状态解析与开关 API。"""

    def test_encrypted_read_from_room_record(self):
        """encrypted 只在房间记录上（订阅没有），缺失时为 False。"""
        c = _Client(mock.Mock())
        c.user_id = "ME"
        sub = {"rid": "R1", "t": "c", "name": "g"}
        self.assertFalse(c._conversation_from_subscription(sub, None).encrypted)
        conv = c._conversation_from_subscription(
            sub, {"_id": "R1", "encrypted": True, "u": {"_id": "OTHER"}})
        self.assertTrue(conv.encrypted)

    def test_set_room_e2e_calls_save_room_settings(self):
        """真机 2026-09-30 实测：参数必须是 `rid`（roomId 报 error-invalid-room）。"""
        rc = mock.Mock()
        rc.call_api_post.return_value = {"success": True}
        c = _Client(rc)
        self.assertTrue(c.set_room_e2e("R1", True))
        rc.call_api_post.assert_called_once_with(
            "rooms.saveRoomSettings",
            body={"rid": "R1", "encrypted": True})

    def test_set_room_e2e_false_body(self):
        rc = mock.Mock()
        rc.call_api_post.return_value = {"success": True}
        _Client(rc).set_room_e2e("R1", False)
        rc.call_api_post.assert_called_once_with(
            "rooms.saveRoomSettings",
            body={"rid": "R1", "encrypted": False})

    def test_set_room_e2e_without_login_raises(self):
        c = _Client(mock.Mock())
        c._rc = None
        with self.assertRaises(Exception):
            c.set_room_e2e("R1", True)


if __name__ == "__main__":
    unittest.main()
