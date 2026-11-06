"""存储设置页测试。离线。"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication  # noqa: E402

from ui.settings_page import SettingsPage  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _sync_run(fn, on_done=None, on_fail=None, parent=None):
    """把 run_async 换成同步执行，便于离线测试。"""
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001
        if on_fail:
            on_fail(str(e))
        return
    if on_done:
        on_done(result)


class StoragePageTest(unittest.TestCase):
    def setUp(self):
        self.page = SettingsPage()

    def tearDown(self):
        self.page.deleteLater()

    def test_nav_has_storage(self):
        self.assertEqual(len(self.page._nav_buttons), 8)
        texts = [b.text().strip() for b in self.page._nav_buttons]
        self.assertEqual(
            texts, ["账号", "通用", "外观", "字体", "快捷键", "存储", "AI", "关于"])
        self.assertEqual(self.page._stack.count(), 8)

    def test_fmt_size(self):
        f = SettingsPage._fmt_size
        self.assertEqual(f(512), "512 B")
        self.assertEqual(f(2048), "2.0 KB")
        self.assertEqual(f(5 * 1024 * 1024), "5.0 MB")
        self.assertEqual(f(3 * 1024 ** 3), "3.0 GB")

    def test_refresh_fills_size_labels(self):
        with mock.patch("ui.api_workers.run_async", side_effect=_sync_run), \
                mock.patch("core.paths.dir_size", return_value=2048):
            self.page.refresh_storage_usage()
        self.assertEqual(self.page._cache_size_label.text(), "2.0 KB")
        self.assertEqual(self.page._logs_size_label.text(), "2.0 KB")

    def test_clear_cache_runs_and_reports(self):
        seen = []
        self.page.storage_message.connect(lambda t, ms: seen.append(t))
        with mock.patch("ui.confirm_dialog.confirm", return_value=True), \
                mock.patch("core.paths.clear_cache") as clear, \
                mock.patch("ui.api_workers.run_async", side_effect=_sync_run), \
                mock.patch("core.paths.dir_size", return_value=0):
            self.page._on_clear_cache()
        clear.assert_called_once()
        self.assertTrue(any("已清空" in t for t in seen))

    def test_clear_cache_cancelled(self):
        with mock.patch("ui.confirm_dialog.confirm", return_value=False), \
                mock.patch("core.paths.clear_cache") as clear:
            self.page._on_clear_cache()
        clear.assert_not_called()

    def test_change_path_migrates_and_updates_labels(self):
        seen = []
        self.page.storage_message.connect(lambda t, ms: seen.append(t))
        with mock.patch("PyQt5.QtWidgets.QFileDialog.getExistingDirectory",
                        return_value="X:/somewhere"), \
                mock.patch("ui.confirm_dialog.confirm", return_value=True), \
                mock.patch("core.paths.migrate_storage",
                           return_value="cache、logs"), \
                mock.patch("core.paths.dir_size", return_value=0), \
                mock.patch("ui.api_workers.run_async", side_effect=_sync_run):
            self.page._on_change_storage_path()
        self.assertTrue(any("存储路径已更改" in t for t in seen))

    def test_change_path_migrate_failure_reports(self):
        seen = []
        self.page.storage_message.connect(lambda t, ms: seen.append(t))
        with mock.patch("PyQt5.QtWidgets.QFileDialog.getExistingDirectory",
                        return_value="X:/somewhere"), \
                mock.patch("ui.confirm_dialog.confirm", return_value=True), \
                mock.patch("core.paths.migrate_storage",
                           side_effect=OSError("disk full")), \
                mock.patch("core.paths.dir_size", return_value=0), \
                mock.patch("ui.api_workers.run_async", side_effect=_sync_run):
            self.page._on_change_storage_path()
        self.assertTrue(any("迁移失败" in t for t in seen))

    def test_change_path_blocked_while_busy(self):
        from PyQt5.QtWidgets import QFileDialog
        self.page._storage_busy = True
        seen = []
        self.page.storage_message.connect(lambda t, ms: seen.append(t))
        with mock.patch("PyQt5.QtWidgets.QFileDialog.getExistingDirectory") as dlg:
            self.page._on_change_storage_path()
        dlg.assert_not_called()
        self.assertTrue(any("请稍候" in t for t in seen))

    def test_clear_cache_blocked_while_busy(self):
        self.page._storage_busy = True
        with mock.patch("ui.confirm_dialog.confirm") as cf:
            self.page._on_clear_cache()
        cf.assert_not_called()


if __name__ == "__main__":
    unittest.main()
