"""P2 优化：消息按 id 索引（_row_by_id）的正确性测试。

验证 find_message_row 在 append / prepend / remove / update / clear 之后
返回的行号始终与 _messages 一致，且重复 id 保留第一个（与原线性扫描语义一致）。
GUI 测试，offscreen 渲染，不联网。
运行：python -m unittest tests.test_row_index -v
"""
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication  # noqa: E402

from core.models import Message  # noqa: E402
from ui.message_model import MessageListModel  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _msg(mid, ts):
    return Message(id=mid, room_id="r", timestamp=ts, msg_type="text",
                   text="t" + mid)


class TestRowIndex(unittest.TestCase):
    def setUp(self):
        self.model = MessageListModel(None)

    def test_append_then_find(self):
        for i in range(5):
            self.model.append_message(_msg("m%d" % i, float(i)))
        for i in range(5):
            self.assertEqual(self.model.find_message_row("m%d" % i), i)
        self.assertEqual(self.model.find_message_row("nope"), -1)

    def test_prepend_shifts_rows(self):
        for i in range(3):
            self.model.append_message(_msg("m%d" % i, float(i)))
        # 顶部插入 2 条更早的历史
        self.model.prepend_messages_immediate(
            [_msg("h0", -2.0), _msg("h1", -1.0)])
        # 新行在 0,1；旧行整体后移
        self.assertEqual(self.model.find_message_row("h0"), 0)
        self.assertEqual(self.model.find_message_row("h1"), 1)
        self.assertEqual(self.model.find_message_row("m0"), 2)
        self.assertEqual(self.model.find_message_row("m2"), 4)
        # 行号与 _messages 完全一致
        for i, m in enumerate(self.model.all_messages()):
            self.assertEqual(self.model.find_message_row(m.id), i)

    def test_remove_updates_index(self):
        for i in range(4):
            self.model.append_message(_msg("m%d" % i, float(i)))
        self.model.remove_message("m1")
        self.assertEqual(self.model.find_message_row("m1"), -1)
        self.assertEqual(self.model.find_message_row("m0"), 0)
        self.assertEqual(self.model.find_message_row("m2"), 1)
        self.assertEqual(self.model.find_message_row("m3"), 2)
        for i, m in enumerate(self.model.all_messages()):
            self.assertEqual(self.model.find_message_row(m.id), i)

    def test_update_keeps_row(self):
        for i in range(3):
            self.model.append_message(_msg("m%d" % i, float(i)))
        new = _msg("m1", 1.0)
        new.text = "edited"
        self.model.update_message(new)
        self.assertEqual(self.model.find_message_row("m1"), 1)
        self.assertEqual(self.model.all_messages()[1].text, "edited")

    def test_duplicate_id_keeps_first(self):
        # 顶部插入含重复 id 时，索引保留第一个（与原线性扫描语义一致）
        self.model.append_message(_msg("m0", 0.0))
        self.model.append_message(_msg("m1", 1.0))
        self.model.prepend_messages_immediate([_msg("dup", -1.0), _msg("m0", 0.0)])
        # m0 的第一个出现位置是索引 1（prepend 的 m0 在 index 2，但保留 index 1 的旧 m0）
        self.assertEqual(self.model.find_message_row("m0"), 1)
        self.assertEqual(self.model.all_messages()[1].id, "m0")

    def test_clear_resets_index(self):
        for i in range(3):
            self.model.append_message(_msg("m%d" % i, float(i)))
        self.model.clear()
        self.assertEqual(self.model.find_message_row("m0"), -1)
        self.assertEqual(self.model.rowCount(), 0)


if __name__ == "__main__":
    unittest.main()
