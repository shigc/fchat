"""markdown 内联图在 MessageListModel 里的加载/重绘链路验证。

锁住：图到货后按消息 id 重绘（_on_inline_image_ready 触发 dataChanged），
且 build_text_doc 会把占位尺寸改写为真实显示尺寸供高度与绘制同源使用。
可点观感与真实加载需本人开真实窗口确认。
"""
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QObject, QUrl, pyqtSignal  # noqa: E402
from PyQt5.QtGui import QColor, QImage, QPixmap, QTextCursor, QTextDocument  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from core.models import Message, User  # noqa: E402
from ui.message_model import MessageListModel  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class StubLoader(QObject):
    image_ready = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.served = {}
        self.orig = {}

    def resolve(self, url):
        return "https://s.example.com" + url if url.startswith("/") else url

    def pixmap_for(self, url, size):
        if url in self.served:
            if url not in self.orig:
                self.orig[url] = (self.served[url].width(), self.served[url].height())
            return self.served[url]
        return None

    def original_size(self, url):
        return self.orig.get(url)

    def is_failed(self, url, size):
        return False


def _msg(mid, text):
    return Message(id=mid, room_id="r", msg_type="text", text=text,
                   sender=User(id="u1", username="a", name="AA"), timestamp=1.0)


def _img_geom(doc):
    """返回 doc 里第一个 image 的 (src, w, h, has_resource)。"""
    cur = QTextCursor(doc)
    cur.movePosition(cur.Start)
    while True:
        fmt = cur.charFormat()
        if fmt.isImageFormat():
            im = fmt.toImageFormat()
            res = doc.resource(QTextDocument.ImageResource, QUrl(im.name())) is not None
            return im.name(), im.width(), im.height(), res
        if cur.atEnd():
            return None
        cur.movePosition(cur.NextCharacter)


class TestInlineImageModel(unittest.TestCase):

    def setUp(self):
        self.model = MessageListModel()
        self.loader = StubLoader()
        self.model.set_image_loader(self.loader)

    def test_build_text_doc_uses_placeholder_before_arrival(self):
        msg = _msg("m1", "看 ![图](https://s.example.com/a.png) 吧")
        self.model.prepend_messages_immediate([msg])
        doc = self.model.build_text_doc(msg)
        g = _img_geom(doc)
        self.assertIsNotNone(g)
        self.assertEqual(g[0], "https://s.example.com/a.png")
        self.assertEqual((g[1], g[2]), (240, 180))  # 占位

    def test_image_ready_rewrites_size_and_emits_data_changed(self):
        url = "https://s.example.com/a.png"
        msg = _msg("m1", "图 ![t](%s) 结束" % url)
        self.model.prepend_messages_immediate([msg])

        # 到货前 build 一次，登记等待并触发请求
        self.model.build_text_doc(msg)

        _img = QImage(200, 100, QImage.Format_ARGB32)
        _img.fill(QColor("red"))
        pm = QPixmap.fromImage(_img)
        self.loader.served[url] = pm
        self.loader.orig[url] = (200, 100)

        seen = []
        self.model.dataChanged.connect(lambda tl, br, roles=None: seen.append(tl.row()))
        self.loader.image_ready.emit(url)

        self.assertIn(url, self.model._inline_sizes)
        self.assertEqual(self.model._inline_sizes[url], (240, 120))
        self.assertEqual(seen, [0])

        # 重 build：尺寸已改写为真实值，且资源按 src 注入
        doc = self.model.build_text_doc(msg)
        g = _img_geom(doc)
        self.assertEqual((g[1], g[2]), (240, 120))
        self.assertTrue(g[3])

    def test_relative_path_resolved_and_injected(self):
        # 相对路径 /file-upload/...：contented 原子 src 注入，实际加载用 resolved
        msg = _msg("m1", "![p](/file-upload/abc.png)")
        self.model.prepend_messages_immediate([msg])
        self.model.build_text_doc(msg)
        self.assertIn("https://s.example.com/file-upload/abc.png",
                      self.model._inline_url_msgs)


if __name__ == "__main__":
    unittest.main()
