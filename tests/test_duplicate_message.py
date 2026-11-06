"""发送一条消息显示两条回归测试。

服务端会把刚发的消息回推，发送方本地又乐观回显一条 local- 假 id 消息，
两路各自插入就会"发一条显示两条"。修复：模型按 id 去重 + 主窗口把
服务端回推并回本地乐观回显。这里锁定这两层。

GUI 测试，offscreen 渲染，不联网。
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
from ui.chat_view import ChatView  # noqa: E402
from ui.message_model import MessageListModel  # noqa: E402
from ui import main_window  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class _NoLogin(main_window.MainWindow):
    """跳过登录：_show_login 里是阻塞的 exec_()，测试不能进。"""

    def _try_auto_login(self):
        self.auto_login_skipped = True


def _me():
    return User(id="me", username="me", name="我", avatar_url="")


class TestAppendMessageDedup(unittest.TestCase):
    """模型层：同一 id 的消息被 append 时应原位更新而非重复。"""

    def setUp(self):
        self.model = MessageListModel(None)

    def test_append_same_id_updates_instead_of_duplicating(self):
        m1 = Message(id="real1", room_id="r", sender=_me(),
                     timestamp=100.0, msg_type="text", text="你好", is_mine=True)
        self.model.append_message(m1)
        # 编辑/回推带来的同 id 副本：内容更新了，但不能再新增一行
        m2 = Message(id="real1", room_id="r", sender=_me(),
                     timestamp=100.0, msg_type="text", text="你好（已编辑）",
                     is_mine=True)
        self.model.append_message(m2)
        self.assertEqual(len(self.model.all_messages()), 1)
        self.assertEqual(self.model.all_messages()[0].text, "你好（已编辑）")

    def test_append_distinct_ids_appends(self):
        for i in range(3):
            self.model.append_message(
                Message(id=f"m{i}", room_id="r", sender=_me(),
                        timestamp=100.0 + i, msg_type="text", text=str(i)))
        self.assertEqual(len(self.model.all_messages()), 3)


class TestCollapseServerDuplicateSystem(unittest.TestCase):
    """服务端把一次系统动作落库成多条消息（id 连续、ts 相差毫秒）时，
    显示层按相邻同人同文本折叠，避免整块重复内容。"""

    def setUp(self):
        self.model = MessageListModel(None)

    @staticmethod
    def _sys(i, ts, text="唐赛 离开频道", username="stang"):
        return Message(id=f"sys{i}", room_id="r",
                       sender=User(id=username, username=username,
                                   name="唐赛"),
                       timestamp=ts, msg_type="system", text=text)

    def test_append_collapses_adjacent_same_event(self):
        for i in range(6):
            self.model.append_message(self._sys(i, 100.0 + i * 0.005))
        self.assertEqual(len(self.model.all_messages()), 1)

    def test_append_keeps_events_outside_window(self):
        self.model.append_message(self._sys(0, 100.0))
        self.model.append_message(self._sys(1, 103.0))
        self.assertEqual(len(self.model.all_messages()), 2)

    def test_append_keeps_different_sender_or_text(self):
        self.model.append_message(self._sys(0, 100.0))
        self.model.append_message(self._sys(1, 100.0, username="alice"))
        self.model.append_message(self._sys(2, 100.0, text="唐赛 加入频道"))
        self.assertEqual(len(self.model.all_messages()), 3)

    def test_append_does_not_collapse_identical_text_messages(self):
        for i in range(2):
            self.model.append_message(
                Message(id=f"t{i}", room_id="r", sender=_me(),
                        timestamp=100.0 + i * 0.005, msg_type="text",
                        text="收到"))
        self.assertEqual(len(self.model.all_messages()), 2)

    def test_prepend_collapses_history_dupes(self):
        msgs = [self._sys(i, 100.0 + i * 0.005) for i in range(6)]
        self.model.prepend_messages_immediate(msgs)
        self.assertEqual(len(self.model.all_messages()), 1)

    def test_prepend_collapses_across_page_boundary(self):
        # 新一页先入屏（折叠成 sys3），旧一页随后从顶部插入，边界上
        # 与新一页首条仍是同一次事件，不能再留一行
        self.model.prepend_messages_immediate(
            [self._sys(i, 100.03 + i * 0.005) for i in range(3)])
        self.model.prepend_messages_immediate(
            [self._sys(i, 100.0 + i * 0.005) for i in range(3)])
        self.assertEqual(len(self.model.all_messages()), 1)


class TestHistorySyncsConversationPreview(unittest.TestCase):
    """左侧预览以实际历史为准：服务端 lm 滞后时不跟着一直错。"""

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

    def test_stale_server_preview_follows_history_tail(self):
        rid = "GENERAL"
        conv = Conversation(id=rid, last_message_text="rfyao: 旧消息",
                            last_message_time=100.0)
        self.win._conversations[rid] = conv
        msgs = [
            Message(id="m1", room_id=rid, text="普通消息", timestamp=101.0),
            Message(id="m2", room_id=rid, msg_type="system",
                    text="唐赛 离开频道",
                    sender=User(username="stang", name="唐赛"),
                    timestamp=102.0),
        ]
        with mock.patch.object(
                self.win.conversation_list,
                "upsert_conversation") as upsert:
            self.win._sync_conversation_from_history(rid, msgs)
        self.assertEqual(conv.last_message_time, 102.0)
        self.assertEqual(conv.last_message_text, "唐赛 离开频道")
        upsert.assert_called_once_with(conv)

    def test_preview_never_moves_backwards(self):
        rid = "R1"
        conv = Conversation(id=rid, last_message_text="新消息",
                            last_message_time=200.0)
        self.win._conversations[rid] = conv
        with mock.patch.object(
                self.win.conversation_list,
                "upsert_conversation") as upsert:
            self.win._sync_conversation_from_history(
                rid, [Message(id="old", room_id=rid, text="旧",
                              timestamp=100.0)])
        self.assertEqual(conv.last_message_time, 200.0)
        self.assertEqual(conv.last_message_text, "新消息")
        upsert.assert_not_called()

    def test_unknown_room_is_noop(self):
        with mock.patch.object(
                self.win.conversation_list,
                "upsert_conversation") as upsert:
            self.win._sync_conversation_from_history(
                "missing", [Message(id="m", timestamp=1.0)])
        upsert.assert_not_called()


class TestAbsorbOwnEcho(unittest.TestCase):
    """主窗口层：服务端回推并入本地乐观回显，不产生第二条。"""

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

    def _add_local_echo(self, rid: str, text: str) -> Message:
        local = Message(id="local-abcdef123", room_id=rid, sender=_me(),
                        timestamp=110.0, msg_type="text", text=text,
                        is_mine=True, status="sending")
        chat_view = self.win.chat_area.chat_view
        chat_view.model.append_message(local)
        self.win._room_messages.setdefault(rid, []).append(local)
        return local

    def test_realtime_echo_absorbs_pending_local(self):
        rid = "r1"
        local = self._add_local_echo(rid, "你好世界")
        # 服务端回推：真实 id、同文本
        echo = Message(id="real-xyz", room_id=rid, sender=_me(),
                       timestamp=110.2, msg_type="text", text="你好世界",
                       is_mine=True, status="sent")
        view = self.win.chat_area.chat_view
        # 模拟 _on_realtime_message 里的吸收分支
        absorbed = self.win._absorb_own_echo(echo)
        self.assertTrue(absorbed)
        # 视图模型里仍只有一条，且 id 已被接管为真实 id
        msgs = view.model.all_messages()
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].id, "real-xyz")
        # 会话缓存不重复（真实 id 只出现一次）
        cache_ids = [m.id for m in self.win._room_messages[rid]]
        self.assertEqual(cache_ids.count("real-xyz"), 1)

    def test_absorb_takes_server_sender(self):
        """吸收后 sender 换成服务端解析版：本地回显的 sender 是 client.me，
        旧登录路径下没有 avatar_url，不换的话头像永远画成首字占位。"""
        rid = "r1"
        self._add_local_echo(rid, "你好世界")
        server_sender = User(id="me", username="me", name="我",
                             avatar_url="https://s.example.com/avatar/me")
        echo = Message(id="real-xyz", room_id=rid, sender=server_sender,
                       timestamp=110.2, msg_type="text", text="你好世界",
                       is_mine=True, status="sent")
        self.win._absorb_own_echo(echo)
        msgs = self.win.chat_area.chat_view.model.all_messages()
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].sender.avatar_url,
                         "https://s.example.com/avatar/me")

    def test_realtime_cache_append_dedups_by_id(self):
        """回推写入缓存按 id 去重：视图已按同 id 原位接管（_done 先改名的
        竞态）时，缓存不能再无条件 append 出两份同 id，否则切回房间重建成双条。"""
        rid = "r9"
        self.win.client = object()          # 仅作非空判断
        self.win._conversations[rid] = Conversation(id=rid)
        self.win._active_room_id = ""
        # upsert 会触发 _render → setCurrentRow → conversation_selected →
        # _open_conversation，拿裸 object 客户端发起异步请求，窗口销毁后
        # 回调砸在悬空 Qt 对象上（全量跑偶发原生崩溃）。本用例只关注缓存
        # 去重，把会话列表渲染整个桩掉。
        with mock.patch.object(self.win.conversation_list,
                               "upsert_conversation"):
            same = Message(id="real-1", room_id=rid, sender=_me(),
                           timestamp=100.0, msg_type="text", text="你好",
                           is_mine=True)
            self.win._room_messages[rid] = [same]
            self.win._on_realtime_message(same)
            self.assertEqual(len(self.win._room_messages[rid]), 1)
            other = Message(id="real-2", room_id=rid, sender=_me(),
                            timestamp=101.0, msg_type="text", text="第二条",
                            is_mine=True)
            self.win._on_realtime_message(other)
            self.assertEqual(len(self.win._room_messages[rid]), 2)

    def test_echo_does_not_absorb_when_no_local_match(self):
        rid = "r2"
        # 没有本地乐观回显（例如历史加载），回推应原样进入待渲染缓冲
        echo = Message(id="other-1", room_id=rid, sender=_me(),
                       timestamp=120.0, msg_type="text", text="历史消息",
                       is_mine=False)
        absorbed = self.win._absorb_own_echo(echo)
        self.assertFalse(absorbed)


class TestReconcileSentEcho(unittest.TestCase):
    """REST 响应归来时的对账：服务端回推可能先于响应被渲染成真实 id 的行
    （含文件的混排消息本地与服务端 text 必不同，吸收不到），此时本地回显
    行成了重复，必须移除而不是改名。"""

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

    def _local_echo(self, rid: str) -> Message:
        local = Message(id="local-abcdef123", room_id=rid, sender=_me(),
                        timestamp=110.0, msg_type="text",
                        text="[报告.docx](D:/local/报告.docx)",
                        is_mine=True, status="sending")
        self.win.chat_area.chat_view.model.append_message(local)
        self.win._room_messages.setdefault(rid, []).append(local)
        return local

    def _server_row(self, rid: str) -> Message:
        # 模拟回推先到且已被 flush 渲染：真实 id、服务端 URL 文本
        sent = Message(id="real-xyz", room_id=rid, sender=_me(),
                       timestamp=130.0, msg_type="text",
                       text="[报告.docx](https://s/file-upload/报告.docx)",
                       is_mine=True, status="sent")
        self.win.chat_area.chat_view.model.append_message(sent)
        self.win._room_messages.setdefault(rid, []).append(sent)
        return sent

    def test_duplicate_local_row_removed_when_server_row_rendered(self):
        rid = "r1"
        local = self._local_echo(rid)
        self._server_row(rid)
        sent = Message(id="real-xyz", room_id=rid, sender=_me(),
                       timestamp=130.0, msg_type="text", text="x",
                       is_mine=True)
        self.win._reconcile_sent_local_echo(local, sent)
        msgs = self.win.chat_area.chat_view.model.all_messages()
        self.assertEqual([m.id for m in msgs], ["real-xyz"])
        cache_ids = [m.id for m in self.win._room_messages[rid]]
        self.assertEqual(cache_ids, ["real-xyz"])

    def test_rename_when_server_row_not_rendered_yet(self):
        rid = "r2"
        local = self._local_echo(rid)
        sent = Message(id="real-xyz", room_id=rid, sender=_me(),
                       timestamp=130.0, msg_type="text", text="x",
                       is_mine=True)
        self.win._reconcile_sent_local_echo(local, sent)
        msgs = self.win.chat_area.chat_view.model.all_messages()
        self.assertEqual([m.id for m in msgs], ["real-xyz"])
        self.assertIs(msgs[0], local)

    def test_no_rename_without_sent_id(self):
        rid = "r3"
        local = self._local_echo(rid)
        sent = Message(id="", room_id=rid, sender=_me(),
                       timestamp=130.0, msg_type="text", text="x",
                       is_mine=True)
        self.win._reconcile_sent_local_echo(local, sent)
        self.assertEqual(local.id, "local-abcdef123")


if __name__ == "__main__":
    unittest.main()
