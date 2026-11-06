"""会话名解析验证：中文显示名必须优先于服务器 ASCII slug。

运行：python -m unittest discover -s tests -v
纯离线，用伪造的 Rocket.Chat 订阅/房间 JSON，不需要真实服务器。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.api_client import ApiClient  # noqa: E402
from ui.widgets import format_timestamp  # noqa: E402

SERVER = "https://chat.example.com"


class TestConversationNames(unittest.TestCase):

    def setUp(self):
        self.client = ApiClient(SERVER)

    def test_channel_prefers_fname_over_slug(self):
        conv = self.client._conversation_from_subscription({
            "rid": "room1", "t": "c", "name": "general", "fname": "综合讨论",
        })
        self.assertEqual(conv.name, "综合讨论")
        # 头像仍按 slug 构造，Rocket.Chat 的 /avatar/ 认的是频道名
        self.assertEqual(conv.avatar_url, f"{SERVER}/avatar/general")

    def test_channel_without_fname_keeps_slug(self):
        conv = self.client._conversation_from_subscription({
            "rid": "room2", "t": "c", "name": "random",
        })
        self.assertEqual(conv.name, "random")

    def test_dm_keeps_username_separate_from_display_name(self):
        """私聊：显示名给 name，username 单独留着供 users.info 反查。"""
        conv = self.client._conversation_from_subscription({
            "rid": "room3", "t": "d", "name": "lisi", "fname": "李四",
        })
        self.assertEqual(conv.name, "李四")
        self.assertEqual(conv.username, "lisi")
        self.assertEqual(conv.avatar_url, f"{SERVER}/avatar/lisi")

    def test_dm_without_fname_falls_back_to_username(self):
        conv = self.client._conversation_from_subscription({
            "rid": "room4", "t": "d", "name": "wangwu",
        })
        self.assertEqual(conv.name, "wangwu")
        self.assertEqual(conv.username, "wangwu")

    def test_cached_user_name_used_before_username(self):
        self.client._user_names["zhaoliu"] = "赵六"
        conv = self.client._conversation_from_subscription({
            "rid": "room5", "t": "d", "name": "zhaoliu",
        })
        self.assertEqual(conv.name, "赵六")

    def test_empty_room_gets_placeholder(self):
        conv = self.client._conversation_from_subscription({"rid": "r", "t": "c"})
        self.assertEqual(conv.name, "未命名")


class TestConversationOrdering(unittest.TestCase):
    """会话列表必须按最后消息时间排序。

    真机服务器的订阅里没有 lastMessage 字段，时间要取 rooms.get 的 lm；
    订阅自身的 ts 是订阅创建时间，不能拿来排序。
    """

    def setUp(self):
        self.client = ApiClient(SERVER)

    def _fake_rc(self, subs, rooms):
        rc = unittest.mock.Mock()
        rc.subscriptions_get.return_value = {"update": subs}
        rc.rooms_get.return_value = {"update": rooms}
        self.client._rc = rc

    def test_order_by_room_lm(self):
        self._fake_rc(
            subs=[
                {"rid": "old-room", "t": "d", "name": "a", "ts": "2026-06-01T00:00:00.000Z"},
                {"rid": "new-room", "t": "d", "name": "b", "ts": "2026-06-01T00:00:00.000Z"},
                {"rid": "empty-room", "t": "c", "name": "c"},
            ],
            rooms=[
                {"_id": "new-room", "lm": "2026-09-18T07:18:51.500Z",
                 "lastMessage": {"ts": "2026-09-18T07:18:51.500Z", "msg": "早", "u": {"username": "b"}}},
                {"_id": "old-room", "lm": "2026-08-01T00:00:00.000Z",
                 "lastMessage": {"ts": "2026-08-01T00:00:00.000Z", "msg": "旧", "u": {"username": "a"}}},
                {"_id": "empty-room", "lm": None},
            ],
        )
        convs = self.client.get_conversations()
        self.assertEqual([c.id for c in convs], ["new-room", "old-room", "empty-room"])
        self.assertEqual(convs[0].last_message_text, "早")
        self.assertEqual(convs[2].last_message_time, 0.0)

    def test_subscription_ts_not_used_as_last_message_time(self):
        self._fake_rc(
            subs=[{"rid": "r1", "t": "c", "name": "x", "ts": "2026-09-18T00:00:00.000Z"}],
            rooms=[{"_id": "r1", "lm": None}],
        )
        convs = self.client.get_conversations()
        self.assertEqual(convs[0].last_message_time, 0.0)

    def test_rooms_get_failure_degrades_to_subscription_order(self):
        rc = unittest.mock.Mock()
        rc.subscriptions_get.return_value = {"update": [
            {"rid": "r1", "t": "c", "name": "x"},
            {"rid": "r2", "t": "c", "name": "y"},
        ]}
        rc.rooms_get.side_effect = Exception("boom")
        self.client._rc = rc
        convs = self.client.get_conversations()
        self.assertEqual([c.id for c in convs], ["r1", "r2"])


class TestRoomDisplayName(unittest.TestCase):

    def test_fname_wins(self):
        r = {"fname": "研发一组", "topic": "这里是研发一组的讨论区，很长的一句话"}
        self.assertEqual(ApiClient._room_display_name(r), "研发一组")

    def test_short_topic_used_when_no_fname(self):
        self.assertEqual(ApiClient._room_display_name(
            {"fname": "", "topic": "产品需求"}), "产品需求")

    def test_long_topic_not_used_as_name(self):
        long_topic = "这个频道用于讨论跨部门的排期问题，每周三下午例会同步进展"
        self.assertEqual(ApiClient._room_display_name(
            {"fname": "", "topic": long_topic}), "")

    def test_blank_fields_tolerated(self):
        self.assertEqual(ApiClient._room_display_name({}), "")
        self.assertEqual(ApiClient._room_display_name(
            {"fname": "   ", "topic": None}), "")


class TestTimestampLocale(unittest.TestCase):
    """会话列表时间列不能出现英文星期。"""

    def test_weekday_is_chinese(self):
        import datetime
        from ui.widgets import _WEEKDAY_CN
        now = datetime.datetime.now()
        for delta in range(2, 7):
            day = now.date() - datetime.timedelta(days=delta)
            ts = datetime.datetime(
                day.year, day.month, day.day, 9, 30).timestamp()
            self.assertEqual(format_timestamp(ts), _WEEKDAY_CN[day.weekday()])

    def test_no_ascii_weekday_abbreviation(self):
        import datetime
        base = datetime.datetime.now()
        for delta in range(2, 7):
            ts = (base - datetime.timedelta(days=delta)).timestamp()
            text = format_timestamp(ts)
            self.assertFalse(any(c.isascii() and c.isalpha() for c in text),
                             f"时间列出现英文: {text!r}")


if __name__ == "__main__":
    unittest.main()
