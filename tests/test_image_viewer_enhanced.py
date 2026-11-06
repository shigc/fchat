"""图片查看器增强：翻页 / 缩放 / 旋转 / 另存为 / 过期请求丢弃。"""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QEvent, Qt  # noqa: E402
from PyQt5.QtGui import QKeyEvent, QPixmap  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from ui.image_viewer import ImageViewer  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class _FakeLoader:
    def __init__(self, original=None, thumb=None):
        self._original = original
        self._thumb = thumb
        self.resolved = []
        self.auth_token = ""
        self.auth_uid = ""

    def cached_original(self, url):
        self.resolved.append(("original", url))
        return self._original

    def cached_pixmap(self, url):
        self.resolved.append(("thumb", url))
        return self._thumb

    def resolve(self, url):
        return url


def _pm(w, h):
    pm = QPixmap(w, h)
    pm.fill()
    return pm


class NavigationTest(unittest.TestCase):

    def setUp(self):
        # 翻页会重新触发加载：整个用例期间都不能发真实网络请求
        patcher = mock.patch.object(ImageViewer, "_start_load")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _viewer(self, urls, index=0):
        loader = _FakeLoader()
        v = ImageViewer(urls[index], loader, urls=urls, index=index)
        self.addCleanup(v.deleteLater)
        return v

    def test_starts_at_given_index(self):
        v = self._viewer(["a.png", "b.png", "c.png"], index=1)
        self.assertEqual(v._url, "b.png")
        self.assertEqual(v._index, 1)
        self.assertEqual(v._index_label.text(), "2 / 3")

    def test_next_and_prev(self):
        v = self._viewer(["a.png", "b.png", "c.png"])
        v.show_next()
        self.assertEqual((v._index, v._url), (1, "b.png"))
        v.show_prev()
        self.assertEqual((v._index, v._url), (0, "a.png"))

    def test_wraps_around(self):
        v = self._viewer(["a.png", "b.png", "c.png"], index=2)
        v.show_next()
        self.assertEqual(v._index, 0)
        v.show_prev()
        self.assertEqual(v._index, 2)

    def test_single_image_navigation_noop(self):
        v = self._viewer(["a.png"])
        v.show_next()
        self.assertEqual(v._url, "a.png")

    def test_arrow_keys_navigate(self):
        v = self._viewer(["a.png", "b.png"])
        v.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Right,
                                  Qt.NoModifier))
        self.assertEqual(v._index, 1)


class ZoomRotateTest(unittest.TestCase):

    def setUp(self):
        loader = _FakeLoader(original=_pm(400, 300))
        with mock.patch.object(ImageViewer, "_start_load"):
            self.v = ImageViewer("a.png", loader)
        self.addCleanup(self.v.deleteLater)

    def test_zoom_in_enlarges_scaled(self):
        w0 = self.v._scaled.width()
        self.v.zoom_in()
        self.assertGreater(self.v._zoom, 1.0)
        self.assertGreater(self.v._scaled.width(), w0)

    def test_zoom_out_after_in_returns_to_fit(self):
        self.v.zoom_in()
        self.v.zoom_out()
        self.assertAlmostEqual(self.v._zoom, 1.0)

    def test_reset_restores_zoom_and_rotation(self):
        self.v.zoom_in()
        self.v.rotate_image()
        self.v.reset_view()
        self.assertEqual(self.v._zoom, 1.0)
        self.assertEqual(self.v._rotation, 0)

    def test_rotate_swaps_aspect(self):
        self.v.rotate_image()
        self.assertEqual(self.v._rotation, 90)
        # 源图 4:3，旋转后显示尺寸变高大于宽
        dpr = self.v._scaled.devicePixelRatio() or 1.0
        self.assertGreater(self.v._scaled.height() / dpr,
                           self.v._scaled.width() / dpr)

    def test_wheel_zooms(self):
        from PyQt5.QtGui import QWheelEvent
        from PyQt5.QtCore import QPoint, QPointF
        event = QWheelEvent(QPointF(100, 100), QPointF(100, 100),
                            QPoint(0, 0), QPoint(0, 120),
                            Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase,
                            False)
        self.v.wheelEvent(event)
        self.assertGreater(self.v._zoom, 1.0)


class SaveTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_default_name_strips_query_and_illegal_chars(self):
        loader = _FakeLoader()
        with mock.patch.object(ImageViewer, "_start_load"):
            v = ImageViewer(
                "https://s/file-upload/abc/pic%201.png?download=1", loader)
        self.addCleanup(v.deleteLater)
        self.assertEqual(v._default_save_name(), "pic 1.png")

    def test_placeholder_not_saved(self):
        loader = _FakeLoader(thumb=_pm(20, 20))
        with mock.patch.object(ImageViewer, "_start_load"):
            v = ImageViewer("a.png", loader)
        self.addCleanup(v.deleteLater)
        self.assertTrue(v._placeholder)
        self.assertIsNone(v._best_save_pixmap())

    def test_save_as_writes_file(self):
        loader = _FakeLoader(original=_pm(64, 48))
        with mock.patch.object(ImageViewer, "_start_load"):
            v = ImageViewer("a.png", loader)
        self.addCleanup(v.deleteLater)
        path = os.path.join(self.tmp.name, "saved.png")
        with mock.patch("ui.image_viewer.QFileDialog.getSaveFileName",
                        return_value=(path, "")):
            v.save_as()
        self.assertTrue(os.path.exists(path))
        pm = QPixmap()
        self.assertTrue(pm.load(path))
        self.assertEqual((pm.width(), pm.height()), (64, 48))

    def test_cancel_save_writes_nothing(self):
        loader = _FakeLoader(original=_pm(10, 10))
        with mock.patch.object(ImageViewer, "_start_load"):
            v = ImageViewer("a.png", loader)
        self.addCleanup(v.deleteLater)
        with mock.patch("ui.image_viewer.QFileDialog.getSaveFileName",
                        return_value=("", "")):
            v.save_as()
        self.assertFalse(os.listdir(self.tmp.name))


class StaleReplyTest(unittest.TestCase):

    def test_old_reply_after_switch_is_discarded(self):
        loader = _FakeLoader(original=None, thumb=None)
        with mock.patch.object(ImageViewer, "_start_load"):
            v = ImageViewer("a.png", loader, urls=["a.png", "b.png"])
        self.addCleanup(v.deleteLater)
        stale = mock.Mock()
        current = mock.Mock()
        v._reply = current
        v._on_finished(stale)
        stale.error.assert_not_called()
        self.assertIsNone(v._pixmap)
        self.assertIs(v._reply, current)


class EscapeTest(unittest.TestCase):

    def test_escape_closes(self):
        loader = _FakeLoader(original=_pm(10, 10))
        with mock.patch.object(ImageViewer, "_start_load"):
            v = ImageViewer("a.png", loader)
        self.addCleanup(v.deleteLater)
        with mock.patch.object(v, "close") as m_close:
            v.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape,
                                      Qt.NoModifier))
            m_close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
