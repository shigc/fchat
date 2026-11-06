"""图片磁盘缓存与头像缓存清理测试。离线，缓存目录指向临时目录。"""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import Qt, QBuffer  # noqa: E402
from PyQt5.QtGui import QPixmap  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from ui import avatar_loader as al  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _png_bytes():
    pm = QPixmap(8, 8)
    pm.fill(Qt.red)
    buf = QBuffer()
    buf.open(QBuffer.ReadWrite)
    pm.save(buf, "PNG")
    return bytes(buf.data())


class ImageDiskCacheTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patches = [
            mock.patch.object(al, "images_dir", lambda: self._tmp.name),
            mock.patch.object(al, "avatars_dir", lambda: self._tmp.name),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_save_and_load_roundtrip(self):
        data = _png_bytes()
        url = "http://s.example.com/file-upload/x.png"
        al._save_image_disk(url, data)
        got = al._load_image_disk(url)
        self.assertIsNotNone(got)
        self.assertFalse(got.isNull())
        self.assertEqual(got.size(), QPixmap(8, 8).size())
        self.assertTrue(os.path.exists(al._image_disk_path(url)))

    def test_load_missing_returns_none(self):
        self.assertIsNone(al._load_image_disk("http://s/never-exists.png"))

    def test_pixmap_for_hits_disk_without_network(self):
        url = "http://s.example.com/img/disk.png"
        al._save_image_disk(url, _png_bytes())
        loader = al.ImageLoader("http://s.example.com")
        loader._nam = mock.Mock()
        pm = loader.pixmap_for(url, 40)
        self.assertIsNotNone(pm)
        self.assertFalse(pm.isNull())
        loader._nam.get.assert_not_called()
        # 同时应已入内存缓存
        self.assertIsNotNone(al._IMAGE_CACHE.get_pixmap(
            (al.canon_url(url), "img", 40)))

    def test_pixmap_for_miss_keeps_network_path(self):
        loader = al.ImageLoader("http://s.example.com")
        loader._nam = mock.Mock()
        self.assertIsNone(loader.pixmap_for("http://s.example.com/none.png", 40))
        loader._nam.get.assert_called_once()

    @staticmethod
    def _png(w, h):
        pm = QPixmap(w, h)
        pm.fill(Qt.blue)
        buf = QBuffer()
        buf.open(QBuffer.ReadWrite)
        pm.save(buf, "PNG")
        return bytes(buf.data())

    def test_original_size_falls_back_to_disk(self):
        """重启后内存 _original_sizes 为空，original_size 必须能从磁盘
        原图兜底读出尺寸，否则气泡行高/命中矩形停留 240x180 回退值。"""
        url = "http://s.example.com/img/size.png"
        al._save_image_disk(url, self._png(30, 20))
        loader = al.ImageLoader("http://s.example.com")
        loader._nam = mock.Mock()
        self.assertEqual(loader.original_size(url), (30, 20))
        # 回填进内存，后续读取不再碰磁盘
        self.assertIn(al.canon_url(url), loader._original_sizes)
        self.assertEqual(loader.original_size(url), (30, 20))

    def test_original_size_relative_url_resolved(self):
        base = "http://s.example.com"
        al._save_image_disk(base + "/img/rel.png", self._png(8, 8))
        loader = al.ImageLoader(base)
        self.assertEqual(loader.original_size("/img/rel.png"), (8, 8))

    def test_original_size_no_disk_returns_none(self):
        loader = al.ImageLoader("http://s.example.com")
        self.assertIsNone(loader.original_size("http://s.example.com/none.png"))
        self.assertIsNone(loader.original_size(""))


class AvatarPurgeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patches = [
            mock.patch.object(al, "images_dir", lambda: self._tmp.name),
            mock.patch.object(al, "avatars_dir", lambda: self._tmp.name),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_purge_removes_memory_and_disk(self):
        loader = al.AvatarLoader()
        url = "http://s.example.com/avatar/u"
        key = (al.canon_url(url), "avatar", al._AVATAR_PX)
        al._AVATAR_CACHE.put(key, QPixmap(4, 4))
        disk = al._url_to_cache_path(url)
        with open(disk, "wb") as f:
            f.write(b"x")
        loader.purge(url)
        self.assertIsNone(al._AVATAR_CACHE.get_pixmap(key))
        self.assertFalse(os.path.exists(disk))

    def test_purge_clears_inflight_dedup(self):
        """purge 必须连在途登记一起清：否则上传新头像后的 load() 被
        _pending/_watching 去重拦截，旧地址的回应答后新头像永远不来。"""
        loader = al.AvatarLoader()
        loader.set_auth("uid", "tok")   # 未登录时守卫不发请求（见 test_avatar_loader）
        loader._nam = mock.Mock()
        url = "http://s.example.com/avatar/u"
        key = al.canon_url(url)
        # 两条登记路径：pixmap_for() 进 _watching（先调，否则 load 的
        # _pending 会把 pixmap_for 去重拦下）、load() 进 _pending
        loader.pixmap_for(url)
        loader.load(mock.Mock(), url)
        self.assertIn(key, loader._pending)
        self.assertIn(key, loader._watching)
        loader.purge(url)
        self.assertNotIn(key, loader._pending)
        self.assertNotIn(key, loader._watching)
        # purge 后 load 不得被在途去重拦截，要能重新发起请求
        loader.load(mock.Mock(), url)
        self.assertEqual(loader._nam.get.call_count, 3)

    def test_purge_empty_url_is_safe(self):
        al.AvatarLoader().purge("")


class AvatarAuthCookieTest(unittest.TestCase):
    """服务端 /avatar/ 只认 Cookie（rc_uid/rc_token），X-Auth-Token 头和不带
    认证都会被降级成默认字母 SVG，真头像永远拉不下来。头像请求必须带 Cookie。"""

    def test_fetch_carries_cookie_when_auth_set(self):
        loader = al.AvatarLoader()
        loader._nam = mock.Mock()
        loader.set_auth("uid-1", "tok-1")
        loader.pixmap_for("http://s.example.com/avatar/u")
        req = loader._nam.get.call_args[0][0]
        cookie = bytes(req.rawHeader(b"Cookie"))
        self.assertEqual(cookie, b"rc_uid=uid-1; rc_token=tok-1")

    def test_fetch_without_auth_is_blocked(self):
        """未登录不发请求：无 Cookie 的 /avatar/ 请求必被服务端降级成
        默认字母 SVG，等于白跑一趟还会把失败哨兵写进缓存（2026-09-22）。"""
        loader = al.AvatarLoader()
        loader._nam = mock.Mock()
        loader.pixmap_for("http://s.example.com/avatar/u")
        loader.load(mock.Mock(), "http://s.example.com/avatar/u")
        loader.refresh_if_changed("http://s.example.com/avatar/u")
        self.assertFalse(loader._nam.get.called)


if __name__ == "__main__":
    unittest.main()
