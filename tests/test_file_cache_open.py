"""文件附件缓存打开测试。离线，缓存目录指向临时目录。"""
import hashlib
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication  # noqa: E402

import ui.chat_view  # noqa: E402  —— OpenCachedFileTest 需经模块属性取被 patch 的 file_cache_dest
from ui.chat_view import ChatView, _safe_filename, file_cache_dest  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _sync_run(fn, on_done=None, on_fail=None, parent=None):
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001
        if on_fail:
            on_fail(str(e))
        return
    if on_done:
        on_done(result)


class FileNameTest(unittest.TestCase):
    def test_safe_filename_strips_illegal(self):
        self.assertEqual(_safe_filename('a/b\\c:d*e?"<>|'), "a_b_c_d_e_____")
        self.assertEqual(_safe_filename(""), "attachment")

    def test_dest_has_md5_prefix_and_name(self):
        url = "http://s.example.com/file-upload/report.pdf"
        dest = file_cache_dest(url, "report.pdf")
        head = hashlib.md5(url.encode()).hexdigest()
        self.assertEqual(os.path.basename(dest), f"{head}-report.pdf")

    def test_dest_borrows_extension_from_url(self):
        """文件名无扩展名时从 URL 路径补（视频附件 title 常无后缀）。"""
        url = "http://s.example.com/file-upload/v/abc123.mp4"
        dest = file_cache_dest(url, "会议录像")
        self.assertTrue(os.path.basename(dest).endswith(".mp4"))
        # URL 也没有扩展名就保持原样
        dest2 = file_cache_dest("http://s.example.com/file-upload/xyz", "附件")
        self.assertFalse(os.path.basename(dest2).endswith((".x", ".yz")))
        self.assertTrue(os.path.basename(dest2).endswith("-附件"))


class OpenCachedFileTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch(
            "ui.chat_view.file_cache_dest", side_effect=self._fake_dest)
        self._patch.start()
        self.view = ChatView()

    def tearDown(self):
        self.view.deleteLater()
        self._patch.stop()
        self._tmp.cleanup()

    def _fake_dest(self, url, name):
        return os.path.join(self._tmp.name, f"{hashlib.md5(url.encode()).hexdigest()}-{name}")

    def test_cached_file_opens_local_without_download(self):
        from PyQt5.QtCore import QUrl
        url = "http://s.example.com/f/x.pdf"
        # 走模块属性：此用例里 file_cache_dest 被 patch 到临时目录
        dest = ui.chat_view.file_cache_dest(url, "x.pdf")
        with open(dest, "wb") as f:
            f.write(b"pdf")
        msg = mock.Mock(file_url=url, file_name="x.pdf", msg_type="file")
        with mock.patch("ui.chat_view.QDesktopServices.openUrl") as op, \
                mock.patch("ui.chat_view.run_async") as ra:
            self.view._open_file_cached(msg)
            op.assert_called_once()
            # QUrl.toLocalFile 返回正斜杠形式，normpath 后再比
            self.assertEqual(
                os.path.normpath(op.call_args[0][0].toLocalFile()), dest)
            ra.assert_not_called()

    def test_uncached_file_downloads_then_opens(self):
        url = "http://s.example.com/f/new.pdf"
        msg = mock.Mock(file_url=url, file_name="new.pdf", msg_type="file")
        # 认证头来自 image_loader，置空避免 None
        self.view.image_loader = None
        with mock.patch("ui.chat_view.run_async", side_effect=_sync_run), \
                mock.patch("requests.get") as get, \
                mock.patch("ui.chat_view.QDesktopServices.openUrl") as op:
            resp = mock.Mock()
            resp.iter_content.return_value = [b"he", b"llo"]
            resp.__enter__ = mock.Mock(return_value=resp)
            resp.__exit__ = mock.Mock(return_value=False)
            get.return_value = resp
            self.view._open_file_cached(msg)
            dest = ui.chat_view.file_cache_dest(url, "new.pdf")
            self.assertTrue(os.path.exists(dest))
            with open(dest, "rb") as f:
                self.assertEqual(f.read(), b"hello")
            op.assert_called_once()
            # .part 临时文件必须被原子改名掉，不能残留
            self.assertFalse(os.path.exists(dest + ".part"))
            self.assertEqual(self.view._downloading, set())

    def test_cross_host_redirect_reissues_without_credentials(self):
        """302 跳到别的主机时，会话令牌不能跟着送出去。

        requests 自动跟随只会剥掉 Authorization，X-Auth-Token 会原样带到
        重定向目标，所以下载改成自己一跳一跳发，每跳重算认证头。
        """
        url = "https://chat.example.com/f/moved.pdf"
        msg = mock.Mock(file_url=url, file_name="moved.pdf",
                        msg_type="file")
        self.view.image_loader = mock.Mock(
            auth_uid="uid1", auth_token="tok1",
            server_url="https://chat.example.com", ssl_verify=True)

        def _resp(status, headers=None, body=b"pdf"):
            r = mock.Mock(status_code=status, headers=headers or {})
            r.__enter__ = mock.Mock(return_value=r)
            r.__exit__ = mock.Mock(return_value=False)
            r.iter_content.return_value = [body]
            return r

        calls = []

        def _get(target, **kw):
            calls.append((target, dict(kw.get("headers") or {})))
            if len(calls) == 1:
                return _resp(302, {"Location": "https://cdn.evil.me/m.pdf"})
            return _resp(200)

        with mock.patch("ui.chat_view.run_async", side_effect=_sync_run), \
                mock.patch("requests.get", side_effect=_get), \
                mock.patch("ui.chat_view.QDesktopServices.openUrl") as op:
            self.view._open_file_cached(msg)

        self.assertEqual([c[0] for c in calls],
                         [url, "https://cdn.evil.me/m.pdf"])
        self.assertEqual(calls[0][1],
                         {"X-Auth-Token": "tok1", "X-User-Id": "uid1"})
        self.assertEqual(calls[1][1], {})
        self.assertEqual(self.view._downloading, set())
        op.assert_called_once()

    def test_same_origin_redirect_keeps_credentials(self):
        """跳到同源（换端口写法/路径）仍应带上认证头，否则 401 打不开。"""
        url = "https://chat.example.com/f/moved.pdf"
        msg = mock.Mock(file_url=url, file_name="moved.pdf",
                        msg_type="file")
        self.view.image_loader = mock.Mock(
            auth_uid="uid1", auth_token="tok1",
            server_url="https://chat.example.com", ssl_verify=True)

        def _resp(status, headers=None, body=b"pdf"):
            r = mock.Mock(status_code=status, headers=headers or {})
            r.__enter__ = mock.Mock(return_value=r)
            r.__exit__ = mock.Mock(return_value=False)
            r.iter_content.return_value = [body]
            return r

        calls = []

        def _get(target, **kw):
            calls.append((target, dict(kw.get("headers") or {})))
            if len(calls) == 1:
                return _resp(307, {"Location": "/file-upload/real.pdf"})
            return _resp(200)

        with mock.patch("ui.chat_view.run_async", side_effect=_sync_run), \
                mock.patch("requests.get", side_effect=_get), \
                mock.patch("ui.chat_view.QDesktopServices.openUrl"):
            self.view._open_file_cached(msg)

        self.assertEqual(calls[1][0],
                         "https://chat.example.com/file-upload/real.pdf")
        self.assertEqual(calls[1][1],
                         {"X-Auth-Token": "tok1", "X-User-Id": "uid1"})

    def test_duplicate_click_while_downloading_is_ignored(self):
        url = "http://s.example.com/f/new.pdf"
        msg = mock.Mock(file_url=url, file_name="new.pdf", msg_type="file")
        self.view.image_loader = None
        self.view._downloading.add(url)
        with mock.patch("ui.chat_view.run_async") as ra, \
                mock.patch("ui.chat_view.QDesktopServices.openUrl") as op:
            self.view._open_file_cached(msg)
            ra.assert_not_called()
            op.assert_not_called()

    def test_download_failure_discards_and_reports(self):
        url = "http://s.example.com/f/bad.pdf"
        msg = mock.Mock(file_url=url, file_name="bad.pdf", msg_type="file")
        self.view.image_loader = None
        seen = []
        self.view.status_message.connect(lambda t, ms: seen.append(t))
        with mock.patch("ui.chat_view.run_async", side_effect=_sync_run), \
                mock.patch("requests.get", side_effect=OSError("boom")), \
                mock.patch("ui.chat_view.QDesktopServices.openUrl") as op:
            self.view._open_file_cached(msg)
        op.assert_not_called()
        self.assertEqual(self.view._downloading, set())   # 失败后可重试
        self.assertTrue(any("下载失败" in t for t in seen))

    def test_download_failure_removes_part_file(self):
        url = "http://s.example.com/f/bad.pdf"
        msg = mock.Mock(file_url=url, file_name="bad.pdf", msg_type="file")
        self.view.image_loader = None
        seen = []
        self.view.status_message.connect(lambda t, ms: seen.append(t))
        dest = ui.chat_view.file_cache_dest(url, "bad.pdf")
        with mock.patch("ui.chat_view.run_async", side_effect=_sync_run), \
                mock.patch("requests.get") as get, \
                mock.patch("ui.chat_view.QDesktopServices.openUrl") as op:
            resp = mock.Mock()
            resp.__enter__ = mock.Mock(return_value=resp)
            resp.__exit__ = mock.Mock(return_value=False)
            resp.raise_for_status = mock.Mock()
            # .part 已经写了（打开即创建）之后传输中断
            resp.iter_content.side_effect = OSError("传输中断")
            get.return_value = resp
            self.view._open_file_cached(msg)
        op.assert_not_called()
        self.assertEqual(self.view._downloading, set())   # 失败后可重试
        self.assertTrue(any("下载失败" in t for t in seen))
        # 失败后 .part 残留必须清掉，不留垃圾文件
        self.assertFalse(os.path.exists(dest + ".part"))

    def test_getsize_error_treated_as_uncached(self):
        url = "http://s.example.com/f/x.pdf"
        dest = ui.chat_view.file_cache_dest(url, "x.pdf")
        with open(dest, "wb") as f:
            f.write(b"pdf")
        msg = mock.Mock(file_url=url, file_name="x.pdf", msg_type="file")
        with mock.patch("os.path.getsize", side_effect=OSError("gone")), \
                mock.patch("ui.chat_view.run_async", side_effect=_sync_run) as ra, \
                mock.patch("requests.get") as get, \
                mock.patch("ui.chat_view.QDesktopServices.openUrl"):
            resp = mock.Mock()
            resp.iter_content.return_value = [b"pdf"]
            resp.__enter__ = mock.Mock(return_value=resp)
            resp.__exit__ = mock.Mock(return_value=False)
            get.return_value = resp
            self.view._open_file_cached(msg)
        ra.assert_called_once()   # 被当作未缓存，走下载


if __name__ == "__main__":
    unittest.main()
