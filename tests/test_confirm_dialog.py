"""统一确认弹窗 ConfirmDialog 测试。离线 offscreen。"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QDialog  # noqa: E402

from ui.confirm_dialog import ConfirmDialog, confirm  # noqa: E402
from ui.theme import ThemeManager  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class ConfirmDialogTest(unittest.TestCase):
    def setUp(self):
        self.tm = ThemeManager()

    def _dlg(self, **kw):
        kw.setdefault("title", "确认")
        kw.setdefault("text", "确定删除吗？")
        dlg = ConfirmDialog(None, self.tm, **kw)
        self.addCleanup(dlg.deleteLater)
        return dlg

    def test_frameless_modal(self):
        dlg = self._dlg()
        self.assertTrue(dlg.windowFlags() & Qt.FramelessWindowHint)
        self.assertTrue(dlg.isModal())

    def test_default_button_is_cancel(self):
        # 安全默认：回车等价于取消，危险操作必须显式确认
        dlg = self._dlg()
        self.assertTrue(dlg._cancel_btn.isDefault())

    def test_notify_without_cancel_has_no_orphan_button(self):
        """show_cancel=False 不得创建取消键：以弹窗为父控件却不加进布局的
        孤儿按钮会显示在左上角（真机见过悬空「取消」）。"""
        dlg = self._dlg(show_cancel=False, icon_name="")
        self.assertFalse(hasattr(dlg, "_cancel_btn"))
        self.assertTrue(dlg._ok_btn.isDefault())

    def test_danger_variant_and_ok_text(self):
        dlg = self._dlg(danger=True, ok_text="删除")
        self.assertTrue(dlg._ok_btn._danger)
        self.assertEqual(dlg._ok_btn.text(), "删除")
        plain = self._dlg()
        self.assertFalse(plain._ok_btn._danger)
        self.assertEqual(plain._ok_btn.text(), "确定")

    def test_ok_accepts_and_cancel_rejects(self):
        dlg = self._dlg()
        dlg._ok_btn.click()
        self.assertEqual(dlg.result(), QDialog.Accepted)
        dlg2 = self._dlg()
        dlg2._cancel_btn.click()
        self.assertEqual(dlg2.result(), QDialog.Rejected)
        dlg2._close_btn.click()
        self.assertEqual(dlg2.result(), QDialog.Rejected)

    def test_set_theme_refreshes_both_modes(self):
        dlg = self._dlg()
        for mode in ("dark", "light"):
            self.tm.theme = mode
            self.tm.apply(_APP)
            dlg.set_theme(self.tm)  # 不抛异常即通过

    def test_paint_does_not_crash(self):
        # offscreen 能验证几何与绘制路径，观感仍需真机确认
        dlg = self._dlg(danger=True)
        dlg.resize(dlg.sizeHint())
        dlg.grab()

    def test_confirm_helper_maps_exec_result(self):
        with mock.patch.object(ConfirmDialog, "exec_",
                               return_value=QDialog.Accepted) as ex, \
                mock.patch.object(ConfirmDialog, "deleteLater") as dl:
            self.assertTrue(confirm(None, self.tm, "标题", "正文"))
            ex.assert_called_once()
            dl.assert_called_once()
        with mock.patch.object(ConfirmDialog, "exec_",
                               return_value=QDialog.Rejected):
            self.assertFalse(confirm(None, self.tm, "标题", "正文"))


if __name__ == "__main__":
    unittest.main()
