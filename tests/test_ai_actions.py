"""单条消息 AI 与输入框写作助手：菜单入口、信号、回填语义与接线。

offscreen；网络与 AI 对话框一律 mock，不联网。
"""
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QPoint  # noqa: E402
from PyQt5.QtGui import QTextCursor  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402
from core.models import Message, User  # noqa: E402
from ui.chat_input import ChatInput  # noqa: E402
from ui.chat_view import ChatView  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _msg(text="hello", msg_type="text", **kw):
    sender = User(id="u1", username="alice", name="Alice")
    return Message(id="m1", room_id="r1", text=text, msg_type=msg_type,
                   sender=sender, **kw)


class ChatViewAIEntryTest(unittest.TestCase):
    def setUp(self):
        self.view = ChatView()

    def tearDown(self):
        self.view.deleteLater()

    def test_hidden_by_default(self):
        self.assertFalse(self.view._ai_available)

    @staticmethod
    def _menu_titles(MQ):
        return [c.args[-1] for c in MQ.return_value.addMenu.call_args_list
                if len(c.args) >= 2]

    def test_ai_submenu_only_when_enabled_and_text(self):
        # 提醒子菜单与 AI 无关：文本消息始终有「提醒」，AI 未启用时无「AI 助手」
        with mock.patch("ui.chat_view.QMenu") as MQ:
            self.view._show_context_menu(_msg(), QPoint(0, 0))
        self.assertNotIn("AI 助手", self._menu_titles(MQ))

        self.view.set_ai_available(True)
        with mock.patch("ui.chat_view.QMenu") as MQ:
            self.view._show_context_menu(_msg(), QPoint(0, 0))
        self.assertIn("AI 助手", self._menu_titles(MQ))
        submenu = MQ.return_value.addMenu.return_value
        labels = [c.args[0] for c in submenu.addAction.call_args_list
                  if c.args[0] in ("翻译", "解释", "提取待办",
                                   "校对纠错", "起草回复")]
        self.assertEqual(labels,
                         ["翻译", "解释", "提取待办", "校对纠错", "起草回复"])

    def test_non_text_or_empty_message_has_no_ai_menu(self):
        self.view.set_ai_available(True)
        for msg in (_msg(msg_type="image", text=""), _msg(text="   ")):
            with mock.patch("ui.chat_view.QMenu") as MQ:
                self.view._show_context_menu(msg, QPoint(0, 0))
            self.assertNotIn("AI 助手", self._menu_titles(MQ))

    def test_ai_action_emits_message_and_key(self):
        self.view.set_ai_available(True)
        got = []
        self.view.ai_action_requested.connect(lambda m, k: got.append((m, k)))
        msg = _msg()
        with mock.patch("ui.chat_view.QMenu") as MQ:
            self.view._show_context_menu(msg, QPoint(0, 0))
        submenu = MQ.return_value.addMenu.return_value
        action = submenu.addAction.return_value
        callbacks = [c.args[0]
                     for c in action.triggered.connect.call_args_list]
        for cb in callbacks:
            cb(False)
        self.assertEqual([k for _, k in got],
                         ["translate", "explain", "todos", "proofread", "reply"])
        self.assertTrue(all(m is msg for m, _ in got))

    def test_e2e_room_still_shows_ai_menu(self):
        self.view.set_ai_available(True)
        self.view.set_room_encrypted(True)
        with mock.patch("ui.chat_view.QMenu") as MQ:
            self.view._show_context_menu(_msg(), QPoint(0, 0))
        self.assertIn("AI 助手", self._menu_titles(MQ))


class ChatInputAITest(unittest.TestCase):
    def setUp(self):
        self.input = ChatInput()

    def tearDown(self):
        self.input.deleteLater()

    def test_ai_button_visibility(self):
        self.assertTrue(self.input.ai_btn.isHidden())
        self.input.set_ai_available(True)
        self.assertFalse(self.input.ai_btn.isHidden())
        self.input.set_ai_available(False)
        self.assertTrue(self.input.ai_btn.isHidden())

    def test_ai_menu_emits_actions(self):
        self.input.set_ai_available(True)
        got = []
        self.input.ai_assist_requested.connect(got.append)
        with mock.patch("ui.chat_input.QMenu") as MQ:
            MQ.return_value.sizeHint.return_value.height.return_value = 120
            self.input._show_ai_menu()
        menu = MQ.return_value
        labels = [c.args[0] for c in menu.addAction.call_args_list]
        self.assertEqual(labels,
                         ["润色", "改写", "精简", "根据最近聊天起草回复"])
        action = menu.addAction.return_value
        for c in action.triggered.connect.call_args_list:
            c.args[0](False)
        self.assertEqual(got, ["polish", "rewrite", "shorten", "reply_draft"])

    def test_ai_menu_ignored_when_disabled(self):
        with mock.patch("ui.chat_input.QMenu") as MQ:
            self.input._show_ai_menu()
        MQ.assert_not_called()

    def test_replace_text_does_not_send(self):
        sent = []
        self.input.send_text.connect(sent.append)
        self.input.input.setPlainText("原文")
        self.input.replace_text("润色后的文本")
        self.assertEqual(self.input.input.toPlainText(), "润色后的文本")
        self.assertEqual(sent, [])

    def test_append_text_adds_newline_between_content(self):
        sent = []
        self.input.send_text.connect(sent.append)
        self.input.input.setPlainText("原有内容")
        self.input.input.moveCursor(QTextCursor.End)
        self.input.append_text("追加内容")
        self.assertEqual(self.input.input.toPlainText(),
                         "原有内容\n追加内容")
        self.assertEqual(sent, [])

    def test_append_text_into_empty_input(self):
        self.input.append_text("回复草稿")
        self.assertEqual(self.input.input.toPlainText(), "回复草稿")

    def test_has_media_content_false_for_text(self):
        self.input.input.setPlainText("纯文字")
        self.assertFalse(self.input.has_media_content())


