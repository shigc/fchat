"""AI 划重点面板：控件、生成态、流式回调、主窗口任务接线。

offscreen；模型与上下文采集一律 mock，不联网。
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

import config  # noqa: E402
from core.ai_context import RoomContext  # noqa: E402
from ui.ai_panel import AIPanel  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.theme import ThemeManager  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class _FakeTask:
    """替身 AITask：记录 job 与取消调用，信号用 mock。"""

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


class AIPanelTest(unittest.TestCase):
    def setUp(self):
        self.panel = AIPanel()

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()

    def test_range_options(self):
        data = [self.panel.range_combo.itemData(i)
                for i in range(self.panel.range_combo.count())]
        self.assertEqual(data, [100, 200, 500])
        self.assertEqual(self.panel.range_combo.currentData(), 200)

    def test_summary_click_emits_kind_and_limit(self):
        got = []
        self.panel.generate_requested.connect(lambda *a: got.append(a))
        self.panel.summary_btn.click()
        self.assertEqual(got, [("summary", 200)])

    def test_daily_weekly_clicks(self):
        got = []
        self.panel.generate_requested.connect(lambda *a: got.append(a))
        self.panel.daily_btn.click()
        self.panel.weekly_btn.click()
        self.assertEqual(got, [("daily", 200), ("weekly", 200)])

    def test_begin_generating_disables_controls(self):
        self.panel.begin_generating(500)
        self.assertTrue(self.panel._generating)
        self.assertFalse(self.panel.summary_btn.isEnabled())
        self.assertFalse(self.panel.range_combo.isEnabled())
        self.assertFalse(self.panel.stop_btn.isHidden())
        self.assertTrue(self.panel.retry_btn.isHidden())

    def test_stream_and_finish(self):
        self.panel.begin_generating(200)
        self.panel.append_delta("第一段")
        self.panel._flush()
        self.assertIn("第一段", self.panel.view.toPlainText())
        self.panel.finish("## 结论\n- 要点", "最近 2 条")
        self.assertFalse(self.panel._generating)
        self.assertIn("结论", self.panel.view.toPlainText())
        self.assertEqual(self.panel.coverage.text(), "最近 2 条")
        self.assertTrue(self.panel.copy_btn.isEnabled())
        self.assertTrue(self.panel.save_btn.isEnabled())
        self.assertFalse(self.panel.retry_btn.isHidden())

    def test_fail_shows_error_and_retry(self):
        self.panel.begin_generating(200)
        self.panel.fail("401 未授权")
        self.assertFalse(self.panel._generating)
        self.assertIn("401", self.panel.status.text())
        self.assertFalse(self.panel.retry_btn.isHidden())

    def test_cancelled_shows_stopped_and_keeps_partial(self):
        self.panel.begin_generating(200)
        self.panel.append_delta("半截结果")
        self.panel.cancelled()
        self.assertEqual(self.panel.status.text(), "已停止")
        self.assertIn("半截结果", self.panel.view.toPlainText())

    def _open(self, rid="r1"):
        btn = QPushButton("ai")
        self.addCleanup(btn.deleteLater)
        self.panel.open_for(rid, "c", "测试房间", btn)

    def test_reopen_same_room_clears_stale_generating_status(self):
        """重开面板不能骗人：无任务在途时状态行不许留着「正在生成…」。"""
        self._open()
        self.panel.begin_generating(200)
        self.panel.append_status("正在生成…")
        self.panel.append_delta("半截")
        self.panel.close()
        self._open()
        self.assertNotIn("正在", self.panel.status.text())
        self.assertTrue(self.panel.status.text())
        self.assertIn("半截", self.panel.view.toPlainText())

    def test_reopen_with_running_task_resumes_generating(self):
        """关面板后任务继续跑：重开要恢复生成态而不是显示「已停止」。"""
        self._open()
        self.panel.begin_generating(200)
        self.panel.append_status("正在生成…")
        self.panel.close()
        self.panel.set_task_running(True)
        self._open()
        self.assertTrue(self.panel._generating)
        self.assertFalse(self.panel.stop_btn.isHidden())
        self.assertTrue(self.panel._flush_timer.isActive())
        self.assertTrue(self.panel.status.text().startswith("正在"))

    def test_reopen_other_room_resets(self):
        self._open("r1")
        self.panel.begin_generating(200)
        self.panel.append_status("正在生成…")
        self.panel.close()
        self._open("r2")
        self.assertEqual(self.panel.status.text(), "")
        self.assertEqual(self.panel.coverage.text(), "范围：未生成")

    def test_stop_button_emits(self):
        got = []
        self.panel.stop_requested.connect(lambda: got.append(True))
        self.panel.stop_btn.click()
        self.assertEqual(got, [True])

    def test_retry_reuses_last_kind_and_limit(self):
        got = []
        self.panel.generate_requested.connect(lambda *a: got.append(a))
        self.panel.range_combo.setCurrentIndex(2)   # 500
        self.panel.weekly_btn.click()
        self.panel.finish("结果")
        self.panel.retry_btn.click()
        self.assertEqual(got, [("weekly", 500), ("weekly", 500)])

    def test_encrypted_warning(self):
        self.assertTrue(self.panel.warn.isHidden())
        self.panel.set_encrypted(True)
        self.assertFalse(self.panel.warn.isHidden())
        self.panel.set_encrypted(False)
        self.assertTrue(self.panel.warn.isHidden())

    def test_save_button_emits_text(self):
        got = []
        self.panel.save_requested.connect(got.append)
        self.panel.finish("保存内容")
        self.panel.save_btn.click()
        self.assertEqual(got, ["保存内容"])

    def test_theme_change_rerenders_finished_text(self):
        self.panel.finish("结论内容")
        self.panel.set_theme(ThemeManager())
        self.assertIn("结论内容", self.panel.view.toPlainText())


class _NoLogin(MainWindow):
    def _try_auto_login(self):
        self.auto_login_skipped = True


class MainWindowAIPanelTest(unittest.TestCase):
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
        _FakeTask.instances = []

    def tearDown(self):
        self.win.realtime and self.win.realtime.stop()
        self.win.deleteLater()
        (config.CONFIG_DIR, config.CONFIG_FILE,
         config.KEY_FILE, config.LEGACY_INI_FILE) = self._orig
        self._tmp.cleanup()

    def _generate(self, kind="summary", limit=200):
        _FakeTask.instances = []
        with mock.patch.object(self.win, "_ensure_ai_ready",
                               return_value=True), \
                mock.patch("ui.ai_worker.AITask", _FakeTask):
            self.win._on_ai_panel_generate(kind, limit)
        return _FakeTask.instances[-1] if _FakeTask.instances else None

    def test_chat_area_has_ai_button_and_visibility(self):
        self.assertIn("ai", self.win.chat_area.panel_buttons)
        self.win.chat_area.set_ai_available(False)
        self.assertTrue(self.win.chat_area.panel_buttons["ai"].isHidden())
        self.win.chat_area.set_ai_available(True)
        self.assertFalse(self.win.chat_area.panel_buttons["ai"].isHidden())

    def test_gate_denied_does_not_start(self):
        with mock.patch.object(self.win, "_ensure_ai_ready",
                               return_value=False), \
                mock.patch("ui.ai_worker.AITask", _FakeTask):
            self.win._on_ai_panel_generate("summary", 200)
        self.assertEqual(_FakeTask.instances, [])
        self.assertFalse(self.win._room_panels.panel("ai")._generating)

    def test_generate_starts_task_and_panel(self):
        task = self._generate("summary", 200)
        self.assertIsNotNone(task)
        self.assertIs(self.win._ai_panel_task, task)
        self.assertTrue(self.win._room_panels.panel("ai")._generating)

    def _run_job(self, kind, limit):
        task = self._generate(kind, limit)
        client = mock.Mock()
        client.chat.return_value = "生成结果"
        worker = mock.Mock()
        worker.cancel_event = mock.Mock()
        ctx = RoomContext(messages=[mock.Mock()], text="聊天内容",
                          coverage="最近 1 条", scanned=1)
        with mock.patch("core.ai_context.collect_room_context",
                        return_value=ctx) as m_collect, \
                mock.patch.object(self.win, "_ai_client", return_value=client):
            result = task.job(worker)
        return result, m_collect.call_args, client.chat.call_args, client

    def test_job_summary_prompt_and_result(self):
        result, call, chat_call, client = self._run_job("summary", 200)
        self.assertEqual(result["text"], "生成结果")
        self.assertEqual(result["coverage"], "最近 1 条")
        self.assertEqual(call.kwargs["limit"], 200)
        self.assertEqual(call.kwargs["since_ts"], 0.0)
        prompt = chat_call.args[0][1]["content"]
        self.assertIn("讨论主题", prompt)
        client.close.assert_called_once()

    def test_job_daily_sets_today_window(self):
        _, call, chat_call, _ = self._run_job("daily", 100)
        self.assertGreater(call.kwargs["since_ts"], 0)
        self.assertIn("日报", chat_call.args[0][1]["content"])

    def test_job_weekly_sets_week_window(self):
        _, call, chat_call, _ = self._run_job("weekly", 500)
        self.assertGreater(call.kwargs["since_ts"], 0)
        self.assertIn("周报", chat_call.args[0][1]["content"])

    def test_done_updates_panel(self):
        self._generate()
        token = self.win._ai_panel_token
        self.win._on_ai_panel_done(
            {"text": "## 要点", "coverage": "最近 5 条"}, token)
        panel = self.win._room_panels.panel("ai")
        self.assertIn("要点", panel.view.toPlainText())
        self.assertEqual(panel.coverage.text(), "最近 5 条")
        self.assertIsNone(self.win._ai_panel_task)

    def test_stale_done_ignored(self):
        self._generate()
        token = self.win._ai_panel_token
        self.win._ai_panel_token = token + 1
        self.win._on_ai_panel_done({"text": "旧结果"}, token)
        panel = self.win._room_panels.panel("ai")
        self.assertNotIn("旧结果", panel.view.toPlainText())

    def test_stop_cancels_task(self):
        task = self._generate()
        self.win._on_ai_panel_stop()
        self.assertTrue(task.cancel_called)

    def test_panel_closed_keeps_task_running(self):
        """关面板不再取消在途生成：任务后台继续，token 不动。"""
        task = self._generate()
        token = self.win._ai_panel_token
        self.win._on_panel_closed("ai")
        self.assertFalse(task.cancel_called)
        self.assertIs(self.win._ai_panel_task, task)
        self.assertEqual(self.win._ai_panel_token, token)
        self.assertTrue(
            self.win._room_panels.panel("ai")._task_running)

    def test_done_while_closed_lands_panel_state(self):
        self._generate()
        token = self.win._ai_panel_token
        self.win._on_panel_closed("ai")
        self.win._on_ai_panel_done(
            {"text": "## 后台完成", "coverage": "最近 3 条"}, token)
        panel = self.win._room_panels.panel("ai")
        self.assertIsNone(self.win._ai_panel_task)
        self.assertFalse(panel._task_running)
        self.assertFalse(panel._generating)
        self.assertIn("后台完成", panel.view.toPlainText())
        self.assertEqual(panel.coverage.text(), "最近 3 条")

    def test_room_switch_cancels_task(self):
        """换房间后在途任务的结果无处安放：切房即取消。"""
        task = self._generate()
        self.assertIs(self.win._ai_panel_rid, "r1")
        self.win._cancel_ai_task_if_room_changed("r1")
        self.assertFalse(task.cancel_called)
        self.win._cancel_ai_task_if_room_changed("r2")
        self.assertTrue(task.cancel_called)
        self.assertIsNone(self.win._ai_panel_task)

    def test_room_switch_cancel_lands_panel_state(self):
        self._generate()
        self.win._cancel_ai_task_if_room_changed("r2")
        QApplication.processEvents()
        panel = self.win._room_panels.panel("ai")
        # token 未递增，worker 自己的 cancelled 回调照常落定面板；
        # _FakeTask 不会自发发 cancelled，这里直接驱动主窗口回调链
        self.win._on_ai_panel_cancelled(self.win._ai_panel_token)
        self.assertFalse(panel._task_running)
        self.assertFalse(panel._generating)

    def _generate_panel_flow(self, kind="summary", limit=200):
        """真实开面板 + 真实 AITask：用来验证跨线程状态收尾。

        补丁用 start/addCleanup 挂到用例结束：任务在后台线程异步执行，
        with 块退出时 job 可能还没跑到，mock 必须在等待期间仍然有效。
        """
        btn = QPushButton("ai")
        self.addCleanup(btn.deleteLater)
        panel = self.win._room_panels.panel("ai")
        panel.set_session(self.win.client, None)
        panel.open_for("r1", "c", "测试房间", btn)
        p_ready = mock.patch.object(self.win, "_ensure_ai_ready",
                                    return_value=True)
        p_collect = mock.patch("core.ai_context.collect_room_context")
        p_client = mock.patch.object(self.win, "_ai_client")
        p_ready.start()
        m_collect = p_collect.start()
        m_client = p_client.start()
        for p in (p_ready, p_collect, p_client):
            self.addCleanup(p.stop)
        m_collect.return_value = RoomContext(
            messages=[mock.Mock()], text="聊天内容",
            coverage="最近 1 条", scanned=1)
        m_client.return_value.chat.return_value = "后台生成结果"
        self.win._on_ai_panel_generate(kind, limit)
        return panel

    def test_generation_continues_after_panel_closed(self):
        """关面板后任务后台继续：完成回调照常落定，重开直接看到结果。"""
        panel = self._generate_panel_flow()
        self.assertTrue(panel._generating)
        panel.close()                                  # → closed("ai")
        self.assertIsNotNone(self.win._ai_panel_task)
        started = time.monotonic()
        while self.win._ai_panel_task is not None and \
                time.monotonic() - started < 2.0:
            QApplication.processEvents()
            time.sleep(0.005)
        self.assertIsNone(self.win._ai_panel_task)
        panel.open_for("r1", "c", "测试房间", QPushButton("ai"))
        self.assertFalse(panel._generating)
        self.assertIn("后台生成结果", panel.view.toPlainText())
        self.assertTrue(panel.retry_btn.isVisible())

    def test_new_generation_cancels_previous(self):
        first = self._generate()
        second = self._generate()
        self.assertTrue(first.cancel_called)
        self.assertIs(self.win._ai_panel_task, second)


if __name__ == "__main__":
    unittest.main()
