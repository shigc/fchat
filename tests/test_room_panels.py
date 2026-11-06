"""房间三功能浮层面板与相关 core API 测试。

覆盖：
- core.models.room_type_cn 官方类型中文映射
- Conversation.is_discussion 快照 roundtrip（conv_cache）
- ApiClient.fetch_room_files / fetch_history_around / add_room_member：
  端点映射、参数、url 绝对化、未登录抛错
- ApiClient._conversation_from_subscription 依据 prid 标讨论串
- ui.icons 新增的 members/files/search 图标
- ChatArea 标题栏三按钮与勾选态
- RoomPanels 控制器互斥开合
- MembersPanel：过滤 / 排除自己 / 滚动续渲染 / 头像可见行加载
- FilesPanel：浏览分页 / 搜索全量链 / token 失效 / 右键另存为
- MessagesPanel：普通搜索 / 非法正则 / 正则全历史扫描 / 停止 / locate 信号
- ChatView.save_file_as 与 _open_file_cached 委托
- MainWindow._locate_message 快路径 / around 命中 / 翻页回退

GUI 测试 offscreen 渲染、不联网；后台拉取一律 mock。
运行：python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication, QPushButton  # noqa: E402

from core import conv_cache, paths  # noqa: E402
from core import api_client as ac  # noqa: E402
from core.models import (  # noqa: E402
    Conversation, Message, User, room_type_cn, RoomType,
)

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def sync_run_async(fn, on_done=None, on_fail=None, parent=None):
    """run_async 的同步替身：直接在当前线程执行并回调。"""
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001
        if on_fail:
            on_fail(e)
        return
    if on_done:
        on_done(result)


def drain_events(n=80):
    # 16ms flush 定时器、singleShot 链都要真实时间流逝，processEvents 不会推进时钟
    for _ in range(n):
        _APP.processEvents()
        time.sleep(0.01)


# --------------------------------------------------------------------- #
# room_type_cn 官方类型映射
# --------------------------------------------------------------------- #
class RoomTypeCnTest(unittest.TestCase):
    def test_common_types(self):
        self.assertEqual(room_type_cn("c"), "公开频道")
        self.assertEqual(room_type_cn("p"), "私有频道")
        self.assertEqual(room_type_cn("d"), "私聊")
        self.assertEqual(room_type_cn("l"), "客服")

    def test_unknown_falls_back(self):
        unknown = "mystery"
        self.assertEqual(room_type_cn(unknown), unknown)

    def test_enum_by_value(self):
        import core.models as m
        # RoomType 枚举值也应能直接查映射（等价于传字符串）
        self.assertEqual(m.ROOM_TYPE_CN[RoomType.CHANNEL], "公开频道")
        self.assertEqual(m.ROOM_TYPE_CN[RoomType.GROUP], "私有频道")


# --------------------------------------------------------------------- #
# Conversation.is_discussion 快照 roundtrip
# --------------------------------------------------------------------- #
class DiscussionSnapshotTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patchers = [
            mock.patch.object(paths, "_current_root", self._tmp.name),
            mock.patch.object(paths, "_root_loaded", True),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self._tmp.cleanup()

    def test_roundtrip_preserves_is_discussion(self):
        conv = Conversation(id="rid1", name="技术讨论", room_type="p",
                            is_discussion=True)
        conv_cache.save_conversations_snapshot("http://s", "u1", [conv])
        loaded = conv_cache.load_conversations_snapshot("http://s", "u1")
        self.assertEqual(len(loaded), 1)
        self.assertTrue(loaded[0].is_discussion)

        normal = Conversation(id="rid2", name="产品研发组", room_type="p",
                              is_discussion=False)
        conv_cache.save_conversations_snapshot("http://s", "u1", [normal])
        loaded = conv_cache.load_conversations_snapshot("http://s", "u1")
        self.assertFalse(loaded[0].is_discussion)


class ApiClientResetter:
    """构造带空 _rc 的 ApiClient，供需要干净 call 记录的场景用。"""
    @staticmethod
    def new(files=None, total=0):
        client = ac.ApiClient.__new__(ac.ApiClient)
        client.server_url = "https://chat.example.com"
        client._rc = mock.Mock()
        client._rc.call_api_get.return_value = {"files": files or [],
                                                "total": total}
        return client


# --------------------------------------------------------------------- #
# ApiClient.fetch_room_files
# --------------------------------------------------------------------- #
class FetchRoomFilesTest(unittest.TestCase):
    def _client(self, **rc_data):
        client = ac.ApiClient.__new__(ac.ApiClient)
        client.server_url = "https://chat.example.com"
        rc = mock.Mock()
        client._rc = rc
        if "side_effect" in rc_data:
            rc.call_api_get.side_effect = rc_data["side_effect"]
        else:
            rc.call_api_get.return_value = rc_data.get("data", {
                "files": [
                    {"_id": "f1", "name": "报告.pdf", "size": 1024,
                     "url": "/file-upload/f1", "userId": "u9"},
                    {"_id": "f2", "name": "图.png", "size": 2048,
                     "url": "https://cdn.example.com/x.png"},
                ],
                "total": 2,
            })
        return client

    def test_endpoint_mapping(self):
        for rtype, ep in (("c", "channels.files"),
                          ("p", "groups.files"),
                          ("d", "dm.files")):
            client = ApiClientResetter.new(files=[], total=0)
            client.fetch_room_files("rid", rtype)
            client._rc.call_api_get.assert_called_once_with(
                ep, roomId="rid", count=50, offset=0)

    def test_url_absolutized_and_total(self):
        client = self._client()
        res = client.fetch_room_files("rid", "c")
        self.assertEqual(res["total"], 2)
        files = res["files"]
        self.assertEqual(files[0]["url"], "https://chat.example.com/file-upload/f1")
        # 已绝对地址的不动
        self.assertEqual(files[1]["url"], "https://cdn.example.com/x.png")

    def test_unknown_room_type_raises(self):
        client = self._client()
        with self.assertRaises(ac.ApiError):
            client.fetch_room_files("rid", "z")

    def test_not_logged_in_raises(self):
        client = ac.ApiClient.__new__(ac.ApiClient)
        client._rc = None
        with self.assertRaises(ac.ApiError):
            client.fetch_room_files("rid", "c")

    def test_failure_wrapped_as_apierror(self):
        client = self._client(side_effect=ValueError("boom"))
        with self.assertRaises(ac.ApiError):
            client.fetch_room_files("rid", "c")


# --------------------------------------------------------------------- #
# ApiClient.fetch_history_around
# --------------------------------------------------------------------- #
class FetchHistoryAroundTest(unittest.TestCase):
    def _client(self, data=None, side_effect=None):
        client = ac.ApiClient.__new__(ac.ApiClient)
        client.server_url = "https://chat.example.com"
        rc = mock.Mock()
        client._rc = rc
        if side_effect is not None:
            rc.call_api_get.side_effect = side_effect
        else:
            rc.call_api_get.return_value = data or {"messages": []}
        return client

    def test_endpoint_mapping(self):
        for rtype, ep in (("c", "channels.history"),
                          ("p", "groups.history"),
                          ("d", "dm.history")):
            client = self._client({"messages": []})
            client.fetch_history_around("rid", 1700000000.5, room_type=rtype)
            client._rc.call_api_get.assert_called_once()
            self.assertEqual(client._rc.call_api_get.call_args[0][0], ep)

    def test_latest_is_iso_utc_plus_one_second(self):
        client = self._client({"messages": []})
        client.fetch_history_around("rid", 1700000000.0, room_type="c")
        kwargs = client._rc.call_api_get.call_args[1]
        # ISO 8601 UTC 含毫秒 Z 后缀，且比目标时刻多 1 秒容差
        self.assertEqual(kwargs["latest"], "2023-11-14T22:13:21.000Z")
        self.assertEqual(kwargs["roomId"], "rid")
        self.assertEqual(kwargs["count"], 30)

    def test_returns_messages_newest_first(self):
        client = self._client({"messages": [{"_id": "m1"}, {"_id": "m2"}]})
        with mock.patch.object(ac.ApiClient, "_message_from_json",
                               side_effect=lambda m, rid: m["_id"]):
            msgs = client.fetch_history_around("rid", 1700000000.0)
        self.assertEqual(msgs, ["m1", "m2"])

    def test_unknown_room_type_raises(self):
        client = self._client()
        with self.assertRaises(ac.ApiError):
            client.fetch_history_around("rid", 1700000000.0, room_type="z")

    def test_not_logged_in_raises(self):
        client = ac.ApiClient.__new__(ac.ApiClient)
        client._rc = None
        with self.assertRaises(ac.ApiError):
            client.fetch_history_around("rid", 1700000000.0)

    def test_failure_wrapped_as_apierror(self):
        client = self._client(side_effect=ValueError("boom"))
        with self.assertRaises(ac.ApiError):
            client.fetch_history_around("rid", 1700000000.0)


# --------------------------------------------------------------------- #
# ApiClient.fetch_messages_since（锚定窗口向下续载）
# --------------------------------------------------------------------- #
class FetchMessagesSinceTest(unittest.TestCase):
    def _client(self, data=None, side_effect=None):
        client = ac.ApiClient.__new__(ac.ApiClient)
        client.server_url = "https://chat.example.com"
        rc = mock.Mock()
        client._rc = rc
        if side_effect is not None:
            rc.call_api_get.side_effect = side_effect
        else:
            rc.call_api_get.return_value = data or {"result": {"updated": []}}
        return client

    def test_calls_sync_messages_with_iso_cursor(self):
        client = self._client({"result": {"updated": []}})
        client.fetch_messages_since("rid", 1700000000.0)
        client._rc.call_api_get.assert_called_once_with(
            "chat.syncMessages", roomId="rid",
            lastUpdate="2023-11-14T22:13:20.000Z")

    def test_returns_only_newer_in_ascending_order(self):
        client = self._client(
            {"result": {"updated": [{"_id": "x"}, {"_id": "y"},
                                    {"_id": "z"}]}})
        import time as _t
        newer = Message(id="c", room_id="rid", text="c", timestamp=300.0)
        edited_old = Message(id="a", room_id="rid", text="a2", timestamp=100.0)
        middle = Message(id="b", room_id="rid", text="b", timestamp=200.0)
        with mock.patch.object(ac.ApiClient, "_message_from_json",
                               side_effect=[edited_old, newer, middle]):
            msgs = client.fetch_messages_since("rid", 150.0)
        self.assertEqual(msgs, [middle, newer])

    def test_not_logged_in_raises(self):
        client = ac.ApiClient.__new__(ac.ApiClient)
        client._rc = None
        with self.assertRaises(ac.ApiError):
            client.fetch_messages_since("rid", 1700000000.0)

    def test_failure_wrapped_as_apierror(self):
        client = self._client(side_effect=ValueError("boom"))
        with self.assertRaises(ac.ApiError):
            client.fetch_messages_since("rid", 1700000000.0)


# --------------------------------------------------------------------- #
# ApiClient.add_room_member
# --------------------------------------------------------------------- #
class AddRoomMemberTest(unittest.TestCase):
    def _client(self, rc=None):
        client = ac.ApiClient.__new__(ac.ApiClient)
        client.server_url = "https://chat.example.com"
        client._rc = rc or mock.Mock()
        return client

    def test_channel_invite_flow(self):
        rc = mock.Mock()
        rc.users_info.return_value = {"user": {"_id": "u_target"}}
        rc.call_api_post.return_value = {"success": True}
        client = self._client(rc)
        self.assertTrue(client.add_room_member("rid", "alice", "c"))
        rc.users_info.assert_called_once_with(username="alice")
        rc.call_api_post.assert_called_once_with(
            "channels.invite", body={"roomId": "rid", "userId": "u_target"})

    def test_group_invite_flow(self):
        rc = mock.Mock()
        rc.users_info.return_value = {"user": {"_id": "u_target"}}
        rc.call_api_post.return_value = {"success": True}
        client = self._client(rc)
        client.add_room_member("rid", "bob", "p")
        rc.call_api_post.assert_called_once_with(
            "groups.invite", body={"roomId": "rid", "userId": "u_target"})

    def test_user_not_found_raises(self):
        rc = mock.Mock()
        rc.users_info.return_value = {}
        client = self._client(rc)
        with self.assertRaises(ac.ApiError):
            client.add_room_member("rid", "ghost", "c")

    def test_dm_not_supported(self):
        client = self._client()
        with self.assertRaises(ac.ApiError):
            client.add_room_member("rid", "x", "d")

    def test_server_error_wrapped(self):
        rc = mock.Mock()
        rc.users_info.return_value = {"user": {"_id": "u1"}}
        rc.call_api_post.side_effect = RuntimeError("已在房间")
        client = self._client(rc)
        with self.assertRaises(ac.ApiError):
            client.add_room_member("rid", "x", "c")

    def test_not_logged_in_raises(self):
        client = ac.ApiClient.__new__(ac.ApiClient)
        client._rc = None
        with self.assertRaises(ac.ApiError):
            client.add_room_member("rid", "x")


# --------------------------------------------------------------------- #
# _conversation_from_subscription 讨论串判定
# --------------------------------------------------------------------- #
class SubscriptionDiscussionTest(unittest.TestCase):
    def setUp(self):
        self.client = ac.ApiClient.__new__(ac.ApiClient)
        self.client.server_url = "https://chat.example.com"
        self.client.user_id = "u_me"
        self.client._user_names = {}

    def test_discussion_from_subscription_prid(self):
        conv = self.client._conversation_from_subscription(
            {"rid": "r1", "t": "c", "name": "debug", "fname": "问题排查",
             "prid": "parent"})
        self.assertTrue(conv.is_discussion)
        self.assertEqual(conv.room_type, "c")

    def test_discussion_from_room_prid(self):
        conv = self.client._conversation_from_subscription(
            {"rid": "r1", "t": "p", "name": "grp"},
            room={"rid": "r1", "prid": "parent"})
        self.assertTrue(conv.is_discussion)

    def test_ordinary_room_not_discussion(self):
        conv = self.client._conversation_from_subscription(
            {"rid": "r1", "t": "p", "name": "grp", "fname": "产品研发组"})
        self.assertFalse(conv.is_discussion)

    def test_dm_not_discussion(self):
        conv = self.client._conversation_from_subscription(
            {"rid": "r1", "t": "d", "name": "zhangsan",
             "fname": "张三"})
        self.assertFalse(conv.is_discussion)
        self.assertEqual(conv.room_type, "d")


# --------------------------------------------------------------------- #
# ui.icons 新增三个面板图标
# --------------------------------------------------------------------- #
class PanelIconsTest(unittest.TestCase):
    def test_new_icons_render(self):
        from ui import icons
        icons.set_theme_colors("#3b3b3b")
        for name in ("members", "files", "search"):
            ic = icons.icon(name, 16)
            self.assertFalse(ic.pixmap(16).isNull(), name)

    def test_unknown_name_returns_empty(self):
        from ui import icons
        icons.set_theme_colors("#3b3b3b")
        self.assertTrue(icons.icon("__nope__", 16).pixmap(16).isNull())


# --------------------------------------------------------------------- #
# ChatArea 标题栏三按钮
# --------------------------------------------------------------------- #
class ChatAreaButtonsTest(unittest.TestCase):
    def setUp(self):
        from ui.main_window import ChatArea
        self.area = ChatArea()

    def tearDown(self):
        self.area.deleteLater()

    def test_four_buttons(self):
        self.assertEqual(set(self.area.panel_buttons),
                         {"members", "files", "messages", "ai"})
        for btn in self.area.panel_buttons.values():
            self.assertTrue(btn.isCheckable())
            self.assertEqual(btn.objectName(), "ChatHeaderBtn")

    def test_click_emits_kind(self):
        got = []
        self.area.panel_requested.connect(got.append)
        self.area.panel_buttons["files"].click()
        self.assertEqual(got, ["files"])

    def test_set_panel_active_checks_one(self):
        self.area.set_panel_active("members")
        self.assertTrue(self.area.panel_buttons["members"].isChecked())
        self.assertFalse(self.area.panel_buttons["files"].isChecked())
        self.area.set_panel_active(None)
        for btn in self.area.panel_buttons.values():
            self.assertFalse(btn.isChecked())

    def test_refresh_icons_rebuilds(self):
        from ui import icons
        before = {k: b.icon().cacheKey()
                  for k, b in self.area.panel_buttons.items()}
        icons.set_theme_colors("#101010")  # 换色后缓存清空
        self.area.refresh_icons()
        after = {k: b.icon().cacheKey()
                 for k, b in self.area.panel_buttons.items()}
        self.assertNotEqual(before, after)


# --------------------------------------------------------------------- #
# RoomPanels 控制器：互斥开合
# --------------------------------------------------------------------- #
class RoomPanelsControllerTest(unittest.TestCase):
    def setUp(self):
        from ui.room_panels import RoomPanels
        self.ctrl = RoomPanels(None)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.ctrl.close_all()
        self.ctrl.deleteLater()

    def test_toggle_opens_and_closes(self):
        btn = QPushButton()
        self.assertTrue(self.ctrl.toggle("members", btn, "r1", "p", "产品研发组"))
        self.assertTrue(self.ctrl.is_open("members"))
        self.assertFalse(self.ctrl.toggle("members", btn, "r1", "p", "产品研发组"))
        self.assertFalse(self.ctrl.is_open("members"))

    def test_toggle_is_mutually_exclusive(self):
        btn = QPushButton()
        self.ctrl.toggle("members", btn, "r1", "p", "x")
        self.ctrl.toggle("files", btn, "r1", "p", "x")
        self.assertFalse(self.ctrl.is_open("members"))
        self.assertTrue(self.ctrl.is_open("files"))

    def test_close_emits_closed_kind(self):
        got = []
        self.ctrl.closed.connect(got.append)
        btn = QPushButton()
        self.ctrl.toggle("files", btn, "r1", "c", "x")
        self.ctrl.close_all()
        drain_events(5)
        self.assertEqual(got, ["files"])

    def test_signals_forwarded(self):
        opens, saves, locates = [], [], []
        self.ctrl.file_open_requested.connect(
            lambda u, n: opens.append((u, n)))
        self.ctrl.file_save_as_requested.connect(
            lambda u, n, s: saves.append((u, n, s)))
        self.ctrl.locate_requested.connect(locates.append)
        self.ctrl.files.file_open_requested.emit("u", "n")
        self.ctrl.files.file_save_as_requested.emit("u", "n", 42)
        msg = Message(id="m1")
        self.ctrl.messages.locate_requested.emit(msg)
        self.assertEqual(opens, [("u", "n")])
        self.assertEqual(saves, [("u", "n", 42)])
        self.assertEqual(locates, [msg])


# --------------------------------------------------------------------- #
# MembersPanel
# --------------------------------------------------------------------- #
class MembersPanelTest(unittest.TestCase):
    def setUp(self):
        from ui.room_panels import MembersPanel
        self.panel = MembersPanel(None)
        self.addCleanup(self.panel.deleteLater)

    def _members(self, n, me_id="u_me"):
        ms = [{"_id": f"u{i}", "username": f"user{i}", "name": f"用户{i}"}
              for i in range(n)]
        ms.append({"_id": me_id, "username": "me", "name": "我自己"})
        return ms

    def test_build_excludes_self_and_counts(self):
        self.panel._client = mock.Mock()
        self.panel._client.user_id = "u_me"
        self.panel._room_type = "p"
        self.panel._build_members(self._members(2))
        self.assertEqual(self.panel.count_label.text(), "成员（2）")
        self.assertEqual(self.panel.member_list.count(), 2)

    def test_dm_label(self):
        self.panel._client = mock.Mock()
        self.panel._client.user_id = "u_me"
        self.panel._room_type = "d"
        self.panel._build_members([])
        self.assertEqual(self.panel.count_label.text(), "对方")

    def test_filter_by_name_and_username(self):
        self.panel._client = mock.Mock()
        self.panel._client.user_id = "u_me"
        self.panel._build_members(
            [{"_id": "u1", "username": "alice", "name": "爱丽丝"},
             {"_id": "u2", "username": "bob", "name": "鲍勃"}])
        self.panel.search.setText("ali")
        self.panel._apply_filter()
        self.assertEqual(self.panel.member_list.count(), 1)
        self.panel.search.setText("BOB")  # 大小写不敏感
        self.panel._apply_filter()
        self.assertEqual(self.panel.member_list.count(), 1)
        self.panel.search.setText("不存在的")
        self.panel._apply_filter()
        self.assertEqual(self.panel.member_list.count(), 1)  # 占位行
        self.assertEqual(self.panel.member_list.item(0).text(),
                         "未找到匹配的成员")

    def test_scroll_paging_grows_list(self):
        from ui.room_panels import _MEMBER_PAGE
        self.panel._client = mock.Mock()
        self.panel._client.user_id = "u_me"
        self.panel._build_members(self._members(_MEMBER_PAGE * 2))
        self.assertEqual(self.panel.member_list.count(), _MEMBER_PAGE)
        self.panel._on_member_scroll(
            self.panel.member_list.verticalScrollBar().maximum())
        self.assertEqual(self.panel.member_list.count(), _MEMBER_PAGE * 2)

    def test_provider_cache_hit_builds_immediately(self):
        self.panel._client = mock.Mock()
        self.panel._client.user_id = "u_me"
        calls = []
        self.panel.set_provider(
            lambda rid, rtype, on_ready: calls.append(rid) or [])
        btn = QPushButton()
        self.panel.open_for("r1", "c", "x", btn)
        self.addCleanup(self.panel.hide)
        self.assertEqual(calls, ["r1"])
        self.assertEqual(self.panel.count_label.text(), "成员（0）")

    def test_provider_fetch_then_callback(self):
        self.panel._client = mock.Mock()
        self.panel._client.user_id = "u_me"
        self.panel.set_provider(lambda rid, rtype, on_ready: None)

        def provider_ready():
            self.panel._on_members_ready(
                [{"_id": "u1", "username": "a", "name": "甲"}])

        def fake_provider(rid, rtype, on_ready):
            provider_ready()
            return None

        self.panel.set_provider(fake_provider)
        btn = QPushButton()
        self.panel.open_for("r1", "c", "x", btn)
        self.addCleanup(self.panel.hide)
        self.assertEqual(self.panel.member_list.count(), 1)

    def test_add_btn_hidden_for_dm(self):
        btn = QPushButton()
        self.panel._client = mock.Mock()
        self.panel._client.user_id = "u_me"
        self.panel.set_provider(lambda rid, rtype, on_ready: [])
        self.panel.open_for("r1", "d", "张三", btn)
        self.addCleanup(self.panel.hide)
        self.assertFalse(self.panel.add_btn.isVisibleTo(self.panel))
        self.panel.open_for("r2", "c", "群", btn)
        self.assertTrue(self.panel.add_btn.isVisibleTo(self.panel))

    def test_add_btn_emits_request(self):
        got = []
        self.panel.add_member_requested.connect(lambda: got.append(True))
        self.panel.add_btn.click()
        self.assertEqual(got, [True])

    def test_avatar_load_only_visible_rows(self):
        from ui.room_panels import _MEMBER_PAGE, _MEMBER_AVATAR_BATCH
        loader = mock.Mock()
        client = mock.Mock()
        client.user_id = "u_me"
        client.get_avatar_url.side_effect = \
            lambda username=None, **k: f"https://x/avatar/{username}"
        self.panel.set_session(client, loader)
        self.panel.resize(300, 400)
        self.panel.show()
        self.addCleanup(self.panel.hide)
        drain_events()
        self.panel._build_members(self._members(_MEMBER_PAGE * 4))
        self.panel._drain_avatars()
        vp = self.panel.member_list.viewport()
        tl = self.panel.member_list.indexAt(vp.rect().topLeft())
        bl = self.panel.member_list.indexAt(vp.rect().bottomLeft())
        visible_rows = (bl.row() if bl.isValid() else 0) \
            - (tl.row() if tl.isValid() else 0) + 1
        self.assertLess(visible_rows, _MEMBER_PAGE)
        self.assertLessEqual(loader.load.call_count, _MEMBER_AVATAR_BATCH)
        self.assertEqual(loader.load.call_count,
                         len(self.panel._avatar_loaded))

    def test_drain_stops_timer_when_all_visible_loaded(self):
        loader = mock.Mock()
        client = mock.Mock()
        client.user_id = "u_me"
        client.get_avatar_url.side_effect = \
            lambda username=None, **k: f"https://x/avatar/{username}"
        self.panel.set_session(client, loader)
        self.panel.show()
        self.addCleanup(self.panel.hide)
        drain_events()
        self.panel._build_members(self._members(3))
        for _ in range(10):
            self.panel._drain_avatars()
        self.assertFalse(self.panel._avatar_timer.isActive())
        self.assertEqual(loader.load.call_count,
                         len(self.panel._avatar_loaded))


# --------------------------------------------------------------------- #
# FilesPanel
# --------------------------------------------------------------------- #
class FilesPanelTest(unittest.TestCase):
    def setUp(self):
        from ui.room_panels import FilesPanel
        self.panel = FilesPanel(None)
        self.panel._client = mock.Mock()
        self.panel._rid = "r1"
        self.panel._room_type = "c"
        self.addCleanup(self.panel.deleteLater)
        ctx = mock.patch.object(
            __import__("ui.room_panels", fromlist=["run_async"]),
            "run_async", sync_run_async)
        ctx.start()
        self.addCleanup(ctx.stop)

    def test_browse_first_page(self):
        self.panel._client.fetch_room_files.return_value = {
            "files": [{"name": "a.pdf", "url": "https://x/a"}],
            "total": 1}
        self.panel._on_open()
        self.assertEqual(self.panel.file_list.count(), 1)
        self.assertEqual(self.panel._files_offset, 1)
        self.assertEqual(self.panel.hint_label.text(), "共 1 个文件")

    def test_browse_scroll_paging(self):
        pages = [
            {"files": [{"name": f"f{i}.pdf"} for i in range(50)], "total": 60},
            {"files": [{"name": f"g{i}.pdf"} for i in range(10)], "total": 60},
        ]
        self.panel._client.fetch_room_files.side_effect = pages
        self.panel._on_open()
        self.panel._load_files(self.panel._files_offset, append=True)
        self.assertEqual(self.panel.file_list.count(), 60)
        self.assertEqual(self.panel.hint_label.text(), "共 60 个文件")

    def _set_filter(self, text):
        """测试辅助：绕开 350ms 防抖定时器直接进入搜索（避免防抖在
        drain_events 中再次触发导致链路跑两遍）。"""
        self.panel._on_filter_changed(text)
        self.panel._filter_timer.stop()

    def test_search_fetches_all_pages_then_filters(self):
        def fetch(rid, rtype, offset=0, count=50):
            if offset == 0:
                return {"files": [{"name": f"报告{i}.pdf"} for i in range(100)],
                        "total": 120}
            return {"files": [{"name": f"照片{i}.png"} for i in range(20)],
                    "total": 120}

        self.panel._client.fetch_room_files.side_effect = fetch
        self.panel._on_open()
        self._set_filter("报告")
        self.panel._run_filter()
        drain_events()
        # 全量拉取后（120 条）客户端过滤出 100 条「报告」
        self.assertEqual(self.panel.file_list.count(), 100)
        self.assertIn("命中 100", self.panel.hint_label.text())

    def test_search_no_match_shows_placeholder(self):
        self.panel._client.fetch_room_files.return_value = {
            "files": [{"name": "a.pdf"}], "total": 1}
        self.panel._on_open()
        self._set_filter("xyz")
        self.panel._run_filter()
        drain_events()
        self.assertEqual(self.panel.file_list.count(), 1)
        self.assertEqual(self.panel.file_list.item(0).text(), "未找到匹配的文件")

    def test_token_invalidates_late_chain(self):
        # token 递增使旧链的晚到回调失效：晚到结果不得渲染进列表
        self.panel._client.fetch_room_files.return_value = {
            "files": [{"name": f"f{i}"} for i in range(100)], "total": 500}
        self.panel._on_open()
        self._set_filter("f")
        self.panel._run_filter()
        drain_events()
        # 正常链路已完成全量拉取（total=500）
        old_token = self.panel._search_token
        self.assertEqual(len(self.panel._all_files), 500)
        # 用旧 token 直接调用晚到的 _search_more：应被 token 校验挡住
        self.panel._search_token += 1  # 模拟期间用户又改了文本
        self.panel._search_more(old_token)
        self.assertEqual(len(self.panel._all_files), 500)

    def test_row_click_emits_open(self):
        got = []
        self.panel.file_open_requested.connect(lambda u, n: got.append((u, n)))
        self.panel._on_file_clicked({"url": "https://x/f", "name": "a.pdf"})
        self.assertEqual(got, [("https://x/f", "a.pdf")])

    def test_context_menu_save_closes_panel_and_emits(self):
        import ui.room_panels as rp
        got = []
        self.panel.file_save_as_requested.connect(
            lambda u, n, s: got.append((u, n, s)))
        row = mock.Mock()
        row._file = {"url": "https://x/f", "name": "a.pdf"}
        act_open, act_save = mock.Mock(name="open"), mock.Mock(name="save")
        menu = mock.MagicMock()
        menu.addAction.side_effect = [act_open, act_save]
        menu.exec_.return_value = act_save
        self.panel.file_list.itemAt = mock.Mock(return_value=mock.Mock())
        self.panel.file_list.itemWidget = mock.Mock(return_value=row)
        with mock.patch.object(rp, "QMenu", return_value=menu), \
                mock.patch.object(rp, "icon", return_value=mock.Mock()):
            self.panel.show()
            self.addCleanup(self.panel.hide)
            self.panel._show_menu(self.panel.file_list.viewport().rect().center())
        self.assertEqual(got, [("https://x/f", "a.pdf", 0)])
        self.assertFalse(self.panel.isVisible())

    def test_uploader_from_by_uid_provider(self):
        self.panel.set_by_uid_provider(
            lambda rid: {"u9": {"_id": "u9", "name": "王五"}})
        self.panel._client.fetch_room_files.return_value = {
            "files": [{"name": "a.pdf", "url": "https://x/a",
                       "userId": "u9", "size": 2048, "uploadedAt": ""}],
            "total": 1}
        self.panel._on_open()
        from PyQt5.QtWidgets import QLabel
        row = self.panel.file_list.itemWidget(self.panel.file_list.item(0))
        meta = " ".join(l.text() for l in row.findChildren(QLabel))
        self.assertIn("王五", meta)
        self.assertIn("2.0 KB", meta)

    def test_file_search_box_is_taller(self):
        """文件名搜索框加高（真机反馈原输入框偏小）。"""
        self.assertGreaterEqual(self.panel.file_search.minimumHeight(), 34)


# --------------------------------------------------------------------- #
# MessagesPanel
# --------------------------------------------------------------------- #
class MessagesPanelTest(unittest.TestCase):
    def setUp(self):
        from ui.room_panels import MessagesPanel
        self.panel = MessagesPanel(None)
        self.panel._client = mock.Mock()
        self.panel._rid = "r1"
        self.panel._room_type = "c"
        self.addCleanup(self.panel.deleteLater)
        ctx = mock.patch.object(
            __import__("ui.room_panels", fromlist=["run_async"]),
            "run_async", sync_run_async)
        ctx.start()
        self.addCleanup(ctx.stop)

    def _msg(self, mid, text, sender="张三"):
        return Message(id=mid, room_id="r1", text=text,
                       sender=User(username="zs", name=sender),
                       timestamp=1700000000.0)

    def _set_text(self, text):
        """测试辅助：设置搜索词后停掉 350ms 防抖，避免 drain_events 中
        防抖超时再次触发搜索影响断言。"""
        self.panel.search_input.setText(text)
        self.panel._debounce.stop()

    def test_plain_search_scans_history(self):
        # 普通搜索与正则共用客户端全历史扫描：服务端 chat.search 基于
        # MongoDB 文本索引，中文不分词基本搜不到
        page1 = [self._msg(f"m{i}", f"普通消息{i}") for i in range(100)]
        page2 = [self._msg("hit", "包含你好的内容"),
                 self._msg("m_end", "尾部")]

        def history(rid, count=100, offset=0, room_type="c"):
            return page1 if offset == 0 else page2

        self.panel._client.get_history.side_effect = history
        self._set_text("你好")
        self.panel._run_search()
        drain_events()
        self.assertEqual(self.panel.search_list.count(), 1)
        self.assertIn("命中 1 条", self.panel.hint_label.text())
        self.panel._client.search_messages.assert_not_called()

    def test_scan_empty_shows_placeholder(self):
        self.panel._client.get_history.return_value = []
        self._set_text("不存在的内容")
        self.panel._run_search()
        drain_events()
        self.assertEqual(self.panel.search_list.count(), 1)
        self.assertEqual(self.panel.search_list.item(0).text(),
                         "未找到匹配的消息")

    def test_plain_search_literal_not_regex(self):
        # 未勾正则时按原文匹配，"." 不当作任意字符
        page1 = [self._msg("hit", "a.b 与 aXb 都在"),
                 self._msg("m_end", "尾部")]
        self.panel._client.get_history.return_value = page1
        self._set_text("a.b")
        self.panel._run_search()
        drain_events()
        self.assertEqual(self.panel.search_list.count(), 1)

    def test_invalid_regex_shows_error_no_request(self):
        self._set_text("(")
        self.panel.rx_box.setChecked(True)  # toggled 触发 _run_search
        self.assertTrue(self.panel.error_label.isVisibleTo(self.panel))
        self.assertIn("正则表达式无效", self.panel.error_label.text())
        self.panel._client.get_history.assert_not_called()

    def test_regex_scan_all_history(self):
        page1 = [self._msg(f"m{i}", f"普通消息{i}") for i in range(100)]
        page2 = [self._msg("hit", "特殊的alpha内容"),
                 self._msg("m_end", "尾部")]

        def history(rid, count=100, offset=0, room_type="c"):
            return page1 if offset == 0 else page2

        self.panel._client.get_history.side_effect = history
        self._set_text("alpha")
        self.panel.rx_box.setChecked(True)
        drain_events()
        self.assertEqual(self.panel.search_list.count(), 1)
        self.assertIn("已扫描全部 102 条", self.panel.hint_label.text())
        self.assertIn("命中 1 条", self.panel.hint_label.text())
        self.assertFalse(self.panel.stop_btn.isVisibleTo(self.panel))

    def test_regex_scan_capped_render(self):
        from ui.room_panels import _MAX_RESULT_ROWS
        page1 = [self._msg(f"m{i}", "命中x") for i in range(100)]
        page2 = [self._msg(f"n{i}", "命中x") for i in range(100)]

        def history(rid, count=100, offset=0, room_type="c"):
            return page1 if offset == 0 else page2

        self.panel._client.get_history.side_effect = history
        self._set_text("命中x")
        self.panel.rx_box.setChecked(True)
        drain_events()
        self.assertLessEqual(self.panel.search_list.count(),
                             _MAX_RESULT_ROWS)
        self.assertIn("仅显示前", self.panel.hint_label.text())

    def test_stop_invalidates_scan(self):
        captured = []
        with mock.patch.object(
                __import__("ui.room_panels", fromlist=["run_async"]),
                "run_async",
                lambda fn, on_done=None, on_fail=None, parent=None:
                captured.append((fn, on_done))):
            self._set_text("x")
            self.panel.rx_box.setChecked(True)
        self.assertTrue(self.panel.stop_btn.isVisibleTo(self.panel))
        token = self.panel._scan_token
        self.panel._stop_scan()
        self.assertGreater(self.panel._scan_token, token)
        # 旧 token 的晚到回调被忽略
        fn, on_done = captured[0]
        on_done(([self._msg("m1", "x命中")], 1))
        self.assertEqual(self.panel.search_list.count(), 0)
        self.assertFalse(self.panel.stop_btn.isVisibleTo(self.panel))

    def test_result_click_emits_locate(self):
        got = []
        self.panel.locate_requested.connect(got.append)
        msg = self._msg("m1", "你好")
        from ui.room_panels import _ResultRow
        r = _ResultRow(msg, self.panel._color)
        r.clicked.connect(self.panel.locate_requested.emit)
        r.clicked.emit(msg)
        self.assertEqual(got, [msg])

    def test_flush_rows_connects_and_limits(self):
        from ui.room_panels import _MAX_RESULT_ROWS
        got = []
        self.panel.locate_requested.connect(got.append)
        self.panel._queue_rows([self._msg(f"m{i}", "t") for i in range(5)])
        self.panel._flush_rows()
        self.assertEqual(self.panel.search_list.count(), 5)
        row = self.panel.search_list.itemWidget(self.panel.search_list.item(0))
        # 点击结果行发出 locate 信号
        row.clicked.emit(self._msg("m0", "t"))
        self.assertEqual(len(got), 1)


# --------------------------------------------------------------------- #
# ChatView.save_file_as / _open_file_cached 委托
# --------------------------------------------------------------------- #
class SaveFileAsTest(unittest.TestCase):
    def _make_view(self):
        from ui.chat_view import ChatView
        view = ChatView.__new__(ChatView)
        view.image_loader = mock.Mock()
        view.image_loader.ssl_verify = True
        view._downloading = set()
        view._theme = None
        view._save_dialogs = set()
        view.status_message = mock.Mock()
        return view

    def _fake_dialog(self):
        """patch 到 ui.chat_view.FileDownloadDialog 的假弹窗类。

        ChatView 经 __new__ 构造，真弹窗会起真 QThread + 发网络请求；整体
        替换成记录构造参数、可手动触发 succeeded/failed 回调的桩。
        """
        dlg = mock.MagicMock()
        return mock.MagicMock(return_value=dlg), dlg

    def _saved_cb(self, dlg):
        return dlg.succeeded.connect.call_args[0][0]

    def _failed_cb(self, dlg):
        return dlg.failed.connect.call_args[0][0]

    def test_save_file_as_opens_dialog_and_toasts_on_success(self):
        view = self._make_view()
        view.image_loader.resolve.return_value = "https://x/f"
        cv = sys.modules["ui.chat_view"]
        cls, dlg = self._fake_dialog()
        with mock.patch.object(cv.QFileDialog, "getSaveFileName",
                               return_value=("D:/out/a.pdf", "")), \
                mock.patch.object(cv, "FileDownloadDialog", cls):
            view.save_file_as("/f", "a.pdf", total=100)
        cls.assert_called_once()
        args, kwargs = cls.call_args
        self.assertEqual(args[0], "https://x/f")    # 解析后的绝对 url
        self.assertEqual(args[1], "D:/out/a.pdf")   # 用户选择的目标路径
        self.assertEqual(kwargs.get("total"), 100)  # 已知大小传入用于兜底
        self.assertTrue(kwargs.get("headers"))      # 带认证头
        self.assertIs(kwargs.get("center_on"), view)  # 按主窗口居中
        self.assertIsNone(kwargs.get("parent"))     # 无 parent → 独立窗口不置顶
        self.assertIn(dlg, view._save_dialogs)      # 持强引用防 GC
        dlg.show.assert_called_once()               # 非模态：show 而非 exec_
        self._saved_cb(dlg)("D:/out/a.pdf")
        view.status_message.emit.assert_called()    # 成功后状态栏提示

    def test_save_file_as_failure_toasts_but_cancel_silent(self):
        view = self._make_view()
        view.image_loader.resolve.return_value = "https://x/f"
        cv = sys.modules["ui.chat_view"]
        cls, dlg = self._fake_dialog()
        with mock.patch.object(cv.QFileDialog, "getSaveFileName",
                               return_value=("D:/out/a.pdf", "")), \
                mock.patch.object(cv, "FileDownloadDialog", cls):
            view.save_file_as("/f", "a.pdf")
        self._failed_cb(dlg)("已取消")
        view.status_message.emit.assert_not_called()    # 取消不提示
        self._failed_cb(dlg)("boom")
        view.status_message.emit.assert_called_once()   # 真失败才提示

    def test_save_file_as_abort_path_no_dialog(self):
        view = self._make_view()
        cv = sys.modules["ui.chat_view"]
        cls, _ = self._fake_dialog()
        with mock.patch.object(cv.QFileDialog, "getSaveFileName",
                               return_value=("", "")), \
                mock.patch.object(cv, "FileDownloadDialog", cls):
            view.save_file_as("https://x/f", "a.pdf")
        cls.assert_not_called()                         # 未选路径不下载

    def test_close_download_dialogs_force_closes_all(self):
        # 主窗口关闭：所有在途弹窗 force_close 并清空集合
        view = self._make_view()
        d1, d2 = mock.MagicMock(), mock.MagicMock()
        view._save_dialogs.update({d1, d2})
        view.close_download_dialogs()
        d1.force_close.assert_called_once()
        d2.force_close.assert_called_once()
        self.assertEqual(view._save_dialogs, set())

    def test_open_file_cached_delegates_to_open_room_file(self):
        view = self._make_view()
        msg = Message(id="m1", file_url="https://x/f", file_name="a.pdf")
        with mock.patch.object(view, "open_room_file") as orf:
            view._open_file_cached(msg)
        orf.assert_called_once_with("https://x/f", "a.pdf")


# --------------------------------------------------------------------- #
# MainWindow._locate_message：快路径 / around 命中 / 翻页回退
# --------------------------------------------------------------------- #
class _LocateHarness(unittest.TestCase):
    def _make_mw(self):
        from ui.main_window import MainWindow
        from ui.main_window import ChatArea
        from ui.room_panels import RoomPanels
        mw = MainWindow.__new__(MainWindow)
        mw.client = mock.Mock()
        # 裸 __new__ 构造没跑 QObject.__init__：访问未赋值的实例属性会抛
        # "super-class __init__() was never called"，系统消息姓名解析用的
        # provider 必须显式给 None
        mw._status_provider = None
        mw._active_room_id = "r1"
        mw._room_type = "c"
        mw._page_size = 30
        mw._conversations = {}
        mw._room_messages = {}
        mw._history_offset = {}
        mw._history_more = {}
        mw._history_anchored = set()
        mw._history_forward_done = set()
        mw._suppress_bottom_load = False
        mw._history_anchor_gen = {}
        mw._history_anchor_ts = {}
        mw._history_loading = set()
        mw._snapshot_shown = set()
        mw._load_gen = {}
        mw._loading_older = False
        mw._loading_newer = False
        mw._latest_by_room = {}
        mw._history_retry_conv = None
        mw.chat_area = ChatArea()
        mw._room_panels = RoomPanels(None)
        mw.statusBar = mock.Mock(return_value=mock.Mock())
        self.addCleanup(mw.chat_area.deleteLater)
        self.addCleanup(mw._room_panels.close_all)
        return mw

    def setUp(self):
        self.mw = self._make_mw()

    def test_fast_path_flashes_without_fetch(self):
        view = self.mw.chat_area.chat_view
        msg = Message(id="m1", room_id="r1", text="t")
        with mock.patch.object(view.model, "find_message_row", return_value=0), \
                mock.patch.object(view, "flash_row") as flash, \
                mock.patch("ui.main_window.run_async") as ra:
            self.mw._locate_message(msg)
        flash.assert_called_once_with("m1")
        ra.assert_not_called()

    def test_other_room_message_ignored(self):
        msg = Message(id="m1", room_id="other", text="t")
        with mock.patch("ui.main_window.run_async") as ra:
            self.mw._locate_message(msg)
        ra.assert_not_called()

    def test_around_hit_rebuilds_window_and_flashes(self):
        view = self.mw.chat_area.chat_view
        target = Message(id="target", room_id="r1", text="t", timestamp=100.0)
        newer = Message(id="newer", room_id="r1", text="t2", timestamp=200.0)
        self.mw.client.fetch_history_around.return_value = [newer, target]
        with mock.patch.object(view, "flash_row") as flash, \
                mock.patch("ui.main_window.run_async", sync_run_async):
            self.mw._locate_message(target)
        self.mw.client.fetch_history_around.assert_called_once_with(
            "r1", 100.0, count=30, room_type="c")
        # rebuild 后模型内为时间正序（旧->新）
        self.assertEqual([m.id for m in self.mw._room_messages["r1"]],
                         ["target", "newer"])
        self.assertFalse(self.mw._history_more["r1"])
        self.assertEqual(self.mw._history_offset["r1"], 2)
        self.assertIn("r1", self.mw._history_anchored)
        flash.assert_called_once_with("target")

    def test_around_miss_falls_back_to_paging(self):
        view = self.mw.chat_area.chat_view
        target = Message(id="target", room_id="r1", text="t", timestamp=100.0)
        self.mw.client.fetch_history_around.return_value = [
            Message(id="other", room_id="r1", text="o", timestamp=200.0)]
        self.mw.client.get_history.return_value = [target]
        with mock.patch.object(view, "flash_row") as flash, \
                mock.patch("ui.main_window.run_async", sync_run_async):
            self.mw._locate_message(target)
        self.mw.client.get_history.assert_called_once()
        flash.assert_called_once_with("target")

    def test_fallback_exhausted_shows_message(self):
        view = self.mw.chat_area.chat_view
        target = Message(id="target", room_id="r1", text="t", timestamp=100.0)
        self.mw.client.fetch_history_around.return_value = []
        self.mw.client.get_history.return_value = []  # 没有更多页
        status = mock.Mock()
        self.mw.statusBar = mock.Mock(return_value=status)
        with mock.patch.object(view, "flash_row") as flash, \
                mock.patch("ui.main_window.run_async", sync_run_async):
            self.mw._locate_message(target)
        flash.assert_not_called()
        status.showMessage.assert_called_once()
        self.assertIn("未能定位", status.showMessage.call_args[0][0])

    def test_date_jump_fetches_page_anchored_at_day_end(self):
        """选日期：latest 锚定该日 23:59:59；该页整页保留（含更早上下文，
        否则少于页容量的一天会让历史消息从视图里消失），闪该日最早一条。"""
        view = self.mw.chat_area.chat_view
        import time as _t
        zero = _t.mktime((2026, 10, 20, 0, 0, 0, 0, 0, -1))
        day = Message(id="day2", room_id="r1", text="b", timestamp=zero + 100)
        day_first = Message(id="day1", room_id="r1", text="a",
                            timestamp=zero + 50)
        before = Message(id="old", room_id="r1", text="o",
                         timestamp=zero - 100)
        self.mw.client.fetch_history_around.return_value = [day, day_first,
                                                            before]
        with mock.patch.object(view, "flash_row") as flash, \
                mock.patch("ui.main_window.run_async", sync_run_async):
            self.mw._on_date_jump(zero)
        self.mw.client.fetch_history_around.assert_called_once_with(
            "r1", zero + 86399, count=30, room_type="c")
        self.assertEqual([m.id for m in self.mw._room_messages["r1"]],
                         ["old", "day1", "day2"])
        self.assertIn("r1", self.mw._history_anchored)
        self.assertEqual(self.mw._history_anchor_ts["r1"], zero + 100)
        flash.assert_called_once_with("day1")

    def test_date_jump_page_without_that_day_keeps_view(self):
        """该日无消息（整页都是更早的上下文）时只提示，不拿旧页覆盖视图。"""
        view = self.mw.chat_area.chat_view
        import time as _t
        zero = _t.mktime((2026, 10, 20, 0, 0, 0, 0, 0, -1))
        before = Message(id="old", room_id="r1", text="o",
                         timestamp=zero - 100)
        self.mw.client.fetch_history_around.return_value = [before]
        status = mock.Mock()
        self.mw.statusBar = mock.Mock(return_value=status)
        with mock.patch.object(view, "rebuild_messages") as rb, \
                mock.patch("ui.main_window.run_async", sync_run_async):
            self.mw._on_date_jump(zero)
        rb.assert_not_called()
        self.assertNotIn("r1", self.mw._history_anchored)
        self.assertIn("该日期没有聊天记录",
                      status.showMessage.call_args[0][0])

    def test_reopen_after_anchor_reloads_latest_first_page(self):
        """锚定页不是完整历史：重进房间必须清窗口、按 offset=0 重拉首屏。"""
        old = Message(id="old", room_id="r1", text="o", timestamp=100.0)
        self.mw._room_messages["r1"] = [old]
        self.mw._history_offset["r1"] = 1
        self.mw._history_more["r1"] = False
        self.mw._history_anchored.add("r1")
        self.mw._history_forward_done.add("r1")
        latest = [Message(id="m2", room_id="r1", text="b", timestamp=300.0),
                  Message(id="m1", room_id="r1", text="a", timestamp=200.0)]
        self.mw.client.get_history.return_value = latest
        self.mw.client.server_url = "https://x"
        self.mw.client.me.username = "u"
        conv = mock.Mock(id="r1", room_type="c")
        with mock.patch("ui.main_window.run_async", sync_run_async), \
                mock.patch("core.msg_cache.load_messages_snapshot",
                           return_value=[]), \
                mock.patch("core.msg_cache.save_messages_snapshot"), \
                mock.patch.object(self.mw, "_snapshot_shown", set()), \
                mock.patch.object(self.mw, "_enrich_quotes", create=True), \
                mock.patch.object(self.mw, "_resolve_system_names",
                                  create=True), \
                mock.patch.object(self.mw, "_sync_conversation_from_history",
                                  create=True), \
                mock.patch.object(self.mw, "_apply_receipts_state_to_view",
                                  create=True), \
                mock.patch.object(self.mw, "_schedule_receipts_fetch",
                                  create=True), \
                mock.patch.object(self.mw, "_apply_unread_divider",
                                  create=True), \
                mock.patch.object(self.mw, "_maybe_locate_pending",
                                  create=True):
            self.mw._load_history(conv)
        self.mw.client.get_history.assert_called_once_with(
            "r1", count=30, offset=0, room_type="c")
        self.assertNotIn("r1", self.mw._history_anchored)
        self.assertNotIn("r1", self.mw._history_forward_done)
        self.assertEqual([m.id for m in self.mw._room_messages["r1"]],
                         ["m1", "m2"])
        self.assertEqual(self.mw._history_offset["r1"], 2)

    def test_anchor_scroll_loads_older_via_latest(self):
        """锚定窗口向上翻页按窗口最早一条的 latest 锚点，而不是 offset。"""
        from PyQt5.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        self.mw._history_anchored.add("r1")
        self.mw._history_more["r1"] = True
        self.mw._history_offset["r1"] = 2
        first = Message(id="a", room_id="r1", text="a", timestamp=100.0)
        second = Message(id="b", room_id="r1", text="b", timestamp=200.0)
        self.mw._room_messages["r1"] = [first, second]
        older = [Message(id="y", room_id="r1", text="y", timestamp=60.0),
                 Message(id="x", room_id="r1", text="x", timestamp=50.0)]
        self.mw.client.fetch_history_around.return_value = older
        view = self.mw.chat_area.chat_view
        with mock.patch("ui.main_window.run_async", sync_run_async), \
                mock.patch.object(view, "prepend_messages") as pp, \
                mock.patch.object(self.mw, "_enrich_quotes", create=True), \
                mock.patch.object(self.mw, "_resolve_system_names",
                                  create=True):
            self.mw._load_older_history()
        self.mw.client.fetch_history_around.assert_called_once_with(
            "r1", 99.0, count=30, room_type="c")
        pp.assert_called_once_with(older)
        self.assertEqual([m.id for m in self.mw._room_messages["r1"]],
                         ["x", "y", "a", "b"])

    def test_anchor_bottom_loads_newer_messages(self):
        """日期跳转后滚到底：syncMessages 补齐锚定页之后的新消息并追加。"""
        self.mw._history_anchored.add("r1")
        first = Message(id="a", room_id="r1", text="a", timestamp=100.0)
        second = Message(id="b", room_id="r1", text="b", timestamp=200.0)
        self.mw._room_messages["r1"] = [first, second]
        newest = Message(id="c", room_id="r1", text="c", timestamp=300.0)
        self.mw.client.fetch_messages_since.return_value = [newest]
        view = self.mw.chat_area.chat_view
        with mock.patch("ui.main_window.run_async", sync_run_async), \
                mock.patch.object(view, "append_messages") as app, \
                mock.patch.object(self.mw, "_enrich_quotes", create=True), \
                mock.patch.object(self.mw, "_resolve_system_names",
                                  create=True):
            self.mw._on_view_bottom()
        self.mw.client.fetch_messages_since.assert_called_once_with(
            "r1", 200.0)
        self.assertEqual([m.id for m in self.mw._room_messages["r1"]],
                         ["a", "b", "c"])
        app.assert_called_once_with([newest])
        self.assertIn("r1", self.mw._history_forward_done)

    def test_anchor_bottom_dedupes_realtime_messages(self):
        """实时推送已追加的同一条消息不能因续载重复入库/入视图。"""
        self.mw._history_anchored.add("r1")
        first = Message(id="a", room_id="r1", text="a", timestamp=100.0)
        pushed = Message(id="c", room_id="r1", text="c", timestamp=300.0)
        self.mw._room_messages["r1"] = [first, pushed]
        self.mw.client.fetch_messages_since.return_value = [pushed]
        view = self.mw.chat_area.chat_view
        with mock.patch("ui.main_window.run_async", sync_run_async), \
                mock.patch.object(view, "append_messages") as app, \
                mock.patch.object(self.mw, "_enrich_quotes", create=True), \
                mock.patch.object(self.mw, "_resolve_system_names",
                                  create=True):
            self.mw._on_view_bottom()
        app.assert_not_called()
        self.assertEqual([m.id for m in self.mw._room_messages["r1"]],
                         ["a", "c"])
        self.assertIn("r1", self.mw._history_forward_done)

    def test_anchor_bottom_uses_anchor_cursor_not_cache_tail(self):
        """锚定期间实时推送/自己发的消息追加到了尾部，游标仍取锚定窗口
        最新一条，否则会跳过「锚定日~现在」的间隔。"""
        self.mw._history_anchored.add("r1")
        self.mw._history_anchor_ts["r1"] = 200.0
        self.mw._room_messages["r1"] = [
            Message(id="a", room_id="r1", text="a", timestamp=100.0),
            Message(id="live", room_id="r1", text="l", timestamp=900.0)]
        self.mw.client.fetch_messages_since.return_value = []
        with mock.patch("ui.main_window.run_async", sync_run_async):
            self.mw._on_view_bottom()
        self.mw.client.fetch_messages_since.assert_called_once_with(
            "r1", 200.0)
        self.assertIn("r1", self.mw._history_forward_done)

    def test_anchor_bottom_fills_only_once(self):
        self.mw._history_anchored.add("r1")
        self.mw._history_forward_done.add("r1")
        self.mw._room_messages["r1"] = [
            Message(id="a", room_id="r1", text="a", timestamp=100.0)]
        with mock.patch("ui.main_window.run_async") as ra:
            self.mw._on_view_bottom()
        ra.assert_not_called()

    def test_bottom_outside_anchor_does_nothing(self):
        self.mw._room_messages["r1"] = [
            Message(id="a", room_id="r1", text="a", timestamp=100.0)]
        with mock.patch("ui.main_window.run_async") as ra:
            self.mw._on_view_bottom()
        ra.assert_not_called()

    def test_bottom_suppressed_while_rebuilding_anchor(self):
        self.mw._history_anchored.add("r1")
        self.mw._suppress_bottom_load = True
        self.mw._room_messages["r1"] = [
            Message(id="a", room_id="r1", text="a", timestamp=100.0)]
        with mock.patch("ui.main_window.run_async") as ra:
            self.mw._on_view_bottom()
        ra.assert_not_called()

    def test_stale_newer_fill_discarded_after_anchor_switch(self):
        """在途续载返回前窗口已换（又跳了别的日期）：结果作废不追加。"""
        self.mw._history_anchored.add("r1")
        self.mw._room_messages["r1"] = [
            Message(id="a", room_id="r1", text="a", timestamp=100.0)]
        captured = {}

        def fake_run_async(fn, on_done=None, on_fail=None, parent=None):
            captured["done"] = on_done

        view = self.mw.chat_area.chat_view
        with mock.patch("ui.main_window.run_async", fake_run_async), \
                mock.patch.object(view, "append_messages") as app:
            self.mw._on_view_bottom()
            self.mw._history_anchor_gen["r1"] = \
                self.mw._history_anchor_gen.get("r1", 0) + 1
            captured["done"]([Message(id="c", room_id="r1", text="c",
                                      timestamp=300.0)])
        app.assert_not_called()
        self.assertEqual([m.id for m in self.mw._room_messages["r1"]],
                         ["a"])
        self.assertNotIn("r1", self.mw._history_forward_done)

    def test_date_jump_empty_day_shows_message(self):
        import time as _t
        zero = _t.mktime((2026, 10, 20, 0, 0, 0, 0, 0, -1))
        self.mw.client.fetch_history_around.return_value = []
        status = mock.Mock()
        self.mw.statusBar = mock.Mock(return_value=status)
        with mock.patch("ui.main_window.run_async", sync_run_async):
            self.mw._on_date_jump(zero)
        status.showMessage.assert_called_once()
        self.assertIn("该日期没有聊天记录",
                      status.showMessage.call_args[0][0])

    def test_date_jump_closes_panel(self):
        import time as _t
        zero = _t.mktime((2026, 10, 20, 0, 0, 0, 0, 0, -1))
        self.mw._room_panels.messages.show()
        self.mw.client.fetch_history_around.return_value = []
        with mock.patch("ui.main_window.run_async", sync_run_async):
            self.mw._on_date_jump(zero)
        self.assertFalse(self.mw._room_panels.messages.isVisible())


class DateJumpPanelTest(unittest.TestCase):
    """记录面板「跳转日期」下拉与自定义日期。"""

    def setUp(self):
        from ui.room_panels import MessagesPanel
        self.panel = MessagesPanel(None)
        self.addCleanup(self.panel.deleteLater)
        self.fired = []
        self.panel.date_jump_requested.connect(
            lambda ts: self.fired.append(ts))

    def test_combo_has_placeholder_seven_days_and_pick(self):
        self.assertEqual(self.panel.date_combo.count(), 9)
        self.assertEqual(self.panel.date_combo.itemText(0), "跳转日期")
        self.assertIsNone(self.panel.date_combo.itemData(0))
        self.assertEqual(self.panel.date_combo.itemText(8), "选择日期…")
        self.assertEqual(self.panel.date_combo.itemData(8), "pick")

    def test_pick_day_emits_local_midnight(self):
        import time as _t
        self.panel.date_combo.setCurrentIndex(2)  # 昨天
        self.assertEqual(len(self.fired), 1)
        ts = self.fired[0]
        lt = _t.localtime(ts)
        self.assertEqual((lt.tm_hour, lt.tm_min, lt.tm_sec), (0, 0, 0))
        yesterday = _t.localtime(_t.time() - 86400)
        self.assertEqual((lt.tm_year, lt.tm_mon, lt.tm_mday),
                         (yesterday.tm_year, yesterday.tm_mon,
                          yesterday.tm_mday))

    def test_today_entry_is_index_one(self):
        import time as _t
        self.panel.date_combo.setCurrentIndex(1)
        lt = _t.localtime(self.fired[0])
        today = _t.localtime(_t.time())
        self.assertEqual((lt.tm_year, lt.tm_mon, lt.tm_mday),
                         (today.tm_year, today.tm_mon, today.tm_mday))

    def test_placeholder_does_not_emit(self):
        self.panel.date_combo.setCurrentIndex(0)
        self.assertEqual(self.fired, [])

    def test_pick_opens_date_edit_then_picked_emits(self):
        self.panel.date_combo.setCurrentIndex(8)
        self.assertFalse(self.panel.date_edit.isHidden())
        self.assertEqual(self.fired, [])
        from PyQt5.QtCore import QDate
        self.panel.date_edit.setDate(QDate(2026, 10, 5))
        self.assertEqual(len(self.fired), 1)
        lt = __import__("time").localtime(self.fired[0])
        self.assertEqual((lt.tm_year, lt.tm_mon, lt.tm_mday),
                         (2026, 10, 5))
        self.assertEqual((lt.tm_hour, lt.tm_min), (0, 0))

    def test_open_resets_combo_and_hides_date_edit(self):
        self.panel.date_combo.setCurrentIndex(8)
        self.assertFalse(self.panel.date_edit.isHidden())
        self.panel._on_open()
        self.assertEqual(self.panel.date_combo.currentIndex(), 0)
        self.assertTrue(self.panel.date_edit.isHidden())

    def test_search_box_gets_full_row_width(self):
        """搜索框独占一行，不再被右侧控件挤成几十像素。"""
        self.panel.layout().activate()
        self.assertGreater(self.panel.search_input.width(),
                           self.panel.width() * 0.8)
        self.assertGreaterEqual(self.panel.search_input.minimumHeight(), 34)

    def test_pick_swaps_combo_for_date_edit(self):
        """选「选择日期…」后日期框与下拉互斥，避免同行超宽被裁切。"""
        self.panel.date_combo.setCurrentIndex(8)
        self.assertTrue(self.panel.date_combo.isHidden())
        self.assertFalse(self.panel.date_edit.isHidden())
        self.assertTrue(self.panel.date_edit.isVisibleTo(self.panel))

    def test_date_edit_fits_panel_width(self):
        """日期框完整落在面板内容区内（真机「控件显示不全」）。"""
        self.panel.layout().activate()
        self.panel.date_combo.setCurrentIndex(8)
        self.panel.layout().activate()
        g = self.panel.date_edit.geometry()
        self.assertLessEqual(g.right(), self.panel.width() - 10)
        self.assertGreaterEqual(g.width(), 120)

    def test_date_picked_restores_combo(self):
        from PyQt5.QtCore import QDate
        self.panel.date_combo.setCurrentIndex(8)
        self.panel.date_edit.setDate(QDate(2026, 10, 6))
        self.assertFalse(self.panel.date_combo.isHidden())
        self.assertEqual(self.panel.date_combo.currentIndex(), 0)
        self.assertTrue(self.panel.date_edit.isHidden())
        self.assertEqual(len(self.fired), 1)


if __name__ == "__main__":
    unittest.main()
