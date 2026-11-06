"""聊天背景预设渲染单测（离线、不依赖显示器）。

覆盖预设注册表完整性、各类型渲染出的像素特征、亮/暗变体、
DPR 尺寸、无效值回落以及自定义图片的 cover+罩层。
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QSize  # noqa: E402
from PyQt5.QtGui import QColor, QPixmap  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from ui.chat_background import (  # noqa: E402
    PRESETS, PRESET_BY_ID, preset_value, render_background, theme_mode,
)

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class RegistryTest(unittest.TestCase):

    def test_kind_counts(self):
        kinds = [p.kind for p in PRESETS]
        self.assertEqual(kinds.count("solid"), 6)
        self.assertEqual(kinds.count("gradient"), 5)
        self.assertEqual(
            sum(kinds.count(k) for k in ("dots", "grid", "stripes", "speckle")),
            4)
        self.assertEqual(len(PRESETS), 15)

    def test_ids_unique_and_indexed(self):
        ids = [p.id for p in PRESETS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(PRESET_BY_ID), set(ids))

    def test_preset_value_roundtrip(self):
        self.assertEqual(preset_value("solid_mist"), "preset:solid_mist")

    def test_theme_mode(self):
        class _Dark:
            theme = "dark"

        self.assertEqual(theme_mode("dark"), "dark")
        self.assertEqual(theme_mode("light"), "light")
        self.assertEqual(theme_mode(_Dark()), "dark")
        self.assertEqual(theme_mode(None), "light")


class RenderTest(unittest.TestCase):

    def test_solid_exact_color(self):
        pm = render_background("preset:solid_mist", 20, 12, "light")
        self.assertIsNotNone(pm)
        self.assertEqual(pm.size(), QSize(20, 12))
        self.assertEqual(pm.toImage().pixelColor(10, 6), QColor("#F5F6F7"))

    def test_all_presets_light_dark_differ(self):
        for preset in PRESETS:
            light = render_background(
                preset_value(preset.id), 30, 20, "light")
            dark = render_background(
                preset_value(preset.id), 30, 20, "dark")
            self.assertIsNotNone(light, preset.id)
            self.assertIsNotNone(dark, preset.id)
            self.assertNotEqual(light.toImage(), dark.toImage(), preset.id)

    def test_gradient_corners_differ(self):
        pm = render_background("preset:grad_dawn", 40, 24, "light")
        img = pm.toImage()
        self.assertEqual(img.pixelColor(0, 0), QColor("#E9F1F5"))
        self.assertEqual(img.pixelColor(39, 23), QColor("#F7F2EC"))

    def test_pattern_presets_have_texture(self):
        for preset in PRESETS:
            if preset.kind in ("solid", "gradient"):
                continue
            pm = render_background(
                preset_value(preset.id), 120, 80, "light")
            img = pm.toImage()
            colors = {img.pixelColor(x, y).rgb()
                      for x in range(img.width())
                      for y in range(img.height())}
            self.assertGreater(len(colors), 1, preset.id)

    def test_speckle_is_deterministic(self):
        a = render_background("preset:pat_speckle", 80, 50, "light")
        b = render_background("preset:pat_speckle", 80, 50, "light")
        self.assertEqual(a.toImage(), b.toImage())

    def test_dpr_scales_physical_size(self):
        pm = render_background("preset:solid_blue", 100, 50, "light", 2.0)
        self.assertEqual(pm.width(), 200)
        self.assertEqual(pm.height(), 100)
        self.assertAlmostEqual(pm.devicePixelRatio(), 2.0)

    def test_default_and_unknown_return_none(self):
        self.assertIsNone(render_background("", 10, 10))
        self.assertIsNone(render_background("preset:", 10, 10))
        self.assertIsNone(render_background("preset:nope", 10, 10))
        missing = os.path.join(tempfile.gettempdir(), "no-such-bg-xyz.png")
        self.assertIsNone(render_background(missing, 10, 10))

    def test_custom_image_cover_with_overlay(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "green.png")
        src = QPixmap(10, 10)
        src.fill(QColor(0, 255, 0))
        self.assertTrue(src.save(path))
        pm = render_background(path, 40, 20, "light")
        self.assertIsNotNone(pm)
        self.assertEqual(pm.size(), QSize(40, 20))
        color = pm.toImage().pixelColor(20, 10)
        # 盖了 55 alpha 黑罩：绿色通道被压暗但远未全黑
        self.assertLess(color.green(), 240)
        self.assertGreater(color.green(), 150)

    def test_unreadable_image_returns_none(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "bad.png")
        with open(path, "wb") as f:
            f.write(b"not an image")
        self.assertIsNone(render_background(path, 20, 10))


class _Theme:
    def __init__(self, mode):
        self.theme = mode

    def color(self, key, default=""):
        return "#f5f5f5" if key == "chat_bg" and \
            self.theme == "light" else default


class ChatViewBackgroundTest(unittest.TestCase):
    """背景必须落在视口 palette 上：delegate 逐行画会盖住其它消息行。"""

    def _view(self):
        from ui.chat_view import ChatView
        view = ChatView()
        view.resize(200, 150)
        view.set_theme(_Theme("light"))
        return view

    def test_preset_brush_on_viewport(self):
        from PyQt5.QtGui import QPalette
        view = self._view()
        view.set_background("preset:pat_grid")
        brush = view.viewport().palette().brush(QPalette.Base)
        self.assertIsNotNone(brush.texture())
        self.assertFalse(brush.texture().isNull())
        self.assertTrue(view.viewport().autoFillBackground())
        self.assertFalse(hasattr(view._delegate, "_wallpaper"))
        view.deleteLater()

    def test_clear_restores_solid_chat_bg(self):
        from PyQt5.QtGui import QPalette
        view = self._view()
        view.set_background("preset:solid_blue")
        view.set_background("")
        brush = view.viewport().palette().brush(QPalette.Base)
        self.assertEqual(brush.color(), QColor("#f5f5f5"))
        self.assertIsNone(view._bg_pixmap)
        view.deleteLater()


class ScrollRepaintTest(unittest.TestCase):
    """设背景图后滚动必须整视口重绘。

    Qt 滚动是 blit：已画像素整体平移、只有新露出的条带按刷子原点
    重画，图片背景会上下错位割裂。修复 = scrollContentsBy 强制
    viewport().update()；用 Paint 事件矩形验证重绘范围。
    """

    def _view(self, bg_path):
        from core.models import Message, User
        from ui.chat_view import ChatView
        view = ChatView()
        # show() 的窗口若不清 close，残留窗口会在后续 setStyleSheet 重算样式时
        # 访问已释放的 Python 包装（与其他测试组合 segfault）；close 触发
        # ChatView.closeEvent 断开持久信号连接破除引用环，deleteLater 才能回收
        def _cleanup():
            view.close()
            view.deleteLater()
            QApplication.processEvents()
        self.addCleanup(_cleanup)
        view.resize(300, 220)
        view.set_theme(_Theme("light"))
        view.set_background(bg_path or "")
        sender = User(id="u1", username="a", name="A")
        for i in range(30):
            view.model.append_message(Message(
                id=f"m{i}", room_id="r", sender=sender,
                msg_type="text", text=f"消息 {i}"))
        view.show()
        view.scroll_to_bottom()
        QApplication.processEvents()
        return view

    def _scroll_paint_rects(self, view):
        from PyQt5.QtCore import QEvent, QRect, QObject

        rects = []

        class _F(QObject):
            def eventFilter(self, obj, ev):
                if ev.type() == QEvent.Paint:
                    rects.append(QRect(ev.rect()))
                return False

        flt = _F(view)
        view.viewport().installEventFilter(flt)
        bar = view.verticalScrollBar()
        bar.setValue(max(0, bar.value() - 40))
        QApplication.processEvents()
        view.viewport().removeEventFilter(flt)
        return rects

    def _bg_image(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "bg.png")
        pm = QPixmap(8, 8)
        pm.fill(QColor(0, 255, 0))
        self.assertTrue(pm.save(path))
        return path

    def test_scroll_with_image_repaints_full_viewport(self):
        view = self._view(self._bg_image())
        vp = view.viewport()
        rects = self._scroll_paint_rects(view)
        self.assertTrue(
            any(r.width() >= vp.width() and r.height() >= vp.height()
                for r in rects),
            [str(r) for r in rects])

    def test_scroll_without_bg_keeps_blit(self):
        view = self._view(None)
        vp = view.viewport()
        rects = self._scroll_paint_rects(view)
        self.assertFalse(
            any(r.height() >= vp.height() for r in rects),
            [str(r) for r in rects])


if __name__ == "__main__":
    unittest.main()
