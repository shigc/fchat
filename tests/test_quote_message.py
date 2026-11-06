"""引用消息：permalink/附件解析、send_text 溯源、get_message、引用块渲染同源。"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QRect  # noqa: E402
from PyQt5.QtGui import QFontMetrics  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from core.messages import parse_message  # noqa: E402
from core.models import Message, User  # noqa: E402
from core.api_client import ApiClient  # noqa: E402
from ui import message_model as mm  # noqa: E402
from ui.message_model import MessageListModel  # noqa: E402

SERVER = "https://chat.example.com"
LINK = f"{SERVER}/direct/room1?msg=abc123"


def _mk_msg(**kw):
    defaults = dict(id="m1", room_id="r", text="hello",
                    sender=User(username="u", name="张三"),
                    timestamp=1789000000.0, msg_type="text")
    defaults.update(kw)
    return Message(**defaults)


class TestPermalinkParse(unittest.TestCase):

    def test_pure_link_is_quote(self):
        m = parse_message({"_id": "a", "msg": LINK,
                           "u": {"_id": "u1", "username": "alice"}}, "room1")
        self.assertEqual(m.msg_type, "quote")
        self.assertEqual(m.quote_msg_id, "abc123")
        self.assertEqual(m.text, "")

    def test_link_with_reply_text(self):
        m = parse_message({"_id": "a", "msg": f"看看这个 {LINK} 谢谢",
                           "u": {"_id": "u1"}}, "room1")
        self.assertEqual(m.msg_type, "quote")
        self.assertEqual(m.quote_msg_id, "abc123")
        # 链接剥离后剩余正文完整保留
        self.assertIn("看看这个", m.text)
        self.assertIn("谢谢", m.text)
        self.assertNotIn("https://", m.text)

    def test_channel_link_recognized(self):
        m = parse_message({"_id": "a",
                           "msg": "https://chat.example.com/channel/general?msg=x-9",
                           "u": {"_id": "u1"}}, "general")
        self.assertEqual(m.msg_type, "quote")
        self.assertEqual(m.quote_msg_id, "x-9")

    def test_plain_text_untouched(self):
        m = parse_message({"_id": "a", "msg": "https://chat.example.com/home",
                           "u": {"_id": "u1"}}, "room1")
        self.assertEqual(m.msg_type, "text")

    def test_text_quote_stores_link(self):
        m = parse_message({"_id": "a", "msg": LINK,
                           "u": {"_id": "u1"}}, "room1")
        self.assertEqual(m.quote_link, LINK)

    def test_web_style_wrapped_link_fully_stripped(self):
        """网页版把 permalink 包成空 label 链接 [ ](permalink)，连壳剥掉，
        不能留下 "[]()" 残壳。"""
        m = parse_message(
            {"_id": "a", "msg": f"[ ]({LINK})\n私聊的话 暂时关闭E2EE",
             "u": {"_id": "u1"}}, "room1")
        self.assertEqual(m.msg_type, "quote")
        self.assertEqual(m.quote_msg_id, "abc123")
        self.assertEqual(m.text, "私聊的话 暂时关闭E2EE")
        self.assertNotIn("[](", m.text)
        self.assertNotIn("https://", m.text)

    def test_wrapped_link_with_label_stripped(self):
        m = parse_message(
            {"_id": "a", "msg": f"[引用]({LINK})\n正文",
             "u": {"_id": "u1"}}, "room1")
        self.assertEqual(m.msg_type, "quote")
        self.assertEqual(m.text, "正文")


class TestAttachmentQuoteParse(unittest.TestCase):

    def test_message_link_attachment_is_quote(self):
        m = parse_message(
            {"_id": "a", "msg": "回复正文", "u": {"_id": "u1"},
             "attachments": [{"author_name": "李四", "text": "原话内容",
                              "message_link": LINK}]}, "room1")
        self.assertEqual(m.msg_type, "quote")
        self.assertEqual(m.text, "回复正文")
        self.assertEqual(m.description, "原话内容")
        self.assertEqual(m.quote_author, "李四")
        self.assertEqual(m.quote_msg_id, "abc123")

    def test_author_name_text_without_link_is_quote(self):
        m = parse_message(
            {"_id": "a", "msg": "回复", "u": {"_id": "u1"},
             "attachments": [{"author_name": "李四", "text": "旧格式引用"}]},
            "room1")
        self.assertEqual(m.msg_type, "quote")
        self.assertEqual(m.quote_author, "李四")
        self.assertEqual(m.quote_msg_id, "")

    def test_title_link_still_file(self):
        m = parse_message(
            {"_id": "a", "msg": "", "u": {"_id": "u1"},
             "attachments": [{"title": "doc.pdf",
                              "title_link": "/file-upload/doc.pdf"}]},
            "room1")
        self.assertEqual(m.msg_type, "file")
        self.assertEqual(m.file_name, "doc.pdf")

    def test_web_quote_empty_text_with_nested_image(self):
        """网页版引用附件 text 为空、原文附件嵌在子 attachments：
        仍识别为引用，且解析期就能拿到作者与缩略图 URL。"""
        raw = json.dumps({
            "_id": "a", "rid": "general",
            "msg": "[ ](https://chat.example.com/channel/general?msg=img1)"
                   "\n私聊的话 暂时关闭E2EE",
            "u": {"_id": "u1"},
            "attachments": [{
                "text": "", "message_link":
                    "https://chat.example.com/channel/general?msg=img1",
                "author_name": "阳俊杰",
                "attachments": [{
                    "title": "Clipboard.png",
                    "title_link": "/file-upload/x/Clipboard.png",
                    "image_url": "/file-upload/x/Clipboard.png",
                    "image_type": "image/png",
                    "description": "这个",
                }],
            }],
        }, ensure_ascii=False)
        m = parse_message(json.loads(raw), "general")
        self.assertEqual(m.msg_type, "quote")
        self.assertEqual(m.quote_author, "阳俊杰")
        self.assertEqual(m.quote_msg_id, "img1")
        self.assertEqual(m.quote_image_url, "/file-upload/x/Clipboard.png")
        # 图文混排被引用的说明在子附件 description（父附件 text 为空）
        self.assertEqual(m.description, "这个")
        self.assertEqual(m.text, "私聊的话 暂时关闭E2EE")

    def test_quote_caption_from_nested_image_description(self):
        """服务端展开的图文引用：说明在子附件 description，引用块要显示它。"""
        m = parse_message(
            {"_id": "a", "msg": "", "u": {"_id": "u1"},
             "attachments": [{
                 "text": "", "author_name": "黄天豪",
                 "message_link": LINK,
                 "attachments": [{
                     "title": "Clipboard.png",
                     "title_link": "/file-upload/x/Clipboard.png",
                     "image_url": "/file-upload/x/Clipboard.png",
                     "description": "这个",
                 }],
             }]}, "room1")
        self.assertEqual(m.quote_image_url, "/file-upload/x/Clipboard.png")
        self.assertEqual(m.description, "这个")

    def test_web_quote_nested_file(self):
        """子附件是文件（无 image_url）时记下文件名供引用块画图标。"""
        m = parse_message(
            {"_id": "a", "msg": "回复", "u": {"_id": "u1"},
             "attachments": [{
                 "text": "", "author_name": "李四",
                 "message_link": LINK,
                 "attachments": [{"title": "报告.pdf",
                                  "title_link": "/file-upload/y/报告.pdf"}],
             }]}, "room1")
        self.assertEqual(m.msg_type, "quote")
        self.assertEqual(m.quote_file_name, "报告.pdf")
        self.assertEqual(m.quote_image_url, "")

    def test_attachment_quote_msg_permalink_stripped(self):
        """网页版引用的 msg 字段可能是 permalink，绝不能显示在回复正文里。"""
        m = parse_message(
            {"_id": "a", "msg": LINK, "u": {"_id": "u1"},
             "attachments": [{"author_name": "李四", "text": "原话内容",
                              "message_link": "/direct/room1?msg=abc123"}]},
            "room1")
        self.assertEqual(m.msg_type, "quote")
        self.assertEqual(m.text, "")
        self.assertNotIn("http", m.text)
        self.assertNotIn("https://", m.description)
        # 引用 id 优先来自附件 message_link，permalink 存档供转发
        self.assertEqual(m.quote_msg_id, "abc123")
        self.assertEqual(m.quote_link, "/direct/room1?msg=abc123")

    def test_quote_forward_carries_message_link(self):
        from ui.forward_dialog import build_forward_payload
        msg = _mk_msg(msg_type="quote", text="回复", description="原话",
                      quote_link="/direct/room1?msg=abc123")
        text, atts = build_forward_payload(msg, lambda u: u)
        # 转发带「转发的消息」标记 + 原文回复正文，附件仍保留原始 permalink
        self.assertIn("\n回复", text)
        self.assertIn("[转发消息]", text)
        self.assertEqual(atts[0]["message_link"], "/direct/room1?msg=abc123")


class TestSendTextQuote(unittest.TestCase):

    def setUp(self):
        self.client = ApiClient(SERVER)
        self.client._rc = mock.Mock()
        self.client._rc.chat_post_message.return_value = {
            "message": {"_id": "s1", "msg": "回复", "rid": "room1",
                        "u": {"_id": "me", "username": "me"},
                        "ts": "2026-01-01T00:00:00.000Z"}}

    def test_quote_permalink_in_body(self):
        # 网页版规范：permalink 嵌进正文首行（空标签 markdown），不塞附件；
        # 服务器据此展开成引用附件。手工附件服务器不认（不会持久化成引用）。
        self.client.send_text(
            "room1", "回复", quote_sender="李四", quote_text="原话",
            quote_msg_id="abc123", quote_link="/direct/room1?msg=abc123")
        args, kwargs = self.client._rc.chat_post_message.call_args
        self.assertEqual(args[0], f"[ ]({SERVER}/direct/room1?msg=abc123)\n回复")
        self.assertIsNone(kwargs.get("attachments"))

    def test_absolute_quote_link_passed_through(self):
        abs_link = f"{SERVER}/direct/room1?msg=abc123"
        self.client.send_text(
            "room1", "回复", quote_sender="李四", quote_text="原话",
            quote_msg_id="abc123", quote_link=abs_link)
        args, _kwargs = self.client._rc.chat_post_message.call_args
        self.assertEqual(args[0], f"[ ]({abs_link})\n回复")

    def test_relative_link_absolutized_with_server_url(self):
        # 服务端要求 permalink 为绝对 URL，相对地址需用登录地址补全
        self.client.send_text(
            "room1", "回复", quote_sender="李四", quote_text="原话",
            quote_msg_id="abc123", quote_link="direct/room1?msg=p9")
        args, _kwargs = self.client._rc.chat_post_message.call_args
        self.assertEqual(args[0], f"[ ]({SERVER}/direct/room1?msg=p9)\n回复")

    def test_file_quote_with_link_uses_permalink(self):
        # 文件/图片类引用没有 quote_text，也走正文 permalink；服务器展开时
        # 自取原消息内容，不依赖客户端传的 quote_text
        self.client.send_text(
            "room1", "[文件] 报告.pdf", quote_sender="李四", quote_text="",
            quote_msg_id="abc123", quote_link="/direct/room1?msg=abc123")
        args, kwargs = self.client._rc.chat_post_message.call_args
        self.assertEqual(
            args[0], f"[ ]({SERVER}/direct/room1?msg=abc123)\n[文件] 报告.pdf")
        self.assertIsNone(kwargs.get("attachments"))

    def test_fallback_attachment_without_link(self):
        # 拿不到 permalink（缺 quote_link）时退回附件卡片，至少本应用能显示引用
        self.client.send_text("room1", "回复",
                              quote_sender="李四", quote_text="原话")
        args, kwargs = self.client._rc.chat_post_message.call_args
        self.assertEqual(args[0], "回复")
        self.assertEqual(kwargs["attachments"],
                         [{"author_name": "李四", "text": "原话"}])

    def test_fallback_attachment_uses_body_when_no_quote_text(self):
        self.client.send_text(
            "room1", "[文件] 报告.pdf", quote_sender="李四", quote_text="",
            quote_msg_id="abc123")
        _args, kwargs = self.client._rc.chat_post_message.call_args
        att = kwargs["attachments"][0]
        self.assertEqual(att["author_name"], "李四")
        # 无正文时用发送正文占位，保证引用块有文字可显示
        self.assertEqual(att["text"], "[文件] 报告.pdf")

    def test_attachment_absent_without_quote(self):
        self.client.send_text("room1", "普通消息")
        args, kwargs = self.client._rc.chat_post_message.call_args
        self.assertEqual(args[0], "普通消息")
        self.assertIsNone(kwargs.get("attachments"))


class TestGetMessage(unittest.TestCase):

    def test_parses_message(self):
        client = ApiClient(SERVER)
        client._rc = mock.Mock()
        client._rc.chat_get_message.return_value = {
            "message": {"_id": "abc123", "msg": "原话内容", "rid": "room2",
                        "u": {"_id": "u9", "username": "zhang",
                              "name": "张三"},
                        "ts": "2026-01-01T00:00:00.000Z"}}
        orig = client.get_message("abc123")
        self.assertIsNotNone(orig)
        self.assertEqual(orig.id, "abc123")
        self.assertEqual(orig.room_id, "room2")
        self.assertEqual(orig.text, "原话内容")

    def test_missing_returns_none(self):
        client = ApiClient(SERVER)
        client._rc = mock.Mock()
        client._rc.chat_get_message.return_value = {}
        self.assertIsNone(client.get_message("nope"))


class TestQuoteCopy(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_copy_quote_without_reply(self):
        """纯 permalink 引用没有回复正文，复制被引用内容而非「[引用]」。"""
        from PyQt5.QtWidgets import QApplication
        from ui.chat_view import ChatView
        view = ChatView()
        msg = _mk_msg(msg_type="quote", text="", description="原话内容")
        view._copy_to_clipboard(msg)
        self.assertEqual(QApplication.clipboard().text(), "原话内容")

    def test_copy_quote_with_reply(self):
        from PyQt5.QtWidgets import QApplication
        from ui.chat_view import ChatView
        view = ChatView()
        msg = _mk_msg(msg_type="quote", text="回复正文", description="原话")
        view._copy_to_clipboard(msg)
        self.assertEqual(QApplication.clipboard().text(), "回复正文")


class TestQuoteRendering(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.model = MessageListModel()
        cls.delegate = mm.MessageDelegate(cls.model, None)

    def test_quote_block_text(self):
        self.assertEqual(
            self.model.quote_block_text(
                _mk_msg(msg_type="quote", quote_author="李四",
                        description="原话")),
            "李四：原话")
        self.assertEqual(
            self.model.quote_block_text(_mk_msg(msg_type="quote")),
            "引用消息")

    def test_quote_caption_strips_placeholder(self):
        """图片/视频引用的说明剥掉占位前缀，纯占位（无说明）返回空。"""
        self.assertEqual(
            self.model.quote_caption(
                _mk_msg(msg_type="quote", description="[图片] 图片")), "")
        self.assertEqual(
            self.model.quote_caption(
                _mk_msg(msg_type="quote", description="[图片] 看这个截图")),
            "看这个截图")
        self.assertEqual(
            self.model.quote_caption(
                _mk_msg(msg_type="quote", description="[视频] demo.mp4")),
            "demo.mp4")
        self.assertEqual(
            self.model.quote_caption(
                _mk_msg(msg_type="quote", description="普通文本引用")), "")

    def test_quote_caption_raw_text_with_thumbnail(self):
        """图片引用（带缩略图）的 description 无占位前缀时就是原消息正文，
        直接当说明显示（引用图片消息时 quote_text 优先传原文）。"""
        self.assertEqual(
            self.model.quote_caption(_mk_msg(
                msg_type="quote", description="这张图是流水线配置",
                quote_image_url="/f/x.png")),
            "这张图是流水线配置")
        self.assertEqual(
            self.model.quote_caption(_mk_msg(
                msg_type="quote", description="图片",
                quote_image_url="/f/x.png")),
            "")

    def test_quote_file_display(self):
        self.assertEqual(
            self.model.quote_file_display(
                _mk_msg(msg_type="quote", quote_file_name="报告.pdf")),
            "报告.pdf")
        self.assertEqual(
            self.model.quote_file_display(
                _mk_msg(msg_type="quote", description="[文件] 合同.docx")),
            "合同.docx")
        self.assertEqual(
            self.model.quote_file_display(
                _mk_msg(msg_type="quote", description="原话")), "")

    def test_image_quote_height_structure(self):
        """图片引用高度 = 内边距 + max(固定缩略图高, 作者/说明列高)，
        与图片是否加载无关；单行说明放得下时不撑高卡片。"""
        fm = QFontMetrics(self.model._small_font)
        avail_w = self.model._available_text_width(0)
        base = (mm.MessageListModel.QUOTE_PAD_TOP
                + max(mm.MessageListModel.QUOTE_THUMB_H, fm.height())
                + mm.MessageListModel.QUOTE_PAD_BOTTOM)
        msg = _mk_msg(msg_type="quote", quote_author="李四",
                      description="[图片] 图片", quote_image_url="/f/x.png")
        self.assertEqual(self.model.quote_block_height(msg, avail_w), base)
        msg_with_cap = _mk_msg(msg_type="quote", quote_author="李四",
                               description="[图片] 这个",
                               quote_image_url="/f/x.png")
        self.assertEqual(
            self.model.quote_block_height(msg_with_cap, avail_w), base)
        long_cap = _mk_msg(msg_type="quote", quote_author="李四",
                           description="[图片] " + "很长的说明" * 40,
                           quote_image_url="/f/x.png")
        self.assertGreater(
            self.model.quote_block_height(long_cap, avail_w), base)

    def test_quote_media_metrics_thumb_and_text_column(self):
        """缩略图与文字列必须并排且都在卡片内，绘制与高度同源。"""
        msg = _mk_msg(msg_type="quote", quote_author="李四",
                      description="这个", quote_image_url="/f/x.png")
        avail_w = self.model._available_text_width(0)
        m = self.model.quote_media_metrics(msg, avail_w)
        thumb = m["thumb"]
        self.assertEqual(thumb.width(), mm.MessageListModel.QUOTE_THUMB_W)
        self.assertEqual(thumb.height(), mm.MessageListModel.QUOTE_THUMB_H)
        self.assertEqual(thumb.left(), mm.MessageListModel.QUOTE_PAD_LEFT)
        self.assertGreaterEqual(m["col_x"],
                                thumb.right() + mm.MessageListModel.QUOTE_MEDIA_GAP)
        self.assertLessEqual(m["col_x"] + m["col_w"],
                             avail_w - mm.MessageListModel.QUOTE_PAD_RIGHT)
        self.assertIsNotNone(m["cap_doc"])
        self.assertEqual(m["height"], self.model.quote_block_height(msg, avail_w))
        # 窄窗口下缩略图收窄，不把说明列挤出卡片
        narrow = self.model.quote_media_metrics(msg, 220)
        self.assertLessEqual(narrow["col_x"] + narrow["col_w"],
                             220 - mm.MessageListModel.QUOTE_PAD_RIGHT)
        self.assertGreaterEqual(narrow["thumb"].width(), 40)

    def test_file_quote_height_structure(self):
        fm = QFontMetrics(self.model._small_font)
        avail_w = self.model._available_text_width(0)
        msg = _mk_msg(msg_type="quote", quote_author="李四",
                      quote_file_name="报告.pdf", description="[文件] 报告.pdf")
        h = self.model.quote_block_height(msg, avail_w)
        self.assertEqual(
            h, mm.MessageListModel.QUOTE_PAD_TOP
            + mm.MessageListModel.QUOTE_PAD_BOTTOM
            + fm.height() + 2 + fm.height())

    def test_long_quote_height_fits(self):
        """长引用完整显示：高度必须大于旧的固定单行值（fm.height()+16）。"""
        msg = _mk_msg(msg_type="quote",
                      description="很长很长很长" * 40)
        avail_w = self.model._available_text_width(0)
        h = self.model.quote_block_height(msg, avail_w)
        fm = QFontMetrics(self.model._small_font)
        self.assertGreater(h, fm.height() + 16)

    def test_quote_block_rect_top_of_content(self):
        model = self.model
        msg = _mk_msg(msg_type="quote", text="回复",
                      description="原话", quote_author="李四")
        model._messages = [msg]
        index = model.index(0, 0)
        row_rect = QRect(0, 0, 800, 200)
        qrect = self.delegate.quote_block_rect(index, row_rect)
        self.assertFalse(qrect.isNull())
        bubble, _avatar, _img = self.delegate.layout_rects(index, row_rect)
        content = bubble.adjusted(mm._BUBBLE_PAD, mm._BUBBLE_PAD,
                                  -mm._BUBBLE_PAD, -mm._BUBBLE_PAD)
        self.assertEqual(qrect.top(), content.top())
        self.assertLessEqual(qrect.bottom(), content.bottom())

    def test_quote_block_rect_null_for_text(self):
        model = self.model
        model._messages = [_mk_msg()]
        row_rect = QRect(0, 0, 800, 200)
        self.assertTrue(
            self.delegate.quote_block_rect(model.index(0, 0), row_rect).isNull())


class TestQuoteSelection(unittest.TestCase):
    """引用消息回复正文的部分选中与复制：命中坐标要扣掉引用块高度。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from ui.chat_view import ChatView
        cls.view = ChatView()
        cls.model = cls.view._model

    def test_hit_pos_skips_quote_block(self):
        """点击落在引用块高度内不命中回复文本 doc。"""
        from PyQt5.QtCore import QPoint
        from ui.message_model import _BUBBLE_PAD
        msg = _mk_msg(msg_type="quote", text="回复正文",
                      description="原话", quote_author="李四")
        bubble = QRect(0, 0, 400, 300)
        qh = self.model.quote_block_height(
            msg, bubble.width() - _BUBBLE_PAD * 2)
        pos_in_quote = QPoint(_BUBBLE_PAD + 5, _BUBBLE_PAD + qh // 2)
        hit, _doc = self.view._text_hit_pos(msg, bubble, pos_in_quote,
                                            fuzzy=True)
        self.assertIsNone(hit)

    def test_hit_pos_offset_matches_paint(self):
        """回复首行的点击命中文本开头：命中偏移与绘制起笔
        （引用块高 + 4）一致。"""
        from PyQt5.QtCore import QPoint
        from ui.message_model import _BUBBLE_PAD
        msg = _mk_msg(msg_type="quote", text="回复正文",
                      description="原话", quote_author="李四")
        bubble = QRect(0, 0, 400, 300)
        qh = self.model.quote_block_height(
            msg, bubble.width() - _BUBBLE_PAD * 2)
        pos_reply = QPoint(_BUBBLE_PAD + 2, _BUBBLE_PAD + qh + 4 + 3)
        hit, _doc = self.view._text_hit_pos(msg, bubble, pos_reply,
                                            fuzzy=True)
        self.assertIsNotNone(hit)
        self.assertLess(hit, 2)

    def test_selected_text_from_reply_range(self):
        """回复文本的选区按 doc 位置取出对应子串。"""
        msg = _mk_msg(msg_type="quote", text="回复正文在这里",
                      description="原话")
        self.model._messages = [msg]
        self.model.sel_msg_id = msg.id
        self.model.sel_start = 0
        self.model.sel_end = 2
        self.model.sel_merge_idx = -1
        self.assertEqual(self.view._selected_text(), "回复")

    def test_text_selection_clears_stale_merge_idx(self):
        """合并卡片选区后改选普通文本：残留的 sel_merge_idx 必须清掉，
        否则 _selected_text 走合并分支对普通消息返回空串。"""
        from PyQt5.QtCore import QPoint
        self.model.sel_merge_idx = 3
        msg = _mk_msg(text="普通消息")
        self.model._messages = [msg]
        self.assertTrue(self.view._begin_text_selection(
            msg, QRect(0, 0, 400, 100), QPoint(50, 20)))
        self.assertEqual(self.model.sel_merge_idx, -1)


class TestImageCaption(unittest.TestCase):
    """图文混排：图片气泡下方说明 + display_text 预览。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.model = MessageListModel()

    def test_display_text_image_with_caption(self):
        """官方图文混排 msg 为空、说明在 description；占位「图片」不当真说明。"""
        official = _mk_msg(msg_type="image", text="", description="流水线配置")
        self.assertEqual(official.display_text, "[图片] 流水线配置")
        local = _mk_msg(msg_type="image", text="看这个", description="")
        self.assertEqual(local.display_text, "[图片] 看这个")
        plain = _mk_msg(msg_type="image", text="图片", description="")
        self.assertEqual(plain.display_text, "[图片] 图片")

    def test_image_caption_precedence(self):
        """说明优先取 description（官方混排），回落 text（本应用路径）；
        占位「图片」不是说明。"""
        self.assertEqual(
            self.model.image_caption(
                _mk_msg(msg_type="image", text="图片",
                        description="官方说明")), "官方说明")
        self.assertEqual(
            self.model.image_caption(
                _mk_msg(msg_type="image", text="正文说明",
                        description="")), "正文说明")
        self.assertEqual(
            self.model.image_caption(
                _mk_msg(msg_type="image", text="图片", description="")), "")
        self.assertEqual(
            self.model.image_caption(
                _mk_msg(msg_type="text", text="说明")), "")

    def test_image_caption_height(self):
        fm = QFontMetrics(self.model._font)
        plain = _mk_msg(msg_type="image", text="图片", description="")
        self.assertEqual(self.model.image_caption_h(plain, 200), 0)
        cap = _mk_msg(msg_type="image", text="", description="有说明")
        h = self.model.image_caption_h(cap, 200)
        self.assertGreater(h, mm._CAPTION_GAP)
        self.assertLessEqual(h, mm._CAPTION_GAP + fm.height() * 2)

    def test_bubble_content_h_with_caption(self):
        """带说明的图片内容高度 = 图片 + 说明；无说明不受影响。"""
        plain = _mk_msg(msg_type="image", text="图片", description="")
        cap = _mk_msg(msg_type="image", text="", description="多一行说明")
        h_plain = self.model._calc_bubble_content_h(plain)
        h_cap = self.model._calc_bubble_content_h(cap)
        self.assertGreater(h_cap, h_plain)
        fm = QFontMetrics(self.model._font)
        self.assertGreaterEqual(h_cap - h_plain,
                                mm._CAPTION_GAP + fm.height() - 2)

    def test_image_bubble_w_follows_caption(self):
        """窄图带说明：气泡按说明自然宽度撑开（不窄于图片、不超上限），
        无说明/短说明保持图片宽。"""
        max_b = 500
        plain = _mk_msg(msg_type="image", text="图片", description="")
        self.assertEqual(self.model.image_bubble_w(plain, 140, max_b), 140)
        short = _mk_msg(msg_type="image", text="", description="看")
        self.assertEqual(self.model.image_bubble_w(short, 140, max_b), 140)
        mid = _mk_msg(msg_type="image", text="",
                      description="说" * 30)
        w = self.model.image_bubble_w(mid, 140, max_b)
        self.assertGreater(w, 140)
        self.assertLessEqual(w, max_b)
        long = _mk_msg(msg_type="image", text="", description="很" * 200)
        self.assertEqual(self.model.image_bubble_w(long, 140, max_b), max_b)
        # 上限与文本气泡一致：视口再宽也不能超过 _BUBBLE_MAX_W
        self.assertEqual(self.model.image_bubble_w(long, 140, 1000),
                         mm._BUBBLE_MAX_W)

    def test_bubble_content_h_narrow_image_wraps_wide(self):
        """窄图长说明按撑开后的气泡宽换行：行数少于按图片宽的老行为。"""
        text = "有同事反馈第一项不过，是代码检查问题。我给他流水线retry了一下。"
        msg = _mk_msg(msg_type="image", text="", description=text)
        self.model._view_width = 640
        img_w, img_h = self.model._get_image_display_size(msg)
        h = self.model._calc_bubble_content_h(msg)
        narrow_cap = mm._CAPTION_GAP + int(
            self.model._caption_doc(text, img_w).size().height())
        wide_cap = mm._CAPTION_GAP + int(
            self.model._caption_doc(text, mm._BUBBLE_MAX_W).size().height())
        self.assertLess(wide_cap, narrow_cap)
        self.assertEqual(h, img_h + wide_cap)


if __name__ == "__main__":
    unittest.main()
