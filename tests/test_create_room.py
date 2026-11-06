"""新建房间（私聊/私有群/频道/讨论）与消息搜索的单元测试。

覆盖：ApiClient 各建房间/搜索方法对 rocketchat_API 的调用与返回解析，
对话框输入的收集，以及会话列表按关键字过滤。offscreen 渲染，不联网。
运行：python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QListWidgetItem  # noqa: E402

from core.api_client import ApiClient  # noqa: E402
from core.models import Conversation, Message, User  # noqa: E402
from ui.conversation_list import ConversationList  # noqa: E402
from ui.create_room_dialogs import (  # noqa: E402
    CreateRoomDialog, CreateDmDialog, CreateDiscussionDialog, MemberSelector,
)

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class _Client(ApiClient):
    """免网络：直接替换底层 rocketchat_API 桩。"""

    def __init__(self, rc):
        super().__init__("https://fake")
        self._rc = rc


class TestApiCreate(unittest.TestCase):
    def _client(self, rc):
        c = _Client(rc)
        return c

    def test_create_channel(self):
        rc = mock.Mock()
        rc.channels_create.return_value = {"channel": {"_id": "C1"}}
        c = self._client(rc)
        self.assertEqual(c.create_channel("研发区", "topic"), "C1")
        rc.channels_create.assert_called_once_with("研发区", topic="topic")

    def test_create_group(self):
        rc = mock.Mock()
        rc.groups_create.return_value = {"group": {"_id": "G1"}}
        c = self._client(rc)
        self.assertEqual(c.create_group("内群"), "G1")
        rc.groups_create.assert_called_once_with("内群", topic="")

    def test_create_discussion(self):
        rc = mock.Mock()
        rc.rooms_create_discussion.return_value = {"discussion": {"_id": "D1"}}
        c = self._client(rc)
        self.assertEqual(c.create_discussion("C1", "周会"), "D1")
        rc.rooms_create_discussion.assert_called_once_with(prid="C1", t_name="周会")

    def test_create_team_private_uses_numeric_type(self):
        """本服务器 teams.create 的 type 是数字（1=私有、0=公开），
        字符串/布尔全被 Match 拒绝（实测 2026-09-24）。"""
        rc = mock.Mock()
        rc.call_api_post.return_value = {"team": {"_id": "T1", "roomId": "R1"}}
        c = self._client(rc)
        self.assertEqual(c.create_team("研发团队", private=True), "R1")
        rc.call_api_post.assert_called_once_with(
            "teams.create", body={"name": "研发团队", "type": 1, "members": []})

    def test_create_team_public_topic_via_channels(self):
        """公开团队主房间 t=c，主题走 channels.setTopic（room 参数会被
        服务端静默丢弃，只能创建后单独设置）。"""
        rc = mock.Mock()
        rc.call_api_post.return_value = {"team": {"roomId": "R1"}}
        c = self._client(rc)
        self.assertEqual(c.create_team("团队", "主题", ["a"], private=False), "R1")
        rc.call_api_post.assert_any_call(
            "teams.create", body={"name": "团队", "type": 0, "members": ["a"]})
        rc.call_api_post.assert_any_call(
            "channels.setTopic", body={"roomId": "R1", "topic": "主题"})

    def test_create_team_private_topic_via_groups(self):
        rc = mock.Mock()
        rc.call_api_post.return_value = {"team": {"roomId": "R1"}}
        c = self._client(rc)
        c.create_team("团队", "主题", private=True)
        rc.call_api_post.assert_any_call(
            "groups.setTopic", body={"roomId": "R1", "topic": "主题"})

    def test_create_team_topic_failure_still_returns_room(self):
        """setTopic 失败只降级为日志，创建本身不算失败。"""
        from rocketchat_API.APIExceptions.RocketExceptions import (
            RocketApiException,
        )
        rc = mock.Mock()
        rc.call_api_post.side_effect = [
            {"team": {"roomId": "R1"}},
            RocketApiException(400, "boom", {}),
        ]
        c = self._client(rc)
        self.assertEqual(c.create_team("团队", "主题"), "R1")

    def test_create_dm(self):
        rc = mock.Mock()
        rc.call_api_post.return_value = {"room": {"_id": "Dm1"}}
        c = self._client(rc)
        self.assertEqual(c.create_dm("alice"), "Dm1")
        rc.call_api_post.assert_called_once_with(
            "im.create", body={"username": "alice"})

    def test_search_messages_passes_max_count(self):
        rc = mock.Mock()
        rc.chat_search.return_value = iter([
            {"_id": "m1", "rid": "R1", "msg": "hello", "u": {"username": "a"}},
        ])
        c = self._client(rc)
        results = c.search_messages("R1", "hello")
        # max_count 必须带上，否则 @paginated 会在大房间整体翻页
        rc.chat_search.assert_called_once_with("R1", "hello", max_count=20)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].id, "m1")

    def test_mark_room_read_posts_room_rid(self):
        rc = mock.Mock()
        rc.call_api_post.return_value = {"success": True}
        c = self._client(rc)
        self.assertTrue(c.mark_room_read("R1"))
        # 必须传房间 rid：传订阅 _id 服务端会回 error-invalid-subscription
        rc.call_api_post.assert_called_once_with(
            "subscriptions.read", body={"rid": "R1"})

    def test_mark_room_read_failure_raises_api_error(self):
        from rocketchat_API.APIExceptions.RocketExceptions import (
            RocketApiException,
        )
        from core.api_client import ApiError
        rc = mock.Mock()
        rc.call_api_post.side_effect = RocketApiException(400, "boom", {})
        c = self._client(rc)
        with self.assertRaises(ApiError):
            c.mark_room_read("R1")

    def test_create_channel_passes_members(self):
        rc = mock.Mock()
        rc.channels_create.return_value = {"channel": {"_id": "C1"}}
        c = self._client(rc)
        self.assertEqual(c.create_channel("研发区", "topic", ["alice", "bob"]), "C1")
        rc.channels_create.assert_called_once_with(
            "研发区", topic="topic", members=["alice", "bob"])

    def test_create_group_passes_members(self):
        rc = mock.Mock()
        rc.groups_create.return_value = {"group": {"_id": "G1"}}
        c = self._client(rc)
        self.assertEqual(c.create_group("内群", "", ["alice"]), "G1")
        rc.groups_create.assert_called_once_with("内群", topic="", members=["alice"])

    def test_search_users_calls_spotlight(self):
        rc = mock.Mock()
        rc.spotlight.return_value = {"users": [
            {"_id": "u1", "username": "alice", "name": "Alice"},
            {"_id": "u2", "username": "bob", "name": ""},
        ]}
        c = self._client(rc)
        users = c.search_users("ali")
        rc.spotlight.assert_called_once_with(query="ali")
        self.assertEqual(len(users), 2)
        self.assertEqual(users[0].username, "alice")
        self.assertEqual(users[0].name, "Alice")
        # 空查询不调接口
        rc.reset_mock()
        self.assertEqual(c.search_users("   "), [])
        rc.spotlight.assert_not_called()

    def test_search_users_dedups_by_username(self):
        """同一用户被服务端重复返回时只保留一条（修复“两条相同记录”bug）。"""
        rc = mock.Mock()
        rc.spotlight.return_value = {"users": [
            {"_id": "u1", "username": "alice", "name": "Alice"},
            {"_id": "u2", "username": "alice", "name": "Alice"},
            {"_id": "u3", "username": "Alice", "name": "Alice"},
            {"_id": "u4", "username": "bob", "name": "Bob"},
        ]}
        c = self._client(rc)
        users = c.search_users("ali")
        # 用户名大小写不敏感去重：alice 只留一条
        self.assertEqual(len(users), 2)
        self.assertEqual([u.username for u in users], ["alice", "bob"])

    def test_search_users_limit_still_applies_after_dedup(self):
        """去重发生在 limit 截断之前，避免重复项挤占有效名额。"""
        rc = mock.Mock()
        rc.spotlight.return_value = {"users": [
            {"_id": "u1", "username": "alice", "name": "Alice"},
            {"_id": "u2", "username": "alice", "name": "Alice"},
            {"_id": "u3", "username": "bob", "name": "Bob"},
            {"_id": "u4", "username": "carol", "name": "Carol"},
        ]}
        c = self._client(rc)
        users = c.search_users("a", limit=2)
        self.assertEqual([u.username for u in users], ["alice", "bob"])


class TestDialogs(unittest.TestCase):
    def test_create_room_channel_collects_name(self):
        dlg = CreateRoomDialog("channel")
        dlg._name.setText("研发区")
        dlg._topic.setText("描述")
        dlg.accept()
        self.assertEqual(dlg.result_name, "研发区")
        self.assertEqual(dlg.result_topic, "描述")
        # 频道默认公开
        self.assertFalse(dlg.result_private)

    def test_create_room_team_defaults_private(self):
        dlg = CreateRoomDialog("team")
        dlg._name.setText("研发团队")
        dlg.accept()
        # 团队默认私有
        self.assertTrue(dlg.result_private)

    def test_create_room_visibility_switchable(self):
        """可见性下拉框决定 result_private（频道也可建成私有）。"""
        dlg = CreateRoomDialog("channel")
        dlg._name.setText("频道")
        dlg._visibility.setCurrentIndex(1)
        dlg.accept()
        self.assertTrue(dlg.result_private)

    def test_create_room_group_empty_name_rejected(self):
        dlg = CreateRoomDialog("team")
        dlg._name.setText("   ")
        dlg.accept()
        # 没填名称不关闭对话框
        self.assertEqual(dlg.result(), 0)

    def test_create_dm_collects_selected_username(self):
        """私聊对话框：从已选成员里取唯一用户名作为私聊对象。"""
        dlg = CreateDmDialog(search_fn=lambda q: [])
        dlg._members._selected = {"bob": "Bob"}
        dlg.accept()
        self.assertEqual(dlg.username, "bob")

    def test_create_dm_no_selection_keeps_open(self):
        """没选人就点开始聊天：不关闭对话框。"""
        dlg = CreateDmDialog(search_fn=lambda q: [])
        dlg.accept()
        self.assertEqual(dlg.result(), 0)

    def test_create_discussion_lists_only_room_parents(self):
        convs = [
            Conversation(id="c1", name="频道", room_type="c"),
            Conversation(id="p1", name="私有群", room_type="p"),
            Conversation(id="d1", name="讨论", room_type="d"),
        ]
        dlg = CreateDiscussionDialog(convs)
        self.assertEqual(dlg._parent.count(), 2)
        dlg._name.setText("周会")
        dlg._parent.setCurrentIndex(1)
        dlg.accept()
        self.assertEqual(dlg.result_parent_id, "p1")

    def test_create_dialogs_are_frameless_cards(self):
        # 统一微信式外观：无边框圆角 + 自绘标题头（随主题），无系统标题栏
        from ui.dialog_base import FramelessCardDialog
        dlg_room = CreateRoomDialog("channel")
        dlg_dm = CreateDmDialog(search_fn=lambda q: [])
        dlg_disc = CreateDiscussionDialog([])
        self.addCleanup(dlg_room.deleteLater)
        self.addCleanup(dlg_dm.deleteLater)
        self.addCleanup(dlg_disc.deleteLater)
        for dlg in (dlg_room, dlg_dm, dlg_disc):
            self.assertIsInstance(dlg, FramelessCardDialog)
            self.assertTrue(dlg.windowFlags() & Qt.FramelessWindowHint)
            self.assertTrue(dlg.isModal())
        # 确定键是默认按钮（回车=创建）
        self.assertTrue(dlg_room._ok_btn.isDefault())


class TestMemberSelector(unittest.TestCase):
    def _sel(self, search_fn=None):
        if search_fn is None:
            search_fn = lambda q: []  # noqa: E731
        return MemberSelector(search_fn)

    # run_async 在测试里同步直调回调，避免真实线程
    def _patch_async(self):
        from ui import create_room_dialogs
        return mock.patch.object(
            create_room_dialogs, "run_async",
            side_effect=lambda fn, on_done=None, on_fail=None, parent=None:
                on_done(fn()) if on_done else None)

    def test_add_and_remove_member(self):
        sel = self._sel()
        sel._search_fn = lambda q: [User(id="u1", username="alice", name="Alice")]
        with self._patch_async():
            sel._input.setText("ali")
            sel._do_search()
        # 候选人列出、未选集合为空
        self.assertEqual(sel._candidates.count(), 1)
        self.assertEqual(sel.selected_usernames, [])
        # 点选加入
        sel._add_candidate(sel._candidates.item(0))
        self.assertEqual(sel.selected_usernames, ["alice"])
        self.assertEqual(sel._candidates.count(), 0)
        # 再搜索再点选，验证多选
        sel._search_fn = lambda q: [User(id="u2", username="bob", name="Bob")]
        with self._patch_async():
            sel._input.setText("bo")
            sel._do_search()
        self.assertEqual(sel._candidates.count(), 1)
        sel._add_candidate(sel._candidates.item(0))
        self.assertEqual(sel.selected_usernames, ["alice", "bob"])
        # 点选已选列表移除
        sel._remove_selected(sel._selected_list.item(0))
        self.assertEqual(sel.selected_usernames, ["bob"])

    def test_do_search_dedups_duplicate_users(self):
        """候选人列表对重复用户名做兜底去重，不出现两条相同记录。"""
        sel = self._sel()
        sel._search_fn = lambda q: [
            User(id="u1", username="alice", name="Alice"),
            User(id="u2", username="alice", name="Alice"),
            User(id="u3", username="Alice", name="Alice"),
            User(id="u4", username="bob", name="Bob"),
        ]
        with self._patch_async():
            sel._input.setText("ali")
            sel._do_search()
        self.assertEqual(sel._candidates.count(), 2)
        self.assertEqual(sel._candidates.item(0).data(Qt.UserRole), "alice")
        self.assertEqual(sel._candidates.item(1).data(Qt.UserRole), "bob")

    def test_single_mode_replaces_selection(self):
        """单选模式下选中新用户替换旧用户，始终只保留一人。"""
        sel = MemberSelector(lambda q: [], single=True)
        sel._add_candidate_item = sel._add_candidate
        # 直接把 UserRole 项放进候选人，模拟搜索后点选
        def _pick(username):
            item = QListWidgetItem(username)
            item.setData(Qt.UserRole, username)
            sel._candidates.addItem(item)
            sel._add_candidate(item)
        _pick("alice")
        self.assertEqual(sel.selected_usernames, ["alice"])
        _pick("bob")
        self.assertEqual(sel.selected_usernames, ["bob"])

    def test_create_room_collects_members(self):
        dlg = CreateRoomDialog("team", search_fn=lambda q: [])
        dlg._name.setText("产品组")
        dlg._members._selected = {"alice": "Alice"}
        dlg.accept()
        self.assertEqual(dlg.result_members, ["alice"])

    def test_create_room_without_search_fn_has_no_members(self):
        dlg = CreateRoomDialog("channel")
        dlg._name.setText("公开频道")
        dlg.accept()
        self.assertEqual(dlg.result_members, [])


class TestRoomTypeLabel(unittest.TestCase):
    """类型角标官方术语：团队主房间 t 仍是 c/p，必须结合 is_team 区分。"""

    def test_labels_official_terms(self):
        from ui.widgets import room_type_label
        self.assertEqual(room_type_label(Conversation(room_type="d")), "私聊")
        self.assertEqual(room_type_label(Conversation(room_type="c")),
                         "公开频道")
        self.assertEqual(room_type_label(Conversation(room_type="p")),
                         "私有频道")
        self.assertEqual(room_type_label(Conversation(room_type="c",
                                                      is_team=True)),
                         "公开团队")
        self.assertEqual(room_type_label(Conversation(room_type="p",
                                                      is_team=True)),
                         "私有团队")


class TestConversationFilter(unittest.TestCase):
    def _list(self):
        cl = ConversationList()
        convs = [
            Conversation(id="c1", name="产品组", room_type="c",
                         username="", last_message_time=100.0),
            Conversation(id="d1", name="alice", room_type="d",
                         username="alice", last_message_time=200.0),
        ]
        cl.set_conversations(convs)
        return cl

    def test_empty_filter_shows_all(self):
        cl = self._list()
        self.assertEqual(cl.count(), 2)

    def test_filter_by_name(self):
        cl = self._list()
        cl.set_search_filter("产品")
        self.assertEqual(cl._row_ids(), ["c1"])

    def test_filter_by_username(self):
        cl = self._list()
        cl.set_search_filter("alice")
        self.assertEqual(cl._row_ids(), ["d1"])

    def test_filter_cleared_restores_all(self):
        cl = self._list()
        cl.set_search_filter("不存在")
        self.assertEqual(cl.count(), 0)
        cl.set_search_filter("")
        self.assertEqual(cl.count(), 2)


if __name__ == "__main__":
    unittest.main()
