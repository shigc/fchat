"""AI 自然语言历史搜索：面板按钮、关键词扫描、主窗口任务接线。

只把查询词上传给模型扩写，聊天正文留在本机全历史扫描。
offscreen；模型与历史接口一律 mock，不联网。
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
from core.models import Message, User  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
import ui.room_panels as room_panels_module  # noqa: E402
from ui.room_panels import MessagesPanel  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def sync_run_async(fn, on_done=None, on_fail=None, parent=None):
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


def _msg(mid, text):
    return Message(id=mid, room_id="r1", text=text,
                   sender=User(username="zs", name="张三"),
                   timestamp=1700000000.0)


# --------------------------------------------------------------------- #
# MessagesPanel：AI 搜索入口与关键词扫描
# --------------------------------------------------------------------- #
class MessagesPanelAISearchTest(unittest.TestCase):
    def setUp(self):
        self.panel = MessagesPanel(None)
        self.panel._client = mock.Mock()
        self.panel._rid = "r1"
        self.panel._room_type = "c"
        self.addCleanup(self.panel.deleteLater)
        ctx = mock.patch.object(room_panels_module, "run_async",
                                sync_run_async)
        ctx.start()
        self.addCleanup(ctx.stop)

    def tearDown(self):
        self.panel._debounce.stop()

    def _set_text(self, text):
        self.panel.search_input.setText(text)
        self.panel._debounce.stop()

    def test_ai_button_present(self):
        self.assertEqual(self.panel.ai_search_btn.text(), "AI 搜索")
        self.assertTrue(self.panel.ai_search_btn.isEnabled())

    def test_empty_query_hint_without_emit(self):
        got = []
        self.panel.ai_search_requested.connect(got.append)
        self.panel._on_ai_search_clicked()
        self.assertEqual(got, [])
        self.assertEqual(self.panel.hint_label.text(), "请先输入要搜索的内容")
        self.assertFalse(self.panel._ai_busy)

    def test_click_trims_and_emits_with_busy_state(self):
        got = []
        self.panel.ai_search_requested.connect(got.append)
        self._set_text("  最近谁提过超时问题  ")
        self.panel._on_ai_search_clicked()
        self.assertEqual(got, ["最近谁提过超时问题"])
        self.assertTrue(self.panel._ai_busy)
        self.assertFalse(self.panel.ai_search_btn.isEnabled())
        self.assertEqual(self.panel.ai_search_btn.text(), "理解中…")
        self.assertEqual(self.panel.hint_label.text(), "正在理解查询…")

    def test_busy_click_ignored(self):
        got = []
        self.panel.ai_search_requested.connect(got.append)
        self._set_text("查询")
        self.panel._on_ai_search_clicked()
        self.panel._on_ai_search_clicked()
        self.assertEqual(got, ["查询"])

    def test_apply_keywords_or_scan(self):
        page = [_msg("m1", "请求超时了"), _msg("m2", "报错信息"),
                _msg("m3", "无关内容")]
        self.panel._client.get_history.return_value = page
        self.panel.set_ai_search_busy(True)
        old_token = self.panel._scan_token
        self.panel.apply_ai_keywords(["超时", "报错"])
        self.assertFalse(self.panel._ai_busy)
        self.assertGreater(self.panel._scan_token, old_token)
        self.assertEqual(self.panel._ai_keywords, ["超时", "报错"])
        hint = self.panel.hint_label.text()
        self.assertIn("AI 关键词：超时、报错", hint)
        self.assertIn("已扫描全部 3 条", hint)
        self.assertIn("命中 2 条", hint)
        drain_events()
        self.assertEqual(self.panel.search_list.count(), 2)

    def test_apply_keywords_escapes_regex_chars(self):
        page = [_msg("m1", "a.b 字面"), _msg("m2", "aXb 不是")]
        self.panel._client.get_history.return_value = page
        self.panel.apply_ai_keywords(["a.b"])
        drain_events()
        self.assertEqual(self.panel.search_list.count(), 1)
        self.assertIn("命中 1 条", self.panel.hint_label.text())

    def test_apply_empty_keywords_no_scan(self):
        with mock.patch.object(self.panel, "_regex_search") as scan:
            self.panel.apply_ai_keywords([])
        scan.assert_not_called()
        self.assertEqual(self.panel.hint_label.text(), "AI 未返回可用关键词")
        self.assertFalse(self.panel._ai_busy)

    def test_fail_ai_search_resets_busy_and_shows_error(self):
        self.panel.set_ai_search_busy(True)
        self.panel.fail_ai_search("401 未授权")
        self.assertFalse(self.panel._ai_busy)
        self.assertTrue(self.panel.ai_search_btn.isEnabled())
        self.assertIn("AI 搜索失败：401 未授权", self.panel.hint_label.text())

    def test_cancel_ai_search_clears_pending_hint(self):
        self.panel.set_ai_search_busy(True)
        self.panel.hint_label.setText("正在理解查询…")
        self.panel.cancel_ai_search()
        self.assertFalse(self.panel._ai_busy)
        self.assertEqual(self.panel.hint_label.text(), "")

    def test_cancel_keeps_scan_progress_hint(self):
        self.panel.set_ai_search_busy(True)
        self.panel.hint_label.setText("AI 关键词：x · 已扫描 10 条 · 命中 1 条")
        self.panel.cancel_ai_search()
        self.assertIn("已扫描 10 条", self.panel.hint_label.text())

    def test_editing_query_exits_keyword_mode(self):
        self.panel._client.get_history.return_value = []
        self.panel.apply_ai_keywords(["超时"])
        self.assertEqual(self.panel._ai_keywords, ["超时"])
        self.panel.search_input.setText("新的查询")
        self.assertEqual(self.panel._ai_keywords, [])
        self.assertTrue(self.panel._debounce.isActive())
        self.panel._debounce.stop()

    def test_on_open_resets_ai_state(self):
        self.panel._ai_keywords = ["旧"]
        self.panel.set_ai_search_busy(True)
        self.panel._on_open()
        self.assertEqual(self.panel._ai_keywords, [])
        self.assertFalse(self.panel._ai_busy)
        self.assertEqual(self.panel.hint_label.text(), "")
        self.assertTrue(self.panel.ai_search_btn.isEnabled())
        self.panel._debounce.stop()

    def test_on_closed_resets_busy(self):
        self.panel._ai_keywords = ["旧"]
        self.panel.set_ai_search_busy(True)
        self.panel._on_closed()
        self.assertEqual(self.panel._ai_keywords, [])
        self.assertFalse(self.panel._ai_busy)


# --------------------------------------------------------------------- #
# MainWindow：AI 搜索任务接线
# --------------------------------------------------------------------- #
class _FakeTask:
    instances = []

    def __init__(self, job, parent=None):
        self.job = job
        self.parent = parent
        self.delta = mock.Mock()
        self.status = mock.Mock()
        self.done = mock.Mock()
        self.failed = mock.Mock()
        self.cancelled = mock.Mock()
        self.cancel_called = False
        _FakeTask.instances.append(self)

    def cancel(self):
        self.cancel_called = True


class _NoLogin(MainWindow):
    def _try_auto_login(self):
        self.auto_login_skipped = True


class MainWindowAISearchTest(unittest.TestCase):
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
        self.win._active_room_id = "r1"
        self.win.client = mock.Mock()
        self.win.client.me.username = "me"
        self.panel = self.win._room_panels.messages
        _FakeTask.instances = []

    def tearDown(self):
        self.win.realtime and self.win.realtime.stop()
        self.win.deleteLater()
        (config.CONFIG_DIR, config.CONFIG_FILE,
         config.KEY_FILE, config.LEGACY_INI_FILE) = self._orig
        self._tmp.cleanup()

    def _request(self, query="谁提过超时"):
        _FakeTask.instances = []
        with mock.patch.object(self.win, "_ensure_ai_ready",
                               return_value=True), \
                mock.patch("ui.ai_worker.AITask", _FakeTask):
            self.win._on_ai_search_requested(query)
        return _FakeTask.instances[-1] if _FakeTask.instances else None

    def test_gate_denied_cancels_without_task(self):
        _FakeTask.instances = []
        with mock.patch.object(self.win, "_ensure_ai_ready",
                               return_value=False), \
                mock.patch.object(self.panel, "cancel_ai_search") as cancel, \
                mock.patch("ui.ai_worker.AITask", _FakeTask):
            self.win._on_ai_search_requested("查询")
        self.assertEqual(_FakeTask.instances, [])
        cancel.assert_called_once()

    def test_no_active_room_cancels_without_task(self):
        self.win._active_room_id = ""
        with mock.patch.object(self.panel, "cancel_ai_search") as cancel, \
                mock.patch("ui.ai_worker.AITask", _FakeTask):
            self.win._on_ai_search_requested("查询")
        self.assertEqual(_FakeTask.instances, [])
        cancel.assert_called_once()

    def test_empty_query_ignored(self):
        with mock.patch.object(self.panel, "cancel_ai_search") as cancel, \
                mock.patch("ui.ai_worker.AITask", _FakeTask):
            self.win._on_ai_search_requested("   ")
        cancel.assert_called_once()
        self.assertEqual(_FakeTask.instances, [])

    def test_encrypted_flag_passed_to_gate(self):
        self.win.chat_area.chat_view._room_encrypted = True
        with mock.patch.object(self.win, "_ensure_ai_ready",
                               return_value=False) as gate, \
                mock.patch.object(self.panel, "cancel_ai_search"):
            self.win._on_ai_search_requested("查询")
        self.assertTrue(gate.call_args.kwargs["encrypted"])

    def test_allowed_starts_task_and_busy(self):
        task = self._request("  谁提过超时  ")
        self.assertIsNotNone(task)
        self.assertIs(self.win._ai_search_task, task)
        self.assertTrue(self.panel._ai_busy)
        self.assertFalse(self.panel.ai_search_btn.isEnabled())

    def test_job_uses_query_only_and_limits_tokens(self):
        task = self._request("谁提过超时")
        client = mock.Mock()
        client.chat.return_value = "超时\n报错"
        worker = mock.Mock()
        worker.cancel_event = mock.Mock()
        with mock.patch.object(self.win, "_ai_client", return_value=client):
            reply = task.job(worker)
        self.assertEqual(reply, "超时\n报错")
        messages = client.chat.call_args.args[0]
        self.assertIn("关键词", messages[0]["content"])
        # 只上传查询词，不带任何聊天正文
        self.assertEqual(messages[1]["content"], "谁提过超时")
        kwargs = client.chat.call_args.kwargs
        self.assertEqual(kwargs["temperature"], 0.2)
        self.assertEqual(kwargs["max_tokens"], 200)
        self.assertIs(kwargs["cancel"], worker.cancel_event)
        client.close.assert_called_once()

    def test_done_parses_keywords_and_applies(self):
        self._request()
        token = self.win._ai_search_token
        with mock.patch.object(self.panel, "apply_ai_keywords") as apply:
            self.win._on_ai_search_done("- 超时\n报错。\n\n超时", "原查询",
                                        token)
        apply.assert_called_once_with(["超时", "报错"])
        self.assertIsNone(self.win._ai_search_task)

    def test_done_empty_reply_falls_back_to_query(self):
        self._request()
        token = self.win._ai_search_token
        with mock.patch.object(self.panel, "apply_ai_keywords") as apply:
            self.win._on_ai_search_done("", "原始查询", token)
        apply.assert_called_once_with(["原始查询"])

    def test_stale_done_ignored(self):
        task = self._request()
        token = self.win._ai_search_token
        self.win._ai_search_token = token + 1
        with mock.patch.object(self.panel, "apply_ai_keywords") as apply:
            self.win._on_ai_search_done("超时", "q", token)
        apply.assert_not_called()
        self.assertIs(self.win._ai_search_task, task)

    def test_failed_shows_error(self):
        self._request()
        token = self.win._ai_search_token
        with mock.patch.object(self.panel, "fail_ai_search") as fail:
            self.win._on_ai_search_failed("连接超时", token)
        fail.assert_called_once_with("连接超时")
        self.assertIsNone(self.win._ai_search_task)

    def test_stale_failed_ignored(self):
        self._request()
        token = self.win._ai_search_token
        self.win._ai_search_token = token + 1
        with mock.patch.object(self.panel, "fail_ai_search") as fail:
            self.win._on_ai_search_failed("旧错误", token)
        fail.assert_not_called()

    def test_cancelled_resets_panel(self):
        self._request()
        token = self.win._ai_search_token
        with mock.patch.object(self.panel, "cancel_ai_search") as cancel:
            self.win._on_ai_search_cancelled(token)
        cancel.assert_called_once()
        self.assertIsNone(self.win._ai_search_task)

    def test_stale_cancelled_ignored(self):
        self._request()
        token = self.win._ai_search_token
        self.win._ai_search_token = token + 1
        with mock.patch.object(self.panel, "cancel_ai_search") as cancel:
            self.win._on_ai_search_cancelled(token)
        cancel.assert_not_called()

    def test_new_request_cancels_previous(self):
        first = self._request()
        second = self._request()
        self.assertTrue(first.cancel_called)
        self.assertIs(self.win._ai_search_task, second)

    def test_panel_closed_cancels_and_invalidates(self):
        task = self._request()
        token = self.win._ai_search_token
        self.win._on_panel_closed("messages")
        self.assertTrue(task.cancel_called)
        self.assertIsNone(self.win._ai_search_task)
        self.assertNotEqual(self.win._ai_search_token, token)

    def test_panel_close_without_task_only_invalidates(self):
        token = self.win._ai_search_token
        self.win._on_panel_closed("messages")
        self.assertIsNone(self.win._ai_search_task)
        self.assertNotEqual(self.win._ai_search_token, token)


if __name__ == "__main__":
    unittest.main()
