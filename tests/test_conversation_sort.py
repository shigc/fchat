"""会话列表排序验证：收藏优先，其余按最后消息时间倒序。

运行：python -m unittest discover -s tests -v
需要 Qt widgets，故使用 offscreen 平台插件，不依赖真实服务器。
"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from core.models import Conversation  # noqa: E402
from ui.conversation_list import ConversationList  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class TestConversationSort(unittest.TestCase):

    def setUp(self):
        # 不读也不写真实用户配置：真实配置里收藏组可能是折叠的
        # （TestFavoritesGroup 同款 patch，漏了会在个别机器上随机失败）
        patcher = mock.patch("ui.conversation_list.config.load_config",
                             return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.list = ConversationList()
        self.t_base = 1_700_000_000.0

    def _conv(self, rid, minutes_ago, favorite=False):
        return Conversation(
            id=rid, name=rid, room_type="c",
            is_favorite=favorite,
            last_message_time=self.t_base - minutes_ago * 60,
            last_message_text=f"{rid} 的消息",
        )

    def _order(self):
        # 分组头行不带 UserRole（None），过滤掉只看会话行
        return [self.list.item(i).data(Qt.UserRole)
                for i in range(self.list.count())
                if self.list.item(i).data(Qt.UserRole)]

    def test_favorite_first_then_time_desc(self):
        # 录入顺序故意打乱：a 最新、b 最旧、c 居中但被收藏
        self.list.set_conversations([
            self._conv("b", 300),
            self._conv("c", 120, favorite=True),
            self._conv("a", 10),
        ])
        self.assertEqual(self._order(), ["c", "a", "b"])

    def test_multiple_favorites_sorted_by_time(self):
        self.list.set_conversations([
            self._conv("a", 10),
            self._conv("p1", 500, favorite=True),
            self._conv("p2", 100, favorite=True),
        ])
        self.assertEqual(self._order(), ["p2", "p1", "a"])

    def test_unfavorite_falls_back_to_time_order(self):
        convs = [self._conv("a", 10), self._conv("b", 300),
                 self._conv("c", 120, favorite=True)]
        self.list.set_conversations(convs)
        self.assertEqual(self._order(), ["c", "a", "b"])

        # 取消收藏：c 不应停在顶部，而应落回自己的时间位置
        by_id = {c.id: c for c in convs}
        by_id["c"].is_favorite = False
        self.list.upsert_conversation(by_id["c"])
        self.assertEqual(self._order(), ["a", "c", "b"])

    def test_newer_message_bumps_conversation_up(self):
        convs = [self._conv("a", 10), self._conv("b", 300)]
        self.list.set_conversations(convs)
        by_id = {c.id: c for c in convs}

        by_id["b"].last_message_time = self.t_base + 1  # 比谁都新
        self.list.upsert_conversation(by_id["b"])
        self.assertEqual(self._order(), ["b", "a"])

        # 收藏的 a 仍然压过更新的消息
        by_id["a"].is_favorite = True
        self.list.upsert_conversation(by_id["a"])
        self.assertEqual(self._order(), ["a", "b"])

    def test_selection_not_reemitted_during_resort(self):
        """重排后选中项没变时，不得再发 conversation_selected 重开会话。"""
        convs = [self._conv("a", 10), self._conv("b", 300),
                 self._conv("c", 120)]
        self.list.set_conversations(convs)
        fired = []
        self.list.conversation_selected.connect(lambda c: fired.append(c.id))
        self.list.select_conversation("b")
        fired.clear()

        # 来一条新消息触发重排，选中项 b 位置变化但不该重开
        by_id = {c.id: c for c in convs}
        by_id["c"].last_message_time = self.t_base + 5
        self.list.upsert_conversation(by_id["c"])
        self.assertEqual(self._order(), ["c", "a", "b"])
        self.assertEqual(fired, [])


class TestFavoritesGroup(unittest.TestCase):
    """收藏分组头行：展开/折叠、头行插入位置、惰性补行游标。"""

    def setUp(self):
        # patch 必须在创建列表之前：ConversationList.__init__ 就会读配置，
        # 真实配置里收藏组折叠会让下面全部断言按折叠态翻车
        patcher = mock.patch("ui.conversation_list.config.load_config",
                             return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)
        save_patcher = mock.patch("ui.conversation_list.config.save_ui_prefs")
        self.save_prefs = save_patcher.start()
        self.addCleanup(save_patcher.stop)
        self.list = ConversationList()
        self.t_base = 1_700_000_000.0

    def _conv(self, rid, minutes_ago, favorite=False):
        return Conversation(
            id=rid, name=rid, room_type="c",
            is_favorite=favorite,
            last_message_time=self.t_base - minutes_ago * 60,
            last_message_text=f"{rid} 的消息",
        )

    def _header_rows(self):
        return [i for i in range(self.list.count())
                if not self.list.item(i).data(Qt.UserRole)]

    def _order(self):
        return [self.list.item(i).data(Qt.UserRole)
                for i in range(self.list.count())
                if self.list.item(i).data(Qt.UserRole)]

    def test_header_inserted_before_favorites(self):
        self.list.set_conversations([
            self._conv("a", 10), self._conv("c", 120, favorite=True),
            self._conv("b", 300),
        ])
        # 行序：头行 c a b；头行文本带数量与展开箭头
        self.assertEqual(self._header_rows(), [0])
        w = self.list.itemWidget(self.list.item(0))
        self.assertIn("⭐ 收藏 (1)", w.label.text())
        self.assertIn("▾", w.label.text())

    def test_no_header_without_favorites_or_on_search(self):
        self.list.set_conversations([self._conv("a", 10)])
        self.assertEqual(self._header_rows(), [])
        self.list.set_conversations([
            self._conv("a", 10), self._conv("c", 120, favorite=True)])
        self.list.set_search_filter("a")
        self.assertEqual(self._header_rows(), [], "搜索态不分组")

    def test_collapse_hides_favorites_keeps_header(self):
        convs = [self._conv("a", 10), self._conv("c", 120, favorite=True)]
        self.list.set_conversations(convs)
        header = self.list.itemWidget(self.list.item(0))
        header.toggled.emit()
        # 折叠后：头行仍在最顶上（▸），收藏行消失，落盘保存
        self.assertEqual(self._header_rows(), [0])
        self.assertIn("▸", self.list.itemWidget(
            self.list.item(0)).label.text())
        self.assertEqual(self._order(), ["a"])
        self.save_prefs.assert_called_once_with(collapsed_favorites=True)

    def test_expand_again_restores_rows(self):
        convs = [self._conv("a", 10), self._conv("c", 120, favorite=True)]
        self.list.set_conversations(convs)
        self.list.itemWidget(self.list.item(0)).toggled.emit()
        self.list.itemWidget(self.list.item(0)).toggled.emit()
        self.assertEqual(self._order(), ["c", "a"])

    def test_upsert_while_collapsed_keeps_group_hidden(self):
        convs = [self._conv("a", 10), self._conv("c", 120, favorite=True)]
        self.list.set_conversations(convs)
        self.list.itemWidget(self.list.item(0)).toggled.emit()
        # 折叠态下普通会话来新消息：只刷行，收藏不冒出来
        convs[0].last_message_time = self.t_base + 5
        self.list.upsert_conversation(convs[0])
        self.assertEqual(self._order(), ["a"])
        self.assertEqual(self._header_rows(), [0])

    def test_lazy_fill_with_header_cursor(self):
        """头行不占数据游标：1 收藏 + 25 普通，补建完整后行数 = 数据行 + 头行。"""
        convs = [self._conv(f"r{i}", i) for i in range(25)]
        convs.append(self._conv("fav", 3, favorite=True))
        self.list.set_conversations(convs)
        while not self.list._fill_all:
            self.list._fill_more()
        self.assertEqual(self.list._built_rows, 26)
        self.assertEqual(self.list.count(), 27)
        # 最后一条是最旧普通行；最后一条数据行不带头行标记
        self.assertEqual(self.list.item(26).data(Qt.UserRole), "r24")
        # 头行在最前
        self.assertEqual(self._header_rows(), [0])

    def test_collapsed_start_hides_favorites_from_top(self):
        """启动即折叠：头行在最顶，收藏行不出现。"""
        self.list._fav_collapsed = True
        self.list.set_conversations([
            self._conv("a", 10), self._conv("c", 120, favorite=True)])
        self.assertEqual(self._header_rows(), [0])
        self.assertEqual(self._order(), ["a"])


class TestRowHeight(unittest.TestCase):
    """带换行的预览（如代码块）不得把行撑高留出一截空白。"""

    def setUp(self):
        self.list = ConversationList()

    def test_multiline_preview_single_line_and_fixed_row_height(self):
        conv = Conversation(
            id="r1", name="张三", room_type="c",
            last_message_time=1_700_000_000.0,
            last_message_text="```\nthis is a test!\n```",
        )
        self.list.set_conversations([conv])
        item = self.list.item(0)
        widget = self.list.itemWidget(item)
        self.assertEqual(widget.preview_label.text(), "``` this is a test! ```")
        self.assertEqual(item.sizeHint().height(), widget.minimumHeight())


class TestLazyRender(unittest.TestCase):
    """启动/重排只建首屏一页，滚动到底部再增量补建。"""

    def setUp(self):
        self.list = ConversationList()
        self.t_base = 1_700_000_000.0

    def _conv(self, rid, minutes_ago):
        return Conversation(
            id=rid, name=rid, room_type="c",
            last_message_time=self.t_base - minutes_ago * 60,
        )

    def test_below_page_renders_all(self):
        convs = [self._conv(f"r{i}", i) for i in range(10)]
        self.list.set_conversations(convs)
        self.assertEqual(self.list.count(), 10)
        self.assertTrue(self.list._fill_all)

    def test_over_page_renders_first_page_only(self):
        convs = [self._conv(f"r{i}", i) for i in range(120)]
        self.list.set_conversations(convs)
        self.assertEqual(self.list.count(), self.list._page_size)
        self.assertFalse(self.list._fill_all)
        # 首屏是最后消息最新的一页（时间倒序，minute_ago 越大越旧）
        self.assertEqual(self.list.item(0).data(Qt.UserRole), "r0")

    def test_fill_more_appends_remaining_in_order(self):
        convs = [self._conv(f"r{i}", i) for i in range(120)]
        self.list.set_conversations(convs)
        # 页大小可调（20/50），按实际页数补建，不写死调用次数
        while not self.list._fill_all:
            self.list._fill_more()
        self.assertTrue(self.list._fill_all)
        self.assertEqual(self.list.count(), 120)
        self.assertEqual(self.list.item(119).data(Qt.UserRole), "r119")

    def test_fill_all_noop(self):
        convs = [self._conv(f"r{i}", i) for i in range(30)]
        self.list.set_conversations(convs)
        self.list._fill_more()
        self.assertEqual(self.list.count(), 30)


if __name__ == "__main__":
    unittest.main()
