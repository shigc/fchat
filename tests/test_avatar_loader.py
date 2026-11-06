"""头像加载器离线单测：未认证守卫与远端一致性检查的磁盘比对逻辑。

回归动因：登录前预渲染会话快照时头像加载器还没有登录态，/avatar/ 无
Cookie 会被服务端降级成默认字母 SVG——不拦住会把 IMG_FAILED 哨兵写进
内存缓存，登录后这些头像就永远不重试了。

运行：python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
import unittest.mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication  # noqa: E402

from ui import avatar_loader  # noqa: E402
from ui.avatar_loader import AvatarLoader, canon_url  # noqa: E402
from ui.widgets import AvatarWidget  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class UnauthGuardTest(unittest.TestCase):
    """未登录（无 auth_token）时头像加载器必须不发请求、不打失败哨兵。"""

    def setUp(self):
        self.loader = AvatarLoader()
        self.fetched = []
        self.loader._fetch = self.fetched.append
        self.url = "https://s.example.com/avatar/u1"

    def _cache_has_entry(self) -> bool:
        key = self.loader._cache_key(canon_url(self.url))
        return avatar_loader._AVATAR_CACHE.get_pixmap(key) is not None

    def test_pixmap_for_without_auth_does_not_fetch(self):
        self.assertIsNone(self.loader.pixmap_for(self.url))
        self.assertEqual(self.fetched, [])
        self.assertFalse(self._cache_has_entry(), "不能写入 IMG_FAILED 哨兵")

    def test_pixmap_for_with_auth_fetches(self):
        self.loader.set_auth("uid", "tok")
        self.assertIsNone(self.loader.pixmap_for(self.url))
        self.assertEqual(self.fetched, [canon_url(self.url)])

    def test_load_without_auth_keeps_placeholder_and_fetches_after_auth(self):
        w = AvatarWidget()
        try:
            self.loader.load(w, self.url)
            self.assertEqual(self.fetched, [])
            self.loader.set_auth("uid", "tok")
            self.loader.load(w, self.url)
            self.assertEqual(len(self.fetched), 1)
        finally:
            w.deleteLater()


class RefreshIfChangedTest(unittest.TestCase):
    """refresh_if_changed 的守卫与 _on_refresh_finished 的落盘/通知逻辑。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.loader = AvatarLoader()
        self.loader.set_auth("uid", "tok")
        self.url = canon_url("https://s.example.com/avatar/u1")
        self._orig_path = avatar_loader._url_to_cache_path
        avatar_loader._url_to_cache_path = (
            lambda u: os.path.join(self.tmp, "a.png"))
        self.emitted = []
        self.loader.avatar_ready.connect(self.emitted.append)

    def tearDown(self):
        avatar_loader._url_to_cache_path = self._orig_path

    def _fake_reply(self, data: bytes, error=None):
        reply = unittest.mock.Mock()
        reply.readAll.return_value = data
        if error is None:
            reply.error.return_value = avatar_loader.QNetworkReply.NoError
            reply.attribute.return_value = None
        else:
            reply.error.return_value = error
            reply.errorString.return_value = "boom"
            reply.attribute.return_value = 500
        return reply

    def test_guard_without_auth(self):
        self.loader.auth_token = ""
        self.loader.refresh_if_changed(self.url)
        self.assertEqual(self.loader._refreshing, set())

    def test_guard_dedup(self):
        self.loader.refresh_if_changed(self.url)
        self.loader.refresh_if_changed(self.url)
        self.assertEqual(len(self.loader._refreshing), 1)

    def test_same_bytes_noop(self):
        data = b"png-bytes"
        with open(os.path.join(self.tmp, "a.png"), "wb") as f:
            f.write(data)
        self.loader._on_refresh_finished(
            self.url, self._fake_reply(data))
        self.assertEqual(self.emitted, [])
        self.assertEqual(self.loader._refreshing, set())

    def test_changed_bytes_updates_disk_and_emits(self):
        with open(os.path.join(self.tmp, "a.png"), "wb") as f:
            f.write(b"old")
        self.loader._refreshing.add(self.url)
        self.loader._on_refresh_finished(
            self.url, self._fake_reply(b"new-bytes"))
        with open(os.path.join(self.tmp, "a.png"), "rb") as f:
            self.assertEqual(f.read(), b"new-bytes")
        self.assertEqual(self.emitted, [self.url])
        self.assertEqual(self.loader._refreshing, set())

    def test_network_error_keeps_cache(self):
        with open(os.path.join(self.tmp, "a.png"), "wb") as f:
            f.write(b"old")
        self.loader._refreshing.add(self.url)
        self.loader._on_refresh_finished(
            self.url,
            self._fake_reply(b"", error=avatar_loader.QNetworkReply.UnknownNetworkError))
        with open(os.path.join(self.tmp, "a.png"), "rb") as f:
            self.assertEqual(f.read(), b"old")
        self.assertEqual(self.emitted, [])

    def test_disk_differs(self):
        path = os.path.join(self.tmp, "a.png")
        self.assertTrue(AvatarLoader._disk_differs(self.url, b"x"))
        with open(path, "wb") as f:
            f.write(b"x")
        self.assertFalse(AvatarLoader._disk_differs(self.url, b"x"))
        self.assertTrue(AvatarLoader._disk_differs(self.url, b"y"))


if __name__ == "__main__":
    unittest.main()
