"""已读状态渲染层：status_spec 判定矩阵 + 单调外推 + 状态块几何 + 图标绘制。

status_spec 是委托绘制与点击命中的唯一判定入口；行高公式在已读状态
变化时必须不变（time_row_h 早已预留），否则会出现裁剪/重叠。

运行：python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QEvent, QRect, Qt  # noqa: E402
from PyQt5.QtGui import QMouseEvent, QPixmap, QPainter  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from core.models import Message, User  # noqa: E402
from ui.chat_view import ChatView  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _me():
    return User(id="me", username="me", name="我", avatar_url="")


def _other():
    return User(id="u1", username="alice", name="张三", avatar_url="")


def _msg(mid, mine, status="sent", msg_type="text", ts=1700000000.0):
    return Message(id=mid, room_id="r", is_mine=mine,
                   sender=_me() if mine else _other(),
                   timestamp=ts, msg_type=msg_type, text="正文",
                   status=status)


class StatusSpecTest(unittest.TestCase):

    def setUp(self):
        self.view = ChatView()
        self.view.resize(640, 420)
        self.view.set_room("r")
        self.model = self.view.model
        # 两条自己的消息夹一条别人的（行号 0,1,2）
        self.model.prepend_messages_immediate([
            _msg("m1", True, ts=1700000000.0),
            _msg("m2", False, ts=1700000060.0),
            _msg("m3", True, ts=1700000120.0),
        ])

    def _spec(self, mid):
        row = self.model.find_message_row(mid)
        return self.model.status_spec(self.model.get_message(row), row)

    def test_other_and_system_never_show_status(self):
        self.assertEqual(self._spec("m2"), ("none", 0))
        sys_msg = _msg("sx", True, msg_type="system")
        self.model.append_message(sys_msg)
        self.assertEqual(self.model.status_spec(sys_msg,
                                                self.model.rowCount() - 1),
                         ("none", 0))

    def test_sending_and_failed_take_priority(self):
        row = self.model.find_message_row("m1")
        self.model.get_message(row).status = "sending"
        self.assertEqual(self._spec("m1"), ("clock", 0))
        self.model.get_message(row).status = "failed"
        self.assertEqual(self._spec("m1"), ("failed", 0))

    def test_dm_sent_without_receipts(self):
        self.model.set_room_context("d", 0)
        self.assertEqual(self._spec("m1"), ("sent", 0))

    def test_dm_read_with_receipts(self):
        self.model.set_room_context("d", 0)
        self.model.set_read_receipts("m3", [{"userId": "u1", "ts": 0}])
        # 回执挂在最后一条上，前面的自己消息靠单调外推也判已读
        self.assertEqual(self._spec("m3"), ("read", 0))
        self.assertEqual(self._spec("m1"), ("read", 0))

    def test_dm_degraded_stays_sent(self):
        self.model.set_room_context("d", 0)
        self.model.set_receipts_supported(False)
        self.model.set_read_receipts("m3", [{"userId": "u1", "ts": 0}])
        self.assertEqual(self._spec("m1"), ("sent", 0))

    def test_group_without_member_count_shows_nothing(self):
        self.model.set_room_context("c", 0)
        self.model.set_read_receipts("m3", [{"userId": "u1", "ts": 0}])
        self.assertEqual(self._spec("m1"), ("none", 0))

    def test_group_unread_count(self):
        """member_count 约定不含发送者本人：3 人未读名单中 1 人已读 → 2。"""
        self.model.set_room_context("c", 3)
        self.model.set_read_receipts("m3", [{"userId": "u1", "ts": 0}])
        self.assertEqual(self._spec("m1"), ("unread_count", 2))

    def test_group_all_read_shows_read_icon(self):
        """群聊全员已读：自己消息画已读双勾（对齐私聊语义）。"""
        self.model.set_room_context("c", 1)
        self.model.set_read_receipts("m3", [{"userId": "u1", "ts": 0}])
        self.assertEqual(self._spec("m1"), ("read", 0))
        self.assertEqual(self._spec("m3"), ("read", 0))

    def test_group_empty_receipts(self):
        """最新消息空名单（还没人读）：本行是准确结论→全员未读；更早
        消息外推被空名单阻断→宁缺勿错不显示，等补拉数据到达再纠正。"""
        self.model.set_room_context("c", 3)
        self.model.set_read_receipts("m3", [])
        self.assertEqual(self._spec("m3"), ("unread_count", 3))
        self.assertEqual(self._spec("m1"), ("none", 0))

    def test_empty_tail_doesnt_block_extrapolation(self):
        """最新消息空名单不能截断外推：更早消息的已读状态来自之后第一
        条非空名单。李肖房间实测：最后一条没人读（剔除自己后为空），
        旧的已读消息全被误判成灰单勾。"""
        self.model.set_room_context("d", 0)
        self.model.set_read_receipts("m3", [])
        self.assertEqual(self._spec("m1"), ("sent", 0))
        self.model.append_message(_msg("m4", True, ts=1700000180.0))
        self.model.set_read_receipts("m4", [{"userId": "u1", "ts": 0}])
        self.assertEqual(self._spec("m4"), ("read", 0))
        self.assertEqual(self._spec("m3"), ("sent", 0))  # 本行空名单=没人读过
        self.assertEqual(self._spec("m1"), ("read", 0))  # 跳过空名单外推到 m4

    def test_group_degraded_shows_nothing(self):
        self.model.set_room_context("c", 3)
        self.model.set_receipts_supported(False)
        self.model.set_read_receipts("m3", [{"userId": "u1", "ts": 0}])
        self.assertEqual(self._spec("m1"), ("none", 0))

    def test_extrapolation_never_looks_backwards(self):
        """只有更早的消息有回执时，更新的消息不受影响（已读不倒挂）。"""
        self.model.set_room_context("d", 0)
        self.model.set_read_receipts("m1", [{"userId": "u1", "ts": 0}])
        self.assertEqual(self._spec("m1"), ("read", 0))
        self.assertEqual(self._spec("m3"), ("sent", 0))

    # ---- 多人私聊（type 仍为 "d" 但对方不止一人）---- #
    def test_multi_dm_shows_unread_count_like_group(self):
        self.model.set_room_context("d", 2)
        self.model.set_read_receipts("m3", [{"userId": "u1", "ts": 0}])
        self.assertEqual(self._spec("m3"), ("unread_count", 1))
        # 单调外推同样作用于更早的自己消息
        self.assertEqual(self._spec("m1"), ("unread_count", 1))

    def test_multi_dm_all_read_shows_read_icon(self):
        self.model.set_room_context("d", 2)
        self.model.set_read_receipts(
            "m3", [{"userId": "u1", "ts": 0}, {"userId": "u2", "ts": 0}])
        self.assertEqual(self._spec("m1"), ("read", 0))
        self.assertEqual(self._spec("m3"), ("read", 0))

    def test_multi_dm_degraded_shows_nothing(self):
        self.model.set_room_context("d", 2)
        self.model.set_receipts_supported(False)
        self.model.set_read_receipts("m3", [{"userId": "u1", "ts": 0}])
        self.assertEqual(self._spec("m1"), ("none", 0))

    def test_dm_pair_keeps_icons_when_count_is_one(self):
        self.model.set_room_context("d", 1)
        self.model.set_read_receipts("m3", [{"userId": "u1", "ts": 0}])
        self.assertEqual(self._spec("m1"), ("read", 0))
        self.assertEqual(self._spec("m3"), ("read", 0))

    def test_dm_unknown_count_falls_back_to_icons(self):
        """成员列表还没拉到（count=0）时先按 1 对 1 画图标。"""
        self.model.set_room_context("d", 0)
        self.model.set_read_receipts("m3", [{"userId": "u1", "ts": 0}])
        self.assertEqual(self._spec("m1"), ("read", 0))

    # ---- 群聊/多人私聊中别人发的消息 ---- #
    def test_group_other_sender_shows_unread_count(self):
        """群聊里别人发的消息同样显示未读人数（外推自更新的消息）。"""
        self.model.set_room_context("c", 3)
        self.model.set_read_receipts("m3", [{"userId": "u1", "ts": 0}])
        self.assertEqual(self._spec("m2"), ("unread_count", 2))

    def test_group_other_sender_extrapolates_to_own_older(self):
        """别人消息的回执同样向更早的消息外推。"""
        self.model.set_room_context("c", 3)
        self.model.set_read_receipts("m2", [{"userId": "u1", "ts": 0}])
        self.assertEqual(self._spec("m2"), ("unread_count", 2))
        self.assertEqual(self._spec("m1"), ("unread_count", 2))
        # m3 比 m2 新，不受更早消息回执影响（已读不倒挂）
        self.assertEqual(self._spec("m3"), ("none", 0))

    def test_group_other_sender_all_read_shows_nothing(self):
        """群聊里别人的消息全员已读也不画勾（已读状态不属于自己）。"""
        self.model.set_room_context("c", 1)
        self.model.set_read_receipts("m2", [{"userId": "u1", "ts": 0}])
        self.assertEqual(self._spec("m2"), ("none", 0))

    def test_dm_other_sender_never_shows_status(self):
        """1 对 1 私聊别人发的消息不显示任何状态。"""
        self.model.set_room_context("d", 1)
        self.model.set_read_receipts("m3", [{"userId": "u1", "ts": 0}])
        self.assertEqual(self._spec("m2"), ("none", 0))

    def test_huge_group_shows_nothing(self):
        """超大群（全公司广播频道）未读数无意义，超过阈值不显示；
        自己消息的发送中/失败瞬态不受影响。"""
        from ui.message_model import UNREAD_COUNT_LIMIT
        self.model.set_room_context("c", UNREAD_COUNT_LIMIT + 1)
        self.model.set_read_receipts("m3", [{"userId": "u1", "ts": 0}])
        self.assertEqual(self._spec("m1"), ("none", 0))
        self.assertEqual(self._spec("m2"), ("none", 0))
        self.assertEqual(self._spec("m3"), ("none", 0))
        row = self.model.find_message_row("m1")
        self.model.get_message(row).status = "failed"
        self.assertEqual(self._spec("m1"), ("failed", 0))


class HeightStabilityTest(unittest.TestCase):

    def test_heights_unchanged_by_receipts(self):
        view = ChatView()
        view.resize(640, 420)
        view.set_room("r")
        model = view.model
        model.prepend_messages_immediate([
            _msg("m1", True, ts=1700000000.0),
            _msg("m2", True, ts=1700000060.0),
        ])
        model.set_room_context("c", 5)
        before = [model._get_height(r) for r in range(model.rowCount())]
        model.set_read_receipts("m2", [{"userId": "u1", "ts": 0}])
        model.set_room_context("d", 0)
        after = [model._get_height(r) for r in range(model.rowCount())]
        self.assertEqual(before, after, "已读状态变化不得改变行高")


class StatusChunkGeometryTest(unittest.TestCase):

    def setUp(self):
        self.view = ChatView()
        self.view.resize(640, 420)
        self.view.set_room("r")
        self.model = self.view.model
        self.delegate = self.view._delegate

    def _add_group_unread(self):
        self.model.set_room_context("c", 5)
        self.model.prepend_messages_immediate([
            _msg("m1", True, ts=1700000000.0),
        ])
        self.model.set_read_receipts("m1", [{"userId": "u1", "ts": 0}])
        row = self.model.find_message_row("m1")
        idx = self.model.index(row, 0)
        return idx, self.view.visualRect(idx)

    def test_unread_chunk_aligns_bubble_right(self):
        idx, row_rect = self._add_group_unread()
        chunk, kind, unread = self.delegate._status_chunk(idx, row_rect)
        self.assertEqual(kind, "unread_count")
        self.assertEqual(unread, 4)
        bubble, _a, _i = self.delegate.layout_rects(idx, row_rect)
        self.assertEqual(chunk.right(), bubble.right())
        self.assertFalse(self.delegate.status_rect(idx, row_rect).isNull())

    def test_dm_icon_not_clickable(self):
        self.model.set_room_context("d", 0)
        self.model.prepend_messages_immediate([
            _msg("m1", True, ts=1700000000.0),
        ])
        row = self.model.find_message_row("m1")
        idx = self.model.index(row, 0)
        row_rect = self.view.visualRect(idx)
        chunk, kind, _u = self.delegate._status_chunk(idx, row_rect)
        self.assertEqual(kind, "sent")
        self.assertFalse(chunk.isNull())
        self.assertTrue(self.delegate.status_rect(idx, row_rect).isNull(),
                        "私聊的状态图标不可点")

    def test_other_sender_chunk_follows_time_not_bubble_right(self):
        """别人消息的未读文本在左对齐时间的右侧，不与时间重叠、不裁剪
        （此前放气泡右缘把时间挤进行左缘裁掉）。"""
        from ui.message_model import _STATUS_GAP, format_chat_time
        from PyQt5.QtGui import QFontMetrics
        self.model.set_room_context("c", 5)
        self.model.prepend_messages_immediate([
            _msg("o1", False, ts=1700000000.0),
        ])
        self.model.set_read_receipts("o1", [{"userId": "u1", "ts": 0}])
        row = self.model.find_message_row("o1")
        idx = self.model.index(row, 0)
        row_rect = self.view.visualRect(idx)
        chunk, kind, _u = self.delegate._status_chunk(idx, row_rect)
        self.assertEqual(kind, "unread_count")
        bubble, _a, _i = self.delegate.layout_rects(idx, row_rect)
        tiny_fm = QFontMetrics(self.model._tiny_font)
        t_w = tiny_fm.horizontalAdvance(format_chat_time(1700000000.0))
        self.assertEqual(chunk.left(), bubble.left() + t_w + _STATUS_GAP)
        self.assertGreaterEqual(chunk.left(), row_rect.left())
        self.assertLessEqual(chunk.right(), row_rect.right())
        # 可点击
        self.assertFalse(self.delegate.status_rect(idx, row_rect).isNull())

    def test_none_kind_null_rect(self):
        self.model.set_room_context("c", 5)
        self.model.prepend_messages_immediate([
            _msg("m1", False, ts=1700000000.0),
        ])
        row = self.model.find_message_row("m1")
        idx = self.model.index(row, 0)
        chunk, _kind, _u = self.delegate._status_chunk(
            idx, self.view.visualRect(idx))
        self.assertTrue(chunk.isNull())


class StatusIconPaintTest(unittest.TestCase):

    def test_all_kinds_paint_without_error(self):
        view = ChatView()
        delegate = view._delegate
        for kind in ("clock", "sent", "read", "failed"):
            pm = QPixmap(32, 32)
            pm.fill()
            p = QPainter(pm)
            try:
                delegate._draw_status_icon(p, kind, QRect(2, 2, 12, 12))
            finally:
                p.end()


class ViewWidthSyncTest(unittest.TestCase):
    """缩放窗口后视口宽度必须同步：委托缓存装在 view 上时过滤时机早于
    QAbstractScrollArea 改 viewport 尺寸，读到的永远滞后一拍，缩窗后
    行宽超过视口、右侧（自己的气泡/未读块/图片）点击全部错位。"""

    def setUp(self):
        self.view = ChatView()
        self.view.resize(1000, 700)
        self.view.set_room("r")
        self.view.show()

    def _synced(self):
        from PyQt5.QtWidgets import QApplication
        QApplication.processEvents()
        vp = self.view.viewport().width()
        return (self.view._delegate._view_width == vp
                and self.view.model._view_width == vp
                and self.view.visualRect(
                    self.view.model.index(0, 0)).width() <= vp)

    def test_width_tracks_resizes(self):
        self.assertTrue(self._synced(), "初次显示即应同步")
        for w in (700, 1200, 900):
            self.view.resize(w, 700)
            self.assertTrue(self._synced(), f"resize 到 {w} 后应同步")

    def test_click_after_shrink(self):
        self.view.model.set_room_context("c", 5)
        self.view.model.prepend_messages_immediate([
            _msg("m1", True, ts=1700000000.0),
        ])
        self.view.model.set_read_receipts("m1", [{"userId": "u1", "ts": 0}])
        got = []
        self.view.read_receipts_requested.connect(
            lambda m, p: got.append(m))
        for w in (700, 1000):
            self.view.resize(w, 700)
            self._synced()
            row = self.view.model.find_message_row("m1")
            idx = self.view.model.index(row, 0)
            srect = self.view._delegate.status_rect(
                idx, self.view.visualRect(idx))
            self.assertFalse(srect.isNull())
            self.assertLessEqual(srect.right(),
                                 self.view.viewport().width() - 1,
                                 "状态块不得落在视口之外")
            before = len(got)
            ev = QMouseEvent(QEvent.MouseButtonPress, srect.center(),
                             Qt.LeftButton, Qt.NoButton, Qt.NoModifier)
            self.view.mousePressEvent(ev)
            self.assertEqual(len(got), before + 1,
                             f"resize 到 {w} 后点击应命中")


if __name__ == "__main__":
    unittest.main()
