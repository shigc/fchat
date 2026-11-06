"""主窗口装配验证：确认各模块之间的线真的接上了。

单模块的单元测试看不出"信号定义了但忘了连接"这类问题，
这里跳过阻塞的登录框，直接检查装配结果。

运行：python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QPoint  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402
from core.models import Conversation, Message, User  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.profile_popup import ProfileCard  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class _NoLogin(MainWindow):
    """跳过登录：_show_login 里是阻塞的 exec_()，测试不能进。"""

    def _try_auto_login(self):
        self.auto_login_skipped = True


class WiringTest(unittest.TestCase):

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

    # ---- 自动登录 ---- #
    def test_auto_login_toggle_persists(self):
        self.assertFalse(self.win.settings_page.auto_login_switch.isChecked())
        self.win.settings_page.auto_login_switch.setChecked(True)
        self.assertTrue(config.load_config()["auto_login"])
        self.win.settings_page.auto_login_switch.setChecked(False)
        self.assertFalse(config.load_config()["auto_login"])

    def test_auto_login_initial_value_restored_from_config(self):
        config.save_auto_login(True)
        second = _NoLogin()
        try:
            self.assertTrue(second.settings_page.auto_login_switch.isChecked())
        finally:
            second.deleteLater()

    def test_theme_change_does_not_wipe_auto_login(self):
        """回归：局部保存器曾互相覆盖配置。"""
        config.save_auto_login(True)
        config.save_appearance(theme="dark")
        self.assertTrue(config.load_config()["auto_login"])
        config.save_pinned_rooms(["r1"])
        cfg = config.load_config()
        self.assertTrue(cfg["auto_login"])
        self.assertEqual(cfg["theme"], "dark")

    # ---- 快速切换 / 图片浏览 ---- #
    def test_quick_switch_routes_conversation(self):
        conv = Conversation(id="r9", name="R9")
        with mock.patch.object(self.win, "_open_conversation") as m_open:
            self.win._on_quick_switch_open(conv)
            m_open.assert_called_once_with(conv)

    def test_quick_switch_routes_directory_entry(self):
        from core.models import DirectoryEntry
        entry = DirectoryEntry(kind="user", rid="", username="bob")
        with mock.patch.object(self.win,
                               "_on_directory_entry_open") as m_open:
            self.win._on_quick_switch_open(entry)
            m_open.assert_called_once_with(entry)

    def test_image_view_gets_all_room_images(self):
        msgs = [
            Message(id="a", room_id="r1", msg_type="image",
                    file_url="/u/a.png", timestamp=1.0),
            Message(id="b", room_id="r1", msg_type="text", text="x",
                    timestamp=2.0),
            Message(id="c", room_id="r1", msg_type="image",
                    file_url="/u/c.png", timestamp=3.0),
        ]
        self.win._room_messages["r1"] = msgs
        self.win._active_room_id = "r1"
        with mock.patch("ui.image_viewer.ImageViewer") as m_viewer:
            self.win._on_image_view_requested(msgs[2])
        kwargs = m_viewer.call_args.kwargs
        self.assertEqual(kwargs["urls"], ["/u/a.png", "/u/c.png"])
        self.assertEqual(kwargs["index"], 1)

    # ---- 加载器接线 ---- #
    def test_loaders_reach_the_delegate(self):
        view = self.win.chat_area.chat_view
        self.assertIs(view.image_loader, self.win.image_loader)
        self.assertIs(view._delegate.image_loader, self.win.image_loader)
        self.assertIs(view._delegate.avatar_loader, self.win.avatar_loader)
        self.assertIs(self.win.conversation_list.avatar_loader,
                      self.win.avatar_loader)

    # ---- 登录成功后的账号页头像 ---- #
    def test_login_success_triggers_avatar_load(self):
        """登录成功必须重新注入头像加载器：show_account 只登记 URL 不加载，
        而 _setup_ui 时的注入发生在登录前（还没有 pending URL）是空操作，
        漏掉这一步账号页头像永远停在占位图。"""
        client = mock.Mock()
        client.server_url = "http://s"
        client.me.username = "alice"
        client.me.name = ""
        client.me.avatar_url = ""
        client.me.department = ""
        # 登录流程里的实时连接/会话拉取会开网络线程，接线不在本用例验证范围；
        # run_async 也要拦（Mock client 的 fetch_department 会把 Mock 递进
        # 真实 set_department，槽内 TypeError 直接终止进程）
        with mock.patch.object(self.win, "_start_realtime"), \
                mock.patch.object(self.win, "_load_conversations"), \
                mock.patch("ui.main_window.run_async"), \
                mock.patch.object(self.win.avatar_loader, "load") as load:
            self.win._complete_login(client)
        load.assert_called_once()
        # 头像 URL 为空时按 server/avatar/<user> 兜底拼接
        self.assertEqual(load.call_args[0][1], "http://s/avatar/alice")
        self.assertIs(load.call_args[0][0], self.win.settings_page._avatar)

    def test_login_success_does_not_purge_own_avatar_cache(self):
        """登录不得 purge 自己头像缓存（强制网络重拉拖慢首屏）：
        磁盘缓存即时显示，改为后台 refresh_if_changed 检测远端变化。"""
        client = mock.Mock()
        client.server_url = "http://s"
        client.me.username = "alice"
        client.me.name = ""
        client.me.avatar_url = ""
        client.me.department = ""
        with mock.patch.object(self.win, "_start_realtime"), \
                mock.patch.object(self.win, "_load_conversations"), \
                mock.patch.object(self.win.avatar_loader, "load"), \
                mock.patch("ui.main_window.run_async"), \
                mock.patch.object(self.win.avatar_loader, "purge") as purge, \
                mock.patch.object(self.win.avatar_loader,
                                  "refresh_if_changed") as refresh:
            self.win._complete_login(client)
        purge.assert_not_called()
        refresh.assert_called_once_with("http://s/avatar/alice")

    # ---- 登录前误开会话与退出收尾 ---- #
    def test_open_conversation_before_login_is_deferred(self):
        """登录前快照渲染会自动选中第一条会话：不能拉历史（client 为
        None），只能挂起，等登录成功后补开。"""
        self.win.client = None
        self.win.realtime = None
        conv = Conversation(id="R1", room_type="c", name="g")
        with mock.patch.object(self.win, "_load_history") as hist:
            self.win._open_conversation(conv)
        hist.assert_not_called()
        self.assertIs(self.win._pending_open_conv, conv)
        # 补开：client 就位后正常打开并清掉挂起
        # （login_success 里其余步骤与本用例无关，patch 掉免开线程）
        self.win.client = mock.Mock()
        self.win.client.server_url = "http://s"
        self.win.client.user_id = "uid"
        self.win.client.auth_token = "tok"
        self.win.client.me.username = "alice"
        self.win.client.me.name = ""
        self.win.client.me.avatar_url = ""
        self.win.client.me.department = ""
        with mock.patch.object(self.win, "_load_history") as hist, \
                mock.patch.object(self.win, "_start_realtime"), \
                mock.patch.object(self.win, "_load_conversations"), \
                mock.patch("ui.main_window.run_async"):
            self.win._complete_login(self.win.client)
        hist.assert_called_once_with(conv)
        self.assertIsNone(self.win._pending_open_conv)

    def test_close_event_waits_for_pending_async_tasks(self):
        """退出时在途 run_async 任务必须等收尾：否则主窗口销毁会带着
        还在运行的 QThread 一起走（QThread: Destroyed while running）。"""
        from ui.api_workers import _AsyncTask
        gate = __import__("threading").Event()
        release = []

        def block():
            gate.wait(5.0)      # 模拟在途网络调用
            release.append(1)
            return None

        task = _AsyncTask(block, None, None, parent=self.win)
        self.assertIn(task, self.win.findChildren(_AsyncTask))
        gate.set()              # 放行，线程很快结束
        # closeEvent 内部会 wait_finished；这里直接验证等待路径可跑通
        self.win.closeEvent(mock.Mock())
        self.assertEqual(release, [1])

    def test_close_event_confirms_unsent_draft(self):
        """真退出时输入框有未发送文字要确认；取消则不关窗。"""
        self.win.chat_area.input.input.setPlainText("还没发出去的话")
        ev = mock.Mock()
        with mock.patch("ui.main_window.confirm", return_value=False):
            self.win.closeEvent(ev)
        ev.ignore.assert_called_once()
        self.assertFalse(self.win._closing)
        # 确认后照常退出
        with mock.patch("ui.main_window.confirm", return_value=True), \
                mock.patch.object(self.win, "_save_current_draft"), \
                mock.patch.object(self.win, "_room_panels"):
            self.win.closeEvent(ev)
        ev.accept.assert_called_once()

    def test_close_event_no_confirm_when_draft_empty(self):
        self.win.chat_area.input.input.setPlainText("")
        ev = mock.Mock()
        with mock.patch("ui.main_window.confirm") as cf, \
                mock.patch.object(self.win, "_save_current_draft"), \
                mock.patch.object(self.win, "_room_panels"):
            self.win.closeEvent(ev)
        cf.assert_not_called()
        ev.accept.assert_called_once()

    def test_close_event_cancel_draft_confirmation_resets_force_quit(self):
        """托盘「关闭程序」置位 _force_quit 后，草稿确认取消必须重置：
        否则之后从右上角点 X 会跳过「缩小到托盘」直接退出。"""
        self.win._force_quit = True
        self.win.chat_area.input.input.setPlainText("还没发出去的话")
        ev = mock.Mock()
        with mock.patch("ui.main_window.confirm", return_value=False):
            self.win.closeEvent(ev)
        ev.ignore.assert_called_once()
        # 用户明确取消退出 → 恢复「点 X 收托盘」行为
        self.assertFalse(self.win._force_quit)

    # ---- 聊天背景（预设 / 自定义图片） ---- #
    def test_wallpaper_changed_saves_and_applies(self):
        """设置壁纸：落盘配置 + 聊天视图拿到背景 pixmap；恢复默认清掉。"""
        from PyQt5.QtGui import QPixmap
        img = os.path.join(self._tmp.name, "wp.png")
        self.assertTrue(QPixmap(8, 6).save(img))
        self.win._on_wallpaper_changed(img)
        self.assertEqual(config.load_config()["chat_wallpaper"], img)
        view = self.win.chat_area.chat_view
        self.assertEqual(view.background_value, img)
        self.assertIsNotNone(view._bg_pixmap)
        self.win._on_wallpaper_changed("")
        self.assertEqual(config.load_config()["chat_wallpaper"], "")
        self.assertEqual(view.background_value, "")
        self.assertIsNone(view._bg_pixmap)

    def test_wallpaper_invalid_path_falls_back(self):
        """路径不存在/坏图/未知预设静默回落默认底色，不崩。"""
        view = self.win.chat_area.chat_view
        self.win._apply_chat_wallpaper(os.path.join(self._tmp.name, "no.png"))
        self.assertIsNone(view._bg_pixmap)
        bad = os.path.join(self._tmp.name, "bad.png")
        with open(bad, "wb") as f:
            f.write(b"not an image")
        self.win._apply_chat_wallpaper(bad)
        self.assertIsNone(view._bg_pixmap)
        self.win._apply_chat_wallpaper("preset:does_not_exist")
        self.assertIsNone(view._bg_pixmap)

    def test_wallpaper_preset_applies_and_selects_swatch(self):
        """点击预设色板：信号链路把配置、聊天视图、色板选中态一起更新。"""
        self.win.settings_page._on_preset_wallpaper("preset:grad_dawn")
        self.assertEqual(config.load_config()["chat_wallpaper"],
                         "preset:grad_dawn")
        view = self.win.chat_area.chat_view
        self.assertEqual(view.background_value, "preset:grad_dawn")
        self.assertIsNotNone(view._bg_pixmap)
        checked = [s.value for s in
                   self.win.settings_page._wallpaper_swatches if s.isChecked()]
        self.assertEqual(checked, ["preset:grad_dawn"])
        self.assertFalse(
            self.win.settings_page.wallpaper_preview.pixmap().isNull())

    def test_wallpaper_restored_on_theme_load(self):
        """启动/主题加载时从配置回读壁纸并回显到设置页。"""
        from PyQt5.QtGui import QPixmap
        img = os.path.join(self._tmp.name, "wp2.png")
        self.assertTrue(QPixmap(8, 6).save(img))
        config.save_appearance(chat_wallpaper=img)
        self.win._load_and_apply_theme()
        view = self.win.chat_area.chat_view
        self.assertEqual(view.background_value, img)
        self.assertIsNotNone(view._bg_pixmap)
        # 设置页缩略回显有 pixmap（不再是空图「默认」占位）
        self.assertFalse(
            self.win.settings_page.wallpaper_preview.pixmap().isNull())

    def test_close_event_detaches_unfinished_tasks(self):
        """放弃等待后未结束的任务必须解绑出主窗口：否则窗口销毁连带销毁
        还在跑的 QThread（QThread: Destroyed while running）。解绑后
        线程要能自行结束（DirectConnection quit，不依赖主事件循环）。"""
        from ui import api_workers
        gate = __import__("threading").Event()

        def block():
            gate.wait(5.0)
            return None

        task = api_workers._AsyncTask(block, None, None, parent=self.win)
        thread = task._thread
        self.assertTrue(thread.isRunning())
        with mock.patch("ui.main_window.CLOSE_DRAIN_TIMEOUT", 0.0):
            self.win.closeEvent(mock.Mock())   # 立即超时 → 走解绑路径
        self.assertIsNone(task.parent())
        self.assertIn(task, api_workers._detached_tasks)
        self.assertNotIn(task, self.win.findChildren(api_workers._AsyncTask))
        # 放行后无需事件循环，线程也应能结束（DirectConnection quit 生效）
        gate.set()
        self.assertTrue(thread.wait(2000))
        self.assertTrue(thread.isFinished())
        api_workers._detached_tasks.remove(task)

    def test_drain_detached_waits_and_cleans(self):
        """exec_ 退出后的兜底：解绑线程自然结束的条目清出保活表，未结束
        的条目保留（入口随即 os._exit，不触发析构）。走真实 detach 路径
        （quit 已发出）后线程才能结束。"""
        from ui import api_workers
        from PyQt5.QtCore import QObject
        gate = __import__("threading").Event()
        holder = QObject()

        def block():
            gate.wait(5.0)
            return None

        task = api_workers._AsyncTask(block, None, None, parent=holder)
        api_workers.detach_unfinished_tasks(holder)
        self.assertIn(task, api_workers._detached_tasks)
        try:
            gate.set()
            self.assertEqual(api_workers.drain_detached(2000), 0)
            self.assertEqual(api_workers._detached_tasks, [])
        finally:
            gate.set()
            task._thread.wait(2000)

    def test_drain_detached_keeps_unfinished(self):
        from ui import api_workers
        from PyQt5.QtCore import QObject
        gate = __import__("threading").Event()
        holder = QObject()

        def block():
            gate.wait(5.0)
            return None

        task = api_workers._AsyncTask(block, None, None, parent=holder)
        api_workers.detach_unfinished_tasks(holder)
        try:
            self.assertEqual(api_workers.drain_detached(0), 1)
            self.assertIn(task, api_workers._detached_tasks)
        finally:
            gate.set()
            task._thread.wait(2000)
            api_workers._detached_tasks.remove(task)

    def test_login_runner_detach_unbinds_and_quits_thread(self):
        from ui.login_dialog import LoginRunner
        runner = LoginRunner(self.win)
        runner._thread = mock.Mock()
        runner.detach()
        self.assertIsNone(runner.parent())
        runner._thread.quit.assert_called_once_with()

    # ---- 打开会话上报已读 ---- #
    def _open_conv(self, unread):
        """patch 掉 run_async 同步执行：返回 (是否发起了已读上报, 收到的 rid)。"""
        fns = []
        win = self.win
        win.client = mock.Mock()
        conv = Conversation(id="R1", room_type="c", unread=unread)
        conv.name = "g"
        with mock.patch("ui.main_window.run_async",
                        side_effect=lambda fn, **kw: fns.append(fn)):
            win._open_conversation(conv)
            for fn in fns:
                fn()
        return win.client

    def test_open_unread_conversation_marks_read_on_server(self):
        """打开带未读的会话必须向服务端上报已读：只清本地角标的话，
        重启后角标会按服务端未读数原样回来。"""
        client = self._open_conv(unread=2)
        client.mark_room_read.assert_called_once_with("R1")

    def test_open_read_conversation_skips_read_report(self):
        client = self._open_conv(unread=0)
        client.mark_room_read.assert_not_called()

    # ---- 右键标为未读 ---- #
    def test_mark_unread_sets_local_badge_and_calls_api(self):
        """标为未读：本地红点至少置 1，并向服务端 subscriptions.unread 同步。"""
        win = self.win
        win.client = mock.Mock()
        conv = Conversation(id="R1", room_type="c", unread=0)
        conv.name = "g"
        win._conversations = {"R1": conv}
        fns = []
        with mock.patch("ui.main_window.run_async",
                        side_effect=lambda fn, **kw: fns.append(fn)), \
                mock.patch.object(win.conversation_list, "upsert_conversation"), \
                mock.patch.object(win, "_refresh_tray_state"):
            win._on_mark_unread("R1")
            for fn in fns:
                fn()
        self.assertGreaterEqual(conv.unread, 1)
        win.client.mark_room_unread.assert_called_once_with("R1")

    # ---- 1 对 1 私聊不拉成员列表 ---- #
    def _open_room(self, conv):
        """打开会话但隔离历史/回执/成员等外围异步，只观察成员拉取。"""
        win = self.win
        win.client = mock.Mock()
        with mock.patch("ui.main_window.run_async"), \
                mock.patch.object(win, "_apply_receipts_state_to_view"), \
                mock.patch.object(win, "_schedule_receipts_fetch"), \
                mock.patch.object(win, "_load_history"), \
                mock.patch.object(win, "_ensure_room_members") as members:
            win._open_conversation(conv)
        return members

    def test_dm_one_to_one_skips_member_fetch(self):
        """1 对 1 私聊成员恒为对方一人：不拉 im.members（启动连开私聊
        时它曾 20s 读超时），@ 候选直接用已知的对方信息注入。"""
        conv = Conversation(id="D1", room_type="d")
        conv.username = "lisi907"
        conv.name = "李四"
        members = self._open_room(conv)
        members.assert_not_called()
        self.assertEqual(self.win.chat_area.input._mention_candidates,
                         [{"username": "lisi907", "name": "李四"}])

    def test_multi_user_dm_still_fetches_members(self):
        """多人私聊（username 带逗号）仍要靠成员数 >1 切换未读数样式。"""
        conv = Conversation(id="D2", room_type="d")
        conv.username = "lisi907,wangwu"
        conv.name = "私聊群"
        members = self._open_room(conv)
        members.assert_called_once_with("D2", "d")

    def test_channel_still_fetches_members(self):
        conv = Conversation(id="C1", room_type="c")
        conv.name = "general"
        members = self._open_room(conv)
        members.assert_called_once_with("C1", "c")

    def test_open_registers_snapshot_conversation(self):
        """快照渲染早于会话列表时，打开会话要把它登记进 _conversations，
        否则历史/实时回调找不到会话，左侧预览同步被跳过。"""
        conv = Conversation(id="C2", room_type="c")
        conv.name = "general"
        self._open_room(conv)
        self.assertIs(self.win._conversations.get("C2"), conv)

    # ---- 消息历史磁盘快照 ---- #
    def _client_for_cache(self):
        win = self.win
        win.client = mock.Mock()
        win.client.server_url = "https://s.example.com"
        win.client.me.username = "alice"
        return win.client

    def _isolate_msg_cache(self):
        """快照读写隔离到临时目录：storage_root 按进程懒加载缓存，套件
        里跑到这里时可能已把根目录烙成真实 %APPDATA%，不隔离会跨运行
        残留快照（R2 残留曾让 without_snapshot 用例随机翻车）。"""
        p = mock.patch("core.msg_cache.cache_root",
                       lambda: os.path.join(self._tmp.name, "cache"))
        p.start()
        self.addCleanup(p.stop)

    def test_first_open_loads_snapshot_and_replaces_on_network(self):
        """首次打开房间：磁盘快照先入库显示（_snapshot_shown 标记），
        网络首屏回来后整体 rebuild 替换（prepend 去重会把快照里的
        编辑/撤回旧内容留在屏上），并把新首屏尾部写回快照。"""
        from core import msg_cache
        win = self.win
        self._isolate_msg_cache()
        client = self._client_for_cache()
        rid = "R1"
        msg_cache.save_messages_snapshot(
            client.server_url, "alice", rid,
            [Message(id="m1", room_id=rid, text="old", timestamp=100.0)])
        conv = Conversation(id=rid, room_type="c")
        conv.name = "g"

        captured = {}

        def _fake_run_async(fn, on_done=None, on_fail=None, parent=None):
            captured["on_done"] = on_done

        view = win.chat_area.chat_view
        with mock.patch("ui.main_window.run_async",
                        side_effect=_fake_run_async), \
                mock.patch.object(view, "rebuild_messages") as rb, \
                mock.patch.object(view, "prepend_messages") as pp, \
                mock.patch.object(win, "_enrich_quotes"), \
                mock.patch.object(win, "_resolve_system_names"), \
                mock.patch.object(win, "_apply_receipts_state_to_view"), \
                mock.patch.object(win, "_schedule_receipts_fetch"), \
                mock.patch.object(win, "_ensure_room_members"), \
                mock.patch.object(win, "_push_mention_candidates"), \
                mock.patch.object(win, "_mark_room_read"):
            win._active_room_id = rid
            win._load_history(conv)
            self.assertEqual([m.id for m in win._room_messages[rid]], ["m1"])
            self.assertIn(rid, win._snapshot_shown)
            # m1 已被编辑（同 id 新内容）+ 新消息 m2
            net = [Message(id="m2", room_id=rid, text="new2", timestamp=200.0),
                   Message(id="m1", room_id=rid, text="edited", timestamp=100.0)]
            captured["on_done"](net)
        self.assertEqual(win._history_offset[rid], 2)
        # _room_messages 统一时间正序（旧->新）
        self.assertEqual([m.id for m in win._room_messages[rid]],
                         ["m1", "m2"])
        rb.assert_called_once_with(win._room_messages[rid])
        pp.assert_not_called()
        self.assertNotIn(rid, win._snapshot_shown)
        tail = msg_cache.load_messages_snapshot(client.server_url,
                                                "alice", rid)
        self.assertEqual([m.id for m in tail], ["m1", "m2"])

    def test_first_open_without_snapshot_uses_prepend(self):
        """没有快照的房间走原 prepend 路径，不触发 rebuild。"""
        win = self.win
        self._isolate_msg_cache()
        self._client_for_cache()
        rid = "R2"
        conv = Conversation(id=rid, room_type="c")
        conv.name = "g"

        captured = {}

        def _fake_run_async(fn, on_done=None, on_fail=None, parent=None):
            captured["on_done"] = on_done

        view = win.chat_area.chat_view
        with mock.patch("ui.main_window.run_async",
                        side_effect=_fake_run_async), \
                mock.patch.object(view, "rebuild_messages") as rb, \
                mock.patch.object(view, "prepend_messages") as pp, \
                mock.patch.object(win, "_enrich_quotes"), \
                mock.patch.object(win, "_resolve_system_names"), \
                mock.patch.object(win, "_apply_receipts_state_to_view"), \
                mock.patch.object(win, "_schedule_receipts_fetch"):
            win._active_room_id = rid
            win._load_history(conv)
            self.assertNotIn(rid, win._snapshot_shown)
            captured["on_done"](
                [Message(id="n1", room_id=rid, text="t", timestamp=1.0)])
        pp.assert_called_once()
        rb.assert_not_called()

    def test_history_done_syncs_left_preview_to_actual_tail(self):
        """服务端房间摘要 lm 可能滞后（真机 2026-10-19 实测停在 16:42:51，
        历史里已有 16:45 的消息）：首屏历史落地后必须用真实最后一条修正
        左侧预览，否则左侧与聊天区会长期不一致。"""
        win = self.win
        self._isolate_msg_cache()
        self._client_for_cache()
        rid = "R4"
        conv = Conversation(id=rid, room_type="c",
                            last_message_text="旧预览",
                            last_message_time=100.0)
        conv.name = "g"
        win._conversations[rid] = conv

        captured = {}

        def _fake_run_async(fn, on_done=None, on_fail=None, parent=None):
            captured["on_done"] = on_done

        view = win.chat_area.chat_view
        with mock.patch("ui.main_window.run_async",
                        side_effect=_fake_run_async), \
                mock.patch.object(view, "prepend_messages"), \
                mock.patch.object(win, "_enrich_quotes"), \
                mock.patch.object(win, "_resolve_system_names"), \
                mock.patch.object(win, "_apply_receipts_state_to_view"), \
                mock.patch.object(win, "_schedule_receipts_fetch"), \
                mock.patch.object(win.conversation_list,
                                  "upsert_conversation") as upsert:
            win._active_room_id = rid
            win._load_history(conv)
            captured["on_done"](
                [Message(id="n1", room_id=rid, text="新消息", timestamp=200.0)])
        self.assertEqual(conv.last_message_time, 200.0)
        self.assertEqual(conv.last_message_text, "新消息")
        upsert.assert_called_once_with(conv)

    def test_conversation_reload_keeps_client_known_tail(self):
        """服务端会话摘要滞后时，整表重拉不能把左侧预览顶回旧值：
        客户端已见的最新尾部（历史/实时）要在 _done 里盖回去。"""
        win = self.win
        win.client = mock.Mock()
        win.client.server_url = "https://s.example.com"
        win.client.me.username = "alice"
        stale = Conversation(id="R5", room_type="c",
                             last_message_text="旧预览",
                             last_message_time=100.0)
        stale.name = "g"
        win._note_latest_message(
            Message(id="m", room_id="R5", text="新消息", timestamp=200.0))
        captured = {}

        def _fake_run_async(fn, on_done=None, on_fail=None, parent=None):
            captured["on_done"] = on_done

        with mock.patch("ui.main_window.run_async",
                        side_effect=_fake_run_async), \
                mock.patch("core.conv_cache.load_conversations_snapshot",
                           return_value=None), \
                mock.patch("core.conv_cache.save_conversations_snapshot"), \
                mock.patch.object(win, "_resolve_display_names"), \
                mock.patch.object(win, "_queue_conversation_statuses"), \
                mock.patch.object(win, "_prefetch_histories"):
            win._load_conversations()
            captured["on_done"]([stale])
        self.assertEqual(stale.last_message_time, 200.0)
        self.assertEqual(stale.last_message_text, "新消息")

    def test_network_done_keeps_realtime_tail_extras(self):
        """快照渲染到网络返回之间经实时推送进来的尾部消息不能被整体
        替换闪掉：不在网络首屏内且时间不早于首屏最新一条的要保留。"""
        win = self.win
        self._isolate_msg_cache()
        self._client_for_cache()
        rid = "R3"
        conv = Conversation(id=rid, room_type="c")
        conv.name = "g"

        captured = {}

        def _fake_run_async(fn, on_done=None, on_fail=None, parent=None):
            captured["on_done"] = on_done

        view = win.chat_area.chat_view
        with mock.patch("ui.main_window.run_async",
                        side_effect=_fake_run_async), \
                mock.patch.object(view, "rebuild_messages"), \
                mock.patch.object(view, "prepend_messages"), \
                mock.patch.object(win, "_enrich_quotes"), \
                mock.patch.object(win, "_resolve_system_names"), \
                mock.patch.object(win, "_apply_receipts_state_to_view"), \
                mock.patch.object(win, "_schedule_receipts_fetch"):
            win._active_room_id = rid
            win._load_history(conv)
            # 网络在途时实时推来两条：一条晚于首屏最新（保留），
            # 一条是旧消息（网络首屏会带回来，去重）
            win._room_messages[rid] = [
                Message(id="o1", room_id=rid, text="old", timestamp=10.0),
                Message(id="rt", room_id=rid, text="live", timestamp=500.0)]
            captured["on_done"](
                [Message(id="o1", room_id=rid, text="old", timestamp=10.0)])
        self.assertEqual([m.id for m in win._room_messages[rid]],
                         ["o1", "rt"])

    # ---- 首屏历史预取 ---- #
    def test_prefetch_histories_skips_guarded_rooms(self):
        """预取只对符合条件的房间发 _load_history：加密房间跳过（E2E
        密钥未就绪时占位密文会写进快照）、已有缓存/在途/已加载/当前
        房间跳过，且受 limit 限制。"""
        win = self.win
        self._isolate_msg_cache()
        self._client_for_cache()
        convs = [Conversation(id=f"P{i}", room_type="c") for i in range(12)]
        convs[0].encrypted = True
        win._room_messages["P2"] = []
        win._history_loading.add("P3")
        win._history_offset["P4"] = 50
        win._active_room_id = "P5"
        with mock.patch.object(win, "_load_history") as lh:
            win._prefetch_histories(convs, limit=5)
        called = [c.args[0].id for c in lh.call_args_list]
        self.assertEqual(called, ["P1", "P6", "P7", "P8", "P9"])

    def test_prefetch_done_renders_after_room_open_bumps_gen(self):
        """预取在途时用户点开同一房间（_open_conversation 递增加载代数）：
        网络首屏回来仍要渲染进视图。渲染判定只看是否当前房间，不看
        代数——否则点开的房间要白屏等下一次操作。"""
        win = self.win
        self._isolate_msg_cache()
        self._client_for_cache()
        rid = "R1"
        conv = Conversation(id=rid, room_type="c")
        conv.name = "g"

        captured = {}

        def _fake_run_async(fn, on_done=None, on_fail=None, parent=None):
            captured["on_done"] = on_done

        view = win.chat_area.chat_view
        with mock.patch("ui.main_window.run_async",
                        side_effect=_fake_run_async), \
                mock.patch.object(view, "prepend_messages") as pp, \
                mock.patch.object(view, "rebuild_messages"), \
                mock.patch.object(win, "_enrich_quotes"), \
                mock.patch.object(win, "_resolve_system_names"), \
                mock.patch.object(win, "_apply_receipts_state_to_view"), \
                mock.patch.object(win, "_schedule_receipts_fetch"):
            win._load_history(conv)          # 预取发起，房间尚未打开
            win._load_gen[rid] = win._load_gen.get(rid, 0) + 1  # 用户点开
            win._active_room_id = rid
            captured["on_done"](
                [Message(id="n1", room_id=rid, text="t", timestamp=1.0)])
        pp.assert_called_once()

    def test_prefetch_done_skips_render_when_room_not_open(self):
        """预取完成时房间未打开：不渲染视图，但缓存与快照照常落地。"""
        win = self.win
        self._isolate_msg_cache()
        client = self._client_for_cache()
        rid = "R1"
        conv = Conversation(id=rid, room_type="c")
        conv.name = "g"

        captured = {}

        def _fake_run_async(fn, on_done=None, on_fail=None, parent=None):
            captured["on_done"] = on_done

        view = win.chat_area.chat_view
        with mock.patch("ui.main_window.run_async",
                        side_effect=_fake_run_async), \
                mock.patch.object(view, "prepend_messages") as pp, \
                mock.patch.object(view, "rebuild_messages") as rb, \
                mock.patch.object(win, "_enrich_quotes"), \
                mock.patch.object(win, "_resolve_system_names"), \
                mock.patch.object(win, "_apply_receipts_state_to_view"), \
                mock.patch.object(win, "_schedule_receipts_fetch"):
            win._load_history(conv)
            captured["on_done"](
                [Message(id="n1", room_id=rid, text="t", timestamp=1.0)])
        pp.assert_not_called()
        rb.assert_not_called()
        self.assertEqual(win._history_offset[rid], 1)
        self.assertEqual([m.id for m in win._room_messages[rid]], ["n1"])
        from core import msg_cache
        tail = msg_cache.load_messages_snapshot(client.server_url,
                                                "alice", rid)
        self.assertEqual([m.id for m in tail], ["n1"])

    def test_image_ready_signal_reaches_delegate(self):
        """image_ready 必须连到委托，否则图片到货不会重绘。"""
        view = self.win.chat_area.chat_view
        msg = Message(id="img1", room_id="r", msg_type="image",
                      file_url="/file-upload/a.png", text="",
                      sender=User(id="u", username="a"))
        view.set_room("r")
        view.model.prepend_messages_immediate([msg])
        self.win.image_loader.server_url = "https://s.example.com"
        url = self.win.image_loader.resolve(msg.file_url)
        # 走真实绘制路径登记等待
        from PyQt5.QtGui import QColor, QPainter, QPixmap
        from PyQt5.QtCore import QRect
        pm = QPixmap(600, 200)
        pm.fill(QColor("white"))
        painter = QPainter(pm)
        view._delegate._paint_bubble_content(
            painter, QRect(10, 10, 400, 200), msg)
        painter.end()
        self.assertIn(url, view._delegate._img_pending)

        seen = []
        view.model.dataChanged.connect(
            lambda tl, br, roles=None: seen.append(tl.row()))
        self.win.image_loader.image_ready.emit(url)
        self.assertEqual(seen, [0])

    # ---- 收藏与排序 ---- #
    def test_favorite_persists_and_reorders(self):
        convs = [
            Conversation(id="a", name="A", room_type="c",
                         last_message_time=300.0),
            Conversation(id="b", name="B", room_type="c",
                         last_message_time=100.0),
        ]
        self.win._conversations = {c.id: c for c in convs}
        self.win.conversation_list.set_conversations(convs)
        self.win.client = mock.Mock()
        order = lambda: self.win.conversation_list._row_ids()
        self.assertEqual(order(), ["a", "b"])

        # 乐观切换收藏状态并重新排序，不写配置（收藏由服务器同步）
        with mock.patch("ui.main_window.run_async",
                        side_effect=lambda fn, on_done, on_fail, parent=None:
                            on_done(fn())):
            self.win._toggle_favorite_room("b")
        self.assertEqual(order(), ["b", "a"])
        self.assertTrue(self.win._conversations["b"].is_favorite)
        self.win.client.set_room_favorite.assert_called_once_with("b", True)
        # 收藏由服务器同步，不得再写进本地配置
        self.assertEqual(config.load_config()["pinned_rooms"], [])

        # 再点一次取消收藏，落回时间序
        with mock.patch("ui.main_window.run_async",
                        side_effect=lambda fn, on_done, on_fail, parent=None:
                            on_done(fn())):
            self.win._toggle_favorite_room("b")
        self.assertEqual(order(), ["a", "b"])
        self.assertFalse(self.win._conversations["b"].is_favorite)
        self.win.client.set_room_favorite.assert_called_with("b", False)

    def test_no_drag_drop_sorting(self):
        """拖拽排序已废弃，不能再把顺序写进配置。"""
        self.assertFalse(hasattr(self.win.conversation_list, "get_order"))
        self.assertFalse(hasattr(self.win.conversation_list, "order_changed"))
        self.assertFalse(hasattr(config, "save_conversation_order"))

    # ---- 头像弹卡 ---- #
    def test_profile_signal_opens_card(self):
        """Qt.Popup 的 show() 会抓鼠标，无事件循环时会阻塞，故只桩掉 show。"""
        view = self.win.chat_area.chat_view
        msg = Message(id="m1", room_id="r", text="hi",
                      sender=User(id="u9", username="zs", name="张三"))
        created = []
        original = ProfileCard.show
        ProfileCard.show = lambda self, *a: created.append(self)
        try:
            view.profile_requested.emit(msg, QPoint(120, 120))
        finally:
            ProfileCard.show = original
        self.assertEqual(len(created), 1, "点头像没有弹出资料卡")
        card = created[0]
        self.assertEqual(card.name_label.text(), "张三")
        self.assertEqual(card._username, "zs")
        card.deleteLater()

    # ---- 视图基础设置 ---- #
    def test_scrollbar_policies(self):
        cl = self.win.conversation_list
        from PyQt5.QtCore import Qt
        self.assertEqual(cl.horizontalScrollBarPolicy(), Qt.ScrollBarAlwaysOff)
        self.assertEqual(cl.verticalScrollBarPolicy(), Qt.ScrollBarAsNeeded)
        view = self.win.chat_area.chat_view
        self.assertEqual(view.verticalScrollBarPolicy(), Qt.ScrollBarAsNeeded)
        self.assertEqual(view.verticalScrollMode(),
                         type(view).ScrollPerPixel)

    # ---- 字体设置接线 ---- #
    def test_chat_view_set_fonts_updates_model(self):
        """气泡由 QPainter 绘制不走 QSS，字体改动必须落到模型 QFont 并整表重排。"""
        from PyQt5.QtGui import QFont
        view = self.win.chat_area.chat_view
        model = view.model
        before_family = model._font.family()
        view.set_fonts("Courier New", 18)
        self.assertEqual(model._font.family(), "Courier New")
        self.assertEqual(model._font.pointSize(), 18)
        self.assertEqual(model._small_font.family(), "Courier New")
        self.assertEqual(model._tiny_font.family(), "Courier New")
        # layoutChanged 触发后行高缓存被清空
        self.assertEqual(model._height_cache, {})
        # 验证小/微字体从相对字号派生
        self.assertEqual(model._small_font.pointSize(), 17)
        self.assertEqual(model._tiny_font.pointSize(), 16)
        self.assertNotEqual(model._font.family(), before_family)

    def test_font_family_changed_reaches_chat_view(self):
        view = self.win.chat_area.chat_view
        self.win._on_font_size_changed(16)
        self.assertEqual(view.model._font.pointSize(), 16)
        self.assertEqual(self.win.theme_manager.font_size, 16)
        self.win._on_font_family_changed("Consolas")
        self.assertEqual(view.model._font.family(), "Consolas")
        self.assertEqual(self.win.theme_manager.font_family, "Consolas")

    # ---- 会话列表顶部 header（搜索 + 新建）---- #
    def test_header_search_filters_conversations(self):
        convs = [
            Conversation(id="c1", name="产品组", room_type="c",
                         last_message_time=100.0),
            Conversation(id="d1", name="alice", room_type="d",
                         username="alice", last_message_time=200.0),
        ]
        self.win._conversations = {c.id: c for c in convs}
        self.win.conversation_list.set_conversations(convs)
        self.win.search_edit.setText("alice")
        self.assertEqual(self.win.conversation_list._row_ids(), ["d1"])
        self.win.search_edit.setText("")
        self.assertEqual(self.win.conversation_list.count(), 2)

    def test_header_search_matches_message_content(self):
        """顶部搜索也能按聊天内容命中：输入最近消息文本即可筛出该会话。"""
        convs = [
            Conversation(id="c1", name="产品组", room_type="c",
                         last_message_time=100.0,
                         last_message_text="今天发布了 3.0 版本"),
            Conversation(id="d1", name="alice", room_type="d",
                         username="alice", last_message_time=200.0,
                         last_message_text="好的收到"),
        ]
        self.win._conversations = {c.id: c for c in convs}
        self.win.conversation_list.set_conversations(convs)
        self.win.search_edit.setText("3.0")
        self.assertEqual(self.win.conversation_list._row_ids(), ["c1"])
        self.win.search_edit.setText("")
        # 恢复后全部显示
        self.assertEqual(self.win.conversation_list.count(), 2)

    def test_header_search_matches_loaded_message_history(self):
        """顶部搜索能命中**较早**的聊天记录（非最新一条）：
        内容取自 _room_messages 全文索引，经 _on_search_changed 传给列表过滤。
        """
        convs = [
            Conversation(id="c1", name="产品组", room_type="c",
                         last_message_time=100.0,
                         last_message_text="好的收到"),
            Conversation(id="d1", name="王翔", room_type="d",
                         username="wangxiang", last_message_time=200.0,
                         last_message_text="已处理"),
        ]
        self.win._conversations = {c.id: c for c in convs}
        self.win.conversation_list.set_conversations(convs)
        # 王翔会话的历史里有一条含 "maintainer" 的较早消息（非最新一条）
        u = User(id="u1", username="wangxiang")
        earlier = Message(id="m1", room_id="d1",
                          text="user is maintainer now", sender=u)
        latest = Message(id="m2", room_id="d1", text="已处理", sender=u)
        self.win._room_messages["d1"] = [earlier, latest]
        # 走真实过滤路径：textChanged -> _on_search_changed -> set_search_filter
        self.win.search_edit.setText("maintainer")
        self.assertEqual(self.win.conversation_list._row_ids(), ["d1"])
        self.win.search_edit.setText("")
        self.assertEqual(self.win.conversation_list.count(), 2)

    def test_header_has_folder_new_button(self):
        self.assertIsNotNone(self.win.new_room_btn)
        self.assertFalse(self.win.new_room_btn.icon().isNull())
        self.assertTrue(self.win.new_room_btn.toolTip())
        # header 容器命名参与 QSS 主题换色
        self.assertEqual(self.win.conversation_header.objectName(),
                         "ConversationHeader")
        self.assertEqual(self.win.search_edit.objectName(), "HeaderSearch")

    def test_search_enter_searches_messages_in_active_room(self):
        """回车触发消息级搜索：命中当前视图内消息则 flash_row 高亮。"""
        conv = Conversation(id="r1", name="研发", room_type="c")
        self.win._conversations = {"r1": conv}
        self.win.conversation_list.set_conversations([conv])
        # 自动选中在登录前会被挂起（见 test_open_conversation_before_login_is_deferred），
        # 这里直接设定当前会话
        self.win._active_room_id = "r1"
        self.win._room_type = "c"
        m = Message(id="m1", room_id="r1", text="缺陷 42", sender=User(
            id="u1", username="a"))
        view = self.win.chat_area.chat_view
        view.rebuild_messages([m])
        self.win.search_edit.setText("缺陷")
        client = mock.Mock()
        client.search_messages.return_value = [m]
        self.win.client = client
        # run_async 走真实线程+队列信号，测试里没有事件循环，改为同步直调回调
        with mock.patch("ui.main_window.run_async",
                        side_effect=lambda fn, on_done, on_fail, parent=None:
                            on_done(fn())):
            self.win.search_edit.returnPressed.emit()
        client.search_messages.assert_called_once_with("r1", "缺陷")
        self.assertEqual(view.model.flash_msg_id, "m1")

    def test_search_enter_no_result_shows_msg(self):
        conv = Conversation(id="r1", name="研发", room_type="c")
        self.win._conversations = {"r1": conv}
        self.win.conversation_list.set_conversations([conv])
        self.win._active_room_id = "r1"
        self.win._room_type = "c"
        self.win.search_edit.setText("无结果词")
        client = mock.Mock()
        client.search_messages.return_value = []
        self.win.client = client
        with mock.patch("ui.main_window.run_async",
                        side_effect=lambda fn, on_done, on_fail, parent=None:
                            on_done(fn())):
            self.win.search_edit.returnPressed.emit()
        client.search_messages.assert_called_once_with("r1", "无结果词")

    def test_new_room_menu_guarded_without_client(self):
        """未登录（client 为 None）时点「+」不弹菜单、不抛异常。"""
        self.win.client = None
        self.win.new_room_btn.click()  # 不抛异常即通过

    # ---- 拖拽发送 ---- #
    def test_chat_view_drop_local_file_emits_drop_files_requested(self):
        """消息列表 dropEvent：拖入本地文件 → 触发 drop_files_requested，不落默认分支。"""
        from PyQt5.QtCore import QMimeData, QUrl
        view = self.win.chat_area.chat_view

        class _Ev:
            def __init__(self, mime):
                self._mime = mime
                self._accepted = False
            def mimeData(self):
                return self._mime
            def acceptProposedAction(self):
                self._accepted = True

        got = []
        view.drop_files_requested.connect(got.append)
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(r"C:\tmp\a.png"),
                      QUrl.fromLocalFile(r"C:\tmp\b.txt")])
        ev = _Ev(mime)
        view.dropEvent(ev)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0], ["C:/tmp/a.png", "C:/tmp/b.txt"])
        self.assertTrue(ev._accepted)

    def test_chat_view_drag_enter_accepts_local_file(self):
        from PyQt5.QtCore import QMimeData, QUrl
        view = self.win.chat_area.chat_view

        class _Ev:
            def __init__(self, mime):
                self._mime = mime
                self._accepted = False
            def mimeData(self):
                return self._mime
            def acceptProposedAction(self):
                self._accepted = True

        ev = _Ev(QMimeData())
        ev._mime.setUrls([QUrl.fromLocalFile(r"C:\tmp\c.txt")])
        with mock.patch("ui.chat_view.QListView.dragEnterEvent") as super_ev:
            view.dragEnterEvent(ev)
        super_ev.assert_not_called()
        self.assertTrue(ev._accepted)

    def test_chat_view_drop_remote_url_not_consumed(self):
        """非本地文件（如外链/文本）不走文件发送分支。"""
        from PyQt5.QtCore import QMimeData, QUrl
        view = self.win.chat_area.chat_view

        class _Ev:
            def __init__(self, mime):
                self._mime = mime
                self._accepted = False
            def mimeData(self):
                return self._mime
            def acceptProposedAction(self):
                self._accepted = True

        got = []
        view.drop_files_requested.connect(got.append)
        ev = _Ev(QMimeData())
        ev._mime.setUrls([QUrl("https://example.com/a.png")])
        with mock.patch("ui.chat_view.QListView.dropEvent") as super_ev:
            view.dropEvent(ev)
        super_ev.assert_called_once()
        self.assertEqual(got, [])

    def test_mixed_markdown_file_part_always_file_link(self):
        """文件 parts 一律拼成 [文件名](url)，不因图床配置变成内联图。"""
        win = self.win
        # 不配图床：文件仍是文件链接
        win.client = mock.Mock()
        win.client.upload_service_url = ""  # 明确无图床
        text = win._mixed_markdown([
            ("text", "附件"), ("file", "C:/tmp/report.txt")])
        self.assertEqual(text, "附件\n\n[report.txt](C:/tmp/report.txt)\n")
        # 即便配了图床，文件也不用 ![..] 内联
        win.client.upload_service_url = "https://img.example.com/up"
        text = win._mixed_markdown([("file", "C:/tmp/report.txt")])
        self.assertEqual(text, "\n[report.txt](C:/tmp/report.txt)\n")

    def test_on_chat_view_drop_guarded_without_client(self):
        """未登录/未选会话时拖入文件不发送、不抛异常。"""
        self.win.client = None
        self.win._active_room_id = None
        with mock.patch.object(self.win, "_on_send_file") as sf:
            self.win._on_chat_view_drop([r"C:\tmp\a.png"])
        sf.assert_not_called()

    def test_on_chat_view_drop_sends_each_file(self):
        """拖入消息列表：与文件选择器同路径，逐个走 _on_send_file（进度弹窗）。

        不再合并成混排消息、不弹预确认框、不消费输入框。
        """
        self.win.client = mock.Mock()
        self.win._active_room_id = "rid1"
        sent = []
        with mock.patch.object(self.win, "_on_send_file", sent.append):
            self.win._on_chat_view_drop([r"C:\tmp\a.png", r"C:\tmp\b.pdf"])
        self.assertEqual(sent, [r"C:\tmp\a.png", r"C:\tmp\b.pdf"])

    def test_on_chat_view_drop_empty_no_send(self):
        self.win.client = mock.Mock()
        self.win._active_room_id = "rid1"
        with mock.patch.object(self.win, "_on_send_file") as sf:
            self.win._on_chat_view_drop([])
        sf.assert_not_called()

    def test_on_send_file_opens_upload_dialog(self):
        """文件选择器发送：建非模态进度弹窗、持强引用、居中于主窗口、无 parent。"""
        self.win.client = mock.Mock()
        self.win._active_room_id = "rid1"
        import ui.main_window as mw
        dlg = mock.MagicMock()
        cls = mock.MagicMock(return_value=dlg)
        with mock.patch.object(mw, "FileUploadDialog", cls):
            self.win._on_send_file(r"C:\tmp\报告.pdf")
        cls.assert_called_once()
        args, kwargs = cls.call_args
        self.assertIs(args[0], self.win.client)        # client
        self.assertEqual(args[1], "rid1")              # rid
        self.assertEqual(args[2], r"C:\tmp\报告.pdf")  # path
        self.assertEqual(args[3], "报告.pdf")          # name
        self.assertIs(kwargs.get("center_on"), self.win)
        self.assertIsNone(kwargs.get("parent"))        # 独立窗口不置顶
        self.assertIn(dlg, self.win._upload_dialogs)   # 持强引用防 GC
        dlg.show.assert_called_once()                  # 非模态：show
        # 完成/取消回调接通状态栏且不崩（取消「已取消」不提示）
        dlg.succeeded.connect.call_args[0][0]("C:/tmp/报告.pdf")
        dlg.failed.connect.call_args[0][0]("已取消")
        dlg.failed.connect.call_args[0][0]("boom")

    def test_on_send_file_guarded_without_room(self):
        self.win.client = mock.Mock()
        self.win._active_room_id = None
        import ui.main_window as mw
        with mock.patch.object(mw, "FileUploadDialog") as cls:
            self.win._on_send_file(r"C:\tmp\a.pdf")
        cls.assert_not_called()

    def test_close_event_force_closes_upload_dialogs(self):
        """退出：在途上传弹窗 force_close 并清空，否则无父窗口卡住 app 退出。"""
        d1, d2 = mock.MagicMock(), mock.MagicMock()
        self.win._upload_dialogs.update({d1, d2})
        ev = mock.Mock()
        self.win.closeEvent(ev)
        d1.force_close.assert_called_once()
        d2.force_close.assert_called_once()
        self.assertEqual(self.win._upload_dialogs, set())
        ev.accept.assert_called_once()

    # ---- 用户状态接线 ---- #
    def test_status_provider_wired_to_panels(self):
        from ui.user_status import UserStatusProvider
        self.assertIsInstance(self.win._status_provider, UserStatusProvider)
        # 绑定方法每次属性访问都生成新对象，不能用 assertIs
        self.assertEqual(self.win._room_panels.members._status_provider,
                         self.win._status_provider.get)
        for page in self.win._room_panels.directory._pages:
            self.assertEqual(page._status_provider,
                             self.win._status_provider.get)

    def _make_conv(self):
        conv = Conversation(id="d1", name="alice", room_type="d",
                            username="alice", unread=0)
        self.win._conversations = {"d1": conv}
        self.win.conversation_list.set_conversations([conv])
        self.win.client = mock.Mock()
        return conv

    def test_open_dm_conversation_fetches_status(self):
        conv = self._make_conv()
        got = []
        with mock.patch.object(self.win._status_provider, "get",
                               side_effect=lambda u, cb, **kw: got.append(u)), \
                mock.patch("ui.main_window.run_async",
                           side_effect=lambda fn, **kw: None):
            self.win._open_conversation(conv)
        self.assertEqual(got, ["alice"])

    def test_open_group_conversation_clears_dm_status(self):
        """切到群房要清掉上一个私聊的状态显示；先置上再打开验证 clear。"""
        conv = Conversation(id="c1", name="群", room_type="c", unread=0)
        self.win._conversations = {"c1": conv}
        self.win.conversation_list.set_conversations([conv])
        self.win.client = mock.Mock()
        self.win.chat_area.set_dm_status("online", "会议中")
        got = []
        with mock.patch.object(self.win._status_provider, "get",
                               side_effect=lambda u, cb, **kw: got.append(u)), \
                mock.patch("ui.main_window.run_async",
                           side_effect=lambda fn, **kw: None):
            self.win._open_conversation(conv)
        self.assertEqual(got, [])
        self.assertTrue(self.win.chat_area.status_icon.isHidden())

    def test_open_dm_preview_skips_status_fetch(self):
        """预览模式未加入房间，不拉状态也不发已读。"""
        conv = self._make_conv()
        got = []
        with mock.patch.object(self.win._status_provider, "get",
                               side_effect=lambda u, cb, **kw: got.append(u)), \
                mock.patch("ui.main_window.run_async",
                           side_effect=lambda fn, **kw: None):
            self.win._open_conversation(conv, preview=True)
        self.assertEqual(got, [])

    def test_status_info_updates_list_row_and_active_title(self):
        """回调路由：会话列表行刷新；当前打开的正是该私聊时标题栏同步。"""
        conv = self._make_conv()
        self.win._active_room_id = "d1"
        self.win._on_status_info("alice", {
            "username": "alice", "status_key": "online",
            "status_text": "会议中",
        })
        cached = self.win.conversation_list._status_cache.get("alice")
        self.assertIsNotNone(cached)
        self.assertEqual(cached["status_key"], "online")
        self.assertFalse(self.win.chat_area.status_icon.isHidden())
        self.assertEqual(self.win.chat_area.status_text.text(), "会议中")

    def test_status_info_other_user_not_painted_on_title(self):
        """回填带房间校验：别的用户的状态不能写到当前标题栏。"""
        conv = self._make_conv()
        self.win._active_room_id = "d1"
        self.win._on_status_info("bob", {"username": "bob",
                                         "status_key": "busy"})
        self.assertTrue(self.win.chat_area.status_icon.isHidden())

    def test_status_fetch_failure_schedules_retry(self):
        """拉取失败（info=None）要安排延时重试并计数；成功后清计数。"""
        self._make_conv()
        with mock.patch("ui.main_window.QTimer") as MQT:
            self.win._on_status_info("alice", None)
        self.assertEqual(self.win._status_attempts.get("alice"), 1)
        timer = MQT.return_value
        timer.start.assert_called_once_with(
            self.win._STATUS_RETRY_DELAY_MS)
        fn = timer.timeout.connect.call_args[0][0]
        # 到点后应重新走 provider.get
        got = []
        with mock.patch.object(self.win._status_provider, "get",
                               side_effect=lambda u, cb, **kw:
                                   got.append((u, cb))):
            fn()
        self.assertEqual(got[0][0], "alice")
        # 成功回调清掉计数
        self.win._on_status_info("alice", {"username": "alice",
                                           "status_key": "online"})
        self.assertNotIn("alice", self.win._status_attempts)

    def test_status_retry_gives_up_after_max(self):
        with mock.patch("ui.main_window.QTimer"):
            for _ in range(self.win._STATUS_RETRY_MAX + 1):
                self.win._on_status_info("alice", None)
        self.assertEqual(self.win._status_attempts.get("alice"),
                         self.win._STATUS_RETRY_MAX + 1)

    def test_multi_user_dm_skips_status_fetch(self):
        """多人私聊 username 是逗号拼接列表，users.info 必 400，不发起拉取。"""
        conv = Conversation(id="m1", name="李肖, 董卫东", room_type="d",
                            username="lixiao7488, dongweidong", unread=0)
        self.win._conversations = {"m1": conv}
        self.win.conversation_list.set_conversations([conv])
        self.win.client = mock.Mock()
        got = []
        with mock.patch.object(self.win._status_provider, "get",
                               side_effect=lambda u, cb, **kw: got.append(u)), \
                mock.patch("ui.main_window.run_async",
                           side_effect=lambda fn, **kw: None):
            self.win._open_conversation(conv)
        self.assertEqual(got, [])

    def test_conversation_load_skips_multi_user_dm_status(self):
        """会话列表批量拉状态同样跳过逗号拼接的多聊 username。"""
        single = Conversation(id="s1", name="alice", room_type="d",
                              username="alice", unread=0)
        multi = Conversation(id="m1", name="a, b", room_type="d",
                             username="usera, userb", unread=0)
        self.win.client = mock.Mock()
        captured = {}

        def _fake_run_async(fn, on_done=None, on_fail=None, parent=None):
            captured["on_done"] = on_done

        with mock.patch("ui.main_window.run_async",
                        side_effect=_fake_run_async), \
                mock.patch.object(self.win._status_provider, "get",
                                  side_effect=lambda u, cb, **kw: None) as m:
            self.win._load_conversations()
            captured["on_done"]([single, multi])
        called = [c.args[0] for c in m.call_args_list]
        # 渲染选中首行还会经 _open_conversation 再拉一次（正常行为），
        # 这里只断言：单聊拉了、多聊逗号用户名一次都没拉
        self.assertIn("alice", called)
        self.assertNotIn("usera", called)
        self.assertTrue(all("," not in u for u in called))

    # ---- 实时推送（stream-notify-user） ---- #
    def test_pushed_user_status_refreshes_surfaces(self):
        conv = self._make_conv()
        self.win._active_room_id = "d1"
        info = {"username": "alice", "status_key": "busy", "status_text": ""}
        with mock.patch.object(self.win._status_provider, "apply_push",
                               return_value=info) as ap, \
                mock.patch.object(self.win.conversation_list,
                                  "apply_status") as m_apply, \
                mock.patch.object(self.win._room_panels.members,
                                  "refresh_status") as m_members, \
                mock.patch.object(self.win._room_panels.directory,
                                  "refresh_status") as m_dir:
            self.win._on_pushed_user_status("alice", "busy")
        ap.assert_called_once_with("alice", "busy")
        m_apply.assert_called_once_with("alice", info)
        m_members.assert_called_once_with("alice", info)
        m_dir.assert_called_once_with("alice", info)
        # 当前打开的正是该私聊：标题栏同步
        self.assertEqual(self.win.chat_area.status_text.text(), "忙碌")

    def test_pushed_user_status_other_user_not_painted_on_title(self):
        conv = self._make_conv()
        self.win._active_room_id = "d1"
        info = {"username": "bob", "status_key": "busy", "status_text": ""}
        with mock.patch.object(self.win._status_provider, "apply_push",
                               return_value=info):
            self.win._on_pushed_user_status("bob", "busy")
        self.assertTrue(self.win.chat_area.status_icon.isHidden())

    def test_pushed_subscription_updated_syncs_fields(self):
        conv = self._make_conv()
        conv.unread = 0
        conv.is_favorite = False
        got = []
        with mock.patch.object(self.win.conversation_list,
                               "upsert_conversation",
                               side_effect=lambda c: got.append(c)):
            self.win._on_pushed_subscription(
                "updated", {"rid": "d1", "unread": 7, "f": True,
                            "fname": "爱丽丝"})
        self.assertEqual(conv.unread, 7)
        self.assertTrue(conv.is_favorite)
        self.assertEqual(conv.name, "爱丽丝")
        self.assertEqual(got, [conv])

    def test_pushed_subscription_updated_active_room_keeps_unread_zero(self):
        """当前会话不上报已读前的计数会闪角标，强制保持 0。"""
        conv = self._make_conv()
        self.win._active_room_id = "d1"
        with mock.patch.object(self.win.conversation_list,
                               "upsert_conversation", side_effect=lambda c: None):
            self.win._on_pushed_subscription(
                "updated", {"rid": "d1", "unread": 3})
        self.assertEqual(conv.unread, 0)

    def test_pushed_subscription_updated_unknown_room_ignored(self):
        got = []
        with mock.patch.object(self.win.conversation_list,
                               "upsert_conversation",
                               side_effect=lambda c: got.append(c)):
            self.win._on_pushed_subscription(
                "updated", {"rid": "ghost", "unread": 3})
        self.assertEqual(got, [])

    def test_pushed_subscription_inserted_schedules_reload(self):
        self.win.client = mock.Mock()
        with mock.patch.object(self.win, "_load_conversations") as m_reload:
            self.win._on_pushed_subscription("inserted", {"rid": "NEW"})
            self.assertIsNotNone(self.win._conv_reload_timer)
            self.assertTrue(self.win._conv_reload_timer.isActive())
            # 到点触发整表重拉
            self.win._conv_reload_timer.timeout.emit()
            m_reload.assert_called_once()
            # 已知房间不触发
            conv = Conversation(id="d1", name="a", room_type="d",
                                username="alice", unread=0)
            self.win._conversations = {"d1": conv}
            self.win._on_pushed_subscription("inserted", {"rid": "d1"})
            self.assertEqual(m_reload.call_count, 1)

    def test_pushed_subscription_removed_clears_conversation(self):
        conv = self._make_conv()
        self.win._active_room_id = "d1"
        self.win._room_messages["d1"] = []
        self.win._on_pushed_subscription("removed", {"rid": "d1"})
        self.assertNotIn("d1", self.win._conversations)
        self.assertNotIn("d1", self.win._room_messages)
        self.assertIsNone(self.win._active_room_id)

    # ---- 登录成功 → 进入主界面 ---- #
    def test_login_success_completes_login(self):
        with mock.patch.object(MainWindow, "_complete_login") as cl:
            self.win._on_login_success(mock.Mock())
            cl.assert_called_once()


