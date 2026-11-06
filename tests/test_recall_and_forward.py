"""撤回系统消息、单行文本宽度、系统行高、高度缓存与转发组装的回归测试。"""
import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from unittest import mock

from PyQt5.QtCore import QRect
from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QFontMetrics

from core.messages import (
    FORWARD_HEAD, MERGE_HEAD, _parse_merge_body, parse_message,
)
from core.models import Message, User
from ui import message_model as mm
from ui.message_model import MessageListModel
from ui.forward_dialog import build_forward_payload, build_forward_payload_many


def _mk_msg(**kw):
    defaults = dict(id="m1", room_id="r", text="hello",
                    sender=User(username="u", name="张三"),
                    timestamp=1789000000.0, msg_type="text")
    defaults.update(kw)
    return Message(**defaults)


class TestRecallSystemMessage(unittest.TestCase):

    def test_recall_type_becomes_system(self):
        m = parse_message(
            {"_id": "a", "msg": "原话内容", "t": "rm",
             "u": {"_id": "u1", "username": "alice"}}, "r")
        self.assertEqual(m.msg_type, "system")
        self.assertEqual(m.text, "alice 撤回了一条消息")

    def test_recall_shows_display_name(self):
        """群聊里"对方"无法分辨是谁，非本人撤回要显示具体名字。"""
        m = parse_message(
            {"_id": "a", "msg": "", "t": "rm",
             "u": {"_id": "u1", "username": "alice", "name": "张三"}}, "r")
        self.assertEqual(m.text, "张三 撤回了一条消息")

    def test_own_recall(self):
        m = parse_message(
            {"_id": "a", "msg": "", "t": "message_removed",
             "u": {"_id": "me", "username": "alice"}}, "r", my_user_id="me")
        self.assertEqual(m.msg_type, "system")
        self.assertEqual(m.text, "你撤回了一条消息")

    def test_empty_message_becomes_system(self):
        # u 无名字/用户名时兜底显示"对方"
        m = parse_message({"_id": "a", "msg": "", "u": {"_id": "u1"}}, "r")
        self.assertEqual(m.msg_type, "system")
        self.assertEqual(m.text, "对方 撤回了一条消息")

    def test_empty_with_attachment_stays_image(self):
        m = parse_message(
            {"_id": "a", "msg": "", "u": {"_id": "u1"},
             "attachments": [{"image_url": "/x.png"}]}, "r")
        self.assertEqual(m.msg_type, "image")

    def test_undecrypted_e2e_not_shown_as_recall(self):
        """E2E 解密失败（t/content 原样保留、msg 为空）显示占位系统消息，
        不能落入空文本分支被误显示成"撤回了一条消息"。"""
        m = parse_message(
            {"_id": "a", "t": "e2e", "msg": "",
             "content": {"iv": "x", "ciphertext": "y"},
             "u": {"_id": "u1", "username": "alice"}}, "r")
        self.assertEqual(m.msg_type, "system")
        self.assertEqual(m.text, "无法解密的加密消息")

    def test_own_undecrypted_e2e(self):
        m = parse_message(
            {"_id": "a", "t": "e2e", "msg": "", "u": {"_id": "me"}}, "r",
            my_user_id="me")
        self.assertEqual(m.msg_type, "system")
        self.assertEqual(m.text, "无法解密的加密消息")


class TestRoomE2ESystemMessage(unittest.TestCase):
    """房间 E2E 开关系统消息（真机 2026-09-30 实测：msg 字段是操作者
    username，与 uj/ul 同模式）。此前未知类型落入空文本分支只显示用户名。"""

    def test_e2e_enabled_template(self):
        m = parse_message(
            {"_id": "a", "t": "room_e2e_enabled", "msg": "alice",
             "u": {"_id": "u1", "username": "alice"}}, "r")
        self.assertEqual(m.msg_type, "system")
        self.assertEqual(m.text, "alice 开启了端到端加密")

    def test_e2e_disabled_template(self):
        m = parse_message(
            {"_id": "a", "t": "room_e2e_disabled", "msg": "alice",
             "u": {"_id": "u1", "username": "alice"}}, "r")
        self.assertEqual(m.msg_type, "system")
        self.assertEqual(m.text, "alice 关闭了端到端加密")


