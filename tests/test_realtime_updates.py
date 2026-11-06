"""实时同 id 更新：编辑/表情回应/置顶星标是原位刷新，不是新消息。

锁住三件事：缓存被新对象替换；不增未读、不弹提醒；刷新不滚到底部。
新增消息的行为保持不变（未读 +1、追加并滚动）。
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
from ui import main_window  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class _NoLogin(main_window.MainWindow):
    def _try_auto_login(self):
        self.auto_login_skipped = True


def _user(username="alice"):
    return User(id=username, username=username, name=username.title())


class RealtimeUpdateTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (config.CONFIG_DIR, config.CONFIG_FILE,
                      config.KEY_FILE, config.LEGACY_INI_FILE)
        tmp = self._tmp.name
        config.CONFIG_DIR = os.path.join(tmp, "cfgdir")
        config.CONFIG_FILE = os.path.join(config.CONFIG_DIR, "config.yaml")
        config.KEY_FILE = os.path.join(config.CONFIG_DIR, "secret.key")
        config.LEGACY_INI_FILE = os.path.join(tmp, "config.ini")
        self.win = _NoLogin()
        self.win.resize(1000, 680)
        # flush 追加消息后会推进本地已读位置，需要能从 client 取到登录身份
        self.win.client = mock.Mock()
        self.win.client.server_url = "https://chat.example"
        self.win.client.me = _user("me")
        self.rid = "r1"
        self.win._conversations[self.rid] = Conversation(
            id=self.rid, last_message_text="旧内容", last_message_time=100.0)
        # 会话列表 upsert 会触发 setCurrentRow → _open_conversation
        # （裸 object client 会发异步请求），这里整体桩掉
        self._upsert = mock.patch.object(
            self.win.conversation_list, "upsert_conversation")
        self._upsert.start()
        self._tray_alert = mock.patch.object(
            self.win, "_add_tray_alert", return_value=False)
        self._tray_alert_mock = self._tray_alert.start()
        self._tray_refresh = mock.patch.object(
            self.win, "_refresh_tray_state")
        self._tray_refresh.start()

    def tearDown(self):
        self._tray_refresh.stop()
        self._tray_alert.stop()
        self._upsert.stop()
        self.win.realtime and self.win.realtime.stop()
        self.win.deleteLater()
        (config.CONFIG_DIR, config.CONFIG_FILE,
         config.KEY_FILE, config.LEGACY_INI_FILE) = self._orig
        self._tmp.cleanup()

    def _seed(self, msg):
        self.win._room_messages[self.rid] = [msg]
        self.win.chat_area.chat_view.set_room(self.rid)
        self.win.chat_area.chat_view.rebuild_messages([msg])

    def _edited_copy(self, text="新内容", **kw):
        return Message(id="m1", room_id=self.rid, text=text,
                       sender=_user(), timestamp=100.0, msg_type="text",
                       edited_at=120.0, **kw)

    def test_update_replaces_cache_without_unread_or_alert(self):
        self.win._active_room_id = ""
        old = Message(id="m1", room_id=self.rid, text="旧内容",
                      sender=_user(), timestamp=100.0)
        self._seed(old)
        new = self._edited_copy()
        self.win._on_realtime_message(new)
        self.assertIs(self.win._room_messages[self.rid][0], new)
        self.assertEqual(self.win._conversations[self.rid].unread, 0)
        self._tray_alert_mock.assert_not_called()
        self.assertEqual(self.win._pending_realtime_messages, [])

    def test_update_of_latest_refreshes_preview(self):
        self.win._active_room_id = ""
        old = Message(id="m1", room_id=self.rid, text="旧内容",
                      sender=_user(), timestamp=100.0)
        self._seed(old)
        conv = self.win._conversations[self.rid]
        conv.last_message_time = 100.0
        self.win._on_realtime_message(self._edited_copy("改过的最后一条"))
        self.assertEqual(conv.last_message_text, "Alice: 改过的最后一条")
        self.assertEqual(conv.last_message_time, 100.0)

    def test_update_of_old_message_leaves_preview(self):
        self.win._active_room_id = ""
        old = Message(id="m1", room_id=self.rid, text="旧内容",
                      sender=_user(), timestamp=100.0)
        self._seed(old)
        newer = Message(id="m2", room_id=self.rid, text="更新的消息",
                        sender=_user(), timestamp=200.0)
        self.win._room_messages[self.rid].append(newer)
        conv = self.win._conversations[self.rid]
        conv.last_message_text = "更新的消息"
        conv.last_message_time = 200.0
        self.win._on_realtime_message(self._edited_copy("改旧消息"))
        self.assertEqual(conv.last_message_text, "更新的消息")

    def test_update_in_active_room_queues_refresh_not_append(self):
        self.win._active_room_id = self.rid
        old = Message(id="m1", room_id=self.rid, text="旧内容",
                      sender=_user(), timestamp=100.0)
        self._seed(old)
        self.win._on_realtime_message(self._edited_copy())
        pending = self.win._pending_realtime_messages
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0][1])

    def test_new_message_still_counts_unread_and_queues_append(self):
        self.win._active_room_id = ""
        fresh = Message(id="m9", room_id=self.rid, text="来了",
                        sender=_user(), timestamp=300.0)
        self.win._on_realtime_message(fresh)
        self.assertEqual(self.win._conversations[self.rid].unread, 1)
        self._tray_alert_mock.assert_called_once()
        self.assertEqual(len(self.win._room_messages[self.rid]), 1)

    def test_new_message_preview_shows_real_name(self):
        self.win._active_room_id = ""
        fresh = Message(id="m9", room_id=self.rid, text="来了",
                        sender=_user(), timestamp=300.0)
        self.win._on_realtime_message(fresh)
        self.assertEqual(self.win._conversations[self.rid].last_message_text,
                         "Alice: 来了")

    def test_new_message_preview_own_shows_wo(self):
        self.win._active_room_id = ""
        mine = Message(id="m9", room_id=self.rid, text="自己发的",
                       sender=_user("me"), is_mine=True, timestamp=300.0)
        self.win._on_realtime_message(mine)
        self.assertEqual(self.win._conversations[self.rid].last_message_text,
                         "我: 自己发的")

    def test_new_message_preview_dm_has_no_prefix(self):
        self.win._active_room_id = ""
        self.win._conversations[self.rid].room_type = "d"
        fresh = Message(id="m9", room_id=self.rid, text="来了",
                        sender=_user(), timestamp=300.0)
        self.win._on_realtime_message(fresh)
        self.assertEqual(self.win._conversations[self.rid].last_message_text,
                         "来了")

    def test_new_message_preview_multi_dm_keeps_sender(self):
        """多人私聊（t=d、成员>2）要显示是谁说的，不能按一对一隐藏。"""
        self.win._active_room_id = ""
        conv = self.win._conversations[self.rid]
        conv.room_type = "d"
        conv.is_multi_dm = True
        fresh = Message(id="m9", room_id=self.rid, text="来了",
                        sender=_user(), timestamp=300.0)
        self.win._on_realtime_message(fresh)
        self.assertEqual(conv.last_message_text, "Alice: 来了")

    def test_flush_update_does_not_scroll(self):
        self.win._active_room_id = self.rid
        old = Message(id="m1", room_id=self.rid, text="旧内容",
                      sender=_user(), timestamp=100.0)
        self._seed(old)
        new = self._edited_copy()
        self.win._pending_realtime_messages = [(new, True)]
        view = self.win.chat_area.chat_view
        with mock.patch.object(view, "scroll_to_bottom") as scroll, \
                mock.patch.object(self.win, "_schedule_receipts_fetch"):
            self.win._flush_realtime_messages()
        scroll.assert_not_called()
        self.assertEqual(view.model.all_messages()[0].text, "新内容")

    def test_flush_new_message_appends_and_scrolls(self):
        self.win._active_room_id = self.rid
        fresh = Message(id="m2", room_id=self.rid, text="新消息",
                        sender=_user(), timestamp=200.0)
        self.win._pending_realtime_messages = [(fresh, False)]
        view = self.win.chat_area.chat_view
        with mock.patch.object(view, "scroll_to_bottom") as scroll, \
                mock.patch.object(
                    view, "append_message",
                    side_effect=view.model.append_message), \
                mock.patch.object(self.win, "_schedule_receipts_fetch"):
            self.win._flush_realtime_messages()
        scroll.assert_called_once_with(deferred=True)
        self.assertEqual(len(view.model.all_messages()), 1)


if __name__ == "__main__":
    unittest.main()
