"""Emoji：Rocket.Chat 短码/肤色解析、回应显示、选择器中英文搜索。

offscreen；不联网。
"""
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QEvent, QPoint, Qt  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import (  # noqa: E402
    QApplication, QDesktopWidget, QLabel, QPushButton,
)

from core.messages import emojize, rc_shortcode, reaction_display  # noqa: E402
from ui.emoji_picker import (  # noqa: E402
    EmojiPicker, emoji_tooltip, search_emojis,
)

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class ToneShortcodeTest(unittest.TestCase):
    """Rocket.Chat/emojione 的 _toneN 方言（真机 :ok_hand_tone5: 显示成原文）。"""

    def test_ok_hand_tone5(self):
        self.assertEqual(reaction_display(":ok_hand_tone5:"), "👌🏿")

    def test_all_tone_numbers(self):
        expected = {
            1: "👍🏻", 2: "👍🏼", 3: "👍🏽", 4: "👍🏾", 5: "👍🏿",
        }
        for tone, char in expected.items():
            self.assertEqual(
                reaction_display(f":thumbsup_tone{tone}:"), char, tone)

    def test_zwj_tone_uses_full_lookup(self):
        # 多人/ZWJ 组合的肤色不能靠基字符末尾拼修饰符，必须整名查表
        self.assertEqual(reaction_display(":woman_astronaut_tone5:"),
                         "👩🏿\u200d🚀")

    def test_tone_on_unsupported_emoji_kept(self):
        # 火箭没有肤色版本：保留原文，不能拼出 emoji+色块
        self.assertEqual(reaction_display(":rocket_tone5:"),
                         ":rocket_tone5:")

    def test_unknown_shortcode_kept(self):
        self.assertEqual(reaction_display(":not_an_emoji_xyz:"),
                         ":not_an_emoji_xyz:")

    def test_plain_shortcode_still_works(self):
        self.assertEqual(reaction_display(":thumbsup:"), "👍")
        self.assertEqual(reaction_display(":heart:"), "❤️")

    def test_unicode_passthrough(self):
        self.assertEqual(reaction_display("👌🏿"), "👌🏿")

    def test_time_text_not_interpreted(self):
        self.assertEqual(emojize("时间 12:30:45"), "时间 12:30:45")

    def test_mixed_text(self):
        self.assertEqual(emojize("收到:ok_hand_tone5: 谢谢:thumbsup:"),
                         "收到👌🏿 谢谢👍")


class RcShortcodeTest(unittest.TestCase):
    """发送 chat.react 前 Unicode -> Rocket.Chat 短码。"""

    def test_base_emoji(self):
        self.assertEqual(rc_shortcode("👍"), ":thumbsup:")
        self.assertEqual(rc_shortcode("👌"), ":ok_hand:")

    def test_variant_emoji(self):
        self.assertEqual(rc_shortcode("❤️"), ":heart:")

    def test_skin_tone(self):
        self.assertEqual(rc_shortcode("👌🏿"), ":ok_hand_tone5:")
        self.assertEqual(rc_shortcode("👍🏻"), ":thumbsup_tone1:")
        self.assertEqual(rc_shortcode("✌🏽"), ":v_tone3:")

    def test_already_shortcode(self):
        self.assertEqual(rc_shortcode(":thumbsup:"), ":thumbsup:")

    def test_roundtrip_with_display(self):
        for char in ("👍", "👌🏿", "❤️", "🎉", "🙏", "✌🏽", "👩🏿\u200d🚀"):
            self.assertEqual(reaction_display(rc_shortcode(char)), char)


class EmojiSearchTest(unittest.TestCase):

    def test_chinese_keyword(self):
        self.assertIn("👍", search_emojis("点赞"))
        self.assertIn("🐱", search_emojis("猫"))

    def test_english_name(self):
        self.assertIn("👍", search_emojis("thumbs"))
        self.assertIn("🐶", search_emojis("dog"))

    def test_shortcode_query(self):
        self.assertIn("👌", search_emojis(":ok_hand:"))

    def test_category_keyword_matches_whole_category(self):
        results = search_emojis("动物")
        for char in ("🐱", "🐶", "🐼"):
            self.assertIn(char, results)

    def test_no_match(self):
        self.assertEqual(search_emojis("zzzznotemoji"), [])

    def test_blank_query(self):
        self.assertEqual(search_emojis("   "), [])

    def test_result_limit(self):
        self.assertLessEqual(len(search_emojis("a")), 120)


