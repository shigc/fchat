"""服务端默认头像识别与本地汉字占位回落。

服务端对未上传头像的用户返回 80x80 但内容只画在左上角约 40x40、
其余全透明的默认字母图，显示出来是"灰圆+左上角小方块"。
锁定：这种图必须被识别为无头像（_is_default_avatar），从而回落到
本地以显示名首字绘制的方形圆角占位，且首字居中。

运行：python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtGui import QColor, QPainter, QPixmap  # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget, QHBoxLayout  # noqa: E402

from ui.avatar_loader import _is_default_avatar  # noqa: E402
from ui.widgets import AvatarWidget  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _pixmap_with_top_left_content():
    """模拟服务端默认字母图：80x80，左上角 40x40 不透明。"""
    pm = QPixmap(80, 80)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.fillRect(0, 0, 40, 40, QColor("#986EE5"))
    p.end()
    return pm


def _photo_pixmap():
    """模拟真实上传头像：满幅不透明。"""
    pm = QPixmap(80, 80)
    pm.fill(QColor("steelblue"))
    return pm


def _photo_pixmap_with_transparent_regions():
    """模拟真实头像（2026-09-20 实测形态）：200x200，不透明像素只占
    0.644，但均匀铺满四个象限。全局占比阈值（<0.7）会把它误判成
    默认字母图，导致用户换了新头像仍显示占位。"""
    pm = QPixmap(200, 200)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    for y in range(0, 200, 4):
        for x in range(0, 200, 4):
            if (x // 4 + y // 4) % 3 != 0:
                p.fillRect(x, y, 4, 4, QColor("steelblue"))
    p.end()
    return pm


class TestDefaultAvatarDetection(unittest.TestCase):
    def test_top_left_partial_is_default(self):
        self.assertTrue(_is_default_avatar(_pixmap_with_top_left_content()))

    def test_full_opaque_is_real(self):
        self.assertFalse(_is_default_avatar(_photo_pixmap()))

    def test_photo_with_transparent_regions_is_real(self):
        self.assertFalse(
            _is_default_avatar(_photo_pixmap_with_transparent_regions()))

    def test_empty_pixmap_is_default(self):
        self.assertTrue(_is_default_avatar(QPixmap()))


class TestPlaceholderRendering(unittest.TestCase):
    """锁定占位几何：方形圆角色块铺满、首字居中由 drawText AlignCenter 保证。

    注意：offscreen 平台下文字完全不渲染（QLabel 与 QPainter.drawText
    均不出像素），汉字观感只能在真实窗口确认，测试不断言文字内容。
    """

    def _render(self, name):
        # 显式给 host 上背景：套件里更早的 MainWindow 测试会对共享
        # QApplication 应用全局 QSS 且不清理，app 级 QSS 生效后普通
        # QWidget 不再画原生调色板背景，圆角外像素会变透明 (0,0,0)
        host = QWidget()
        host.setAttribute(Qt.WA_StyledBackground)
        host.setStyleSheet("background-color: rgb(239,239,239);")
        lay = QHBoxLayout(host)
        w = AvatarWidget(44)
        w.set_name(name)
        lay.addWidget(w)
        host.show()
        _APP.processEvents()
        return w.grab().toImage()

    def test_rounded_square_placeholder(self):
        img = self._render("李品海")
        center = img.pixelColor(22, 22)
        self.assertEqual(center.alpha(), 255)
        # 四角透出父控件底色（圆角），边缘中点应是占位色
        corner = img.pixelColor(0, 0)
        self.assertEqual(
            (corner.red(), corner.green(), corner.blue()), (239, 239, 239))
        edge = img.pixelColor(22, 1)
        self.assertEqual((edge.red(), edge.green(), edge.blue()),
                         (center.red(), center.green(), center.blue()))

    def test_color_depends_on_name(self):
        img_a = self._render("李品海")
        img_b = self._render("赵潇")
        # 不同名字颜色大概率不同（颜色盘 10 色，同名必同色）
        c_a = img_a.pixelColor(22, 22)
        c_b = img_b.pixelColor(22, 22)
        self.assertTrue(c_a.alpha() == c_b.alpha() == 255)


if __name__ == "__main__":
    unittest.main()
