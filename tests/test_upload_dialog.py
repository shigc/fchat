"""上传进度弹窗测试。离线：worker 直接 run()（不起线程），client 全 mock。

弹窗 UI 槽测试把 _FileUploadWorker 换成 MagicMock，避免真起 QThread 发网络。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QProgressBar  # noqa: E402

from core.api_client import ApiError, UploadCancelled  # noqa: E402
from ui.upload_dialog import (  # noqa: E402
    FileUploadDialog, _FileUploadWorker, _fmt_size,
)

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class FmtSizeTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(_fmt_size(512), "512 B")
        self.assertEqual(_fmt_size(2048), "2.0 KB")
        self.assertEqual(_fmt_size(5 << 20), "5.0 MB")
        self.assertEqual(_fmt_size(3 << 30), "3.0 GB")


class WorkerRunTest(unittest.TestCase):
    """_FileUploadWorker.run() 同步执行，client.send_file 全 mock。"""

    def _run(self, worker):
        progress, ok, failed = [], [], []
        worker.progress.connect(lambda d, t: progress.append((d, t)))
        worker.finished_ok.connect(lambda: ok.append(True))
        worker.failed.connect(lambda e: failed.append(e))
        worker.run()
        return progress, ok, failed

    def test_success_emits_finished_ok_and_progress(self):
        client = mock.Mock()

        def send(rid, path, progress_cb=None, cancel=None):
            progress_cb(50, 100)
            progress_cb(100, 100)
            return True

        client.send_file.side_effect = send
        w = _FileUploadWorker(client, "r1", "/tmp/a.pdf", 100)
        progress, ok, failed = self._run(w)
        self.assertEqual(progress, [(50, 100), (100, 100)])
        self.assertEqual(ok, [True])
        self.assertEqual(failed, [])

    def test_passes_progress_cb_and_cancel_event_to_send_file(self):
        client = mock.Mock()
        client.send_file.return_value = True
        w = _FileUploadWorker(client, "r1", "/tmp/a.pdf", 100)
        self._run(w)
        kwargs = client.send_file.call_args.kwargs
        self.assertEqual(client.send_file.call_args.args, ("r1", "/tmp/a.pdf"))
        self.assertTrue(callable(kwargs["progress_cb"]))
        self.assertIs(kwargs["cancel"], w._cancel)

    def test_cancel_event_set_by_cancel(self):
        client = mock.Mock()
        client.send_file.return_value = True
        w = _FileUploadWorker(client, "r1", "/tmp/a.pdf", 100)
        self.assertFalse(w._cancel.is_set())
        w.cancel()
        self.assertTrue(w._cancel.is_set())

    def test_upload_cancelled_reports_cancelled(self):
        client = mock.Mock()
        client.send_file.side_effect = UploadCancelled("已取消")
        w = _FileUploadWorker(client, "r1", "/tmp/a.pdf", 100)
        _, ok, failed = self._run(w)
        self.assertEqual(ok, [])
        self.assertEqual(failed, ["已取消"])

    def test_api_error_reports_message(self):
        client = mock.Mock()
        client.send_file.side_effect = ApiError("boom")
        w = _FileUploadWorker(client, "r1", "/tmp/a.pdf", 100)
        _, ok, failed = self._run(w)
        self.assertEqual(ok, [])
        self.assertEqual(failed, ["boom"])

    def test_send_file_false_reports_failure(self):
        client = mock.Mock()
        client.send_file.return_value = False
        w = _FileUploadWorker(client, "r1", "/tmp/a.pdf", 100)
        _, ok, failed = self._run(w)
        self.assertEqual(ok, [])
        self.assertEqual(failed, ["发送失败"])

    def test_progress_total_falls_back_to_known(self):
        # core 回调 total 给 0 时，worker 用构造时的 known total 兜底
        client = mock.Mock()

        def send(rid, path, progress_cb=None, cancel=None):
            progress_cb(30, 0)
            return True

        client.send_file.side_effect = send
        w = _FileUploadWorker(client, "r1", "/tmp/a.pdf", 100)
        progress, _, _ = self._run(w)
        self.assertEqual(progress, [(30, 100)])


class DialogUiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "a.pdf")

    def tearDown(self):
        self._tmp.cleanup()

    def _make_dialog(self, theme=None, total=100):
        import ui.upload_dialog as ud
        patcher = mock.patch.object(ud, "_FileUploadWorker")
        patcher.start()
        self.addCleanup(patcher.stop)
        dlg = ud.FileUploadDialog(mock.Mock(), "r1", self.path, "a.pdf",
                                  total=total, theme=theme)
        self.addCleanup(self._close, dlg)
        return dlg

    def _close(self, dlg):
        try:
            dlg._cleanup()
        except Exception:  # noqa: BLE001
            pass
        dlg.deleteLater()

    def test_frameless_card_with_progress_bar(self):
        dlg = self._make_dialog()
        self.assertTrue(dlg.windowFlags() & Qt.FramelessWindowHint)
        self.assertIsInstance(dlg._bar, QProgressBar)
        self.assertEqual(dlg._bar.height(), 8)
        self.assertFalse(dlg._bar.isTextVisible())

    def test_dialog_is_modeless(self):
        dlg = self._make_dialog()
        self.assertFalse(dlg.isModal())
        self.assertEqual(dlg.windowModality(), Qt.NonModal)

    def test_dialog_is_independent_top_level(self):
        # 无 widget parent 的独立顶层窗口：切到主程序时能被压到主窗口之后
        dlg = self._make_dialog()
        self.assertIsNone(dlg.parentWidget())
        self.assertTrue(dlg.isWindow())

    def test_progress_known_total_sets_percent(self):
        dlg = self._make_dialog()
        dlg._on_progress(50, 100)
        self.assertEqual(dlg._bar.maximum(), 100)
        self.assertEqual(dlg._bar.value(), 50)
        self.assertIn("50%", dlg._status.text())
        self.assertIn("50 B", dlg._status.text())

    def test_progress_unknown_total_is_busy(self):
        dlg = self._make_dialog(total=0)
        dlg._on_progress(2048, 0)
        self.assertEqual(dlg._bar.maximum(), 0)   # 忙碌指示
        self.assertIn("已上传", dlg._status.text())

    def test_ok_marks_success_and_fills_bar(self):
        dlg = self._make_dialog()
        dlg._on_ok()
        self.assertTrue(dlg.ok)
        self.assertEqual(dlg._bar.value(), 100)
        self.assertTrue(dlg._closing)             # 成功后自动关窗

    def test_fail_shows_error_and_relabels_button(self):
        dlg = self._make_dialog()
        dlg._on_fail("boom")
        self.assertEqual(dlg.error, "boom")
        self.assertIn("发送失败", dlg._status.text())
        self.assertEqual(dlg._cancel_btn.text(), "关闭")
        self.assertFalse(dlg._closing)            # 失败保留窗口等用户关闭

    def test_user_cancel_closes_dialog(self):
        dlg = self._make_dialog()
        dlg._user_cancelled = True
        dlg._on_fail("已取消")
        self.assertTrue(dlg._closing)

    def test_force_close_cancels_and_hides_without_waiting(self):
        dlg = self._make_dialog()
        dlg.show()
        dlg.force_close()
        self.assertTrue(dlg._user_cancelled)
        dlg._worker.cancel.assert_called_once()
        self.assertTrue(dlg._closing)
        self.assertFalse(dlg.isVisible())


class DialogThemeTest(unittest.TestCase):
    def test_accent_from_theme_manager(self):
        from ui.theme import ThemeManager
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "a.pdf")
        import ui.upload_dialog as ud
        patcher = mock.patch.object(ud, "_FileUploadWorker")
        patcher.start()
        self.addCleanup(patcher.stop)
        theme = ThemeManager()
        dlg = ud.FileUploadDialog(mock.Mock(), "r1", path, "a.pdf",
                                  total=100, theme=theme)
        self.addCleanup(dlg.deleteLater)
        accent = getattr(theme, "accent", None)
        self.assertIsNotNone(accent)
        self.assertIn(accent.lower(), dlg._bar.styleSheet().lower())


if __name__ == "__main__":
    unittest.main()
