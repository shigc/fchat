"""矢量菜单图标验证：每个注册的图标都要真的画出墨迹，且跟随主题色。

项目不引外部图标素材，全部 QPainter 现画，所以"有没有画出来"和
"颜色对不对"只能靠像素检查——光看不报错不代表画对了。

运行：python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtGui import QColor, QImage  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from ui import icons  # noqa: E402
from ui.theme import _DARK, _LIGHT  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _ink(pixmap):
    """返回 (不透明像素数, 第一个不透明像素的颜色)。"""
    img = pixmap.toImage().convertToFormat(QImage.Format_ARGB32)
    on = 0
    first = None
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixel(x, y)
            if ((c >> 24) & 0xFF) > 90:
                on += 1
                if first is None:
                    first = QColor(c).name()
    return on, first


class TestIcons(unittest.TestCase):

    def tearDown(self):
        icons.set_theme_colors(_LIGHT["text_primary"])

    def test_registry_names_unique(self):
        self.assertTrue(icons._DRAWERS, "图标注册表为空")
        self.assertEqual(len(set(icons._DRAWERS)), len(icons._DRAWERS))

    def test_every_icon_renders_visible_ink(self):
        icons.set_theme_colors(_LIGHT["text_primary"])
        for name in icons._DRAWERS:
            pm = icons.icon(name, 16).pixmap(16, 16)
            self.assertFalse(pm.isNull(), f"{name} 渲染出空图")
            on, _c = _ink(pm)
            self.assertGreaterEqual(on, 15, f"{name} 几乎没画出来（ink={on}）")
            self.assertLessEqual(on, 200, f"{name} 画糊了，覆盖了整格（ink={on}）")

    def test_unknown_name_gives_empty_icon(self):
        self.assertTrue(icons.icon("no_such_icon").isNull())

    def test_theme_color_propagates(self):
        """暗色主题下描边要跟着反白，否则黑底黑线等于看不见。"""
        icons.set_theme_colors(_LIGHT["text_primary"])
        light_ink, light_color = _ink(icons.icon("copy", 16).pixmap(16, 16))
        icons.set_theme_colors(_DARK["text_primary"])
        dark_ink, dark_color = _ink(icons.icon("copy", 16).pixmap(16, 16))

        self.assertEqual(light_ink, dark_ink, "换色不该改变形状")
        self.assertNotEqual(light_color, dark_color)
        self.assertTrue(QColor(dark_color).lightness() > 128,
                        f"暗色主题图标应当是浅色，实际 {dark_color}")

    def test_accepts_hex_string_and_qcolor(self):
        icons.set_theme_colors("#123456")
        _n, c1 = _ink(icons.icon("quote", 16).pixmap(16, 16))
        icons.set_theme_colors(QColor("#123456"))
        _n, c2 = _ink(icons.icon("quote", 16).pixmap(16, 16))
        self.assertEqual(c1, c2)

    def test_theme_manager_apply_syncs_icons(self):
        """ThemeManager.apply 必须把颜色传给图标模块，否则切主题图标不变色。"""
        from ui.theme import ThemeManager
        tm = ThemeManager()
        tm.theme = "dark"
        tm.apply(_APP)
        _n, c = _ink(icons.icon("pin", 16).pixmap(16, 16))
        self.assertTrue(QColor(c).lightness() > 128,
                        f"apply(dark) 后图标仍是深色：{c}")

    def test_stroke_override_renders_bolder(self):
        """stroke 覆盖要真加粗：细线在并排文字的场景显得发灰（设置页导航）。"""
        icons.set_theme_colors(_LIGHT["text_primary"])
        thin = icons.icon("pin", 16).pixmap(64, 64).toImage()
        bold = icons.icon("pin", 16, stroke=2.6).pixmap(64, 64).toImage()

        def ink(img):
            return sum(img.pixelColor(x, y).alpha()
                       for y in range(img.height())
                       for x in range(img.width()))

        self.assertGreater(ink(bold), ink(thin) * 1.5,
                           "加粗描边后的墨量应显著超过默认 1.4")

    def test_stroke_cache_key_separated(self):
        icons.set_theme_colors(_LIGHT["text_primary"])
        a = icons.icon("copy", 16, stroke=1.9)
        b = icons.icon("copy", 16, stroke=1.9)
        c = icons.icon("copy", 16)
        self.assertIs(a, b)
        self.assertIsNot(a, c)


class TestWindowIcons(unittest.TestCase):
    """主窗口三按钮图标：位置语义（最小化线在下半、方框满格、还原双框）。"""

    def setUp(self):
        icons.set_theme_colors(_LIGHT["text_primary"])

    def tearDown(self):
        icons.set_theme_colors(_LIGHT["text_primary"])

    def _points(self, name):
        img = icons.icon(name, 16).pixmap(16, 16).toImage() \
            .convertToFormat(QImage.Format_ARGB32)
        pts = []
        for y in range(img.height()):
            for x in range(img.width()):
                if ((img.pixel(x, y) >> 24) & 0xFF) > 90:
                    pts.append((x, y))
        return pts

    def test_registered(self):
        for name in ("min", "max", "restore"):
            self.assertIn(name, icons._DRAWERS)
            self.assertTrue(self._points(name), f"{name} 没画出墨迹")

    def test_min_line_in_bottom_half(self):
        pts = self._points("min")
        self.assertTrue(all(y >= 8 for _x, y in pts), "最小化线应画在下半格")

    def test_max_box_fills_grid(self):
        pts = self._points("max")
        xs = [x for x, _y in pts]
        ys = [y for _x, y in pts]
        self.assertLess(min(xs), 4, "方框应贴左缘")
        self.assertGreater(max(xs), 11, "方框应贴右缘")
        self.assertLess(min(ys), 4, "方框应贴上缘")
        self.assertGreater(max(ys), 11, "方框应贴下缘")

    def test_restore_has_two_overlapping_frames(self):
        pts = self._points("restore")
        top_right = any(x >= 10 and y < 5 for x, y in pts)
        bottom_left = any(x < 5 and y >= 10 for x, y in pts)
        self.assertTrue(top_right, "还原图标缺右上背框")
        self.assertTrue(bottom_left, "还原图标缺左下前框")


class TestNavIcons(unittest.TestCase):
    """侧栏导航图标（chats/settings）与 icon() 的显式 color 渲染。"""

    def setUp(self):
        icons.set_theme_colors(_LIGHT["text_primary"])

    def tearDown(self):
        icons.set_theme_colors(_LIGHT["text_primary"])

    def test_registered_with_ink(self):
        for name in ("chats", "settings"):
            self.assertIn(name, icons._DRAWERS)
            pm = icons.icon(name, 22).pixmap(22, 22)
            on, _c = _ink(pm)
            self.assertGreaterEqual(on, 15, f"{name} 墨迹不足（ink={on}）")

    def test_chats_bubble_with_tail_and_dots(self):
        img = icons.icon("chats", 16).pixmap(16, 16).toImage() \
            .convertToFormat(QImage.Format_ARGB32)
        pts = [(x, y) for y in range(img.height())
               for x in range(img.width())
               if ((img.pixel(x, y) >> 24) & 0xFF) > 90]
        self.assertTrue(any(y >= 12 for _x, y in pts), "气泡缺左下尾巴")
        self.assertTrue(any(y <= 4 for _x, y in pts), "气泡体未贴上缘")

    def test_settings_is_gear(self):
        # 与设置页「通用」齿轮同形：直接复用同一 drawer
        self.assertIs(icons._DRAWERS["settings"], icons._DRAWERS["general"])

    def test_explicit_color_overrides_without_touching_theme(self):
        _n, c = _ink(icons.icon("copy", 16, color="#ff0000").pixmap(16, 16))
        self.assertEqual(c, "#ff0000")
        # 全局主题色不能被显式色带跑（首个不透明像素可能带抗锯齿混合，
        # 只断言仍是深色即可）
        _n2, c2 = _ink(icons.icon("pin", 16).pixmap(16, 16))
        self.assertLess(QColor(c2).lightness(), 128,
                        f"全局主题色被显式色污染，实际 {c2}")


if __name__ == "__main__":
    unittest.main()