class _NoLogin(MainWindow):
    def _try_auto_login(self):
        self.auto_login_skipped = True


class MainWindowAIActionTest(unittest.TestCase):
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

    def tearDown(self):
        self.win.realtime and self.win.realtime.stop()
        self.win.deleteLater()
        (config.CONFIG_DIR, config.CONFIG_FILE,
         config.KEY_FILE, config.LEGACY_INI_FILE) = self._orig
        self._tmp.cleanup()

    def test_entries_hidden_by_default(self):
        self.assertFalse(self.win.chat_area.chat_view._ai_available)
        self.assertTrue(self.win.chat_area.input.ai_btn.isHidden())

    def test_enabling_ai_shows_entries_immediately(self):
        self.win.settings_page.llm_enabled_switch.setChecked(True)
        self.assertTrue(self.win.chat_area.chat_view._ai_available)
        self.assertFalse(self.win.chat_area.input.ai_btn.isHidden())
        self.win.settings_page.llm_enabled_switch.setChecked(False)
        self.assertFalse(self.win.chat_area.chat_view._ai_available)
        self.assertTrue(self.win.chat_area.input.ai_btn.isHidden())

    def test_message_action_gate_denied_does_not_open_dialog(self):
        with mock.patch.object(self.win, "_ensure_ai_ready",
                               return_value=False), \
                mock.patch("ui.main_window.run_ai_dialog") as m_dlg:
            self.win._on_ai_message_action(_msg(), "translate")
        m_dlg.assert_not_called()

    def test_message_action_appends_result(self):
        self.win.chat_area.input.input.setPlainText("已有内容")
        self.win.chat_area.input.input.moveCursor(QTextCursor.End)
        with mock.patch.object(self.win, "_ensure_ai_ready",
                               return_value=True), \
                mock.patch("ui.main_window.run_ai_dialog",
                           return_value="译文") as m_dlg:
            self.win._on_ai_message_action(_msg(), "translate")
        m_dlg.assert_called_once()
        self.assertEqual(m_dlg.call_args.args[2], "AI 翻译")
        self.assertEqual(self.win.chat_area.input.input.toPlainText(),
                         "已有内容\n译文")

    def test_empty_message_notifies(self):
        with mock.patch.object(self.win, "_ensure_ai_ready",
                               return_value=True), \
                mock.patch("ui.main_window.run_ai_dialog") as m_dlg, \
                mock.patch("ui.main_window.notify") as m_notify:
            self.win._on_ai_message_action(_msg(text="  "), "translate")
        m_notify.assert_called_once()
        m_dlg.assert_not_called()

    def test_assist_replaces_input(self):
        self.win.chat_area.input.input.setPlainText("原文")
        with mock.patch.object(self.win, "_ensure_ai_ready",
                               return_value=True), \
                mock.patch("ui.main_window.run_ai_dialog",
                           return_value="润色后"):
            self.win._on_ai_assist("polish")
        self.assertEqual(self.win.chat_area.input.input.toPlainText(),
                         "润色后")

    def test_assist_empty_input_notifies(self):
        with mock.patch.object(self.win, "_ensure_ai_ready",
                               return_value=True), \
                mock.patch("ui.main_window.run_ai_dialog") as m_dlg, \
                mock.patch("ui.main_window.notify") as m_notify:
            self.win._on_ai_assist("rewrite")
        m_notify.assert_called_once()
        m_dlg.assert_not_called()

    def test_assist_media_input_notifies(self):
        self.win.chat_area.input.input.setPlainText("文字")
        with mock.patch.object(self.win, "_ensure_ai_ready",
                               return_value=True), \
                mock.patch.object(self.win.chat_area.input,
                                  "has_media_content", return_value=True), \
                mock.patch("ui.main_window.run_ai_dialog") as m_dlg, \
                mock.patch("ui.main_window.notify") as m_notify:
            self.win._on_ai_assist("shorten")
        m_notify.assert_called_once()
        m_dlg.assert_not_called()

    def test_assist_reply_draft_requires_room(self):
        self.win._active_room_id = None
        with mock.patch.object(self.win, "_ensure_ai_ready",
                               return_value=True), \
                mock.patch("ui.main_window.run_ai_dialog") as m_dlg, \
                mock.patch("ui.main_window.notify") as m_notify:
            self.win._on_ai_assist("reply_draft")
        m_notify.assert_called_once()
        m_dlg.assert_not_called()

    def test_assist_reply_draft_appends_result(self):
        self.win._active_room_id = "r1"
        self.win.client = mock.Mock()
        self.win._conversations = {}
        with mock.patch.object(self.win, "_ensure_ai_ready",
                               return_value=True), \
                mock.patch("ui.main_window.run_ai_dialog",
                           return_value="好的，我来处理") as m_dlg:
            self.win._on_ai_assist("reply_draft")
        m_dlg.assert_called_once()
        self.assertEqual(m_dlg.call_args.args[2], "AI 起草回复")
        self.assertEqual(self.win.chat_area.input.input.toPlainText(),
                         "好的，我来处理")

    def test_draft_reply_job_factory_is_callable_without_args(self):
        """真机踩到的：起草回复把 job 本身当工厂传给对话框 → 「missing worker」。

        AIDialog 的契约是 job_factory() → job(worker)，重试要靠工厂重新取
        上下文，所以三个入口传的第 4 个实参都必须能零参调用。
        """
        import inspect
        self.win._active_room_id = "r1"
        self.win.client = mock.Mock()
        self.win.client.me.username = "me"
        self.win._conversations = {}
        captured = []
        with mock.patch.object(self.win, "_ensure_ai_ready", return_value=True), \
                mock.patch("ui.main_window.run_ai_dialog",
                           side_effect=lambda *a, **k: captured.append(a)):
            self.win._on_ai_message_action(_msg(text="帮我看看"), "translate")
            self.win.chat_area.input.input.setPlainText("原文")
            self.win._on_ai_assist("polish")
            self.win._on_ai_assist("reply_draft")
        self.assertEqual(len(captured), 3)
        for args in captured:
            factory = args[3]
            job = factory()                       # 零参可调 = 工厂
            self.assertTrue(callable(job))
            self.assertEqual(len(inspect.signature(job).parameters), 1)

    def test_draft_reply_job_collects_and_returns_text(self):
        """起草回复的 job 真跑一遍：采集 → 提示词 → 模型 → 关闭客户端。"""
        from core.ai_context import RoomContext
        self.win._active_room_id = "r1"
        self.win.client = mock.Mock()
        self.win.client.me.username = "me"
        self.win._conversations = {}
        ctx = RoomContext(messages=[mock.Mock()], text="聊天内容",
                          coverage="最近 1 条", scanned=1)
        ai = mock.Mock()
        ai.chat.return_value = "草稿内容"
        worker = mock.Mock()
        worker.cancel_event = threading.Event()
        with mock.patch.object(self.win, "_ensure_ai_ready", return_value=True), \
                mock.patch("core.ai_context.collect_room_context",
                           return_value=ctx), \
                mock.patch.object(self.win, "_ai_client", return_value=ai), \
                mock.patch("ui.main_window.run_ai_dialog") as m_dlg:
            self.win._on_ai_assist("reply_draft")
            # job 必须在 patch 生效期间跑：_job 内部是函数级 import
            self.assertEqual(m_dlg.call_args.args[3]()(worker), "草稿内容")
        ai.close.assert_called_once()

    def test_ai_job_builds_client_and_closes(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            def chat(self, messages, **kwargs):
                self.calls.append((messages, kwargs))
                kwargs["on_delta"]("块")
                return "完整结果"

            def close(self):
                self.closed = True

        class Worker:
            def __init__(self):
                self.cancel_event = threading.Event()
                self.deltas = []
                self.statuses = []

            def emit_delta(self, text):
                self.deltas.append(text)

            def emit_status(self, text):
                self.statuses.append(text)

        fake = FakeClient()
        worker = Worker()
        messages = [{"role": "user", "content": "hi"}]
        with mock.patch.object(self.win, "_ai_client", return_value=fake):
            job = self.win._ai_job(messages)
            result = job(worker)
        self.assertEqual(result, "完整结果")
        self.assertEqual(fake.calls[0][0], messages)
        self.assertIs(fake.calls[0][1]["cancel"], worker.cancel_event)
        self.assertEqual(worker.deltas, ["块"])
        self.assertTrue(worker.statuses)
        self.assertTrue(fake.closed)


if __name__ == "__main__":
    unittest.main()
