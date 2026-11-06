"""消息互动底座：解析新增字段、回应 chip 几何、命中信号。

行高、绘制与点击命中共用 reaction_band——这里锁住布局与命中矩形一致。
"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QPoint, QRect, Qt  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

import ui.message_model as mm  # noqa: E402
from core.messages import parse_message, reaction_display  # noqa: E402
from core.models import Message, User  # noqa: E402
from ui.chat_view import ChatView  # noqa: E402
from ui.message_model import MessageListModel, RoleMessage  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _raw(**kw):
    base = {
        "_id": "m1",
        "msg": "hello",
        "ts": {"$date": 1700000000000},
        "u": {"_id": "u1", "username": "alice", "name": "Alice"},
    }
    base.update(kw)
    return base


class TestParseInteractionFields(unittest.TestCase):

    def test_edited_starred_pinned(self):
        msg = parse_message(_raw(
            editedAt={"$date": 1700000100000},
            starred=True, pinned=True), "r1")
        self.assertAlmostEqual(msg.edited_at, 1700000100.0)
        self.assertTrue(msg.starred)
        self.assertTrue(msg.pinned)

    def test_defaults_are_false(self):
        msg = parse_message(_raw(), "r1")
        self.assertEqual(msg.edited_at, 0.0)
        self.assertFalse(msg.starred)
        self.assertFalse(msg.pinned)
        self.assertFalse(msg.mentions_me)
        self.assertFalse(msg.mention_all)

    def test_mentions_me(self):
        msg = parse_message(_raw(
            mentions=[{"_id": "me1", "username": "me"}]), "r1",
            my_user_id="me1")
        self.assertTrue(msg.mentions_me)
        self.assertFalse(msg.mention_all)

    def test_mention_all_for_all_and_here(self):
        for ident in ("all", "here"):
            msg = parse_message(_raw(
                mentions=[{"_id": ident, "username": ident}]), "r1",
                my_user_id="me1")
            self.assertTrue(msg.mention_all, ident)
            self.assertFalse(msg.mentions_me, ident)

    def test_mentions_me_requires_login_id(self):
        raw = _raw(mentions=[{"_id": "me1", "username": "me"}])
        self.assertFalse(parse_message(raw, "r1").mentions_me)


class TestReactionDisplay(unittest.TestCase):

    def test_unicode_passthrough(self):
        self.assertEqual(reaction_display("👍"), "👍")

    def test_shortcode_becomes_unicode(self):
        self.assertEqual(reaction_display(":thumbsup:"), "👍")


class TestReactionBand(unittest.TestCase):

    def setUp(self):
        self.model = MessageListModel(None)

    @staticmethod
    def _msg(reactions):
        return Message(id="m1", room_id="r", text="hi",
                       msg_type="text", reactions=reactions)

    def test_empty_reactions(self):
        self.assertEqual(self.model.reaction_band(self._msg({}), 500), ([], 0))

    def test_layout_and_mine_flag(self):
        self.model.set_my_username("me")
        msg = self._msg({
            "👍": {"usernames": ["me", "bob"]},
            "❤": {"usernames": ["bob"]},
        })
        chips, h = self.model.reaction_band(msg, 500)
        self.assertEqual(len(chips), 2)
        self.assertGreater(h, 0)
        self.assertEqual(chips[0][0], "👍")
        self.assertEqual(chips[0][1], 2)
        self.assertTrue(chips[0][2])
        self.assertEqual(chips[1][0], "❤")
        self.assertFalse(chips[1][2])
        # 同一行、不重叠
        self.assertLess(chips[0][3].right(), chips[1][3].left())
        self.assertEqual(chips[0][3].top(), chips[1][3].top())

    def test_overflow_uses_hidden_chip(self):
        reactions = {f":k{i}:": {"usernames": ["u"]} for i in range(10)}
        chips, h = self.model.reaction_band(self._msg(reactions), 120)
        self.assertTrue(chips)
        # 单行不换行：最后一个 chip 是 +N（key 为空）
        self.assertEqual(chips[-1][0], "")
        self.assertGreater(chips[-1][1], 0)
        self.assertLessEqual(chips[-1][3].right() + 1, 120)
        # 高度与完整显示时一致（chip 单行，行高稳定）
        full, full_h = self.model.reaction_band(self._msg(reactions), 5000)
        self.assertEqual(h, full_h)

    def test_band_width_matches_chips(self):
        msg = self._msg({"👍": {"usernames": ["a"]}})
        chips, _h = self.model.reaction_band(msg, 500)
        self.assertEqual(self.model.reaction_band_width(msg, 500),
                         chips[-1][3].right() + 1)


class TestReactionHitTest(unittest.TestCase):

    def setUp(self):
        self.view = ChatView()
        self.view.resize(640, 420)
        self.view.set_room("r")
        self.msg = Message(
            id="m1", room_id="r", text="带回应的消息", msg_type="text",
            sender=User(id="u1", username="alice", name="Alice",
                        avatar_url=""),
            timestamp=1700000000.0,
            reactions={"👍": {"usernames": ["alice", "bob"]}})
        self.view.model.prepend_messages_immediate([self.msg])
        self.delegate = self.view._delegate
        self.index = self.view.model.index(0, 0)
        self.row_rect = QRect(
            0, 0, 640, self.index.data(mm.RoleRowHeight))

    def test_reaction_rects_inside_bubble_and_band(self):
        rects = self.delegate.reaction_rects(self.index, self.row_rect)
        self.assertEqual(len(rects), 1)
        key, rect = rects[0]
        self.assertEqual(key, "👍")
        bubble, _avatar, _img = self.delegate.layout_rects(
            self.index, self.row_rect)
        self.assertTrue(bubble.contains(rect.topLeft()))
        self.assertLessEqual(rect.bottom(), bubble.bottom())

    def test_row_height_reserves_band(self):
        plain = Message(id="m2", room_id="r", text="带回应的消息",
                        msg_type="text", timestamp=1700000060.0)
        self.view.model.append_message(plain)
        idx2 = self.view.model.index(1, 0)
        h_with = self.index.data(mm.RoleRowHeight)
        h_without = idx2.data(mm.RoleRowHeight)
        self.assertGreater(h_with, h_without)

    def test_click_chip_emits_toggle(self):
        rects = self.delegate.reaction_rects(self.index, self.row_rect)
        key, rect = rects[0]
        self.view.show()
        QApplication.processEvents()
        got = []
        self.view.reaction_toggle_requested.connect(
            lambda msg, k: got.append((msg.id, k)))
        QTest.mouseClick(self.view.viewport(), Qt.LeftButton,
                         pos=rect.center())
        self.assertEqual(got, [("m1", "👍")])


class TestContextMenu(unittest.TestCase):
    """右键菜单入口：失败重发、表情回应、置顶/星标、编辑（E2E 隐藏）。"""

    def setUp(self):
        self.view = ChatView()
        self.view.resize(640, 420)
        self.view.set_room("r")

    def _menu(self, msg, encrypted=False):
        """打开右键菜单；返回 ({label: action_mock})。"""
        self.view.set_room_encrypted(encrypted)
        acts = {}

        def _make_action(*args, **_kw):
            act = mock.Mock()
            acts[args[-1]] = act
            return act

        with mock.patch("ui.chat_view.QMenu") as MQ:
            MQ.return_value.addAction.side_effect = _make_action
            self.view._show_context_menu(msg, QPoint(0, 0))
        return acts

    def test_failed_local_message_offers_retry(self):
        msg = Message(id="local-1", room_id="r", text="hi", is_mine=True,
                      status="failed", msg_type="text",
                      sender=User(username="me", name="我"))
        acts = self._menu(msg)
        self.assertIn("重新发送", acts)
        got = []
        self.view.retry_requested.connect(lambda m: got.append(m.id))
        acts["重新发送"].triggered.connect.call_args[0][0](False)
        self.assertEqual(got, ["local-1"])

    def test_sent_message_has_no_retry(self):
        msg = Message(id="m1", room_id="r", text="hi", is_mine=True,
                      status="sent", msg_type="text")
        self.assertNotIn("重新发送", self._menu(msg))

    def test_edit_visible_for_own_text(self):
        msg = Message(id="m1", room_id="r", text="hi", is_mine=True,
                      msg_type="text", sender=User(username="me"))
        self.assertIn("编辑", self._menu(msg))

    def test_edit_hidden_in_e2e_room(self):
        msg = Message(id="m1", room_id="r", text="hi", is_mine=True,
                      msg_type="text", sender=User(username="me"))
        self.assertNotIn("编辑", self._menu(msg, encrypted=True))

    def test_edit_hidden_for_others_forwarded_and_non_text(self):
        for msg in (
                Message(id="m1", room_id="r", text="hi", is_mine=False,
                        msg_type="text", sender=User(username="bob")),
                Message(id="m2", room_id="r", text="hi", is_mine=True,
                        msg_type="text", is_forwarded=True),
                Message(id="m3", room_id="r", text="hi", is_mine=True,
                        msg_type="file", file_name="a.txt")):
            self.assertNotIn("编辑", self._menu(msg), msg.id)

    def test_pin_and_star_toggle_flags(self):
        msg = Message(id="m1", room_id="r", text="hi", msg_type="text",
                      sender=User(username="bob"), pinned=False, starred=True)
        acts = self._menu(msg)
        self.assertIn("置顶消息", acts)
        self.assertIn("取消星标", acts)
        pins, stars = [], []
        self.view.pin_requested.connect(lambda m, v: pins.append((m.id, v)))
        self.view.star_requested.connect(lambda m, v: stars.append((m.id, v)))
        acts["置顶消息"].triggered.connect.call_args[0][0](False)
        acts["取消星标"].triggered.connect.call_args[0][0](False)
        self.assertEqual(pins, [("m1", True)])
        self.assertEqual(stars, [("m1", False)])

    def test_system_message_hides_interactions(self):
        msg = Message(id="s1", room_id="r", text="x", msg_type="system")
        acts = self._menu(msg)
        for label in ("添加表情回应…", "置顶消息", "星标消息"):
            self.assertNotIn(label, acts)

    def test_reaction_add_entry_emits(self):
        msg = Message(id="m1", room_id="r", text="hi", msg_type="text")
        acts = self._menu(msg)
        self.assertIn("添加表情回应…", acts)
        got = []
        self.view.reaction_add_requested.connect(lambda m: got.append(m.id))
        acts["添加表情回应…"].triggered.connect.call_args[0][0](False)
        self.assertEqual(got, ["m1"])


class TestTimeRowMarkers(unittest.TestCase):
    """时间行标记（已编辑/置顶/星标）：只影响绘制，不改行高。"""

    def setUp(self):
        self.view = ChatView()
        self.delegate = self.view._delegate

    def test_markers_follow_flags(self):
        msg = Message(id="m1", room_id="r", text="hi", pinned=True,
                      starred=True, edited_at=1700000000.0)
        markers = self.delegate._time_markers(msg)
        kinds = [m[0] for m in markers]
        self.assertIn("icon", kinds)
        self.assertIn(("text", "已编辑"), markers)

    def test_no_markers_for_plain_message(self):
        msg = Message(id="m1", room_id="r", text="hi")
        self.assertEqual(self.delegate._time_markers(msg), [])

    def test_markers_sit_next_to_right_aligned_time(self):
        """宽气泡时间右对齐：标记要贴时间文字，不能落到气泡左缘。

        真机截图里宽气泡的星标画在了时间行的最左端，与右侧时间脱节。
        """
        from PyQt5.QtGui import QFontMetrics, QImage, QPainter
        from PyQt5.QtWidgets import QStyleOptionViewItem
        msg = Message(id="m1", room_id="r", text="x" * 300,
                      msg_type="text", is_mine=True, starred=True,
                      timestamp=1700000000.0,
                      sender=User(id="u1", username="me", name="Me"))
        self.view.set_room("r")
        self.view.model.prepend_messages_immediate([msg])
        idx = self.view.model.index(0, 0)
        row = QRect(0, 0, 640, idx.data(mm.RoleRowHeight))
        opt = QStyleOptionViewItem()
        opt.rect = row
        img = QImage(640, max(1, row.height()), QImage.Format_ARGB32)
        img.fill(0xFFFFFFFF)
        painter = QPainter(img)
        got = []
        real = self.delegate._paint_time_markers

        def _capture(p, m, x, y, h):
            got.append(x)
            return real(p, m, x, y, h)

        try:
            with mock.patch.object(self.delegate, "_paint_time_markers",
                                   side_effect=_capture):
                self.delegate.paint(painter, opt, idx)
        finally:
            painter.end()
        self.assertEqual(len(got), 1)
        bubble, _avatar, _image = self.delegate.layout_rects(idx, row)
        fm = QFontMetrics(self.view.model._tiny_font)
        text_w = fm.horizontalAdvance(
            mm.format_chat_time(msg.timestamp))
        markers_w = self.delegate._time_markers_width(fm, msg)
        chunk, _kind, _unread = self.delegate._status_chunk(idx, row)
        right = (chunk.left() - mm._STATUS_GAP if not chunk.isNull()
                 else bubble.left() + bubble.width())
        expected = right - text_w - mm._STATUS_GAP - markers_w
        self.assertAlmostEqual(got[0], expected, delta=1)
        # 宽气泡：不能退到气泡左缘附近
        self.assertGreater(got[0], bubble.left() + 20)


if __name__ == "__main__":
    unittest.main()