class TestSystemMessageRealName(unittest.TestCase):
    """系统消息动作对象是 u 本人时显示真实姓名，不用 username。"""

    def test_uj_msg_matches_username_shows_name(self):
        m = parse_message(
            {"_id": "a", "t": "uj", "msg": "alice",
             "u": {"_id": "u1", "username": "alice", "name": "张三"}}, "r")
        self.assertEqual(m.msg_type, "system")
        self.assertEqual(m.text, "张三 加入频道")

    def test_ul_empty_msg_shows_name(self):
        m = parse_message(
            {"_id": "a", "t": "ul",
             "u": {"_id": "u1", "username": "alice", "name": "张三"}}, "r")
        self.assertEqual(m.text, "张三 离开频道")

    def test_third_party_target_shows_operator(self):
        """au 的 msg 是被添加人 username、u 是添加人（真机 2026-10-08
        实测）：目标本地无姓名先显示 username 并记入待解析，操作者
        直接显示真实姓名。"""
        m = parse_message(
            {"_id": "a", "t": "au", "msg": "wangwu",
             "u": {"_id": "u1", "username": "alice", "name": "李四"}}, "r")
        self.assertEqual(m.msg_type, "system")
        self.assertEqual(m.text, "wangwu 被 李四 添加到频道")
        self.assertEqual(m.system_names, {"user": "wangwu"})
        self.assertEqual(m.system_template, "{user} 被 {operator} 添加到频道")

    def test_ru_template(self):
        m = parse_message(
            {"_id": "a", "t": "ru", "msg": "wangwu",
             "u": {"_id": "u1", "username": "alice", "name": "李四"}}, "r")
        self.assertEqual(m.text, "wangwu 被 李四 移出频道")

    def test_format_system_text_after_resolve(self):
        m = parse_message(
            {"_id": "a", "t": "au", "msg": "wangwu",
             "u": {"_id": "u1", "username": "alice", "name": "李四"}}, "r")
        from core.messages import format_system_text
        self.assertEqual(
            format_system_text(m, {"user": "王五"}),
            "王五 被 李四 添加到频道")

    def test_format_cumulative_resolves(self):
        """回归：u 无 name 时 user/operator 都待解析，先后解析回调重排
        不能把先解析出的名字丢回 username 或误用 sender 兜底。"""
        m = parse_message(
            {"_id": "a", "t": "au", "msg": "wangwu",
             "u": {"_id": "u1", "username": "alice"}}, "r")
        from core.messages import format_system_text
        self.assertEqual(m.text, "wangwu 被 alice 添加到频道")
        self.assertEqual(m.system_names,
                         {"user": "wangwu", "operator": "alice"})
        m.system_resolved = {"user": "王五"}
        m.text = format_system_text(m, {"user": "王五"})
        m.system_resolved["operator"] = "李四"
        self.assertEqual(
            format_system_text(m, {"operator": "李四"}),
            "王五 被 李四 添加到频道")

    def test_recall_pending_name(self):
        """u 只有 username 没有 name 时先显示 username，并登记待解析。"""
        m = parse_message(
            {"_id": "a", "msg": "", "t": "rm",
             "u": {"_id": "u1", "username": "alice"}}, "r")
        self.assertEqual(m.text, "alice 撤回了一条消息")
        self.assertEqual(m.system_names, {"user": "alice"})
        from core.messages import format_system_text
        self.assertEqual(
            format_system_text(m, {"user": "张三"}), "张三 撤回了一条消息")

    def test_recall_no_name_falls_back(self):
        # u 连 username 都没有：无法异步解析，兜底"对方"
        m = parse_message(
            {"_id": "a", "msg": "", "t": "rm", "u": {"_id": "u1"}}, "r")
        self.assertEqual(m.text, "对方 撤回了一条消息")
        self.assertEqual(m.system_names, {})

    def test_no_name_falls_back_username(self):
        m = parse_message(
            {"_id": "a", "t": "uj", "msg": "alice",
             "u": {"_id": "u1", "username": "alice"}}, "r")
        self.assertEqual(m.text, "alice 加入频道")


