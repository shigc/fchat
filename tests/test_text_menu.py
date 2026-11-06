"""文本框中文右键菜单（ui.widgets.install_text_menu）。

PyQt5 附带的 qt_zh_CN.qm 是空壳，标准动作中文化不了，所以自建菜单；
这里验证菜单内容、可用态、复制/粘贴行为与各控件的真实接线。

运行：python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QPoint, Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QMenu, QTextBrowser, QTextEdit  # noqa: E402

from ui.widgets import install_text_menu  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class _FakeMenu(QMenu):
    """替身菜单：只记录打开这一动作，不进事件循环。"""

    last = None

    def __init__(self, parent=None):
        super().__init__(parent)
        _FakeMenu.last = self

    def exec_(self, *args, **kwargs):
        pass


class EditableMenuTest(unittest.TestCase):

    def setUp(self):
        _FakeMenu.last = None
        self.edit = QTextEdit()
        self.edit.setPlainText("一段可编辑的文字")
        install_text_menu(self.edit)

    def tearDown(self):
        self.edit.deleteLater()

    def _open_menu(self, pos=None):
        _FakeMenu.last = None
        with mock.patch("PyQt5.QtWidgets.QMenu", _FakeMenu):
            self.edit.customContextMenuRequested.emit(pos or QPoint(2, 2))
        self.assertIsNotNone(_FakeMenu.last)
        return _FakeMenu.last

    def test_policy_is_custom(self):
        self.assertEqual(self.edit.contextMenuPolicy(), Qt.CustomContextMenu)

    def test_chinese_items_with_native_english_gone(self):
        texts = [a.text() for a in self._open_menu().actions() if a.text()]
        for label in ("撤销", "剪切", "复制", "粘贴", "全选"):
            self.assertIn(label, texts)
        self.assertFalse([t for t in texts if t.startswith("&")],
                         texts)

    def test_copy_action_follows_selection(self):
        menu = self._open_menu()
        copy = next(a for a in menu.actions() if a.text() == "复制")
        self.assertFalse(copy.isEnabled())   # 没有选区
        self.edit.selectAll()
        menu = self._open_menu()
        copy = next(a for a in menu.actions() if a.text() == "复制")
        self.assertTrue(copy.isEnabled())
        copy.trigger()
        self.assertEqual(_APP.clipboard().text(), "一段可编辑的文字")

    def test_paste_respects_can_paste(self):
        _APP.clipboard().setText("剪贴板内容")
        self.edit.clear()
        menu = self._open_menu()
        paste = next(a for a in menu.actions() if a.text() == "粘贴")
        self.assertTrue(paste.isEnabled())
        paste.trigger()
        self.assertEqual(self.edit.toPlainText(), "剪贴板内容")

    def test_shortcuts_displayed(self):
        menu = self._open_menu()
        copy = next(a for a in menu.actions() if a.text() == "复制")
        self.assertFalse(copy.shortcut().isEmpty())


class ReadOnlyMenuTest(unittest.TestCase):

    def setUp(self):
        _FakeMenu.last = None
        self.view = QTextBrowser()
        self.view.setPlainText("只读内容")
        install_text_menu(self.view)

    def tearDown(self):
        self.view.deleteLater()

    def test_no_edit_actions(self):
        texts = []
        with mock.patch("PyQt5.QtWidgets.QMenu", _FakeMenu):
            self.view.customContextMenuRequested.emit(QPoint(2, 2))
        texts = [a.text() for a in _FakeMenu.last.actions() if a.text()]
        self.assertIn("复制", texts)
        self.assertIn("全选", texts)
        for label in ("剪切", "粘贴", "撤销", "重做"):
            self.assertNotIn(label, texts)

    def test_link_copy_action(self):
        self.view.setHtml('<a href="https://s.example.com/x">链接</a>')
        # offscreen 不做文本命中测试（栅格扫描找不到链接位置），直接把
        # anchorAt 打桩成命中，只验证菜单项与复制行为本身
        with mock.patch.object(self.view, "anchorAt",
                               lambda pos: "https://s.example.com/x"), \
                mock.patch("PyQt5.QtWidgets.QMenu", _FakeMenu):
            self.view.customContextMenuRequested.emit(QPoint(2, 2))
        texts = [a.text() for a in _FakeMenu.last.actions() if a.text()]
        self.assertIn("复制链接", texts)
        copy = next(a for a in _FakeMenu.last.actions()
                    if a.text() == "复制链接")
        copy.trigger()
        self.assertEqual(_APP.clipboard().text(), "https://s.example.com/x")


class RealWidgetsWiringTest(unittest.TestCase):

    def test_input_edit_and_preview(self):
        from ui.chat_input import ChatInput, ChatInputEdit
        edit = ChatInputEdit()
        self.addCleanup(edit.deleteLater)
        self.assertEqual(edit.contextMenuPolicy(), Qt.CustomContextMenu)
        widget = ChatInput()
        self.addCleanup(widget.deleteLater)
        self.assertEqual(widget.preview_label.contextMenuPolicy(),
                         Qt.CustomContextMenu)

    def test_ai_dialog_and_panel_views(self):
        from ui.ai_dialog import AIDialog
        from ui.ai_panel import AIPanel
        dlg = AIDialog(None, None, job_factory=None)
        self.addCleanup(dlg.deleteLater)
        self.assertEqual(dlg._view.contextMenuPolicy(), Qt.CustomContextMenu)
        panel = AIPanel()
        self.addCleanup(panel.deleteLater)
        self.assertEqual(panel.view.contextMenuPolicy(), Qt.CustomContextMenu)

    def test_ocr_dialog_view(self):
        from ui.ocr_dialog import OcrResultDialog
        dlg = OcrResultDialog(None, None, text="x")
        self.addCleanup(dlg.deleteLater)
        self.assertEqual(dlg._view.contextMenuPolicy(), Qt.CustomContextMenu)


if __name__ == "__main__":
    unittest.main()
