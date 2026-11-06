"""聊天图片渲染链路验证。

锁住三件事（都是这次"一直显示加载图片"的相关缺陷）：
1. 委托必须真的去查图并登记等待，而不是只画占位符；
2. 图到货后按**消息 id** 找回所在行重绘 —— 等待期间加载历史会让行号位移；
3. 图像气泡高度固定，到货只重绘、不改变布局（否则会打乱滚动定位）。

运行：python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QObject, QRect, pyqtSignal  # noqa: E402
from PyQt5.QtGui import QColor, QImage, QPainter, QPixmap  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from core.models import Message, User  # noqa: E402
from ui.chat_view import ChatView  # noqa: E402
from ui.message_model import RoleMessage  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _red_pixmap(w=240, h=180):
    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(QColor("red"))
    return QPixmap.fromImage(img)


class StubImageLoader(QObject):
    """替身：按需返回缓存图，不发网络请求。"""

    image_ready = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.served = {}       # url -> QPixmap
        self.failed = set()
        self.queried = []      # 记录 pixmap_for 被问过哪些 url

    def resolve(self, url):
        return "https://s.example.com" + url if url.startswith("/") else url

    def pixmap_for(self, url, size):
        self.queried.append((url, size))
        return self.served.get(url)

    def is_failed(self, url, size):
        return url in self.failed

    def original_size(self, url):
        return None


class StubAvatarLoader(QObject):
    avatar_ready = pyqtSignal(str)

    def pixmap_for(self, url):
        return None

    def is_failed(self, url):
        return False


def _image_msg(mid="img1", url="/file-upload/pic.png"):
    return Message(id=mid, room_id="r", msg_type="image", file_url=url,
                   text="", sender=User(id="u1", username="alice", name="小艾"),
                   timestamp=1700000000.0)


def _text_msg(n):
    return Message(id=f"t{n}", room_id="r", text=f"普通消息 {n}",
                   sender=User(id="u2", username="bob", name="小博"),
                   timestamp=1700000000.0 + n)


def _paint_row(view, row):
    """走真实绘制路径，让委托完成"查图 + 登记等待"。"""
    target = QPixmap(700, 300)
    target.fill(QColor("white"))
    idx = view.model.index(row, 0)
    painter = QPainter(target)
    try:
        view._delegate._paint_bubble_content(
            painter, QRect(10, 10, 500, 220), idx.data(RoleMessage))
    finally:
        painter.end()


class TestImagePainting(unittest.TestCase):

    def setUp(self):
        self.view = ChatView()
        self.view.resize(700, 400)
        self.view.set_room("r")
        self.loader = StubImageLoader()
        self.view.set_image_loader(self.loader)
        self.view.set_avatar_loader(StubAvatarLoader())
        self.delegate = self.view._delegate
        self.url = self.loader.resolve("/file-upload/pic.png")

    def test_asks_loader_and_registers_pending(self):
        self.view.model.prepend_messages_immediate([_image_msg()])
        _paint_row(self.view, 0)
        self.assertIn((self.url, 240), self.loader.queried,
                      "委托没有向加载器取图（就是永远卡占位符的原因）")
        self.assertEqual(self.delegate._img_pending.get(self.url), {"img1"})

    def test_arrival_repaints_row_after_rows_shift(self):
        """等待期间向上翻历史会在行首插行，到货必须找到位移后的新行号。"""
        self.view.model.prepend_messages_immediate([_image_msg()])
        _paint_row(self.view, 0)

        older = [_text_msg(n) for n in range(10)]
        self.view.model.prepend_messages_immediate(older)
        moved = self.view.model.find_message_row("img1")
        self.assertEqual(moved, 10, "用例前提：img1 应被推到第 10 行")

        seen = []
        self.view.model.dataChanged.connect(
            lambda tl, br, roles=None: seen.append(tl.row()))
        self.loader.served[self.url] = _red_pixmap()
        self.loader.image_ready.emit(self.url)

        self.assertEqual(seen, [moved],
                         f"到货刷错了行：期望 {moved}，实际 {seen}")
        # 重绘后不再需要等待
        self.assertNotIn(self.url, self.delegate._img_pending)

    def test_height_stable_before_and_after_arrival(self):
        """行高不能随图片到货变化，否则滚动定位会被打乱。"""
        msg = _image_msg()
        self.view.model.prepend_messages_immediate([msg])
        before = self.view.model._get_height(0)
        _paint_row(self.view, 0)
        self.loader.served[self.url] = _red_pixmap()
        self.view.model.invalidate_height_cache()
        after = self.view.model._get_height(0)
        self.assertEqual(before, after)
        self.assertGreater(before, 0)

    def test_failed_url_shows_failure_and_stops_waiting(self):
        self.view.model.prepend_messages_immediate([_image_msg()])
        self.loader.failed.add(self.url)
        _paint_row(self.view, 0)
        self.assertNotIn(self.url, self.delegate._img_pending,
                         "已失败的图不该继续登记等待（否则每帧重发请求）")

    def test_pending_cleared_on_room_switch(self):
        self.view.model.prepend_messages_immediate([_image_msg()])
        _paint_row(self.view, 0)
        self.assertTrue(self.delegate._img_pending)
        self.view.set_room("other")
        self.assertEqual(self.delegate._img_pending, {})

    def test_avatar_pending_also_routes_by_message_id(self):
        """头像与图片共用同一套等待/回投机制。"""
        loader = StubAvatarLoader()
        self.view.set_avatar_loader(loader)
        msg = _text_msg(1)
        msg.sender.avatar_url = "https://s.example.com/avatar/bob"
        self.view.model.prepend_messages_immediate([msg])
        target = QPixmap(700, 300)
        target.fill(QColor("white"))
        painter = QPainter(target)
        try:
            self.delegate._paint_avatar(
                painter, 10, 10, "小博", msg.sender.avatar_url, msg_id=msg.id)
        finally:
            painter.end()
            self.assertEqual(
                self.delegate._img_pending.get(msg.sender.avatar_url), {"t1"})


if __name__ == "__main__":
    unittest.main()
