"""消息文本选区：命中、拖拽延伸、复制，以及选区状态不破坏其他交互。

选区不建任何覆盖层控件——覆盖层会重影且鼠标事件向父级传播导致
递归爆栈（都已踩过）。选区状态存模型，委托绘制高亮。

运行：python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PyQt5.QtGui import QKeyEvent, QMouseEvent  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from core.models import Message, User  # noqa: E402
from ui.chat_view import ChatView  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _sender(i=0):
    return User(id=f"u{i}", username=f"user{i}", name=f"用户{i}", avatar_url="")


def _msg(n, text="这是一条测试消息内容"):
    return Message(id=f"m{n}", room_id="r", sender=_sender(),
                   timestamp=1700000000.0 + n * 60, msg_type="text", text=text)


def _merge_msg(n):
    """合并转发卡片消息，等价于接收端 _parse_merge_body 的 forward_list。"""
    return Message(
        id=f"m{n}", room_id="r", sender=_sender(),
        timestamp=1700000000.0 + n * 60, msg_type="merge",
        text=f"聊天记录·共2条",
        forward_list=[
            {"sender": "张三", "ts": 1700000100.0, "ts_text": "10:30",
             "text": "看这个 [a.png](https://s.example/a.png)",
             "att": {"title": "a.png", "image_url": "https://s.example/a.png"}},
            {"sender": "李四", "ts": 1700000200.0, "ts_text": "10:31",
             "text": "纯文本一条", "att": None},
        ])



def _mouse(view, type_, pos):
    # QMouseEvent(type, pos, button, buttons, modifiers)：
    # move 期间 buttons 必须是 LeftButton，否则拖拽分支不触发
    ev = QMouseEvent(type_, QPointF(pos), Qt.LeftButton,
                     Qt.LeftButton if type_ != QEvent.MouseButtonRelease
                     else Qt.NoButton, Qt.NoModifier)
    QApplication.sendEvent(view.viewport(), ev)
    QApplication.sendEvent(view, ev)


class TestTextSelection(unittest.TestCase):

    def setUp(self):
        self.view = ChatView()
        self.view.resize(640, 420)
        self.view.set_room("r")
        msgs = [_msg(i) for i in range(6)]
        self.view.model.prepend_messages_immediate(msgs)
        self.model = self.view.model

    def _bubble_center(self, row=2):
        idx = self.model.index(row, 0)
        bubble, avatar, image = self.view._delegate.layout_rects(
            idx, self.view.visualRect(idx))
        return bubble, bubble.center()

    def test_press_sets_selection_anchor(self):
        bubble, center = self._bubble_center()
        _mouse(self.view, QEvent.MouseButtonPress, center)
        self.assertEqual(self.model.sel_msg_id, self.model.get_message(2).id)
        self.assertEqual(self.model.sel_start, self.model.sel_end)

    def test_drag_extends_selection(self):
        bubble, center = self._bubble_center()
        _mouse(self.view, QEvent.MouseButtonPress, center)
        _mouse(self.view, QEvent.MouseMove, QPoint(center.x() - 30,
                                                   center.y()))
        self.assertNotEqual(self.model.sel_start, self.model.sel_end)

    def test_selected_text_copies_to_clipboard(self):
        bubble, center = self._bubble_center()
        _mouse(self.view, QEvent.MouseButtonPress, center)
        _mouse(self.view, QEvent.MouseMove,
               QPoint(bubble.left() + 2, bubble.top() + 2))
        text = self.view._selected_text()
        self.assertTrue(text, "拖拽后应有选中文本")
        _mouse(self.view, QEvent.MouseButtonRelease, QPoint(bubble.left() + 2,
                                                             bubble.top() + 2))
        self.assertIn(text, _msg(2).text)

    def test_press_outside_clears_selection(self):
        bubble, center = self._bubble_center()
        _mouse(self.view, QEvent.MouseButtonPress, center)
        self.view._clear_text_selection()
        self.assertIsNone(self.model.sel_msg_id)

    def test_mention_html_has_anchor(self):
        """提及消息渲染出的文档必须包含 mention: anchor，点击才能弹卡片。"""
        from core import markdown
        msg = Message(id="mm", room_id="r", sender=_sender(),
                      timestamp=1.0, msg_type="text",
                      text="你好 @zhangsan 看看",
                      mentions=[{"id": "u9", "username": "zhangsan",
                                 "name": "张三"}])
        doc = self.model.build_text_doc(msg)
        self.assertIn("mention:", doc.toHtml(), "提及未渲染为 anchor")

    def test_markdown_and_mention_height_match_paint_doc(self):
        """行高与绘制用同一文档后，高度应等于绘制文档实际高度。"""
        msg = Message(id="md", room_id="r", sender=_sender(),
                      timestamp=1.0, msg_type="text",
                      text="**加粗** @zhangsan 很长的一行内容需要换行显示呀" * 3,
                      mentions=[{"id": "u9", "username": "zhangsan",
                                 "name": "张三三三"}])
        avail = self.model._available_text_width(0)
        _w, h = self.model.text_bubble_metrics(msg, avail)
        doc = self.model.build_text_doc(msg)
        doc.setTextWidth(_w)
        self.assertEqual(h, int(doc.size().height()))

    def test_paint_with_selection_does_not_raise(self):
        """选中态绘制抛异常会导致整行只剩背景（真机气泡变空白）。

        PyQt5 的 QAbstractTextDocumentLayout.Selection 是字段式结构体，
        不能按 PyQt6 的 Selection(cursor, format) 构造。
        """
        from PyQt5.QtCore import QRect
        from PyQt5.QtGui import QImage, QPainter
        from PyQt5.QtWidgets import QStyleOptionViewItem
        idx = self.model.index(2, 0)
        self.model.sel_msg_id = self.model.get_message(2).id
        self.model.sel_start = 2
        self.model.sel_end = 10
        opt = QStyleOptionViewItem()
        opt.rect = QRect(0, self.view.visualRect(idx).y(), 640,
                         self.view.visualRect(idx).height())
        img = QImage(640, 420, QImage.Format_ARGB32)
        img.fill(0xFFFFFFFF)
        painter = QPainter(img)
        try:
            self.view._delegate.paint(painter, opt, idx)  # 不应抛异常
        finally:
            painter.end()

    def test_ctrl_c_copies_selection_without_crash(self):
        """Ctrl+C 复制选区：曾因 event.Copy 写成 QKeyEvent 属性而崩溃。"""
        bubble, center = self._bubble_center()
        _mouse(self.view, QEvent.MouseButtonPress, center)
        _mouse(self.view, QEvent.MouseMove,
               QPoint(bubble.left() + 2, bubble.top() + 2))
        # 拖拽后产生选区
        QApplication.clipboard().clear()
        ev = QKeyEvent(QEvent.KeyPress, Qt.Key_C, Qt.ControlModifier)
        QApplication.sendEvent(self.view, ev)  # 不应抛 AttributeError
        self.assertTrue(QApplication.clipboard().text(),
                        "Ctrl+C 后剪贴板应有选中文本")


class TestMergeSelection(unittest.TestCase):
    """合并卡片文本选择：链接命中、条目内选区、复制。

    高度/绘制/命中共用同一 merge_content_layout，这里验证数据流：
    按下锚定 sel_merge_idx、拖拽延伸、_selected_text 取到条目文本。
    """

    def setUp(self):
        self.view = ChatView()
        self.view.resize(640, 620)
        self.view.set_room("r")
        self.view.model.prepend_messages_immediate([_merge_msg(0)])
        self.model = self.view.model

    def _idx(self):
        return self.model.index(0, 0)

    def _layout(self):
        return self.view._merge_layout(self.model.get_message(0), self._idx())

    def test_entry_layout_has_per_entry_doc(self):
        layout = self._layout()
        self.assertEqual(len(layout), 2)
        self.assertIsNotNone(layout[0]["doc"])
        self.assertIsNotNone(layout[1]["doc"])
        # 第一条内容含 markdown 链接，渲染成锚点
        html = layout[0]["doc"].toHtml()
        self.assertIn("https://s.example/a.png", html)

    def test_anchor_at_file_link_returns_href(self):
        item = self._layout()[0]
        # 沿内容行横向扫描，链接被渲染成锚点，命中处应返回 href
        msg = self.model.get_message(0)
        hrefs = set()
        y = item["rect"].top() + item["rect"].height() // 2
        for x in range(item["rect"].left(), item["rect"].right(), 2):
            h = self.view._merge_anchor_at(
                msg, self._idx(), QPoint(x, y))
            if h:
                hrefs.add(h)
        self.assertEqual(hrefs, {"https://s.example/a.png"})

    def test_anchor_plain_text_returns_none(self):
        # 第二条是纯文本，内容行无锚点
        msg = self.model.get_message(0)
        item2 = self._layout()[1]
        y = item2["rect"].top() + item2["rect"].height() // 2
        for x in range(item2["rect"].left(), item2["rect"].right(), 2):
            self.assertIsNone(
                self.view._merge_anchor_at(msg, self._idx(), QPoint(x, y)))

    def test_press_anchor_no_selection(self):
        item = self._layout()[0]
        y = item["rect"].top() + item["rect"].height() // 2
        hit = None
        msg = self.model.get_message(0)
        for x in range(item["rect"].left(), item["rect"].right(), 2):
            if self.view._merge_anchor_at(msg, self._idx(), QPoint(x, y)):
                hit = QPoint(x, y)
                break
        self.assertIsNotNone(hit, "应能扫描到链接像素")
        _mouse(self.view, QEvent.MouseButtonPress, hit)
        # 命中链接：打开文件，不进入选区
        self.assertIsNone(self.model.sel_msg_id)

    def test_press_on_text_sets_merge_anchor(self):
        item = self._layout()[0]
        mid = item["rect"].top() + item["rect"].height() // 2
        bottom_left = QPoint(item["rect"].left() + 2, mid)
        _mouse(self.view, QEvent.MouseButtonPress, bottom_left)
        self.assertEqual(self.model.sel_msg_id, self.model.get_message(0).id)
        self.assertEqual(self.model.sel_merge_idx, 0)
        self.assertEqual(self.model.sel_start, self.model.sel_end)

    def test_drag_extends_merge_selection(self):
        item = self._layout()[0]
        mid = item["rect"].top() + item["rect"].height() // 2
        p1 = QPoint(item["rect"].left() + 2, mid)
        p2 = QPoint(item["rect"].right() - 2, mid)
        _mouse(self.view, QEvent.MouseButtonPress, p1)
        _mouse(self.view, QEvent.MouseMove, p2)
        self.assertNotEqual(self.model.sel_start, self.model.sel_end)
        self.assertEqual(self.model.sel_merge_idx, 0)
        text = self.view._selected_text()
        self.assertIn("看这个", text)
        self.assertIn("a.png", text)
        _mouse(self.view, QEvent.MouseButtonRelease, p2)
        self.assertEqual(self.view._selected_text(), text)

    def test_second_entry_selection_independent(self):
        """两条被选在各自条目 doc 内，选区索引互不串。"""
        item1 = self._layout()[0]
        item2 = self._layout()[1]
        mid1 = item1["rect"].top() + item1["rect"].height() // 2
        mid2 = item2["rect"].top() + item2["rect"].height() // 2
        _mouse(self.view, QEvent.MouseButtonPress,
               QPoint(item1["rect"].left() + 2, mid1))
        _mouse(self.view, QEvent.MouseMove,
               QPoint(item1["rect"].right() - 2, mid1))
        self.assertEqual(self.model.sel_merge_idx, 0)
        _mouse(self.view, QEvent.MouseButtonPress,
               QPoint(item2["rect"].left() + 2, mid2))
        _mouse(self.view, QEvent.MouseMove,
               QPoint(item2["rect"].right() - 2, mid2))
        self.assertEqual(self.model.sel_merge_idx, 1)
        self.view._clear_text_selection()
        self.assertEqual(self.model.sel_merge_idx, -1)
        self.assertIsNone(self.model.sel_msg_id)


def _merge_image_msg(n):
    """含方案二图片条目的合并卡片：图片条目 text 恰为「右键打开链接查看原图」。"""
    return Message(
        id=f"m{n}", room_id="r", sender=_sender(),
        timestamp=1700000000.0 + n * 60, msg_type="merge",
        text=f"聊天记录·共1条",
        forward_list=[
            {"sender": "张三", "ts": 1700000100.0, "ts_text": "10:30",
             "text": "[右键打开链接查看原图](/x.png)",
             "att": {"title": "@张三 · 10:30", "image_url": "/x.png"}},
        ])


class TestMergeImageClick(unittest.TestCase):
    """合并卡片图片条目：点击弹原图查看器、悬停手型，不进文本选区。"""

    def setUp(self):
        self.view = ChatView()
        self.view.resize(640, 620)
        self.view.set_room("r")
        self.view.model.prepend_messages_immediate([_merge_image_msg(0)])
        self.model = self.view.model

    def _idx(self):
        return self.model.index(0, 0)

    def test_image_entry_layout_has_no_doc_and_rect(self):
        layout = self.view._merge_layout(self.model.get_message(0), self._idx())
        self.assertEqual(len(layout), 1)
        item = layout[0]
        # 图片条目：doc 为 None（不渲染文本），rect 有实际高度
        self.assertIsNone(item["doc"])
        self.assertGreaterEqual(item["rect"].height(), 40)
        self.assertEqual(
            (item["entry"].get("att") or {}).get("image_url"), "/x.png")

    def test_merge_image_at_returns_url(self):
        msg = self.model.get_message(0)
        item = self.view._merge_layout(msg, self._idx())[0]
        y = item["rect"].top() + item["rect"].height() // 2
        got = set()
        for x in range(item["rect"].left(), item["rect"].right(), 2):
            u = self.view._merge_image_at(msg, self._idx(), QPoint(x, y))
            if u:
                got.add(u)
        self.assertEqual(got, {"/x.png"})

    def test_press_image_emits_viewer_signal_no_selection(self):
        revealed = []
        self.view.inline_image_view_requested.connect(revealed.append)
        msg = self.model.get_message(0)
        item = self.view._merge_layout(msg, self._idx())[0]
        mid = item["rect"].top() + item["rect"].height() // 2
        hit = QPoint(item["rect"].left() + item["rect"].width() // 2, mid)
        _mouse(self.view, QEvent.MouseButtonPress, hit)
        self.assertEqual(revealed, ["/x.png"])
        # 命中了图片，不进入文本选区
        self.assertIsNone(self.model.sel_msg_id)


def _image_msg(n, caption="这个"):
    """官方图文混排：msg 正文为空，说明在附件 description。"""
    return Message(id=f"m{n}", room_id="r", sender=_sender(),
                   timestamp=1700000000.0 + n * 60, msg_type="image",
                   text="", description=caption,
                   file_url="https://s.example/a.png")


class TestImageCaptionSelection(unittest.TestCase):
    """图片气泡下方的说明文字：可部分选中、复制，图片区域仍走查看器。

    命中/拖拽/取文本必须与委托绘制同源（说明画在图片下方的通栏区域，
    不是文本气泡的内边距坐标系）。
    """

    def setUp(self):
        self.view = ChatView()
        self.view.resize(640, 520)
        self.view.set_room("r")
        self.view.model.prepend_messages_immediate([_image_msg(0)])
        self.model = self.view.model

    def _bubble(self):
        idx = self.model.index(0, 0)
        bubble, _avatar, _img = self.view._delegate.layout_rects(
            idx, self.view.visualRect(idx))
        return bubble

    def _caption_top(self, bubble):
        msg = self.model.get_message(0)
        doc = self.model._caption_doc(
            self.model.image_caption(msg), bubble.width())
        return bubble.top() + bubble.height() - int(doc.size().height())

    def test_caption_hit_returns_position(self):
        bubble = self._bubble()
        top = self._caption_top(bubble)
        hit, doc = self.view._text_hit_pos(
            self.model.get_message(0), bubble,
            QPoint(bubble.left() + 2, top + 2), fuzzy=True)
        self.assertIsNotNone(hit)
        self.assertEqual(doc.toPlainText(), "这个")

    def test_image_area_hit_returns_none(self):
        bubble = self._bubble()
        hit, _doc = self.view._text_hit_pos(
            self.model.get_message(0), bubble,
            QPoint(bubble.left() + 10, bubble.top() + 10), fuzzy=True)
        self.assertIsNone(hit, "图片本体不应进入文本选区")

    def test_press_and_drag_selects_caption(self):
        bubble = self._bubble()
        top = self._caption_top(bubble)
        _mouse(self.view, QEvent.MouseButtonPress,
               QPoint(bubble.left() + 1, top + 2))
        self.assertEqual(self.model.sel_msg_id, self.model.get_message(0).id)
        _mouse(self.view, QEvent.MouseMove,
               QPoint(bubble.right() - 1, top + 2))
        self.assertEqual(self.view._selected_text(), "这个")
        _mouse(self.view, QEvent.MouseButtonRelease,
               QPoint(bubble.right() - 1, top + 2))

    def test_selected_text_range_from_caption(self):
        msg = self.model.get_message(0)
        self.model.sel_msg_id = msg.id
        self.model.sel_start = 0
        self.model.sel_end = 1
        self.model.sel_merge_idx = -1
        self.assertEqual(self.view._selected_text(), "这")

    def test_copy_without_selection_uses_caption(self):
        QApplication.clipboard().clear()
        self.view._copy_to_clipboard(self.model.get_message(0))
        self.assertEqual(QApplication.clipboard().text(), "这个")

    def test_press_image_opens_viewer_not_selection(self):
        revealed = []
        self.view.image_view_requested.connect(revealed.append)
        bubble = self._bubble()
        _mouse(self.view, QEvent.MouseButtonPress,
               QPoint(bubble.left() + 10, bubble.top() + 10))
        self.assertEqual(len(revealed), 1)
        self.assertIsNone(self.model.sel_msg_id)

    def test_paint_caption_selection_does_not_raise(self):
        from PyQt5.QtCore import QRect
        from PyQt5.QtGui import QImage, QPainter
        from PyQt5.QtWidgets import QStyleOptionViewItem
        idx = self.model.index(0, 0)
        self.model.sel_msg_id = self.model.get_message(0).id
        self.model.sel_start = 0
        self.model.sel_end = 2
        opt = QStyleOptionViewItem()
        opt.rect = QRect(0, self.view.visualRect(idx).y(), 640,
                         self.view.visualRect(idx).height())
        img = QImage(640, 520, QImage.Format_ARGB32)
        img.fill(0xFFFFFFFF)
        painter = QPainter(img)
        try:
            self.view._delegate.paint(painter, opt, idx)
        finally:
            painter.end()

    def test_narrow_image_caption_widens_bubble(self):
        """窄图长说明：气泡按说明撑宽，图片矩形保持原始宽，
        选区文档换行宽与气泡同源（说明不再被挤成一字一行）。"""
        self.model._get_image_display_size = lambda m: (140, 100)
        long_cap = _image_msg(
            1, caption="有同事反馈第一项不过，是代码检查问题。"
                       "我给他流水线retry了一下，就又通过了。")
        self.view.model.append_message(long_cap)
        msg = self.model.get_message(1)
        idx = self.model.index(1, 0)
        bubble, _avatar, img = self.view._delegate.layout_rects(
            idx, self.view.visualRect(idx))
        self.assertGreater(bubble.width(), 140)
        self.assertEqual(img.width(), 140)
        doc, _origin = self.view._selectable_doc(msg, bubble)
        self.assertIsNotNone(doc)
        self.assertEqual(doc.textWidth(), bubble.width())


class TestImageWithoutCaption(unittest.TestCase):
    """无说明的图片消息不进入文本选区，点击图片仍是查看器。"""

    def setUp(self):
        self.view = ChatView()
        self.view.resize(640, 420)
        self.view.set_room("r")
        self.view.model.prepend_messages_immediate([_image_msg(0, caption="")])
        self.model = self.view.model

    def test_no_caption_no_hit(self):
        idx = self.model.index(0, 0)
        bubble, _a, _i = self.view._delegate.layout_rects(
            idx, self.view.visualRect(idx))
        hit, _doc = self.view._text_hit_pos(
            self.model.get_message(0), bubble, bubble.center(), fuzzy=True)
        self.assertIsNone(hit)

    def test_press_does_not_set_selection(self):
        idx = self.model.index(0, 0)
        bubble, _a, _i = self.view._delegate.layout_rects(
            idx, self.view.visualRect(idx))
        _mouse(self.view, QEvent.MouseButtonPress,
               QPoint(bubble.left() + 10, bubble.top() + 10))
        self.assertIsNone(self.model.sel_msg_id)


if __name__ == "__main__":
    unittest.main()