class TestInlineImageVsFileLink(unittest.TestCase):
    """urls 字段下，![..](url) 内联图片识别为 image，[..](url) 文件链接保持 text。"""

    def _parse(self, msg, url):
        return parse_message(
            {"_id": "a", "msg": msg, "u": {"_id": "u1"},
             "urls": [{"url": url}]}, "r")

    def test_inline_image_stays_image(self):
        m = self._parse("![x](https://h/a.png)", "https://h/a.png")
        self.assertEqual(m.msg_type, "image")
        self.assertEqual(m.file_url, "https://h/a.png")

    def test_file_link_stays_text(self):
        m = self._parse("[x](https://h/a.png)", "https://h/a.png")
        self.assertEqual(m.msg_type, "text")
        self.assertEqual(m.file_url, "")

    def test_mixed_text_with_file_link_stays_text(self):
        # 对应真实图文混排：正文文字 + [name](url) 文件链接
        m = self._parse("文字\n\n[文件名](https://h/file.png)\n",
                        "https://h/file.png")
        self.assertEqual(m.msg_type, "text")
        self.assertEqual(m.file_url, "")


class TestSingleLineNoWrap(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_long_token_single_line(self):
        model = MessageListModel()
        msg = _mk_msg(text="FCLCLOCK_SYS_UTIME_FORMAT_LONG_TOKEN_0123")
        doc = model.build_text_doc(msg)
        doc.setTextWidth(-1)
        ideal = doc.idealWidth()
        w = model.text_content_width(msg, 800)
        # ceil+1 必须覆盖 idealWidth，否则长 token 会整词折行
        self.assertGreaterEqual(w, math.ceil(ideal))
        doc_wide = model.build_text_doc(msg)
        doc_wide.setTextWidth(w)
        doc_single = model.build_text_doc(msg)
        doc_single.setTextWidth(4000)
        self.assertEqual(int(doc_wide.size().height()),
                         int(doc_single.size().height()),
                         "按计算宽度排版应为单行")


class TestSystemRowHeight(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_height_fits_bubble(self):
        model = MessageListModel()
        model._messages = [_mk_msg(msg_type="system", text="张三 加入频道")]
        h = model._calc_height(model._messages[0], 0)
        fm = QFontMetrics(model._small_font)
        # 气泡高 + 上下边距必须完整落在行高内（历史 bug：硬编码 24 导致裁底）
        self.assertGreaterEqual(h, mm._MARGIN_V * 2 + fm.height() + 4)


class TestHeightCacheById(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_cache_survives_prepend(self):
        model = MessageListModel()
        a = _mk_msg(id="A", text="第一条")
        c = _mk_msg(id="C", text="第二条")
        model.append_message(a)
        model.append_message(c)
        model._get_height(0)
        model._get_height(1)
        self.assertIn("C", model._height_cache)
        model.prepend_messages_immediate([_mk_msg(id="B", text="更早的")])
        # 顶部插入只允许失效原首行（A），其余行的缓存必须保留
        self.assertNotIn("A", model._height_cache)
        self.assertIn("C", model._height_cache, "顶部插入不应清掉旧行缓存")


class TestFileCardMetrics(unittest.TestCase):
    """文件/音频/无封面视频卡片：宽高与绘制三方同源（_FILE_CARD_W /
    _file_card_h / _paint_file_content），只改一边会裁剪或错位。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.model = MessageListModel()
        cls.delegate = mm.MessageDelegate(cls.model, None)

    def test_width_fixed_and_clamped(self):
        msg = _mk_msg(msg_type="file", file_name="ai.cmd", text="")
        self.assertEqual(self.delegate._calc_bubble_width(msg, 600),
                         mm._FILE_CARD_W)
        # 窄窗口钳制到可用宽
        msg = _mk_msg(msg_type="file", file_name="a.txt", text="")
        self.assertEqual(self.delegate._calc_bubble_width(msg, 100),
                         100 - mm._BUBBLE_PAD * 2)

    def test_height_matches_card_formula(self):
        msg = _mk_msg(msg_type="file", file_name="a.pdf", text="")
        self.assertEqual(
            self.model._calc_bubble_content_h(msg),
            mm._file_card_h(self.model._font, self.model._small_font))

    def test_audio_video_same_card(self):
        for t in ("audio", "video"):
            msg = _mk_msg(msg_type=t, file_name="clip.mp4", text="")
            self.assertEqual(self.delegate._calc_bubble_width(msg, 600),
                             mm._FILE_CARD_W)
            self.assertEqual(
                self.model._calc_bubble_content_h(msg),
                mm._file_card_h(self.model._font, self.model._small_font))

    def test_badge_mapping(self):
        cases = {"a.pdf": "#d9534f", "b.xlsx": "#3f9e6b",
                 "c.mp3": "#8e6bbf", "无扩展名": "#8a9299"}
        for name, color in cases.items():
            _label, bg = mm._file_badge(
                _mk_msg(msg_type="file", file_name=name, text=""))
            self.assertEqual(bg, color, name)
        self.assertEqual(mm._file_badge(
            _mk_msg(msg_type="file", file_name="无扩展名", text=""))[0],
            "文件")
        self.assertEqual(mm._file_badge(
            _mk_msg(msg_type="audio", file_name="无扩展名", text=""))[0],
            "音频")
        # 双扩展名取最后一段；大小写归一
        self.assertEqual(mm._file_badge(
            _mk_msg(msg_type="file", file_name="archive.tar.gz",
                    text=""))[0], "GZ")
        self.assertEqual(mm._file_badge(
            _mk_msg(msg_type="file", file_name="x.DOCX", text=""))[0],
            "DOCX")

    def test_width_adaptive_long_name(self):
        # 长文件名在上限内撑开完整显示，超限钳到 _FILE_CARD_MAX_W
        long_name = "QoderCNIDEUserSetup-x64-1.25.0-remote-ssh-chat-errcode.log"
        msg = _mk_msg(msg_type="file", file_name=long_name, text="")
        fm = QFontMetrics(self.model._font)
        natural = (mm._FILE_BADGE + mm._FILE_TEXT_GAP
                   + fm.horizontalAdvance(long_name))
        w = self.delegate._calc_bubble_width(msg, 800)
        self.assertEqual(w, min(natural, mm._FILE_CARD_MAX_W))
        self.assertLessEqual(w, mm._FILE_CARD_MAX_W)

    def test_flash_feedback(self):
        self.assertFalse(self.delegate._flash_active("m1"))
        self.delegate.flash("m1", ms=50)
        self.assertTrue(self.delegate._flash_active("m1"))
        self.delegate._flash_done(self.delegate._flash_gen)
        self.assertFalse(self.delegate._flash_active("m1"))
        # 旧一代还原定时器不影响新一轮 flash
        self.delegate.flash("m2", ms=50)
        self.delegate._flash_done(self.delegate._flash_gen - 1)
        self.assertTrue(self.delegate._flash_active("m2"))

    def test_tooltip_on_elided_file_name(self):
        from PyQt5.QtCore import QEvent, QPoint
        from PyQt5.QtGui import QHelpEvent
        from PyQt5.QtWidgets import QStyleOptionViewItem
        long_name = "季度总结与下年度预算规划讨论稿-最终版-再改一版.docx" * 3
        msg = _mk_msg(msg_type="file", file_name=long_name, text="")
        self.model.append_message(msg)
        idx = self.model.index(self.model.rowCount() - 1, 0)
        opt = QStyleOptionViewItem()
        opt.rect = QRect(0, 0, 800, self.model._get_height(idx.row()))
        ev = QHelpEvent(QEvent.ToolTip, QPoint(10, 10), QPoint(10, 10))
        self.assertTrue(self.delegate.helpEvent(ev, None, opt, idx))
        # 名字放得下时不弹 tooltip
        short = _mk_msg(msg_type="file", file_name="a.txt", text="")
        self.model.append_message(short)
        idx2 = self.model.index(self.model.rowCount() - 1, 0)
        opt2 = QStyleOptionViewItem()
        opt2.rect = QRect(0, 0, 800, self.model._get_height(idx2.row()))
        ev2 = QHelpEvent(QEvent.ToolTip, QPoint(10, 10), QPoint(10, 10))
        self.assertFalse(self.delegate.helpEvent(ev2, None, opt2, idx2))

    def test_badge_font_fits(self):
        # 4 字符扩展名与中文类别词都必须完整落进徽标（真机 DOCX 溢出回归）
        for label in ("DOCX", "XLSX", "文件", "音频"):
            font = mm._badge_fit_font(self.model._small_font, label,
                                      mm._FILE_BADGE)
            self.assertLessEqual(
                QFontMetrics(font).horizontalAdvance(label),
                mm._FILE_BADGE - 8, label)

    def test_full_time_text_format(self):
        self.assertRegex(mm._full_time_text(1789000000.0),
                         r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

    def test_text_bubble_tooltip_full_date(self):
        from PyQt5.QtCore import QEvent, QPoint
        from PyQt5.QtGui import QHelpEvent
        from PyQt5.QtWidgets import QStyleOptionViewItem
        msg = _mk_msg(text="你好")
        self.model.append_message(msg)
        idx = self.model.index(self.model.rowCount() - 1, 0)
        opt = QStyleOptionViewItem()
        opt.rect = QRect(0, 0, 800, self.model._get_height(idx.row()))
        bubble, _avatar, _img = self.delegate.layout_rects(idx, opt.rect)
        ev = QHelpEvent(QEvent.ToolTip, bubble.center(),
                        bubble.center())
        self.assertTrue(self.delegate.helpEvent(ev, None, opt, idx))
        # 气泡外不弹
        ev2 = QHelpEvent(QEvent.ToolTip, QPoint(1, 1), QPoint(1, 1))
        self.assertFalse(self.delegate.helpEvent(ev2, None, opt, idx))

    def test_paint_file_card_smoke(self):
        from PyQt5.QtGui import QPainter, QPixmap
        for t, kw in (("file", dict(file_name="季度报告.docx",
                                    file_size=2048)),
                      ("audio", dict(file_name="语音消息.ogg")),
                      ("video", dict(file_name="会议录像"))):
            msg = _mk_msg(msg_type=t, text="", **kw)
            pm = QPixmap(400, 120)
            pm.fill()
            painter = QPainter(pm)
            rect = QRect(8, 8, mm._FILE_CARD_W,
                         mm._file_card_h(self.model._font,
                                         self.model._small_font))
            self.delegate._paint_file_content(painter, rect, msg)
            painter.end()


class TestForwardPayload(unittest.TestCase):

    def test_attachment_urls_resolved(self):
        msg = _mk_msg(msg_type="image", text="图片",
                      attachments=[{"image_url": "/file-upload/x.png",
                                    "title": "x.png"}])
        text, atts = build_forward_payload(
            msg, lambda u: "https://s.example" + u)
        # 图片转发：正文为空，只带标记行；图片占位文案不跟着转发
        self.assertEqual(text, f"{FORWARD_HEAD} 张三")
        self.assertEqual(atts[0]["image_url"],
                         "https://s.example/file-upload/x.png")

    def test_quote_forward(self):
        msg = _mk_msg(msg_type="quote", text="回复内容", description="原话")
        text, atts = build_forward_payload(msg, lambda u: u)
        self.assertEqual(text, f"{FORWARD_HEAD} 张三\n回复内容")
        self.assertEqual(atts, [{"author_name": "张三", "text": "原话"}])

    def test_plain_text_forward(self):
        text, atts = build_forward_payload(_mk_msg(text="hi"), lambda u: u)
        self.assertEqual((text, atts), (f"{FORWARD_HEAD} 张三\nhi", None))

    def test_forward_body_stripped_on_parse(self):
        # 逐条转发发送后，接收端 parse_message 剥离标记行并设 is_forwarded
        body = f"{FORWARD_HEAD} 张三\nhello"
        m = parse_message({"_id": "x", "msg": body,
                           "u": {"_id": "u1", "username": "bob"}}, "r")
        self.assertTrue(m.is_forwarded)
        self.assertEqual(m.forward_author, "张三")
        self.assertEqual(m.text, "hello")

    def test_parse_merge_body(self):
        body = (f"{MERGE_HEAD}共2条"
                f"\n\x1f0\x1f张三\x1f1789000000\x1f行一"
                f"\n\x1f0\x1f李四\x1f1789000100\x1f行二")
        entries = _parse_merge_body(body)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["sender"], "张三")
        self.assertEqual(entries[0]["text"], "行一")
        self.assertEqual(entries[0]["ts"], 1789000000.0)
        self.assertEqual(entries[1]["sender"], "李四")

    def test_parse_merge_body_readable_time(self):
        # 可读时间文本 → ts_text；数字仍是 epoch（向后兼容旧卡片）
        body = (f"{MERGE_HEAD}共2条"
                f"\n\x1f0\x1f张三\x1f10:30\x1f行一"
                f"\n\x1f0\x1f李四\x1f1789000100\x1f行二")
        entries = _parse_merge_body(body)
        self.assertEqual(entries[0]["ts_text"], "10:30")
        self.assertEqual(entries[0]["ts"], 0.0)
        self.assertEqual(entries[1]["ts_text"], "")
        self.assertEqual(entries[1]["ts"], 1789000100.0)

    def test_parse_merge_body_att_json(self):
        # att_flag=1 时第 6 字段是附件 JSON，解析成 att
        body = (f"{MERGE_HEAD}共1条"
                f"\n\x1f1\x1f张三\x1f10:30\x1f[x.png](/x.png)"
                f"\x1f{{\"image_url\": \"/x.png\", \"title\": \"x.png\"}}")
        entries = _parse_merge_body(body)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["att"]["image_url"], "/x.png")
        self.assertEqual(entries[0]["ts_text"], "10:30")

    def test_parse_merge_message_type(self):
        body = f"{MERGE_HEAD}共1条\n\x1f0\x1f张三\x1f1789000000\x1f内容"
        m = parse_message({"_id": "y", "msg": body,
                           "u": {"_id": "u1", "username": "bob"}}, "r")
        self.assertEqual(m.msg_type, "merge")
        self.assertEqual(len(m.forward_list), 1)
        self.assertEqual(m.forward_list[0]["text"], "内容")


class TestForwardPayloadMany(unittest.TestCase):

    def setUp(self):
        # 固定 format_chat_time，让卡片时间文本可确定性断言
        patcher = mock.patch(
            "ui.forward_dialog.format_chat_time", return_value="10:30")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_merge_produces_attachments_cards(self):
        # 方案二：合并转发正文只留 MERGE_HEAD 汇总行，每条子消息一张附件卡片
        msgs = [
            _mk_msg(id="a", msg_type="image", text="图片",
                    attachments=[{"image_url": "/file-upload/a.png",
                                  "title": "a.png"}]),
            _mk_msg(id="b", text="第二条"),
            _mk_msg(id="c", msg_type="image", text="图片",
                    attachments=[{"image_url": "/file-upload/c.png",
                                  "title": "c.png"}]),
        ]
        out = build_forward_payload_many(
            msgs, lambda u: "https://s.example" + u, merge=True)
        self.assertEqual(len(out), 1)
        body, attachments = out[0]
        self.assertEqual(body, f"{MERGE_HEAD}共3条")
        self.assertNotIn("{", body)  # 附件 JSON 不再混进正文
        self.assertEqual(len(attachments), 3)
        for att in attachments:
            self.assertEqual(att["color"], "#1d74f5")
            self.assertIs(att["collapsed"], False)
            self.assertTrue(att["title"].startswith("@"))
            self.assertIn(" · 10:30", att["title"])
        # 图片子消息：text 只留「右键打开链接查看原图」，额外带 image_url
        self.assertEqual(attachments[0]["text"],
                         "[右键打开链接查看原图](https://s.example/file-upload/a.png)")
        self.assertEqual(attachments[0]["image_url"],
                         "https://s.example/file-upload/a.png")
        self.assertEqual(attachments[0]["title"], "@张三 · 10:30")
        self.assertEqual(attachments[1]["text"], "第二条")

    def test_merge_preserves_all_senders_text(self):
        msgs = [
            _mk_msg(id="a", text="行一"),
            _mk_msg(id="b", text="行二"),
        ]
        out = build_forward_payload_many(msgs, lambda u: u, merge=True)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], f"{MERGE_HEAD}共2条")
        self.assertEqual([a["text"] for a in out[0][1]], ["行一", "行二"])

    def test_merge_keeps_text_with_own_attachment(self):
        # 既有文本又有附件的消息：配文与 markdown 文件链接同行保留在卡片
        msgs = [
            _mk_msg(id="a", text="看这个", msg_type="image",
                    attachments=[{"image_url": "/x.png", "title": "x.png"}]),
            _mk_msg(id="b", text="纯文本"),
        ]
        out = build_forward_payload_many(msgs, lambda u: u, merge=True)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], f"{MERGE_HEAD}共2条")
        # 图片消息：只留查看原图链接，配文不再拼入卡片
        self.assertEqual(out[0][1][0]["text"], "[右键打开链接查看原图](/x.png)")
        self.assertEqual(out[0][1][0]["image_url"], "/x.png")
        self.assertEqual(out[0][1][1]["text"], "纯文本")

    def test_each_returns_per_message_payloads(self):
        msgs = [
            _mk_msg(id="a", msg_type="image", text="图片",
                    attachments=[{"image_url": "/x.png", "title": "x.png"}]),
            _mk_msg(id="b", text="hi"),
        ]
        out = build_forward_payload_many(msgs, lambda u: u, merge=False)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0][1][0]["image_url"], "/x.png")
        # merge=False 逐条发送：图片只带标记行，文本带标记头
        self.assertEqual(out[0][0], f"{FORWARD_HEAD} 张三")
        self.assertEqual(out[1], (f"{FORWARD_HEAD} 张三\nhi", None))

    def test_merge_single_message_payload(self):
        msgs = [_mk_msg(text="只有一条")]
        out = build_forward_payload_many(msgs, lambda u: u, merge=True)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], f"{MERGE_HEAD}共1条")
        self.assertEqual(out[0][1][0]["text"], "只有一条")

    def test_merge_all_attachments_no_text(self):
        # 全是有附件的消息：一张卡片挂全部附件条目，不拆成多条发送
        msgs = [
            _mk_msg(id="a", msg_type="image", text="图片",
                    attachments=[{"image_url": "/a.png", "title": "a.png"}]),
            _mk_msg(id="b", msg_type="image", text="图片",
                    attachments=[{"image_url": "/b.png", "title": "b.png"}]),
        ]
        out = build_forward_payload_many(msgs, lambda u: u, merge=True)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], f"{MERGE_HEAD}共2条")
        self.assertEqual(len(out[0][1]), 2)
        # 附件信息在 text 的 markdown 链接 [name](url) 里，不再塞进 att JSON 字段
        for att in out[0][1]:
            self.assertRegex(att["text"], r"^\[[^]]+\]\([^)]+\)$")

    def test_merge_roundtrip_parse_to_forward_list(self):
        # 方案二产物反解：parse_message 从 attachments 重建 forward_list，
        # sender/ts_text/text 不丢，供 UI 渲染（显示效果与旧卡片一致）
        msgs = [
            _mk_msg(id="a", msg_type="image", text="图片",
                    attachments=[{"image_url": "/x.png", "title": "x.png"}]),
            _mk_msg(id="b", text="纯文本"),
        ]
        out = build_forward_payload_many(msgs, lambda u: u, merge=True)
        body, attachments = out[0]
        m = parse_message({"_id": "z", "msg": body,
                           "attachments": attachments,
                           "u": {"_id": "u1", "username": "bob"}}, "r")
        self.assertEqual(m.msg_type, "merge")
        self.assertEqual(len(m.forward_list), 2)
        self.assertEqual(m.forward_list[0]["sender"], "张三")
        self.assertEqual(m.forward_list[0]["ts_text"], "10:30")
        self.assertEqual(m.forward_list[0]["text"],
                         "[右键打开链接查看原图](/x.png)")
        # 图片条目标记 att，供渲染器识别为图片、只画图不显示文本
        self.assertEqual(m.forward_list[0]["att"]["image_url"], "/x.png")
        self.assertEqual(m.forward_list[0]["att"]["title"],
                         "@张三 · 10:30")
        self.assertEqual(m.forward_list[1]["att"], None)
        self.assertEqual(m.forward_list[1]["sender"], "张三")
        self.assertEqual(m.forward_list[1]["text"], "纯文本")

    def test_merge_file_attachment_stays_link(self):
        # 文件附件（title_link）统一走 markdown 链接 [name](url)
        msgs = [_mk_msg(id="a", msg_type="file", text="",
                        attachments=[{"title_link": "/file-upload/a.exe",
                                      "title": "a.exe"}])]
        out = build_forward_payload_many(msgs, lambda u: u, merge=True)
        self.assertEqual(out[0][1][0]["text"], "[a.exe](/file-upload/a.exe)")
        self.assertNotIn("!", out[0][1][0]["text"])

    def test_merge_backward_compat_no_attachments(self):
        # 旧格式（纯文本卡片、无 attachments）仍走 _parse_merge_body 兼容
        body = (f"{MERGE_HEAD}共1条\n"
                f"\x1f0\x1f张三\x1f1789000000\x1f旧卡片内容")
        m = parse_message({"_id": "z", "msg": body,
                           "u": {"_id": "u1", "username": "bob"}}, "r")
        self.assertEqual(m.msg_type, "merge")
        self.assertEqual(len(m.forward_list), 1)
        self.assertEqual(m.forward_list[0]["text"], "旧卡片内容")


class TestForwardDisplayHeight(unittest.TestCase):
    """合并卡片与逐条转发标记的高度计算：高度必须覆盖绘制内容，避免裁剪。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.model = MessageListModel()
        cls.delegate = mm.MessageDelegate(cls.model, None)

    def test_merge_bubble_height_matches_block_height(self):
        msg = _mk_msg(msg_type="merge",
                      forward_list=[{"sender": "张三", "ts": 1789000000.0,
                                     "text": "第一条内容", "att": None},
                                    {"sender": "李四", "ts": 1789000100.0,
                                     "text": "第二条", "att": None}])
        avail = mm._BUBBLE_MAX_W - mm._BUBBLE_PAD * 2
        self.assertEqual(
            self.model._calc_bubble_content_h(msg),
            self.model.merge_block_height(msg, avail))

    def test_merge_attachment_entry_reserves_line_height(self):
        # 带配文的 legacy 条目（text 有真实内容）仍按文本渲染，高度公式与
        # 气泡计算同源；不因 att 带 image_url 就误判成图片条目
        msg = _mk_msg(msg_type="merge",
                      forward_list=[{"sender": "张三", "ts": 1789000000.0,
                                     "ts_text": "10:30", "text": "看这个",
                                     "att": {"title": "x.png",
                                             "image_url": "/x.png"}}])
        avail = mm._BUBBLE_MAX_W - mm._BUBBLE_PAD * 2
        self.assertEqual(
            self.model._calc_bubble_content_h(msg),
            self.model.merge_block_height(msg, avail))

    def test_merge_image_entry_reserves_image_height(self):
        # 方案二图片卡片（text 恰为「右键打开链接查看原图」链接）按图片条目
        # 渲染，高度=图片高度，行高/布局/绘制同源
        msg = _mk_msg(msg_type="merge",
                      forward_list=[{"sender": "张三", "ts": 1789000000.0,
                                     "ts_text": "10:30",
                                     "text": "[右键打开链接查看原图](/x.png)",
                                     "att": {"title": "@张三 · 10:30",
                                             "image_url": "/x.png"}}])
        avail = mm._BUBBLE_MAX_W - mm._BUBBLE_PAD * 2
        self.assertEqual(
            self.model._calc_bubble_content_h(msg),
            self.model.merge_block_height(msg, avail))
        layout = self.model.merge_content_layout(
            msg, QRect(0, 0, avail, 400))
        self.assertEqual(len(layout), 1)
        self.assertIsNone(layout[0]["doc"])  # 图片条目不产生文本文档
        self.assertGreaterEqual(layout[0]["rect"].height(), 40)

    def test_forwarded_text_height_reserves_tag(self):
        plain = _mk_msg(text="你好")
        forwarded = _mk_msg(text="你好", is_forwarded=True)
        base = self.model._calc_bubble_content_h(plain)
        with_tag = self.model._calc_bubble_content_h(forwarded)
        self.assertEqual(with_tag, base + mm._FORWARD_TAG_H)

    def test_merge_card_uses_full_bubble_width(self):
        msg = _mk_msg(msg_type="merge", text=f"{MERGE_HEAD}共1条",
                      forward_list=[{"sender": "张三", "ts": 1789000000.0,
                                     "text": "内容", "att": None}])
        self.assertEqual(self.delegate._calc_bubble_width(msg, 700), 700)


if __name__ == "__main__":
    unittest.main()
