"""下载进度弹窗测试。离线：worker 直接 run()（不起线程），requests 全 mock。

弹窗 UI 槽测试把 _FileDownloadWorker 换成 MagicMock，避免真起 QThread 发网络。
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

from core.url_guard import MAX_REDIRECT_HOPS  # noqa: E402
from ui.download_dialog import (  # noqa: E402
    FileDownloadDialog, _FileDownloadWorker, _fmt_size,
)

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _fake_resp(chunks, headers=None, raise_for_status=None):
    """构造一个可作上下文管理器的假 requests 响应。"""
    resp = mock.MagicMock()
    resp.headers = headers or {}
    resp.iter_content.return_value = chunks
    if raise_for_status is not None:
        resp.raise_for_status.side_effect = raise_for_status
    else:
        resp.raise_for_status = mock.Mock()
    cm = mock.MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return cm


class FmtSizeTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(_fmt_size(512), "512 B")
        self.assertEqual(_fmt_size(2048), "2.0 KB")
        self.assertEqual(_fmt_size(5 << 20), "5.0 MB")
        self.assertEqual(_fmt_size(3 << 30), "3.0 GB")


class WorkerRunTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dest = os.path.join(self._tmp.name, "sub", "a.pdf")

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, worker):
        progress, ok, failed = [], [], []
        worker.progress.connect(lambda d, t: progress.append((d, t)))
        worker.finished_ok.connect(lambda: ok.append(True))
        worker.failed.connect(lambda e: failed.append(e))
        worker.run()
        return progress, ok, failed

    def test_success_writes_file_and_reports_progress(self):
        w = _FileDownloadWorker("http://x/f", self.dest, headers={"X": "1"})
        with mock.patch("requests.get",
                        return_value=_fake_resp([b"he", b"llo"],
                                                {"Content-Length": "5"})):
            progress, ok, failed = self._run(w)
        self.assertEqual(progress, [(2, 5), (5, 5)])
        self.assertEqual(ok, [True])
        self.assertEqual(failed, [])
        self.assertTrue(os.path.exists(self.dest))
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), b"hello")
        self.assertFalse(os.path.exists(self.dest + ".part"))

    def test_unknown_total_reports_zero(self):
        w = _FileDownloadWorker("http://x/f", self.dest)
        with mock.patch("requests.get",
                        return_value=_fake_resp([b"ab", b"cd"], {})):
            progress, ok, _ = self._run(w)
        self.assertEqual(progress, [(2, 0), (4, 0)])
        self.assertEqual(ok, [True])
        self.assertTrue(os.path.exists(self.dest))

    def test_known_total_used_when_header_missing(self):
        # 服务端不给 Content-Length：回退到调用方传入的已知文件大小
        w = _FileDownloadWorker("http://x/f", self.dest, known_total=4)
        with mock.patch("requests.get",
                        return_value=_fake_resp([b"ab", b"cd"], {})):
            progress, ok, _ = self._run(w)
        self.assertEqual(progress, [(2, 4), (4, 4)])
        self.assertEqual(ok, [True])

    def test_content_length_wins_over_known_total(self):
        w = _FileDownloadWorker("http://x/f", self.dest, known_total=999)
        with mock.patch("requests.get",
                        return_value=_fake_resp([b"ab"], {"Content-Length": "2"})):
            progress, _, _ = self._run(w)
        self.assertEqual(progress, [(2, 2)])

    def _hop_resp(self, status, headers=None, chunks=(b"tail",)):
        """带真实状态码的假响应：_fake_resp 的 status_code 是 Mock，
        走不到跳转分支，测重定向必须给整数。"""
        resp = mock.MagicMock()
        resp.status_code = status
        resp.headers = headers or {}
        resp.iter_content.return_value = list(chunks)
        resp.raise_for_status = mock.Mock()
        cm = mock.MagicMock()
        cm.__enter__.return_value = resp
        cm.__exit__.return_value = False
        return cm

    def _recording_get(self, hops):
        """按 hops 依次返回响应，并记下每次请求的 (url, headers)。"""
        seen = []

        def _get(url, **kw):
            seen.append((url, dict(kw.get("headers") or {})))
            return hops[len(seen) - 1]
        _get.seen = seen
        return _get

    def test_cross_host_redirect_drops_auth_headers(self):
        """跳转换了主机就不能再带会话令牌。

        requests 自动跟随只剥 Authorization，X-Auth-Token 会原样送到
        重定向目标，所以下载是一跳一跳自己发的。
        """
        get = self._recording_get([
            self._hop_resp(302, {"Location": "https://cdn.evil.me/f.pdf"}),
            self._hop_resp(200, {"Content-Length": "4"})])
        w = _FileDownloadWorker("https://chat.example.com/f/1.pdf", self.dest,
                                headers={"X-Auth-Token": "tok"})
        with mock.patch("requests.get", side_effect=get):
            _, ok, failed = self._run(w)
        self.assertEqual(failed, [])
        self.assertEqual(ok, [True])
        self.assertEqual([u for u, _ in get.seen],
                         ["https://chat.example.com/f/1.pdf",
                          "https://cdn.evil.me/f.pdf"])
        self.assertEqual(get.seen[0][1], {"X-Auth-Token": "tok"})
        self.assertEqual(get.seen[1][1], {})
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), b"tail")

    def test_same_origin_redirect_keeps_auth_headers(self):
        get = self._recording_get([
            self._hop_resp(307, {"Location": "/file-upload/real.pdf"}),
            self._hop_resp(200, {"Content-Length": "4"})])
        w = _FileDownloadWorker("https://chat.example.com/f/1.pdf", self.dest,
                                headers={"X-Auth-Token": "tok"})
        with mock.patch("requests.get", side_effect=get):
            _, ok, failed = self._run(w)
        self.assertEqual(failed, [])
        self.assertEqual(get.seen[1][0],
                         "https://chat.example.com/file-upload/real.pdf")
        self.assertEqual(get.seen[1][1], {"X-Auth-Token": "tok"})

    def test_redirect_without_usable_target_fails(self):
        """3xx 但 Location 不可用：不能把跳转提示当成文件内容写出去。"""
        get = self._recording_get([self._hop_resp(302, {})])
        w = _FileDownloadWorker("https://chat.example.com/f/1.pdf", self.dest,
                                headers={})
        with mock.patch("requests.get", side_effect=get):
            _, ok, failed = self._run(w)
        self.assertEqual(ok, [])
        self.assertTrue(failed and "重定向" in failed[0], failed)
        self.assertFalse(os.path.exists(self.dest))

    def test_redirect_loop_stops_at_hop_limit(self):
        hops = [self._hop_resp(302, {"Location": "https://h%d.example.com/f"
                                     % i}) for i in range(10)]
        get = self._recording_get(hops)
        w = _FileDownloadWorker("https://a.example.com/f.pdf", self.dest,
                                headers={})
        with mock.patch("requests.get", side_effect=get):
            _, ok, failed = self._run(w)
        self.assertEqual(ok, [])
        self.assertEqual(len(get.seen), MAX_REDIRECT_HOPS + 1)
        self.assertTrue(failed and "重定向" in failed[0], failed)
        self.assertFalse(os.path.exists(self.dest))

    def test_cancel_removes_part_and_no_dest(self):
        w = _FileDownloadWorker("http://x/f", self.dest)
        w.cancel()
        with mock.patch("requests.get",
                        return_value=_fake_resp([b"a"], {"Content-Length": "1"})):
            _, ok, failed = self._run(w)
        self.assertEqual(ok, [])
        self.assertEqual(failed, ["已取消"])
        self.assertFalse(os.path.exists(self.dest))
        self.assertFalse(os.path.exists(self.dest + ".part"))

    def test_network_failure_reports_and_cleans(self):
        w = _FileDownloadWorker("http://x/f", self.dest)
        with mock.patch("requests.get", side_effect=OSError("boom")):
            _, ok, failed = self._run(w)
        self.assertEqual(ok, [])
        self.assertEqual(failed, ["boom"])
        self.assertFalse(os.path.exists(self.dest))
        self.assertFalse(os.path.exists(self.dest + ".part"))

    def test_http_error_status_fails(self):
        w = _FileDownloadWorker("http://x/f", self.dest)
        resp = _fake_resp([b"x"], {"Content-Length": "1"},
                          raise_for_status=Exception("404"))
        with mock.patch("requests.get", return_value=resp):
            _, ok, failed = self._run(w)
        self.assertEqual(ok, [])
        self.assertEqual(failed, ["404"])
        self.assertFalse(os.path.exists(self.dest + ".part"))


class DialogUiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dest = os.path.join(self._tmp.name, "a.pdf")

    def tearDown(self):
        self._tmp.cleanup()

    def _make_dialog(self, theme=None):
        import ui.download_dialog as dd
        patcher = mock.patch.object(dd, "_FileDownloadWorker")
        patcher.start()
        self.addCleanup(patcher.stop)
        dlg = dd.FileDownloadDialog("http://x/f", self.dest, "a.pdf",
                                    theme=theme)
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
        # 非模态：下载期间主窗口仍可操作/切换
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
        dlg = self._make_dialog()
        dlg._on_progress(2048, 0)
        self.assertEqual(dlg._bar.maximum(), 0)   # 忙碌指示
        self.assertIn("已下载", dlg._status.text())

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
        self.assertIn("下载失败", dlg._status.text())
        self.assertEqual(dlg._cancel_btn.text(), "关闭")
        self.assertFalse(dlg._closing)            # 失败保留窗口等用户关闭

    def test_user_cancel_closes_dialog(self):
        dlg = self._make_dialog()
        dlg._user_cancelled = True
        dlg._on_fail("已取消")
        self.assertFalse(dlg._thread.isRunning())   # 走了 _finish_close 收尾
        self.assertTrue(dlg._closing)

    def test_force_close_cancels_and_hides_without_waiting(self):
        # 主窗口关闭路径：取消 worker、置 _closing、hide，但不 wait 线程
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
        tm = ThemeManager()
        tm.accent = "#123456"
        import ui.download_dialog as dd
        with mock.patch.object(dd, "_FileDownloadWorker"):
            dlg = dd.FileDownloadDialog("http://x/f",
                                        os.path.join(tmp.name, "a"),
                                        "a", theme=tm)
        self.addCleanup(dlg._cleanup)
        self.addCleanup(dlg.deleteLater)
        self.assertIn("#123456", dlg._bar.styleSheet())


if __name__ == "__main__":
    unittest.main()
