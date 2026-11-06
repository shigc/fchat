"""AvatarWidget 可点击/悬停相机角标测试。离线。"""
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QPoint, QEvent, Qt  # noqa: E402
from PyQt5.QtGui import QMouseEvent, QPixmap  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from ui.widgets import AvatarWidget  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _release(pos):
    return QMouseEvent(QEvent.MouseButtonRelease, pos,
                       Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)


class AvatarClickTest(unittest.TestCase):
    def test_not_clickable_by_default(self):
        av = AvatarWidget(40)
        seen = []
        # 不能 connect(list.append)：PyQt5 5.15 对内建方法槽 emit 时直接段错误
        av.clicked.connect(lambda: seen.append(1))
        av.mouseReleaseEvent(_release(QPoint(10, 10)))
        self.assertEqual(seen, [])

    def test_clickable_emits_clicked(self):
        av = AvatarWidget(40)
        av.set_clickable(True)
        seen = []
        av.clicked.connect(lambda: seen.append(1))
        av.mouseReleaseEvent(_release(QPoint(10, 10)))
        self.assertEqual(seen, [1])

    def test_clickable_paint_placeholder_and_pixmap_no_crash(self):
        # 悬停态绘制（占位与已有 pixmap 两条分支）不应抛异常
        av = AvatarWidget(40)
        av.set_clickable(True)
        av.set_name("张")
        av._hovered = True
        av.repaint()
        pm = QPixmap(40, 40)
        pm.fill(Qt.red)
        av.set_pixmap_rounded(pm)
        av.repaint()

    def test_leave_clears_hover(self):
        av = AvatarWidget(40)
        av.set_clickable(True)
        av.enterEvent(QEvent(QEvent.Enter))
        self.assertTrue(av._hovered)
        av.leaveEvent(QEvent(QEvent.Leave))
        self.assertFalse(av._hovered)


if __name__ == "__main__":
    unittest.main()
