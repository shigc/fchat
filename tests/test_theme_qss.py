"""theme.py QSS 模板守门：QSS 只认 /* */ 注释，行首 # 是 ID 选择器语法。

踩过的坑：模板里写过 Python 风格的 # 注释，QSS 解析器把它当选择器、
把后面的 #Sidebar 深色背景规则整条吞掉，侧栏从来没画出过深色底。
运行：python -m unittest discover -s tests -v
"""
import os
import re
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication  # noqa: E402

from ui.theme import _QSS_TEMPLATE, ThemeManager  # noqa: E402
from ui.sidebar import Sidebar  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class TestQssTemplate(unittest.TestCase):

    def test_no_python_style_comments(self):
        for i, line in enumerate(_QSS_TEMPLATE.splitlines(), 1):
            s = line.strip()
            # ID 选择器行必然是 #字母 开头（#Sidebar {）；# 后跟空格或
            # 非标识符（如中文、数字色值打头）的都是误写的注释
            if s.startswith("#") and not re.match(r"^#[A-Za-z_]", s):
                self.fail(f"QSS 模板第 {i} 行像 Python 注释（QSS 里 # 是 "
                          f"ID 选择器语法，会吞掉后续规则）：{s[:40]}")

    def test_template_rules_survive_parsing(self):
        """端到端：生成的 QSS 里 #Sidebar 深色背景要真的画出来。"""
        old_qss = _APP.styleSheet()
        _APP.setStyleSheet(ThemeManager().generate_qss())
        sb = Sidebar()
        sb.resize(56, 300)
        sb.show()
        _APP.processEvents()
        try:
            c = sb.grab().toImage().pixelColor(28, 150)
            self.assertLess(c.lightness(), 100,
                            f"#Sidebar 深色背景没画出来，实际 {c.name()}")
        finally:
            sb.hide()
            _APP.setStyleSheet(old_qss)


if __name__ == "__main__":
    unittest.main()
