"""主窗口互动逻辑：表情回应、编辑、置顶/星标、免打扰、失败重发、@计数。

只验证信号/回调接线与乐观更新/回滚，不联网；run_async 同步执行。
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402
from core.models import Conversation, Message, User  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _sync_run_async(task, on_done=None, on_fail=None, parent=None, **_kw):
    try:
        result = task()
    except Exception as exc:  # noqa: BLE001 —— 测试里原样交给 on_fail
        if on_fail is not None:
            on_fail(exc)
    else:
        if on_done is not None:
            on_done(result)
    return mock.Mock()


class _NoLogin(MainWindow):

    def _try_auto_login(self):
        self.auto_login_skipped = True


def _me():
    return User(id="me", username="me", name="我")


def _msg(mid="m1", rid="r1", mine=False, text="你好", **kw):
    return Message(id=mid, room_id=rid, text=text,
                   sender=User(username="bob", name="小明"),
                   timestamp=time.time(), is_mine=mine, **kw)


class InteractionWiringTest(unittest.TestCase):

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
        self.win.client = mock.Mock()
        self.win.client.me = _me()
        self.win.client.server_url = "http://s"

    def tearDown(self):
        self.win.realtime and self.win.realtime.stop()
        self.win.deleteLater()
        (config.CONFIG_DIR, config.CONFIG_FILE,
         config.KEY_FILE, config.LEGACY_INI_FILE) = self._orig
        self._tmp.cleanup()

    # ---- 表情回应 ---- #
    def test_reaction_adds_me_and_calls_api(self):
        msg = _msg(reactions={"👍": {"usernames": ["bob"]}})
        with mock.patch.object(self.win, "_apply_local_message_update") as apply_, \
                mock.patch("ui.main_window.run_async",
                           side_effect=_sync_run_async):
            self.win._on_reaction_toggle(msg, "👍")
        self.assertEqual(msg.reactions["👍"]["usernames"], ["bob", "me"])
        apply_.assert_called_once_with(msg)
        self.win.client.react_message.assert_called_once_with("m1", "👍", True)

    def test_reaction_toggle_off_removes_key_when_last(self):
        msg = _msg(reactions={"👍": {"usernames": ["me"]}})
        with mock.patch.object(self.win, "_apply_local_message_update"), \
                mock.patch("ui.main_window.run_async",
                           side_effect=_sync_run_async):
            self.win._on_reaction_toggle(msg, "👍")
        self.assertNotIn("👍", msg.reactions)
        self.win.client.react_message.assert_called_once_with("m1", "👍", False)

    def test_reaction_picker_tone_emoji_uses_rc_shortcode(self):
        # 面板回传的是 Unicode，服务端要 emojione 的 _toneN 方言
        msg = _msg()
        self.win._reaction_target = msg
        with mock.patch.object(self.win, "_apply_local_message_update"), \
                mock.patch("ui.main_window.run_async",
                           side_effect=_sync_run_async):
            self.win._react_with_emoji("👌🏿")
        self.win.client.react_message.assert_called_once_with(
            "m1", ":ok_hand_tone5:", True)
        self.assertIn(":ok_hand_tone5:", msg.reactions)

    def test_reaction_picker_plain_emoji_uses_alias(self):
        msg = _msg()
        self.win._reaction_target = msg
        with mock.patch.object(self.win, "_apply_local_message_update"), \
                mock.patch("ui.main_window.run_async",
                           side_effect=_sync_run_async):
            self.win._react_with_emoji("🎉")
        self.win.client.react_message.assert_called_once_with(
            "m1", ":tada:", True)

    def test_reaction_failure_rolls_back(self):
        original = {"👍": {"usernames": ["bob"]}}
        msg = _msg(reactions={
            "👍": {"usernames": list(original["👍"]["usernames"])}})
        self.win.client.react_message.side_effect = RuntimeError("boom")
        with mock.patch.object(self.win, "_apply_local_message_update"), \
                mock.patch("ui.main_window.run_async",
                           side_effect=_sync_run_async):
            self.win._on_reaction_toggle(msg, "👍")
        self.assertEqual(msg.reactions["👍"]["usernames"], ["bob"])
        self.assertIn("表情回应失败", self.win.statusBar().currentMessage())

    def test_reaction_ignored_for_local_echo(self):
        msg = _msg(mid="local-abc")
        self.win._on_reaction_toggle(msg, "👍")
        self.win.client.react_message.assert_not_called()

    # ---- 置顶 / 星标 ---- #
    def test_pin_message_optimistic_and_api(self):
        msg = _msg()
        with mock.patch.object(self.win, "_apply_local_message_update"), \
                mock.patch("ui.main_window.run_async",
                           side_effect=_sync_run_async):
            self.win._on_pin_message(msg, True)
        self.assertTrue(msg.pinned)
        self.win.client.pin_message.assert_called_once_with("m1", True)

    def test_star_failure_rolls_back(self):
        msg = _msg(starred=False)
        self.win.client.star_message.side_effect = RuntimeError("nope")
        with mock.patch.object(self.win, "_apply_local_message_update"), \
                mock.patch("ui.main_window.run_async",
                           side_effect=_sync_run_async):
            self.win._on_star_message(msg, True)
        self.assertFalse(msg.starred)
        self.assertIn("星标失败", self.win.statusBar().currentMessage())

    # ---- 免打扰 ---- #
    def test_mute_optimistic_and_saves(self):
        conv = Conversation(id="r1", name="房间")
        self.win._conversations = {"r1": conv}
        with mock.patch.object(self.win.conversation_list,
                               "upsert_conversation"), \
                mock.patch.object(self.win, "_drop_tray_alerts"), \
                mock.patch.object(self.win, "_refresh_tray_state"), \
                mock.patch("core.conv_cache.save_conversations_snapshot") as save, \
                mock.patch("ui.main_window.run_async",
                           side_effect=_sync_run_async):
            self.win._on_toggle_mute("r1")
        self.assertTrue(conv.muted)
        self.win.client.set_room_muted.assert_called_once_with("r1", True)
        save.assert_called_once()

    def test_mute_failure_rolls_back_with_hint(self):
        conv = Conversation(id="r1", name="房间")
        self.win._conversations = {"r1": conv}
        self.win.client.set_room_muted.side_effect = RuntimeError("404")
        with mock.patch.object(self.win.conversation_list,
                               "upsert_conversation"), \
                mock.patch.object(self.win, "_drop_tray_alerts"), \
                mock.patch.object(self.win, "_refresh_tray_state"), \
                mock.patch("ui.main_window.run_async",
                           side_effect=_sync_run_async):
            self.win._on_toggle_mute("r1")
        self.assertFalse(conv.muted)
        self.assertIn("免打扰", self.win.statusBar().currentMessage())

    # ---- 失败重发 ---- #
    def test_retry_resends_with_same_local_id(self):
        msg = _msg(mid="local-abc", mine=True)
        msg.status = "failed"
        self.win._retry_payloads[msg.id] = {
            "room_id": "r1", "text": "你好"}
        with mock.patch.object(self.win, "_send_text_payload") as send:
            self.win._on_retry_message(msg)
        send.assert_called_once_with(self.win._retry_payloads[msg.id],
                                     local_id=msg.id)

    def test_retry_ignored_when_not_failed_or_no_payload(self):
        msg = _msg(mid="local-abc", mine=True)
        msg.status = "sent"
        self.win._retry_payloads[msg.id] = {"room_id": "r1", "text": "x"}
        with mock.patch.object(self.win, "_send_text_payload") as send:
            self.win._on_retry_message(msg)
            send.assert_not_called()
            msg.status = "failed"
            self.win._retry_payloads.clear()
            self.win._on_retry_message(msg)
            send.assert_not_called()

    # ---- 编辑 ---- #
    def _seed_editable(self, encrypted=False):
        view = self.win.chat_area.chat_view
        self.win._active_room_id = "r1"
        view.set_room("r1")
        view.set_room_encrypted(encrypted)
        msg = _msg(mid="m1", mine=True, text="旧内容")
        view.model.append_message(msg)
        self.win._room_messages["r1"] = [msg]
        return msg

    def test_edit_enters_edit_mode(self):
        msg = self._seed_editable()
        with mock.patch.object(self.win, "_save_current_draft"):
            self.win._on_edit_message(msg)
        self.assertEqual(self.win._editing_msg_id, "m1")
        self.assertTrue(self.win.chat_area.input.is_editing())
        self.assertEqual(self.win.chat_area.input.input.toPlainText(), "旧内容")

    def test_edit_blocked_in_e2e_room(self):
        msg = self._seed_editable(encrypted=True)
        self.win._on_edit_message(msg)
        self.assertFalse(self.win._editing_msg_id)
        self.assertFalse(self.win.chat_area.input.is_editing())

    def test_submit_edit_updates_message_and_finishes(self):
        self._seed_editable()
        self.win._editing_msg_id = "m1"
        self.win.chat_area.input.begin_edit("m1", "旧内容")
        updated = _msg(mid="m1", mine=True, text="新内容", edited_at=200.0)
        self.win.client.update_message.return_value = updated
        with mock.patch.object(self.win, "_apply_local_message_update") as apply_, \
                mock.patch("ui.main_window.run_async",
                           side_effect=_sync_run_async):
            self.win._submit_edit("r1", "新内容")
        self.win.client.update_message.assert_called_once_with(
            "r1", "m1", "新内容")
        apply_.assert_called_once_with(updated)
        self.assertFalse(self.win._editing_msg_id)
        self.assertFalse(self.win.chat_area.input.is_editing())

    def test_submit_edit_failure_keeps_editing(self):
        self._seed_editable()
        self.win._editing_msg_id = "m1"
        self.win.chat_area.input.begin_edit("m1", "旧内容")
        self.win.client.update_message.side_effect = RuntimeError("nope")
        with mock.patch("ui.main_window.run_async",
                        side_effect=_sync_run_async):
            self.win._submit_edit("r1", "新内容")
        self.assertEqual(self.win._editing_msg_id, "m1")
        self.assertTrue(self.win.chat_area.input.is_editing())
        self.assertIn("编辑失败", self.win.statusBar().currentMessage())

    # ---- 连接横幅 ---- #
    def test_realtime_status_updates_banner(self):
        for state in ("connecting", "connected", "disconnected",
                      "reconnecting", "ready"):
            with mock.patch.object(
                    self.win.chat_area, "set_connection_state") as setter:
                self.win._on_realtime_status(state)
            setter.assert_called_once_with(state)

    def test_reconnect_button_calls_realtime(self):
        self.win.realtime = mock.Mock()
        with mock.patch.object(
                self.win.chat_area, "set_connection_state") as setter:
            self.win._on_reconnect_clicked()
        setter.assert_called_once_with("reconnecting")
        self.win.realtime.reconnect_now.assert_called_once()

    # ---- @ 计数 ---- #
    def test_subscription_updates_mention_count(self):
        conv = Conversation(id="r1", name="R", unread=2)
        self.win._conversations = {"r1": conv}
        self.win._active_room_id = "other"
        with mock.patch.object(self.win.conversation_list,
                               "upsert_conversation"), \
                mock.patch.object(self.win, "_refresh_tray_state"):
            self.win._on_pushed_subscription(
                "updated", {"rid": "r1", "unread": 2, "userMentions": 3})
        self.assertEqual(conv.mention_count, 3)

    def test_current_room_mention_count_forced_zero(self):
        conv = Conversation(id="r1", name="R", unread=2, mention_count=4)
        self.win._conversations = {"r1": conv}
        self.win._active_room_id = "r1"
        with mock.patch.object(self.win.conversation_list,
                               "upsert_conversation"), \
                mock.patch.object(self.win, "_refresh_tray_state"):
            self.win._on_pushed_subscription(
                "updated", {"rid": "r1", "unread": 2, "userMentions": 3})
        self.assertEqual(conv.mention_count, 0)
        self.assertEqual(conv.unread, 0)


if __name__ == "__main__":
    unittest.main()
