"""MessageDelegate 主题取色测试：色板注入与回退。

GUI 相关测试统一 offscreen。
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest import mock  # noqa: E402

from PyQt5.QtCore import QObject, pyqtSignal  # noqa: E402
from PyQt5.QtGui import QPalette, QPainter, QPixmap, QTextDocument  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from ui.message_model import MessageDelegate  # noqa: E402


class _StubModel(QObject):
    """最小模型桩：仅提供 set_palette 需要的 layoutChanged 信号。"""
    layoutChanged = pyqtSignal()


class TestDelegateTheme(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_no_palette_uses_fallback_color(self):
        d = MessageDelegate(_StubModel())
        self.assertIsNone(d._palette)
        # 回退色应等于默认浅色
        self.assertEqual(d._col("text_primary", "#191919").name(), "#191919")

    def test_palette_color_used_when_set(self):
        d = MessageDelegate(_StubModel())
        d.set_palette({"text_primary": "#e0e0e0"})
        self.assertEqual(d._col("text_primary", "#191919").name(), "#e0e0e0")

    def test_palette_missing_key_falls_back(self):
        d = MessageDelegate(_StubModel())
        d.set_palette({"text_primary": "#e0e0e0"})
        # 色板缺键时回退到默认
        self.assertEqual(d._col("bubble_mine", "#95ec69").name(), "#95ec69")

    def test_theme_manager_palette_follows_theme(self):
        from ui.theme import ThemeManager, _LIGHT, _DARK
        tm = ThemeManager()
        tm.theme = "light"
        self.assertIs(tm.palette, _LIGHT)
        tm.theme = "dark"
        self.assertIs(tm.palette, _DARK)
        self.assertEqual(tm.palette["text_primary"], _DARK["text_primary"])

    def test_interaction_keys_in_both_palettes(self):
        from ui.theme import _LIGHT, _DARK
        for palette in (_LIGHT, _DARK):
            for key in ("reaction_bg", "reaction_border", "reaction_mine_bg",
                        "reaction_mine_text", "mention_highlight"):
                self.assertIn(key, palette)

    def test_reaction_and_mention_fallbacks(self):
        d = MessageDelegate(_StubModel())
        self.assertTrue(d._col("reaction_bg", "#f0f2f5").isValid())
        self.assertTrue(d._col("reaction_mine_text", "#1256b8").isValid())
        self.assertTrue(d._col("mention_highlight", "#fff3d6").isValid())
        d.set_palette({"reaction_bg": "#222222"})
        self.assertEqual(d._col("reaction_bg", "#f0f2f5").name(), "#222222")
        # 色板缺新键时仍回退到内置浅色
        self.assertEqual(d._col("mention_highlight", "#fff3d6").name(),
                         "#fff3d6")


class _StubTheme:
    """最小主题桩：提供 _c 所需的 color(key, default)。"""

    def __init__(self, colors):
        self._colors = colors

    def color(self, key, default):
        return self._colors.get(key, default)


class _DocDrawTest(unittest.TestCase):
    """文档绘制必须带 PaintContext.palette：doc.drawContents 用全局调色板
    （黑字），暗色主题下气泡会画出深底黑字（对比度不足的真机反馈）。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _make_doc(self):
        doc = QTextDocument()
        doc.setPlainText("hello")
        layout = doc.documentLayout()
        # 布局的 Python 包装必须保活：fake_draw 挂在包装的实例属性上，
        # 包装被 GC 后 documentLayout() 会给出全新包装、属性丢失
        doc._layout_ref = layout
        captured = {}

        def fake_draw(painter, ctx):
            captured["text"] = ctx.palette.color(QPalette.Text).name()
            captured["selections"] = list(ctx.selections)

        layout.draw = fake_draw
        return doc, captured

    def _painter(self):
        # pixmap 要随 painter 一起保活：先于 end() 被 GC 会触发
        # 「Cannot destroy paint device that is being painted」段错误
        pm = QPixmap(60, 20)
        pm.fill()
        p = QPainter(pm)
        p._device = pm
        return p

    def test_draw_doc_plain_uses_theme_text_color(self):
        d = MessageDelegate(_StubModel())
        d.set_theme(_StubTheme({"text_primary": "#e0e0e0"}))
        doc, captured = self._make_doc()
        p = self._painter()
        try:
            d._draw_doc_plain(p, doc)
        finally:
            p.end()
        self.assertEqual(captured["text"], "#e0e0e0")

    def test_draw_doc_plain_secondary_key(self):
        d = MessageDelegate(_StubModel())
        d.set_theme(_StubTheme({"text_tertiary": "#aaaaaa"}))
        doc, captured = self._make_doc()
        p = self._painter()
        try:
            d._draw_doc_plain(p, doc, "text_tertiary", "#666666")
        finally:
            p.end()
        self.assertEqual(captured["text"], "#aaaaaa")

    def test_draw_doc_with_selection_sets_text_and_selection(self):
        d = MessageDelegate(_StubModel())
        d.set_theme(_StubTheme({"text_primary": "#e0e0e0"}))
        m = d.model_ref
        m.sel_msg_id = "m1"
        m.sel_start = 0
        m.sel_end = 2
        m.sel_merge_idx = -1
        msg = type("M", (), {"id": "m1"})()
        doc, captured = self._make_doc()
        p = self._painter()
        try:
            d._draw_doc_with_selection(p, doc, msg)
        finally:
            p.end()
        self.assertEqual(captured["text"], "#e0e0e0")
        self.assertEqual(len(captured["selections"]), 1)

    def test_draw_doc_with_selection_no_selection_still_theme_color(self):
        d = MessageDelegate(_StubModel())
        d.set_theme(_StubTheme({"text_primary": "#e0e0e0"}))
        m = d.model_ref
        m.sel_msg_id = None
        m.sel_start = 0
        m.sel_end = 0
        m.sel_merge_idx = -1
        msg = type("M", (), {"id": "m1"})()
        doc, captured = self._make_doc()
        p = self._painter()
        try:
            d._draw_doc_with_selection(p, doc, msg)
        finally:
            p.end()
        self.assertEqual(captured["text"], "#e0e0e0")
        self.assertEqual(captured["selections"], [])


if __name__ == "__main__":
    unittest.main()
