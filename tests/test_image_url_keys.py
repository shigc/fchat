"""图片 URL 缓存键一致性回归测试。

QUrl.toString() 默认 PrettyDecoded 会把 %20、中文转义解码成明文，
回包后按 request URL 反查在途键就对不上，文件名含中文/空格的附件
全部"图片请求超时"。锁定：登记键与回包提取键必须同为 FullyEncoded。

运行：python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QBuffer, QIODevice, QUrl  # noqa: E402
from PyQt5.QtGui import QColor, QImage, QPixmap  # noqa: E402
from PyQt5.QtNetwork import QNetworkReply, QNetworkRequest  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from ui import avatar_loader as al  # noqa: E402
from ui.avatar_loader import ImageLoader, canon_url  # noqa: E402

_APP = None

ENCODED_URL = ("https://s.example.com/file-upload/6a866a98526404caf24f6cd5/"
               "Clipboard%20-%202026%E5%B9%B48%E6%9C%8820%E6%97%A5.png")


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _png_bytes() -> bytes:
    img = QImage(4, 4, QImage.Format_ARGB32)
    img.fill(QColor("red"))
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


class StubReply:
    """最小 QNetworkReply 替身：只提供 _on_finished 用到的接口。"""

    def __init__(self, url: str, data: bytes):
        self._req = QNetworkRequest(QUrl(url))
        self._data = data

    def request(self):
        return self._req

    def error(self):
        return QNetworkReply.NoError

    def attribute(self, _key):
        return None

    def readAll(self):
        return self._data

    def deleteLater(self):
        pass


class TestCanonUrl(unittest.TestCase):
    def test_encoded_url_kept_as_is(self):
        self.assertEqual(canon_url(ENCODED_URL), ENCODED_URL)

    def test_decoded_form_maps_to_same_key(self):
        # 旧代码从 request URL 取 PrettyDecoded 明文串，规范化后
        # 必须与原始编码串一致，键才不会丢
        decoded = QUrl(ENCODED_URL).toString()
        self.assertNotEqual(decoded, ENCODED_URL)
        self.assertEqual(canon_url(decoded), ENCODED_URL)


class TestImageLoaderKeyConsistency(unittest.TestCase):
    def setUp(self):
        # _on_finished 成功解码后会把原始字节落盘（_save_image_disk），
        # 不隔离缓存目录会把测试图片写进真实用户缓存，且下次运行
        # pixmap_for 磁盘命中返回非 None 直接打挂 assertIsNone 断言
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

    def test_finished_uses_same_key_as_pixmap_for(self):
        loader = ImageLoader(server_url="https://s.example.com")
        self.assertIsNone(loader.pixmap_for(ENCODED_URL, 240))
        loader._on_finished(StubReply(ENCODED_URL, _png_bytes()))
        # 回包后同一 URL 必须直接命中缓存，而不是仍在等待中
        self.assertIsNotNone(loader.pixmap_for(ENCODED_URL, 240))
        self.assertNotIn(ENCODED_URL, loader._inflight)

    def test_relative_url_resolved_and_cached(self):
        loader = ImageLoader(server_url="https://s.example.com")
        raw = "/file-upload/abc/截图%20测试.png"
        self.assertIsNone(loader.pixmap_for(raw, 240))
        full = "https://s.example.com" + raw
        loader._on_finished(StubReply(full, _png_bytes()))
        self.assertIsNotNone(loader.pixmap_for(full, 240))


class TestImageQuality(unittest.TestCase):
    """缩略图清晰度与查看器垫图回归。"""

    def setUp(self):
        # 同上：隔离磁盘缓存目录，避免测试图片写进真实用户缓存
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

    def _load_one(self, loader, full_url: str, size: int):
        """走真实加载路径：先登记在途再回包，才能入缓存。"""
        self.assertIsNone(loader.pixmap_for(full_url, size))
        loader._on_finished(StubReply(full_url, _png_bytes()))
        return loader.pixmap_for(full_url, size)

    def test_thumb_cached_at_hidpi_scale(self):
        """聊天图按 2x 冗余缓存，委托下采样不发糊。"""
        from ui import avatar_loader
        loader = ImageLoader(server_url="https://s.example.com")
        url = "https://s.example.com/cached/a.png"
        pm = self._load_one(loader, url, 240)
        self.assertIsNotNone(pm)
        self.assertEqual(pm.width(), 240 * avatar_loader._HIDPI_SCALE)
        self.assertEqual(pm.height(), 240 * avatar_loader._HIDPI_SCALE)

    def test_cached_pixmap_returns_loaded_thumb(self):
        """查看器垫图：已加载的图应能直接取到缩略图。"""
        loader = ImageLoader(server_url="https://s.example.com")
        url = "https://s.example.com/cached/b.png"
        self.assertIsNone(loader.cached_pixmap(url))
        self._load_one(loader, url, 240)
        pm = loader.cached_pixmap(url)
        self.assertIsNotNone(pm)
        self.assertEqual(pm.width(), 240 * 2)

    def test_cached_pixmap_relative_url(self):
        loader = ImageLoader(server_url="https://s.example.com")
        raw = "/file-upload/abc/图.png"
        full = "https://s.example.com" + raw
        self._load_one(loader, full, 240)
        self.assertIsNotNone(loader.cached_pixmap(raw))


if __name__ == "__main__":
    unittest.main()
