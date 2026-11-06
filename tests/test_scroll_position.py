"""消息流滚动定位验证：首屏停在最新，向上翻页不跳阅读位置。

运行：python -m unittest discover -s tests -v
用 offscreen 平台插件，只验证几何与滚动位置，不渲染文字。
"""
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from core.models import Message, User  # noqa: E402
from ui.chat_view import ChatView  # noqa: E402
from ui.message_model import RoleMessage  # noqa: E402

_APP = None
ROOM = "room1"


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _msgs(start, count):
    """造 count 条消息（新→旧顺序，与 get_history 返回一致）。"""
    out = []
    for i in range(count):
        n = start + i
        out.append(Message(
            id=f"m{n}", room_id=ROOM, is_mine=(n % 3 == 0),
            sender=User(id=f"u{n%4}", username=f"user{n%4}"),
            timestamp=1700000000.0 - n * 60,
            text="内容 " + ("很长的一段文字 " * (1 + n % 4)),
        ))
    return out


def _drain_events(app, rounds=30):
    """跑掉 singleShot(0) 与分批用的 16ms 定时器。"""
    for _ in range(rounds):
        app.processEvents()
        QTimer.singleShot(1, lambda: None)
        app.processEvents()


class TestJumpBottomButton(unittest.TestCase):
    """跳到底部悬浮钮：滚离底部 200px 出现、回到底部消失、点击回底。"""

    def setUp(self):
        self.view = ChatView()
        self.view.resize(620, 400)
        self.view.show()
        _drain_events(_APP)

    def tearDown(self):
        self.view.close()
        _drain_events(_APP)

    def _load(self):
        self.view.set_room(ROOM)
        self.view.prepend_messages(_msgs(0, 120), first_page=True)
        _drain_events(_APP)

    def test_hidden_at_bottom_shown_when_scrolled_up(self):
        self._load()
        self.view.scroll_to_bottom()
        _drain_events(_APP)
        self.assertTrue(self.view._jump_btn is None
                        or not self.view._jump_btn.isVisible())
        bar = self.view.verticalScrollBar()
        bar.setValue(max(0, bar.maximum() - 600))
        _drain_events(_APP)
        self.assertIsNotNone(self.view._jump_btn)
        self.assertTrue(self.view._jump_btn.isVisible())
        # 位置钉在 viewport 右下角
        vp = self.view.viewport()
        self.assertLess(vp.width() - self.view._jump_btn.x()
                        - self.view._jump_btn.width(), 40)

    def test_click_returns_to_bottom(self):
        self._load()
        bar = self.view.verticalScrollBar()
        bar.setValue(max(0, bar.maximum() - 600))
        _drain_events(_APP)
        self.view._jump_btn.click()
        _drain_events(_APP)
        self.assertEqual(bar.value(), bar.maximum())
        self.assertFalse(self.view._jump_btn.isVisible())

    def test_hidden_when_room_empty(self):
        self.view.set_room(ROOM)
        _drain_events(_APP)
        self.assertTrue(self.view._jump_btn is None
                        or not self.view._jump_btn.isVisible())


class TestFirstPageLandsAtBottom(unittest.TestCase):

    def setUp(self):
        self.view = ChatView()
        self.view.resize(620, 400)
        self.view.show()
        _drain_events(_APP)

    def tearDown(self):
        self.view.close()
        self.view.deleteLater()

    def _top_message_id(self):
        idx = self.view.indexAt(self.view.viewport().rect().topLeft())
        if not idx.isValid():
            return None
        msg = idx.data(RoleMessage)
        return msg.id if msg else None

    def test_first_page_scrolls_to_newest(self):
        self.view.set_room(ROOM)
        self.view.prepend_messages(_msgs(0, 30), first_page=True)
        _drain_events(_APP)

        bar = self.view.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0, "消息没有产生可滚动内容")
        self.assertAlmostEqual(bar.value(), bar.maximum(), delta=1)
        self.assertFalse(self.view._first_page_pending)

    def test_load_older_keeps_reading_position(self):
        self.view.set_room(ROOM)
        self.view.prepend_messages(_msgs(0, 30), first_page=True)
        _drain_events(_APP)

        # 往下读几条，然后向上翻页
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum() - 300)
        anchor_id = self._top_message_id()
        self.assertIsNotNone(anchor_id)

        self.view.prepend_messages(_msgs(30, 30))
        _drain_events(_APP)

        self.assertEqual(self._top_message_id(), anchor_id,
                         "加载更早消息后阅读位置跳走了")

    def test_batching_history_guard(self):
        """插行期间 is_batching_history 必须为真，供上层抑制重复翻页。"""
        self.view.set_room(ROOM)
        self.assertTrue(self.view.is_batching_history)   # 首屏待渲染
        self.view.prepend_messages(_msgs(0, 30), first_page=True)
        _drain_events(_APP)
        self.assertFalse(self.view.is_batching_history)

    def test_rebuild_then_scroll_to_bottom(self):
        """切回已缓存的会话：重建后也要停在最新消息。"""
        self.view.set_room(ROOM)
        cached = list(reversed(_msgs(0, 30)))   # 缓存按旧→新存
        self.view.rebuild_messages(cached)
        self.view.scroll_to_bottom(deferred=True)
        _drain_events(_APP)
        bar = self.view.verticalScrollBar()
        self.assertAlmostEqual(bar.value(), bar.maximum(), delta=1)


if __name__ == "__main__":
    unittest.main()
