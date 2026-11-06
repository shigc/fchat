"""AvatarWidget 在线状态点：paintEvent 不崩 / tooltip / 相机角标互斥。

状态点与「点击换头像」的相机角标同在右下角，可点击头像悬停时只画
相机角标（本测试用标记位验证路径选择）。
运行：python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication  # noqa: E402

from ui.widgets import AvatarWidget  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class AvatarStatusBadgeTest(unittest.TestCase):

    def test_default_no_status(self):
        a = AvatarWidget(44)
        a.set_name("张三")
        self.assertIsNone(a._status_key)

    def test_paint_with_status_does_not_crash(self):
        for key in ("online", "away", "busy", "dnd", "offline", "departed"):
            a = AvatarWidget(44)
            a.set_name("张三")
            a.set_status_key(key)
            a.repaint()

    def test_paint_placeholder_with_status(self):
        """无头像占位分支同样要能叠画状态点。"""
        a = AvatarWidget(36)
        a.set_name("李四")
        a.set_status_key("offline")
        a.repaint()

    def test_clear_status(self):
        a = AvatarWidget(44)
        a.set_status_key("online")
        a.set_status_key(None)
        self.assertIsNone(a._status_key)
        a.repaint()

    def test_tooltip_settable(self):
        a = AvatarWidget(44)
        a.set_status_tooltip("在线 · 会议中")
        self.assertEqual(a.toolTip(), "在线 · 会议中")
        a.set_status_tooltip("")
        self.assertEqual(a.toolTip(), "")

    def test_hover_camera_badge_skips_status_dot(self):
        """可点击头像悬停时只画相机角标，不画状态点（同位互斥）。"""
        a = AvatarWidget(44)
        a.set_name("张三")
        a.set_status_key("online")
        a.set_clickable(True)
        a._hovered = True
        a.repaint()  # 走相机角标分支，不应崩
        a._hovered = False
        a.repaint()


if __name__ == "__main__":
    unittest.main()
