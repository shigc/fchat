"""目录面板（频道/用户/团队）与预览模式测试。

覆盖：
- ApiClient.fetch_directory_channels/groups/users：端点、分页参数、
  字段映射、缺失字段降级（无 lm → 0.0）
- ApiClient.fetch_directory_teams / search_directory_users /
  search_directory_rooms：directory 端点（text/type/workspace 平级
  URL 参数）、字段映射、data 包裹兜底、排除自己、空词不发请求、
  错误包装
- ApiClient.fetch_room_info / join_channel / join_team 降级链
- ui.icons 新增 directory/hash/team 图标
- DirectoryPanel：页签懒加载 / 三页签搜索下推服务端 / token 失效 /
  行渲染（"—"/徽标）/ 相对主窗口居中
- RoomPanels 控制器互斥（directory 与 members）
- ChatArea 预览横幅显隐；MainWindow._open_conversation(preview=True)
  跳过订阅与已读上报；_join_preview_room 成功走 _reload_and_open

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

from PyQt5.QtWidgets import (  # noqa: E402
    QApplication, QDesktopWidget, QLabel, QPushButton,
)

import config  # noqa: E402
from core import api_client as ac  # noqa: E402
from core.models import Conversation, DirectoryEntry  # noqa: E402
from ui.icons import icon  # noqa: E402
from ui.room_panels import RoomPanels  # noqa: E402

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
    for _ in range(n):
        _APP.processEvents()
        time.sleep(0.01)


def _client_with(rc):
    c = ac.ApiClient("http://x")
    c._rc = rc
    c.user_id = "me"
    return c


# --------------------------------------------------------------------- #
# core 解析
# --------------------------------------------------------------------- #
class DirectoryApiTest(unittest.TestCase):
    def test_channels_mapping_and_dedup(self):
        rc = mock.Mock()
        rc.call_api_get.return_value = {"channels": [
            {"_id": "r1", "name": "general", "fname": "综合",
             "usersCount": 5, "ts": "2024-01-02T03:04:05Z",
             "lm": "2026-01-01T00:00:00Z"},
            # 无 fname/lm/usersCount/ts：逐字段降级
            {"_id": "r2", "name": "dev"},
            # 团队主房间：目录频道页要去重（团队页负责展示）
            {"_id": "r3", "name": "team-main", "teamMain": True},
        ], "total": 3}
        c = _client_with(rc)
        out = c.fetch_directory_channels(offset=50, count=50)
        rc.call_api_get.assert_called_once_with(
            "channels.list", count=50, offset=50)
        self.assertEqual(out["total"], 3)
        entries = out["entries"]
        self.assertEqual([e.rid for e in entries], ["r1", "r2"])
        self.assertEqual(entries[0].name, "综合")
        self.assertEqual(entries[0].users_count, 5)
        self.assertGreater(entries[0].last_ts, 0)
        self.assertGreater(entries[0].created_ts, 0)
        self.assertFalse(entries[0].is_private)
        self.assertEqual(entries[1].name, "dev")
        self.assertEqual(entries[1].last_ts, 0.0)
        self.assertEqual(entries[1].created_ts, 0.0)
        self.assertEqual(entries[1].users_count, 0)

    def test_groups_endpoint_and_private(self):
        rc = mock.Mock()
        rc.call_api_get.return_value = {"groups": [
            {"_id": "g1", "name": "secret", "usersCount": 3},
        ], "total": 1}
        c = _client_with(rc)
        out = c.fetch_directory_groups()
        rc.call_api_get.assert_called_once_with(
            "groups.list", count=50, offset=0)
        e = out["entries"][0]
        self.assertEqual((e.kind, e.rid), ("group", "g1"))
        self.assertTrue(e.is_private)
        self.assertEqual(e.room_type, "p")

    def test_users_department_and_exclude_self(self):
        rc = mock.Mock()
        rc.call_api_get.return_value = {"users": [
            {"_id": "me", "username": "self", "name": "自己"},
            {"_id": "u2", "username": "bob", "name": "Bob",
             "customFields": {"department": "研发"},
             "createdAt": "2024-05-06T00:00:00Z"},
            {"_id": "u3", "username": "cat", "name": "Cat",
             "department": "测试"},
        ], "total": 3}
        c = _client_with(rc)
        out = c.fetch_directory_users()
        rc.call_api_get.assert_called_once_with(
            "users.list", count=50, offset=0)
        entries = out["entries"]
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].username, "bob")
        self.assertEqual(entries[0].department, "研发")
        self.assertGreater(entries[0].created_ts, 0)
        self.assertEqual(entries[1].department, "测试")

    def test_search_directory_users_directory(self):
        rc = mock.Mock()
        rc.call_api_get.return_value = {"result": [
            {"_id": "me", "username": "self", "name": "自己"},
            {"_id": "u2", "username": "bob", "name": "Bob",
             "department": "研发", "createdAt": "2024-05-06T00:00:00Z"},
        ], "total": 1}
        c = _client_with(rc)
        out = c.search_directory_users("bob")
        rc.call_api_get.assert_called_once()
        args = rc.call_api_get.call_args
        self.assertEqual(args.args[0], "directory")
        self.assertEqual(args.kwargs["type"], "users")
        self.assertEqual(args.kwargs["text"], "bob")
        self.assertEqual(args.kwargs["workspace"], "local")
        self.assertEqual(out["total"], 1)
        self.assertEqual([e.username for e in out["entries"]], ["bob"])
        self.assertEqual(out["entries"][0].department, "研发")
        self.assertGreater(out["entries"][0].created_ts, 0)

    def test_search_directory_users_empty_no_request(self):
        rc = mock.Mock()
        c = _client_with(rc)
        self.assertEqual(c.search_directory_users("   "),
                         {"entries": [], "total": 0})
        rc.call_api_get.assert_not_called()

    def test_search_directory_users_error_wrapped(self):
        rc = mock.Mock()
        rc.call_api_get.side_effect = Exception("boom")
        c = _client_with(rc)
        with self.assertRaises(ac.ApiError):
            c.search_directory_users("x")

    def test_search_directory_rooms_directory(self):
        rc = mock.Mock()
        rc.call_api_get.return_value = {"result": [
            {"_id": "r1", "fname": "综合", "t": "c", "usersCount": 5,
             "ts": "2024-01-02T00:00:00Z",
             "lastMessage": {"ts": "2026-01-01T00:00:00Z"}},
            {"_id": "r2", "name": "dev", "t": "p"},  # 无 fname/ts/lm 降级
        ], "total": 2}
        c = _client_with(rc)
        out = c.search_directory_rooms("综合")
        rc.call_api_get.assert_called_once()
        args = rc.call_api_get.call_args
        self.assertEqual(args.args[0], "directory")
        self.assertEqual(args.kwargs["type"], "channels")
        self.assertEqual(args.kwargs["text"], "综合")
        entries = out["entries"]
        self.assertEqual([e.rid for e in entries], ["r1", "r2"])
        self.assertEqual(entries[0].name, "综合")
        self.assertEqual(entries[0].users_count, 5)
        self.assertFalse(entries[0].is_private)
        self.assertGreater(entries[0].created_ts, 0)
        self.assertGreater(entries[0].last_ts, 0)
        self.assertEqual(entries[1].users_count, 0)
        self.assertTrue(entries[1].is_private)
        self.assertEqual(entries[1].created_ts, 0.0)
        self.assertEqual(entries[1].last_ts, 0.0)

    def test_search_directory_teams_directory(self):
        rc = mock.Mock()
        rc.call_api_get.return_value = {"result": [
            {"_id": "room1", "teamId": "tid1", "fname": "产品研发组",
             "t": "c", "roomsCount": 0, "ts": "2026-04-28T00:00:00Z"},
        ], "total": 1}
        c = _client_with(rc)
        out = c.search_directory_teams("产品")
        args = rc.call_api_get.call_args
        self.assertEqual(args.args[0], "directory")
        self.assertEqual(args.kwargs["type"], "teams")
        self.assertEqual(args.kwargs["text"], "产品")
        self.assertEqual(args.kwargs["workspace"], "local")
        e = out["entries"][0]
        self.assertEqual((e.kind, e.rid, e.team_id, e.name),
                         ("team", "room1", "tid1", "产品研发组"))
        self.assertEqual(out["total"], 1)

    def test_teams_directory_mapping(self):
        rc = mock.Mock()
        rc.call_api_get.return_value = {"result": [
            {"_id": "room1", "teamId": "tid1", "fname": "公开团队",
             "t": "c", "teamMain": True, "usersCount": 7,
             "roomsCount": 0, "ts": "2024-01-01T00:00:00Z"},
            {"_id": "room2", "teamId": "tid2", "name": "私有团队",
             "t": "p", "teamMain": True},  # 无 roomsCount/ts 降级
        ], "total": 11}
        c = _client_with(rc)
        out = c.fetch_directory_teams(offset=50, count=50)
        args = rc.call_api_get.call_args
        self.assertEqual(args.args[0], "directory")
        self.assertEqual((args.kwargs["type"], args.kwargs["workspace"]),
                         ("teams", "local"))
        self.assertEqual((args.kwargs["text"], args.kwargs["offset"],
                          args.kwargs["count"]), ("", 50, 50))
        e0, e1 = out["entries"]
        self.assertEqual((e0.kind, e0.rid, e0.team_id, e0.name),
                         ("team", "room1", "tid1", "公开团队"))
        self.assertFalse(e0.is_private)
        self.assertEqual(e0.room_type, "c")
        self.assertEqual(e0.users_count, 7)
        self.assertEqual(e0.rooms_count, 0)
        self.assertGreater(e0.created_ts, 0)
        self.assertTrue(e1.is_private)      # t='p' 即私有团队
        self.assertEqual(e1.room_type, "p")
        self.assertEqual(e1.rooms_count, -1)
        self.assertEqual(e1.created_ts, 0.0)
        self.assertEqual(out["total"], 11)

    def test_teams_data_wrapper_fallback(self):
        rc = mock.Mock()
        rc.call_api_get.return_value = {"data": {"result": [
            {"_id": "room1", "teamId": "tid", "fname": "t", "t": "c"},
        ], "total": 1}}
        c = _client_with(rc)
        out = c.fetch_directory_teams()
        self.assertEqual(out["entries"][0].rid, "room1")

    def test_teams_missing_id_kept(self):
        rc = mock.Mock()
        rc.call_api_get.return_value = {"result": [
            {"teamId": "tid", "fname": "t"},  # _id 缺失：UI 置灰不响应
        ], "total": 1}
        c = _client_with(rc)
        out = c.fetch_directory_teams()
        self.assertEqual(out["entries"][0].rid, "")

    def test_not_logged_in_raises(self):
        c = ac.ApiClient("http://x")
        for fn, args in ((c.fetch_directory_channels, ()),
                         (c.fetch_directory_groups, ()),
                         (c.fetch_directory_users, ()),
                         (c.fetch_directory_teams, ()),
                         (c.join_channel, ("r",))):
            with self.assertRaises(ac.ApiError):
                fn(*args)
        with self.assertRaises(ac.ApiError):
            c.join_team("r")

    def test_join_channel(self):
        rc = mock.Mock()
        rc.call_api_post.return_value = {"success": True}
        c = _client_with(rc)
        self.assertTrue(c.join_channel("r1"))
        rc.call_api_post.assert_called_once_with(
            "channels.join", body={"roomId": "r1", "joinCode": ""})

    def test_join_team_fallback_chain(self):
        rc = mock.Mock()
        rc.call_api_post.side_effect = [Exception("404"), {"success": True}]
        c = _client_with(rc)
        self.assertTrue(c.join_team("rid", team_id="tid", is_public=True))
        self.assertEqual(rc.call_api_post.call_count, 2)
        first, second = rc.call_api_post.call_args_list
        self.assertEqual(first.args[0], "teams.join")
        self.assertEqual(first.kwargs["body"], {"teamId": "tid"})
        self.assertEqual(second.args[0], "channels.join")
        self.assertEqual(second.kwargs["body"],
                         {"roomId": "rid", "joinCode": ""})

    def test_join_team_private_no_request(self):
        rc = mock.Mock()
        c = _client_with(rc)
        with self.assertRaises(ac.ApiError):
            c.join_team("rid", team_id="", is_public=False)
        rc.call_api_post.assert_not_called()

    def test_fetch_room_info(self):
        rc = mock.Mock()
        rc.call_api_get.return_value = {"room": {"t": "c", "fname": "名"}}
        c = _client_with(rc)
        self.assertEqual(c.fetch_room_info("r1"), {"t": "c", "fname": "名"})
        rc.call_api_get.assert_called_once_with("rooms.info", roomId="r1")

    def test_fetch_room_info_failure_returns_empty(self):
        rc = mock.Mock()
        rc.call_api_get.side_effect = Exception("boom")
        c = _client_with(rc)
        self.assertEqual(c.fetch_room_info("r1"), {})


# --------------------------------------------------------------------- #
# 图标
# --------------------------------------------------------------------- #
class DirectoryIconsTest(unittest.TestCase):
    def test_icons_rendered(self):
        for name in ("directory", "hash", "team"):
            self.assertFalse(icon(name, 16).isNull(), name)


# --------------------------------------------------------------------- #
# 目录面板
# --------------------------------------------------------------------- #
def _entry(kind, rid="", name="n", **kw):
    return DirectoryEntry(kind=kind, rid=rid, name=name, **kw)


class DirectoryPanelTest(unittest.TestCase):
    def setUp(self):
        self._ra = mock.patch("ui.directory_panel.run_async", sync_run_async)
        self._ra.start()
        self.client = mock.Mock()
        self.client.get_avatar_url.return_value = ""
        self.panel = RoomPanels()
        self.panel.set_theme(None)
        self.panel.set_session(self.client, None)
        self.panel.directory.set_joined_provider(
            lambda e: e.rid == "joined")
        self.btn = QPushButton()

    def tearDown(self):
        self.panel.close_all()
        self.panel.deleteLater()
        self._ra.stop()

    def _open(self):
        opened = self.panel.toggle("directory", self.btn, "", "", "")
        drain_events()
        return opened

    def test_open_loads_channels_merged(self):
        self.client.fetch_directory_channels.return_value = {
            "entries": [_entry("channel", "r1", "频道A")],
            "total": 1}
        self.client.fetch_directory_groups.return_value = {
            "entries": [_entry("group", "r2", "群B")], "total": 1}
        self.assertTrue(self._open())
        page = self.panel.directory._pages[0]
        self.assertEqual(page.list.count(), 2)
        self.assertIn("共 1 条", page.status.text())

    def test_channel_row_joined_badge(self):
        self.client.fetch_directory_channels.return_value = {
            "entries": [_entry("channel", "joined", "已加入频道"),
                        _entry("channel", "fresh", "未加入频道")],
            "total": 2}
        self.client.fetch_directory_groups.return_value = {
            "entries": [], "total": 0}
        self._open()
        page = self.panel.directory._pages[0]
        badges = []
        for i in range(page.list.count()):
            wid = page.list.itemWidget(page.list.item(i))
            badges.append(any(l.text() == "已加入"
                              for l in wid.findChildren(QLabel)))
        self.assertEqual(badges, [True, False])

    def test_user_row_sub_text(self):
        self.client.fetch_directory_users.return_value = {
            "entries": [_entry("user", name="Bob", username="bob",
                               department="研发")],
            "total": 1}
        self._open()
        self.panel.directory._switch_tab(1)
        drain_events()
        page = self.panel.directory._pages[1]
        self.assertEqual(page.list.count(), 1)
        wid = page.list.itemWidget(page.list.item(0))
        texts = [l.text() for l in wid.findChildren(QLabel)]
        self.assertIn("@bob · 研发", texts)

    def test_channel_search_goes_server_side(self):
        self.client.fetch_directory_channels.return_value = {
            "entries": [], "total": 0}
        self.client.fetch_directory_groups.return_value = {
            "entries": [], "total": 0}
        self.client.search_directory_rooms.return_value = {
            "entries": [_entry("channel", "joined", "综合频道"),
                        _entry("channel", "r2", "公开新频道")],
            "total": 2}
        self._open()
        page = self.panel.directory._pages[0]
        page.search.setText("综合")
        drain_events()
        self.client.search_directory_rooms.assert_called_with("综合")
        self.assertEqual(page.list.count(), 2)
        badges = []
        for i in range(page.list.count()):
            wid = page.list.itemWidget(page.list.item(i))
            badges.append(any(l.text() == "已加入"
                              for l in wid.findChildren(QLabel)))
        self.assertEqual(badges, [True, False])
        # 搜索结果不应再触发分页请求（offset 已 >= total）
        calls = (self.client.fetch_directory_channels.call_count,
                 self.client.fetch_directory_groups.call_count)
        page._on_scroll(0)
        self.assertEqual(
            (self.client.fetch_directory_channels.call_count,
             self.client.fetch_directory_groups.call_count), calls)

    def test_user_search_goes_server_side(self):
        self.client.fetch_directory_users.return_value = {
            "entries": [], "total": 0}
        self.client.search_directory_users.return_value = {
            "entries": [_entry("user", name="张三", username="zs",
                               department="测试")],
            "total": 1}
        self._open()
        self.panel.directory._switch_tab(1)
        page = self.panel.directory._pages[1]
        page.search.setText("张三")
        drain_events()
        self.client.search_directory_users.assert_called_with("张三")
        self.assertEqual(page.list.count(), 1)
        # 搜索结果不应再触发分页请求（offset 已 >= total）
        calls_before = self.client.fetch_directory_users.call_count
        page._on_scroll(0)
        self.assertEqual(self.client.fetch_directory_users.call_count,
                         calls_before)

    def test_team_search_goes_teams_side(self):
        self.client.fetch_directory_teams.return_value = {
            "entries": [], "total": 0}
        self.client.search_directory_teams.return_value = {
            "entries": [_entry("team", "rt", "产品研发组")],
            "total": 1}
        self._open()
        self.panel.directory._switch_tab(2)
        page = self.panel.directory._pages[2]
        page.search.setText("产品")
        drain_events()
        self.client.search_directory_teams.assert_called_with("产品")
        self.assertEqual(page.entries[0].kind, "team")
        self.assertEqual(page.list.count(), 1)

    def test_user_search_cleared_returns_to_browse(self):
        self.client.fetch_directory_users.return_value = {
            "entries": [_entry("user", name="Bob", username="bob")],
            "total": 1}
        self.client.search_directory_users.return_value = {
            "entries": [], "total": 0}
        self._open()
        self.panel.directory._switch_tab(1)
        page = self.panel.directory._pages[1]
        page.search.setText("zz")
        drain_events()
        self.assertEqual(page.list.count(), 0)
        page.search.clear()
        drain_events()
        self.assertEqual(page.list.count(), 1)
        self.assertEqual(page._server_query, "")

    def test_position_centers_on_window(self):
        btn = QPushButton()
        btn.resize(560, 660)
        btn.move(10, 10)
        panel = self.panel.directory
        panel._position(btn)
        screen = QDesktopWidget().availableGeometry(panel)
        wg = btn.geometry()
        exp_x = wg.x() + (wg.width() - panel.width()) // 2
        exp_y = wg.y() + (wg.height() - panel.height()) // 2
        self.assertEqual(
            panel.x(),
            max(screen.left(), min(exp_x, screen.right() - panel.width())))
        self.assertEqual(
            panel.y(),
            max(screen.top(), min(exp_y, screen.bottom() - panel.height())))

    def test_team_page_loads(self):
        # 团队页也走分页（directory type=teams，公开团队浏览即可见）
        self.client.fetch_directory_teams.return_value = {
            "entries": [_entry("team", "rid", "团队",
                               rooms_count=-1, created_ts=0.0)],
            "total": 1}
        self._open()
        self.panel.directory._switch_tab(2)
        drain_events()
        page = self.panel.directory._pages[2]
        self.client.fetch_directory_teams.assert_called_with(offset=0,
                                                             count=50)
        self.assertEqual(page.list.count(), 1)
        wid = page.list.itemWidget(page.list.item(0))
        texts = [l.text() for l in wid.findChildren(QLabel)]
        self.assertTrue(any("—个频道" in t for t in texts), texts)
        self.assertTrue(any("创建 —" in t for t in texts), texts)

    def test_late_feed_after_close_dropped(self):
        self.client.fetch_directory_channels.return_value = {
            "entries": [], "total": 0}
        self.client.fetch_directory_groups.return_value = {
            "entries": [], "total": 0}
        self._open()
        page = self.panel.directory._pages[0]
        stale_token = page.token
        self.panel.close_all()
        page.feed([_entry("channel", "r9", "迟到数据")], 1, 0, True,
                  stale_token)
        self.assertEqual(page.list.count(), 0)

    def test_open_requested_and_dm_signals(self):
        emitted = {"open": [], "dm": []}
        self.panel.directory.open_requested.connect(emitted["open"].append)
        self.panel.directory.dm_requested.connect(emitted["dm"].append)
        self.panel.directory._on_row_activated(
            _entry("user", name="Bob", username="bob"))
        self.panel.directory._on_row_activated(_entry("channel", "r1"))
        # rid 缺失的团队条目不响应
        self.panel.directory._on_row_activated(_entry("team", ""))
        self.assertEqual(emitted["dm"], ["bob"])
        self.assertEqual(len(emitted["open"]), 1)

    def test_rooms_panels_mutual_exclusion(self):
        self.assertTrue(self._open())
        self.assertTrue(self.panel.directory.isVisible())
        btn2 = QPushButton()
        self.assertTrue(self.panel.toggle("members", btn2, "r", "c", "n"))
        self.assertFalse(self.panel.directory.isVisible())
        self.assertTrue(self.panel.members.isVisible())

    def test_error_shown(self):
        self.client.fetch_directory_channels.side_effect = ac.ApiError("403")
        self.client.fetch_directory_groups.side_effect = ac.ApiError("403")
        self._open()
        page = self.panel.directory._pages[0]
        self.assertIn("加载失败", page.status.text())


# --------------------------------------------------------------------- #
# 预览模式（MainWindow）
# --------------------------------------------------------------------- #
from ui.main_window import MainWindow  # noqa: E402


class _NoLogin(MainWindow):
    """跳过登录：_try_auto_login 里可能进阻塞的登录流程。"""

    def _try_auto_login(self):
        pass


class PreviewModeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = self._tmp.name
        self._orig = (config.CONFIG_DIR, config.CONFIG_FILE,
                      config.KEY_FILE, config.LEGACY_INI_FILE)
        config.CONFIG_DIR = os.path.join(tmp, "cfgdir")
        config.CONFIG_FILE = os.path.join(config.CONFIG_DIR, "config.yaml")
        config.KEY_FILE = os.path.join(config.CONFIG_DIR, "secret.key")
        config.LEGACY_INI_FILE = os.path.join(tmp, "config.ini")
        self.win = _NoLogin()
        self.win.resize(1000, 680)

    def tearDown(self):
        self.win.realtime and self.win.realtime.stop()
        self.win.deleteLater()
        (config.CONFIG_DIR, config.CONFIG_FILE,
         config.KEY_FILE, config.LEGACY_INI_FILE) = self._orig
        self._tmp.cleanup()

    def test_preview_mode_toggles_banner_and_input(self):
        area = self.win.chat_area
        self.assertTrue(area.input.isVisibleTo(area))
        area.set_preview_mode(True, is_team=True)
        self.assertFalse(area.input.isVisibleTo(area))
        self.assertTrue(area.preview_banner.isVisibleTo(area))
        self.assertEqual(area.join_btn.text(), "加入团队")
        area.set_preview_mode(False)
        self.assertTrue(area.input.isVisibleTo(area))
        self.assertFalse(area.preview_banner.isVisibleTo(area))

    def test_open_preview_registers_entry_and_skips_subscribe(self):
        win = self.win
        win.client = mock.Mock()
        win.client.fetch_room_info.return_value = {
            "t": "c", "fname": "名", "teamMain": False}
        win.client.get_history.return_value = []
        win.realtime = mock.Mock()
        with mock.patch("ui.main_window.run_async", sync_run_async):
            win._open_preview(_entry("channel", "RP", "综合"))
        win.realtime.subscribe.assert_not_called()
        win.client.mark_room_read.assert_not_called()
        self.assertIn("预览", win.chat_area.title.text())
        self.assertTrue(win.chat_area.preview_banner.isVisibleTo(win))
        self.assertIn("RP", win._preview_entries)

    def test_normal_open_exits_preview(self):
        win = self.win
        win.client = mock.Mock()
        win.client.get_history.return_value = []
        win.client.fetch_room_members.return_value = []
        win.realtime = mock.Mock()
        win._preview_entries["RP"] = _entry("channel", "RP")
        conv = Conversation(id="RP", room_type="c", name="频道")
        with mock.patch("ui.main_window.run_async", sync_run_async):
            win._open_conversation(conv)
        self.assertNotIn("RP", win._preview_entries)
        self.assertFalse(win.chat_area.preview_banner.isVisibleTo(win))
        win.realtime.subscribe.assert_called_once_with("RP")

    def test_join_preview_room_channel(self):
        win = self.win
        win.client = mock.Mock()
        win.client.join_channel.return_value = True
        win._active_room_id = "RP"
        win._preview_entries["RP"] = _entry("channel", "RP")
        opened = []
        with mock.patch.object(win, "_reload_and_open",
                               side_effect=opened.append):
            with mock.patch("ui.main_window.run_async", sync_run_async):
                win._join_preview_room()
        win.client.join_channel.assert_called_once_with("RP")
        self.assertEqual(opened, ["RP"])
        self.assertNotIn("RP", win._preview_entries)

    def test_join_preview_room_team_uses_join_team(self):
        win = self.win
        win.client = mock.Mock()
        win.client.join_team.return_value = True
        win._active_room_id = "RT"
        win._preview_entries["RT"] = _entry("team", "RT", team_id="tid")
        opened = []
        with mock.patch.object(win, "_reload_and_open",
                               side_effect=opened.append):
            with mock.patch("ui.main_window.run_async", sync_run_async):
                win._join_preview_room()
        win.client.join_team.assert_called_once_with(
            "RT", "tid", is_public=True)

    def test_directory_button_exists(self):
        self.assertTrue(hasattr(self.win, "directory_btn"))
        self.assertEqual(self.win.directory_btn.objectName(),
                         "DirectoryButton")


if __name__ == "__main__":
    unittest.main()
