"""图片查看器：磁盘原图优先（服务器清理历史文件后 404 的兜底）。

2026-10-08 实测：服务器会清理历史粘贴文件（带认证头重拉 404），
气泡靠磁盘缓存显示正常，而查看器只走网络 → 404 → 静默停在放大的
缩略图上，表现为"查看的图永远模糊"。修复后查看器优先读磁盘原图。
运行：python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QBuffer, QByteArray  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402
from PyQt5.QtGui import QPixmap  # noqa: E402

from ui.avatar_loader import ImageLoader, _save_image_disk, canon_url  # noqa: E402
from ui.image_viewer import ImageViewer  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _png_bytes(w=64, h=48) -> bytes:
    pm = QPixmap(w, h)
    pm.fill()
    buf = QBuffer()
    buf.open(QBuffer.WriteOnly)
    pm.save(buf, "PNG")
    return bytes(buf.data())


class _FakeLoader:
    """ImageViewer 依赖的最小 loader 替身。"""

    def __init__(self, original=None, thumb=None):
        self._original = original
        self._thumb = thumb
        self.resolved = []

    def cached_original(self, url):
        return self._original

    def cached_pixmap(self, url):
        return self._thumb

    def resolve(self, url):
        self.resolved.append(url)
        return url

    auth_token = ""
    auth_uid = ""


class CachedOriginalTest(unittest.TestCase):
    """ImageLoader.cached_original：磁盘缓存命中原尺寸、未命中 None。"""

    def setUp(self):
        self.loader = ImageLoader("https://s.example.com")
        self.data = _png_bytes()

    def test_hit_returns_full_size_pixmap(self):
        url = "https://s.example.com/file-upload/x1/a b.png"
        _save_image_disk(canon_url(url), self.data)
        try:
            pm = self.loader.cached_original(url)
            self.assertIsNotNone(pm)
            self.assertEqual(pm.width(), 64)
            self.assertEqual(pm.height(), 48)
        finally:
            from ui.avatar_loader import _image_disk_path
            os.path.exists(_image_disk_path(canon_url(url))) and \
                os.remove(_image_disk_path(canon_url(url)))

    def test_miss_returns_none(self):
        self.assertIsNone(self.loader.cached_original(
            "https://s.example.com/file-upload/x2/missing.png"))

    def test_relative_url_resolved(self):
        _save_image_disk(canon_url("https://s.example.com/file-upload/x3/a.png"),
                         self.data)
        try:
            pm = self.loader.cached_original("/file-upload/x3/a.png")
            self.assertIsNotNone(pm)
        finally:
            from ui.avatar_loader import _image_disk_path
            os.remove(_image_disk_path(
                canon_url("https://s.example.com/file-upload/x3/a.png")))


class ViewerSourceTest(unittest.TestCase):
    """查看器取图路径：磁盘原图 > 缩略图垫底+网络 > 网络。"""

    def _viewer(self, loader):
        v = ImageViewer("https://s.example.com/file-upload/x/a.png", loader)
        v.deleteLater()
        return v

    def test_disk_original_used_without_network(self):
        """磁盘命中即原图：不发网络请求，也不垫缩略图。"""
        orig = QPixmap(640, 480)
        orig.fill()
        loader = _FakeLoader(original=orig, thumb=QPixmap(10, 10))
        with mock.patch.object(ImageViewer, "_start_load") as m_start:
            v = self._viewer(loader)
            self.assertIsNotNone(v._pixmap)
            self.assertEqual(v._pixmap.width(), 640)
            m_start.assert_not_called()

    def test_no_disk_falls_back_to_thumb_and_network(self):
        loader = _FakeLoader(original=None, thumb=QPixmap(20, 20))
        with mock.patch.object(ImageViewer, "_start_load") as m_start:
            v = self._viewer(loader)
            self.assertEqual(v._pixmap.width(), 20)
            m_start.assert_called_once_with()

    def test_no_cache_goes_straight_to_network(self):
        loader = _FakeLoader()
        with mock.patch.object(ImageViewer, "_start_load") as m_start:
            v = self._viewer(loader)
            self.assertIsNone(v._pixmap)
            m_start.assert_called_once_with()

    def test_network_failure_shows_hint_keeps_placeholder(self):
        """原图拉取失败：保留垫图的同时底部出红字提示，不再纯静默。"""
        loader = _FakeLoader(original=None, thumb=QPixmap(20, 20))
        with mock.patch.object(ImageViewer, "_start_load"):
            v = ImageViewer("https://s.example.com/file-upload/x/a.png", loader)
        try:
            reply = mock.Mock()
            reply.error.return_value = 2  # != QNetworkReply.NoError
            reply.errorString.return_value = "Not Found HTTP 404"
            reply.readAll.return_value = QByteArray()
            v._on_finished(reply)
            self.assertEqual(v._pixmap.width(), 20)
            self.assertIn("原图加载失败", v._hint)
        finally:
            v.deleteLater()

    def test_network_failure_without_placeholder_shows_fail_text(self):
        loader = _FakeLoader()
        with mock.patch.object(ImageViewer, "_start_load"):
            v = ImageViewer("https://s.example.com/file-upload/x/a.png", loader)
        try:
            reply = mock.Mock()
            reply.error.return_value = 2
            reply.errorString.return_value = "Not Found HTTP 404"
            v._on_finished(reply)
            self.assertIsNone(v._pixmap)
            self.assertEqual(v._label.text(), "图片加载失败")
        finally:
            v.deleteLater()


if __name__ == "__main__":
    unittest.main()
