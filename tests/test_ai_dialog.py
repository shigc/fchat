"""ui.ai_dialog 测试：流式渲染、完成/失败/停止/重试、复制/插入、关闭取消。

offscreen；任务函数为本地假实现，不联网。
"""
import os
import sys
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication, QDialog  # noqa: E402

from core.ai_client import AICancelled  # noqa: E402
from ui.ai_dialog import AIDialog  # noqa: E402
from ui.theme import ThemeManager  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def spin_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _APP.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    _APP.processEvents()
    return predicate()


def make_dialog(job_factory, note="", allow_insert=True):
    return AIDialog(None, ThemeManager(), "AI 测试", job_factory,
                    context_note=note, allow_insert=allow_insert)


class AIDialogTest(unittest.TestCase):
    def test_streams_then_renders_markdown(self):
        def job(worker):
            worker.emit_status("最近 10 条")
            for chunk in ("## 标题\n", "- 第一项\n", "- 第二项"):
                worker.emit_delta(chunk)
                time.sleep(0.01)
            return {"text": "## 标题\n- 第一项\n- 第二项",
                    "coverage": "最近 10 条（10-20 10:00 ~ 10:05）"}

        dlg = make_dialog(lambda: job, note="上下文说明")
        dlg.start()
        self.assertTrue(spin_until(lambda: dlg.raw_text.startswith("## 标题")))
        self.assertTrue(spin_until(lambda: "完成" in dlg._status.text()))
        self.assertEqual(dlg._note.text(), "最近 10 条（10-20 10:00 ~ 10:05）")
        self.assertTrue(dlg._stop_btn.isHidden())
        self.assertFalse(dlg._retry_btn.isHidden())
        html = dlg._view.toHtml()
        self.assertIn("标题", html)
        self.assertIn("第一项", html)
        self.assertTrue(dlg._copy_btn.isEnabled())
        self.assertTrue(dlg._insert_btn.isEnabled())
        dlg.deleteLater()

    def test_status_updates_do_not_overwrite_context_note(self):
        def job(worker):
            worker.emit_status("正在请求模型…")
            return "译好的内容"

        note = "来源：小明 的一条消息；内容仅用于本次请求。"
        dlg = make_dialog(lambda: job, note=note)
        dlg.start()
        self.assertTrue(spin_until(lambda: "完成" in dlg._status.text()))
        self.assertEqual(dlg._note.text(), note)
        self.assertEqual(dlg.raw_text, "译好的内容")
        dlg.deleteLater()

    def test_failure_shows_error_and_retry_recovers(self):
        calls = {"n": 0}

        def factory():
            calls["n"] += 1
            if calls["n"] == 1:
                def fail(_worker):
                    raise RuntimeError("网络不可达")
                return fail

            def ok(_worker):
                return "第二次成功"
            return ok

        dlg = make_dialog(factory)
        dlg.start()
        self.assertTrue(spin_until(
            lambda: "生成失败" in dlg._status.text()))
        self.assertIn("网络不可达", dlg._status.text())
        self.assertTrue(dlg._stop_btn.isHidden())
        dlg._retry_btn.click()
        self.assertTrue(spin_until(
            lambda: "第二次成功" in dlg.raw_text))
        self.assertTrue(spin_until(lambda: "完成" in dlg._status.text()))
        self.assertEqual(calls["n"], 2)
        dlg.deleteLater()

    def test_stop_keeps_partial_text(self):
        def job(worker):
            worker.emit_delta("已经生成的部分")
            for _ in range(500):
                if worker.is_cancelled():
                    raise AICancelled("已取消")
                time.sleep(0.01)
            return "不会到这里"

        dlg = make_dialog(lambda: job)
        dlg.start()
        self.assertTrue(spin_until(lambda: bool(dlg._raw)))
        dlg._stop_btn.click()
        self.assertTrue(spin_until(lambda: "已停止" in dlg._status.text()))
        self.assertIn("已经生成的部分", dlg.raw_text)
        self.assertEqual(dlg._note.text(), "")  # 无上下文说明
        self.assertTrue(dlg._copy_btn.isEnabled())
        dlg.deleteLater()

    def test_copy_puts_text_on_clipboard(self):
        dlg = make_dialog(lambda: (lambda _w: "复制内容"))
        dlg.start()
        self.assertTrue(spin_until(lambda: dlg.raw_text == "复制内容"))
        dlg._copy_btn.click()
        self.assertEqual(QApplication.clipboard().text(), "复制内容")
        self.assertIn("已复制", dlg._status.text())
        dlg.deleteLater()

    def test_insert_returns_text_and_accepts(self):
        dlg = make_dialog(lambda: (lambda _w: "插入的文本"))
        dlg.start()
        self.assertTrue(spin_until(lambda: dlg.raw_text == "插入的文本"))
        dlg._insert_btn.click()
        self.assertEqual(dlg.inserted_text, "插入的文本")
        self.assertEqual(dlg.result(), QDialog.Accepted)
        dlg.deleteLater()

    def test_insert_hidden_when_disallowed(self):
        dlg = make_dialog(lambda: (lambda _w: "x"), allow_insert=False)
        dlg.start()
        self.assertTrue(spin_until(lambda: dlg.raw_text == "x"))
        self.assertTrue(dlg._insert_btn.isHidden())
        dlg._on_insert()
        self.assertIsNone(dlg.inserted_text)
        dlg.reject()
        dlg.deleteLater()

    def test_reject_cancels_running_task(self):
        def job(worker):
            for _ in range(500):
                if worker.is_cancelled():
                    raise AICancelled("已取消")
                time.sleep(0.01)
            return "late"

        dlg = make_dialog(lambda: job)
        dlg.start()
        task = dlg._task
        self.assertIsNotNone(task)
        dlg.reject()
        self.assertEqual(dlg.result(), QDialog.Rejected)
        self.assertTrue(task.is_cancelled())
        self.assertTrue(spin_until(task.is_finished))
        dlg.deleteLater()

    def test_empty_factory_failure_shows_error(self):
        dlg = make_dialog(lambda: None)
        dlg.start()
        self.assertTrue(spin_until(
            lambda: "生成失败" in dlg._status.text()))
        self.assertIn("未配置", dlg._status.text())
        dlg.deleteLater()

    def test_retry_ignored_while_running(self):
        def job(worker):
            time.sleep(0.15)
            return "done"

        dlg = make_dialog(lambda: job)
        dlg.start()
        task = dlg._task
        dlg._on_retry()
        self.assertIs(dlg._task, task)
        self.assertTrue(spin_until(lambda: "done" in dlg.raw_text))
        dlg.deleteLater()

    def test_theme_switch_rerenders_finished_text(self):
        dlg = make_dialog(lambda: (lambda _w: "**加粗**"))
        dlg.start()
        self.assertTrue(spin_until(lambda: "完成" in dlg._status.text()))
        theme = ThemeManager()
        theme.theme = "dark"
        dlg.set_theme(theme)
        self.assertIn("加粗", dlg._view.toPlainText())
        dlg.deleteLater()


if __name__ == "__main__":
    unittest.main()
