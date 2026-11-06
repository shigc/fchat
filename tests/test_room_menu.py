"""标题栏「更多」菜单、房间 E2E 开关与房间信息卡的回归测试。

覆盖：菜单按钮装配（非 checkable、预览隐藏、图标刷新）、菜单文案随
encrypted 切换与权限置灰、开关的乐观更新/失败回滚/密钥缓存失效、
房间信息卡按房间类型分流（私聊 ProfileCard / 频道团队 RoomInfoCard）。
offscreen 渲染，不联网。
运行：python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402
from core.models import Conversation, Message, User  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.profile_popup import ProfileCard  # noqa: E402
import ui.room_info_popup as room_info_popup  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class _NoLogin(MainWindow):
    """跳过登录：_show_login 里是阻塞的 exec_()，测试不能进。"""

    def _try_auto_login(self):
        self.auto_login_skipped = True


def _sync_run_async(fn, on_done=None, on_fail=None, parent=None):
    """同步替身：立即执行任务并回调，避免测试里开线程。"""
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001
        if on_fail:
            on_fail(e)
        return
    if on_done:
        on_done(result)


class RoomMenuTest(unittest.TestCase):

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
        self.win.client = mock.Mock()
        self.win.client.e2e = mock.Mock()

    def tearDown(self):
        self.win.realtime and self.win.realtime.stop()
        self.win.deleteLater()
        (config.CONFIG_DIR, config.CONFIG_FILE,
         config.KEY_FILE, config.LEGACY_INI_FILE) = self._orig
        self._tmp.cleanup()

    def _activate(self, conv):
        self.win._conversations = {conv.id: conv}
        self.win.conversation_list.set_conversations([conv])
        self.win._active_room_id = conv.id
        self.win._room_type = conv.room_type

    # ---- 菜单按钮装配 ---- #
    def test_menu_button_exists_not_checkable(self):
        btn = self.win.chat_area.menu_btn
        self.assertIsNotNone(btn)
        self.assertFalse(btn.isCheckable())
        self.assertFalse(btn.icon().isNull())

    def test_menu_button_hidden_in_preview_mode(self):
        self.win.chat_area.set_preview_mode(True)
        self.assertTrue(self.win.chat_area.menu_btn.isHidden())
        self.win.chat_area.set_preview_mode(False)
        self.assertFalse(self.win.chat_area.menu_btn.isHidden())

    def test_refresh_icons_covers_menu_button(self):
        """主题切换重画图标必须包含「更多」按钮（否则暗色下图标残留亮色）。"""
        with mock.patch.object(self.win.chat_area.menu_btn, "setIcon") as si:
            self.win.chat_area.refresh_icons()
        si.assert_called_once()

    # ---- 菜单构建 ---- #
    def _open_menu(self):
        """桩掉 QMenu.exec_ 打开菜单，返回 (菜单 mock, e2e/星标/置顶/信息 action)。"""
        with mock.patch("ui.main_window.QMenu") as MQ, \
                mock.patch("ui.main_window.run_async"):
            act_e2e = mock.Mock()
            act_starred = mock.Mock()
            act_pinned = mock.Mock()
            act_info = mock.Mock()
            MQ.return_value.addAction.side_effect = [
                act_e2e, act_starred, act_pinned, act_info]
            MQ.return_value.exec_.return_value = None
            self.win._on_room_menu()
        return MQ.return_value, act_e2e, act_starred, act_pinned, act_info

    def test_menu_texts_follow_encrypted_state(self):
        conv = Conversation(id="R1", room_type="p", name="g", i_created=True)
        self._activate(conv)
        menu, act_e2e, act_starred, act_pinned, _ = self._open_menu()
        self.assertEqual(menu.addAction.call_args_list[0].args[1],
                         "开启端到端加密")
        self.assertEqual(menu.addAction.call_args_list[1].args[1],
                         "星标消息")
        self.assertEqual(menu.addAction.call_args_list[2].args[1],
                         "置顶消息")
        self.assertEqual(menu.addAction.call_args_list[3].args[1],
                         "查看房间信息")
        act_e2e.setEnabled.assert_called_once_with(True)
        conv.encrypted = True
        menu, act_e2e, _s, _p, _i = self._open_menu()
        self.assertEqual(menu.addAction.call_args_list[0].args[1],
                         "关闭端到端加密")
        act_e2e.setEnabled.assert_called_once_with(True)

    def test_menu_starred_and_pinned_open_message_lists(self):
        conv = Conversation(id="R1", room_type="c", name="g")
        self._activate(conv)
        _, _e2e, act_starred, act_pinned, _ = self._open_menu()
        # action 是 Mock：triggered.connect 只记录 lambda，手动取出调用
        cb_star = act_starred.triggered.connect.call_args[0][0]
        cb_pin = act_pinned.triggered.connect.call_args[0][0]
        with mock.patch.object(self.win, "_show_message_list") as show:
            cb_star(False)
            cb_pin(False)
        self.assertEqual(
            [c.args for c in show.call_args_list],
            [("starred",), ("pinned",)])

    def test_menu_e2e_allowed_for_dm(self):
        """私聊（含自聊）允许开关加密，不限创建者。"""
        conv = Conversation(id="R1", room_type="d", name="alice",
                            username="alice", i_created=False)
        self._activate(conv)
        _, act_e2e, _s, _p, _i = self._open_menu()
        act_e2e.setEnabled.assert_called_once_with(True)

    def test_menu_e2e_allowed_for_own_private_room(self):
        """私有频道/私有团队（t=p）本人创建时可开关。"""
        conv = Conversation(id="R2", room_type="p", name="g", i_created=True)
        self._activate(conv)
        _, act_e2e, _s, _p, _i = self._open_menu()
        act_e2e.setEnabled.assert_called_once_with(True)

    def test_menu_e2e_disabled_for_public_channel(self):
        """公开频道(t=c)一律置灰：真机实测服务端拒绝
        error-action-not-allowed（仅私有群/私聊可加密）。"""
        conv = Conversation(id="R3", room_type="c", name="g", i_created=True)
        self._activate(conv)
        _, act_e2e, _s, _p, _i = self._open_menu()
        act_e2e.setEnabled.assert_called_once_with(False)
        conv = Conversation(id="R4", room_type="p", name="g", i_created=False)
        self._activate(conv)
        _, act_e2e, _s, _p, _i = self._open_menu()
        act_e2e.setEnabled.assert_called_once_with(False)

    def test_room_menu_guarded_without_client(self):
        """未登录（client 为 None）点「更多」不弹菜单、不抛异常。"""
        self.win.client = None
        conv = Conversation(id="R1", room_type="c", name="g")
        self._activate(conv)
        self.win._on_room_menu()  # 不抛异常即通过

    # ---- E2E 开关 ---- #
    def test_toggle_e2e_optimistic_and_invalidates_keys(self):
        conv = Conversation(id="R1", room_type="p", name="g", i_created=True)
        self._activate(conv)
        self.win.client.get_history.return_value = []
        with mock.patch("ui.main_window.run_async", side_effect=_sync_run_async):
            self.win._toggle_room_e2e(conv)
        self.win.client.set_room_e2e.assert_called_once_with("R1", True)
        self.assertTrue(conv.encrypted)
        self.win.client.e2e.invalidate_room.assert_called_once_with("R1")

    def test_toggle_e2e_rollback_on_fail(self):
        conv = Conversation(id="R1", room_type="p", name="g",
                            i_created=True, encrypted=True)
        self._activate(conv)

        def _boom(fn, on_done=None, on_fail=None, parent=None):
            on_fail(RuntimeError("server said no"))

        with mock.patch("ui.main_window.run_async", side_effect=_boom):
            self.win._toggle_room_e2e(conv)
        self.assertTrue(conv.encrypted, "失败后必须回滚到原状态")
        self.win.client.e2e.invalidate_room.assert_not_called()

    def test_toggle_e2e_guarded_for_public_and_non_creator(self):
        """公开频道与非本人创建的私有群不能开关；私聊不受限。"""
        for conv in (Conversation(id="R2", room_type="c", name="g",
                                  i_created=True),
                     Conversation(id="R2b", room_type="p", name="g",
                                  i_created=False)):
            with mock.patch("ui.main_window.run_async") as ra:
                self.win._toggle_room_e2e(conv)
            ra.assert_not_called()
        self.win.client.set_room_e2e.assert_not_called()

    def test_toggle_e2e_allowed_for_dm(self):
        conv = Conversation(id="R3", room_type="d", name="alice",
                            username="alice", i_created=False)
        self.win.client.get_history.return_value = []
        with mock.patch("ui.main_window.run_async",
                        side_effect=_sync_run_async):
            self.win._toggle_room_e2e(conv)
        self.win.client.set_room_e2e.assert_called_once_with("R3", True)
        self.assertTrue(conv.encrypted)

    def test_toggle_success_refetches_history_and_merges(self):
        """订阅就绪前的开关系统消息服务端不补发（真机 15:23 实测只显示
        一条）：开关成功后必须重拉最近一页历史，按 id 合并进缓存与当前视图。"""
        conv = Conversation(id="R1", room_type="d", name="alice",
                            username="alice")
        self.win._active_room_id = "R1"
        old = Message(id="m0", room_id="R1", text="旧", timestamp=100.0,
                      sender=User(id="u", username="a"))
        sysmsg = Message(id="sys1", room_id="R1", msg_type="system",
                         text="alice 开启了端到端加密", timestamp=200.0,
                         sender=User(id="me", username="alice"))
        self.win.chat_area.chat_view.rebuild_messages([old])
        self.win._room_messages["R1"] = [old]
        self.win.client.get_history.return_value = [sysmsg]  # 新->旧
        with mock.patch("ui.main_window.run_async",
                        side_effect=_sync_run_async):
            self.win._toggle_room_e2e(conv)
        self.win.client.get_history.assert_called_once_with(
            "R1", self.win._page_size, 0, "d")
        model = self.win.chat_area.chat_view.model
        self.assertGreaterEqual(model.find_message_row("sys1"), 0)
        self.assertEqual([m.id for m in self.win._room_messages["R1"]],
                         ["m0", "sys1"])

    def test_merge_tail_skips_stale_missing_in_view_but_caches(self):
        """重拉页里比视图末尾更早的缺失消息不追加到尾部（避免错序），
        但缓存要补齐，切回房间重建时可见。"""
        conv = Conversation(id="R1", room_type="d", name="alice",
                            username="alice")
        self.win._active_room_id = "R1"
        new = Message(id="m2", room_id="R1", text="新", timestamp=300.0,
                      sender=User(id="u", username="a"))
        stale = Message(id="m1", room_id="R1", text="旧", timestamp=150.0,
                        sender=User(id="u", username="a"))
        self.win.chat_area.chat_view.rebuild_messages([new])
        self.win._room_messages["R1"] = [new]
        self.win.client.get_history.return_value = [new, stale]  # 新->旧
        with mock.patch("ui.main_window.run_async",
                        side_effect=_sync_run_async):
            self.win._toggle_room_e2e(conv)
        model = self.win.chat_area.chat_view.model
        self.assertEqual(model.find_message_row("m1"), -1)
        # 缓存按时间正序补齐
        self.assertEqual([m.id for m in self.win._room_messages["R1"]],
                         ["m1", "m2"])

    # ---- 房间信息卡 ---- #
    def test_room_info_dm_opens_profile_card(self):
        conv = Conversation(id="R1", room_type="d", name="alice",
                            username="alice")
        self._activate(conv)
        created = []
        original = ProfileCard.show
        ProfileCard.show = lambda self_, *a: created.append(self_)
        try:
            with mock.patch("ui.main_window.run_async"):
                self.win._show_room_info(conv)
        finally:
            ProfileCard.show = original
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]._username, "alice")
        created[0].deleteLater()

    def test_room_info_channel_opens_room_info_card(self):
        conv = Conversation(id="R1", room_type="c", name="研发",
                            i_created=True, encrypted=False)
        self._activate(conv)
        created = []
        original = room_info_popup.RoomInfoCard.show
        room_info_popup.RoomInfoCard.show = lambda self_, *a: created.append(self_)
        try:
            with mock.patch("ui.api_workers.run_async",
                            side_effect=_sync_run_async) as ra:
                self.win.client.fetch_room_info.return_value = {
                    "_id": "R1", "name": "研发", "t": "c", "topic": "需求讨论",
                    "usersCount": 12, "ts": {"$date": 1700000000000},
                    "u": {"username": "alice", "name": "开发者"},
                    "encrypted": True,
                }
                self.win._show_room_info(conv)
        finally:
            room_info_popup.RoomInfoCard.show = original
        self.assertEqual(len(created), 1)
        card = created[0]
        # run_async 被同步替身直接执行：fetch_room_info 已跑、回填完成
        ra.assert_called_once()
        self.win.client.fetch_room_info.assert_called_once_with("R1")
        self.assertEqual(card.name_label.text(), "研发")
        self.assertEqual(card.topic_label.text(), "需求讨论")
        self.assertEqual(card._rows["members"].text(), "12 人")
        # rooms.info 的 encrypted 是权威值，必须回写会话对象校准菜单文案
        self.assertTrue(conv.encrypted)
        self.assertEqual(card._rows["e2e"].text(), "已开启")
        card.deleteLater()


if __name__ == "__main__":
    unittest.main()