class EmojiPickerGridTest(unittest.TestCase):

    def setUp(self):
        self.picker = EmojiPicker(close_on_select=False)
        self.addCleanup(self.picker.deleteLater)

    def _cell_texts(self):
        out = []
        for i in range(self.picker._grid.count()):
            w = self.picker._grid.itemAt(i).widget()
            if isinstance(w, QPushButton) and w.objectName() == "EmojiCell":
                out.append(w.text())
        return out

    def _labels(self):
        out = []
        for i in range(self.picker._grid.count()):
            w = self.picker._grid.itemAt(i).widget()
            if isinstance(w, QLabel):
                out.append(w.text())
        return out

    def test_chinese_search_shows_flat_results(self):
        self.picker.search.setText("点赞")
        self.assertIn("👍", self._cell_texts())
        self.assertEqual(self._labels(), [])

    def test_english_search(self):
        self.picker.search.setText("thumbs")
        self.assertIn("👍", self._cell_texts())

    def test_clear_restores_categories(self):
        self.picker.search.setText("点赞")
        self.picker.search.setText("")
        self.assertTrue(any("笑脸" in t for t in self._labels()))
        self.assertIn("😀", self._cell_texts())

    def test_no_match_shows_hint(self):
        self.picker.search.setText("zzzznotemoji")
        self.assertEqual(self._cell_texts(), [])
        self.assertTrue(any("没有找到" in t for t in self._labels()))

    def test_tooltip_has_english_name_and_chinese(self):
        tip = emoji_tooltip("👍")
        self.assertIn("thumbs", tip)
        self.assertIn("赞", tip)

    def test_click_emits_char(self):
        got = []
        self.picker.emoji_selected.connect(got.append)
        self.picker.search.setText("点赞")
        for i in range(self.picker._grid.count()):
            w = self.picker._grid.itemAt(i).widget()
            if isinstance(w, QPushButton) and w.text() == "👍":
                w.click()
                break
        self.assertEqual(got, ["👍"])

    # ---------------- 布局/窗口回归 ---------------- #
    def _first_cell(self):
        for i in range(self.picker._grid.count()):
            w = self.picker._grid.itemAt(i).widget()
            if isinstance(w, QPushButton) and w.objectName() == "EmojiCell":
                return w
        return None

    def _cell_buttons(self):
        out = []
        for i in range(self.picker._grid.count()):
            w = self.picker._grid.itemAt(i).widget()
            if isinstance(w, QPushButton) and w.objectName() == "EmojiCell":
                out.append(w)
        return out

    def test_window_uses_tool_not_popup(self):
        # Popup 窗口在 Windows 上 IME 无法合成中文，必须是无边框 Tool 窗；
        # Tool 的类型位本身含 Popup 位，只能按 WindowType_Mask 判类型
        flags = self.picker.windowFlags()
        self.assertEqual(flags & Qt.WindowType_Mask, Qt.Tool)
        self.assertNotEqual(flags & Qt.WindowType_Mask, Qt.Popup)
        self.assertTrue(flags & Qt.FramelessWindowHint)

    def test_search_results_start_at_top_left(self):
        # 回归：清空网格后 QGridLayout 的 31×8 行列残留会把 2 个搜索结果
        # 摆到面板中间（84/199px），看起来像「排版错乱」
        self.picker._grid.activate()
        self.picker.search.setText("laugh")
        self.picker._grid.activate()
        cells = self._cell_buttons()
        self.assertGreaterEqual(len(cells), 1)
        first = cells[0].geometry()
        self.assertLessEqual(first.left(), 12)
        self.assertLessEqual(first.top(), 12)
        if len(cells) > 1:
            self.assertLessEqual(cells[1].geometry().left(),
                                 first.left() + first.width() + 8)

    def test_search_hides_category_bar(self):
        self.assertTrue(self.picker._cat_bar.isVisibleTo(self.picker))
        self.picker.search.setText("laugh")
        self.assertFalse(self.picker._cat_bar.isVisibleTo(self.picker))
        self.picker.search.setText("")
        self.assertTrue(self.picker._cat_bar.isVisibleTo(self.picker))

    def test_hover_uses_footer_hint_not_native_tooltip(self):
        self.picker.search.setText("点赞")
        btn = self._first_cell()
        self.assertEqual(btn.toolTip(), "")
        QApplication.sendEvent(btn, QEvent(QEvent.Enter))
        self.assertIn("赞", self.picker.hint.text())
        self.assertIn("thumbs", self.picker.hint.text().lower())
        QApplication.sendEvent(btn, QEvent(QEvent.Leave))
        self.assertIn("悬停", self.picker.hint.text())

    def test_category_buttons_keep_color_emoji_font(self):
        # 分类按钮漏配 emoji 字体族时 ❤ 会退化成黑色单色字形
        qss = self.picker.styleSheet()
        start = qss.index("QPushButton#EmojiCatBtn{")
        block = qss[start:qss.index("}", start)]
        self.assertIn("Segoe UI Emoji", block)

    def test_escape_closes_picker(self):
        self.picker.show()
        QApplication.processEvents()
        self.picker.search.setFocus()
        QTest.keyClick(self.picker.search, Qt.Key_Escape)
        self.assertFalse(self.picker.isVisible())

    def test_recently_closed_grace(self):
        self.assertFalse(self.picker.was_recently_closed())
        self.picker.show()
        QApplication.processEvents()
        self.picker.close()
        self.assertTrue(self.picker.was_recently_closed())

    def test_open_at_clamps_into_screen(self):
        self.picker.open_at(QPoint(-500, -500))
        self.assertGreaterEqual(self.picker.pos().x(), 0)
        self.assertGreaterEqual(self.picker.pos().y(), 0)
        self.picker.close()

    def test_reaction_picker_never_overflows_screen(self):
        # 回应面板原先只 max(0,…)，靠右/靠下的消息会把面板弹出屏幕
        screen = QDesktopWidget().availableGeometry(self.picker)
        self.picker.open_at(QPoint(screen.right() + 500,
                                   screen.bottom() + 500))
        geo = self.picker.geometry()
        self.assertLessEqual(geo.right(), screen.right())
        self.assertLessEqual(geo.bottom(), screen.bottom())
        self.picker.close()


if __name__ == "__main__":
    unittest.main()
