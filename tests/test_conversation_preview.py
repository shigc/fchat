"""会话列表最后一消息预览的统一格式。

REST 摘要（api_client）与实时/历史（main_window）必须得到同一种字符串：
频道/群「真实姓名: 内容」，自己发「我: 内容」，私聊和系统消息不加前缀。
此前两条路径各写一份格式化，列表里有的房间带发送人、有的不带。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.api_client import ApiClient  # noqa: E402
from core.messages import preview_text, preview_with_sender  # noqa: E402
from core.models import Message, User  # noqa: E402

SERVER = "https://chat.example.com"


def _msg(text="内容", msg_type="text", name="李四", username="lisi",
         mine=False, **kw):
    return Message(id="m", room_id="r", text=text, msg_type=msg_type,
                   sender=User(id="u2", username=username, name=name),
                   is_mine=mine, **kw)


class PreviewTextTest(unittest.TestCase):

    def test_media_uses_type_placeholder(self):
        self.assertEqual(preview_text(_msg(msg_type="image")), "[图片]")
        self.assertEqual(
            preview_text(_msg(msg_type="video", file_name="a.mp4")),
            "[视频] a.mp4")
        self.assertEqual(
            preview_text(_msg(msg_type="audio", file_name="a.m4a")),
            "[语音] a.m4a")

    def test_file_falls_back_to_text_when_no_name(self):
        self.assertEqual(
            preview_text(_msg(msg_type="file", file_name="计划.docx")),
            "[文件] 计划.docx")
        self.assertEqual(
            preview_text(_msg(msg_type="file", text="见附件")),
            "[文件] 见附件")

    def test_quote_without_text_does_not_leave_trailing_space(self):
        self.assertEqual(preview_text(_msg(msg_type="quote")), "[引用] 内容")
        self.assertEqual(preview_text(_msg("", msg_type="quote")), "[引用]")

    def test_plain_and_system_text_pass_through(self):
        self.assertEqual(preview_text(_msg("你好")), "你好")
        self.assertEqual(
            preview_text(_msg("唐赛 离开频道", msg_type="system")),
            "唐赛 离开频道")


class PreviewWithSenderTest(unittest.TestCase):

    def test_channel_uses_real_name_not_username(self):
        self.assertEqual(preview_with_sender(_msg()), "李四: 内容")

    def test_missing_name_falls_back_to_username(self):
        self.assertEqual(preview_with_sender(_msg(name="")), "lisi: 内容")

    def test_own_message_shows_wo(self):
        self.assertEqual(preview_with_sender(_msg(mine=True)), "我: 内容")

    def test_dm_has_no_prefix(self):
        self.assertEqual(preview_with_sender(_msg(), is_dm=True), "内容")

    def test_system_message_has_no_duplicate_actor(self):
        self.assertEqual(
            preview_with_sender(_msg("唐赛 离开频道", msg_type="system")),
            "唐赛 离开频道")

    def test_empty_text_and_missing_sender_have_no_prefix(self):
        self.assertEqual(preview_with_sender(_msg("")), "")
        self.assertEqual(
            preview_with_sender(Message(id="m", text="内容")), "内容")


class ApiClientPreviewTest(unittest.TestCase):
    """REST 摘要路径：lastMessage 的 u 里带真实姓名（真机已确认）。"""

    def setUp(self):
        self.client = ApiClient(SERVER)

    def _conv(self, sub, last, **room_kw):
        room = {"lm": "2026-10-22T03:00:00.000Z", "lastMessage": last}
        room.update(room_kw)
        return self.client._conversation_from_subscription(sub, room=room)

    def test_channel_preview_uses_real_name(self):
        conv = self._conv(
            {"rid": "r1", "t": "c", "name": "general"},
            {"msg": "内容", "ts": "2026-10-22T03:00:00.000Z",
             "u": {"_id": "u2", "username": "lisi", "name": "李四"}})
        self.assertEqual(conv.last_message_text, "李四: 内容")

    def test_own_last_message_shows_wo(self):
        self.client.user_id = "u1"
        conv = self._conv(
            {"rid": "r1", "t": "c", "name": "general"},
            {"msg": "内容", "ts": "2026-10-22T03:00:00.000Z",
             "u": {"_id": "u1", "username": "alice", "name": "张三"}})
        self.assertEqual(conv.last_message_text, "我: 内容")

    def test_dm_preview_has_no_prefix(self):
        conv = self._conv(
            {"rid": "r2", "t": "d", "name": "lisi"},
            {"msg": "内容", "ts": "2026-10-22T03:00:00.000Z",
             "u": {"_id": "u2", "username": "lisi", "name": "李四"}})
        self.assertEqual(conv.last_message_text, "内容")
        self.assertFalse(conv.is_multi_dm)

    def test_multi_dm_preview_keeps_sender(self):
        """多人私聊 t 仍是 d，但要知道是谁说的，必须带发送人。"""
        conv = self._conv(
            {"rid": "r3", "t": "d", "name": "lisi, wangwu"},
            {"msg": "内容", "ts": "2026-10-22T03:00:00.000Z",
             "u": {"_id": "u2", "username": "lisi", "name": "李四"}},
            usernames=["lisi", "wangwu", "me"], usersCount=3)
        self.assertTrue(conv.is_multi_dm)
        self.assertEqual(conv.last_message_text, "李四: 内容")

    def test_multi_dm_detected_from_subscription_name_when_rooms_get_failed(self):
        """rooms.get 失败时房间成员拿不到，只能靠订阅 name 的逗号拼接判断。"""
        conv = self.client._conversation_from_subscription(
            {"rid": "r4", "t": "d", "name": "lisi, wangwu",
             "fname": "李四, 王五"})
        self.assertTrue(conv.is_multi_dm)

    def test_own_multi_dm_message_shows_wo(self):
        self.client.user_id = "u1"
        conv = self._conv(
            {"rid": "r3", "t": "d", "name": "lisi, wangwu"},
            {"msg": "内容", "ts": "2026-10-22T03:00:00.000Z",
             "u": {"_id": "u1", "username": "alice", "name": "张三"}},
            usernames=["lisi", "wangwu", "alice"], usersCount=3)
        self.assertEqual(conv.last_message_text, "我: 内容")

    def test_image_last_message_preview(self):
        conv = self._conv(
            {"rid": "r1", "t": "c", "name": "general"},
            {"ts": "2026-10-22T03:00:00.000Z",
             "u": {"_id": "u2", "username": "lisi", "name": "李四"},
             "attachments": [{"image_url": f"{SERVER}/file/x.png"}]})
        self.assertEqual(conv.last_message_text, "李四: [图片]")

    def test_system_last_message_has_no_prefix(self):
        conv = self._conv(
            {"rid": "r1", "t": "c", "name": "general"},
            {"t": "room-archived", "ts": "2026-10-22T03:00:00.000Z",
             "u": {"_id": "u2", "username": "lisi", "name": "李四"}})
        self.assertEqual(conv.last_message_text, "频道已被归档")


if __name__ == "__main__":
    unittest.main()
