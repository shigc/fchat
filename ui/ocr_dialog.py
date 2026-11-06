"""离线 OCR 结果对话框：可编辑的识别文本 + 复制 / 插入输入框。

识别由调用方在主窗口后台线程完成（core.ocr），本对话框只负责展示与
回传文本——文字全部留在本机，不上传模型。结果框是可编辑的：OCR 偶尔
会把形近字认错，让用户改完再插入比事后重打省事。
「插入输入框」沿用 AIDialog 的约定：只回填、绝不自动发送。
"""
from __future__ import annotations

import logging
from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication, QHBoxLayout, QTextEdit,
)

from .dialog_base import FramelessCardDialog
from .widgets import WxLabel, WxPushButton, _color, install_text_menu

log = logging.getLogger(__name__)


class OcrResultDialog(FramelessCardDialog):
    """展示识别结果；点「插入输入框」时 accept 并由调用方取 inserted_text。"""

    def __init__(self, parent=None, theme=None, title: str = "识别文字",
                 text: str = "", width: int = 560, stay_on_top: bool = False):
        super().__init__(title, theme, parent, width=width, resizable=True)
        # 图片查看器是 WindowStaysOnTopHint 全屏窗，普通模态窗会被压在
        # 它后面（真机实测结果窗"凭空消失"）；从查看器发起识别时必须置顶
        if stay_on_top:
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.resize(width, 520)
        self.setWindowTitle(title)
        self.inserted_text: Optional[str] = None

        self._note = WxLabel("识别结果可先编辑再插入；文字在本机识别，不会上传。",
                             theme, self)
        self._note.set_variant("hint")
        self._note.setWordWrap(True)
        self._layout.addWidget(self._note)

        self._view = QTextEdit(self)
        self._view.setLineWrapMode(QTextEdit.WidgetWidth)
        self._view.setMinimumHeight(320)
        install_text_menu(self._view)
        self._view.setPlainText(text or "")
        self._layout.addWidget(self._view, 1)
        self._apply_view_style()

        self._status = WxLabel("", theme, self)
        self._status.set_variant("hint")
        self._layout.addWidget(self._status)

        foot = QHBoxLayout()
        foot.setSpacing(10)
        self._count = WxLabel(self._count_text(), theme, self)
        self._count.set_variant("hint")
        foot.addWidget(self._count)
        foot.addStretch(1)
        self._copy_btn = WxPushButton("复制", theme, parent=self)
        self._copy_btn.clicked.connect(self._on_copy)
        foot.addWidget(self._copy_btn)
        self._insert_btn = WxPushButton("插入输入框", theme, parent=self)
        self._insert_btn.clicked.connect(self._on_insert)
        foot.addWidget(self._insert_btn)
        self._close_btn_bottom = WxPushButton("关闭", theme, parent=self)
        self._close_btn_bottom.clicked.connect(self.reject)
        foot.addWidget(self._close_btn_bottom)
        self._layout.addLayout(foot)

        self._theme_widgets.extend([
            self._note, self._status, self._count,
            self._copy_btn, self._insert_btn, self._close_btn_bottom])
        self._update_actions()
        self._view.textChanged.connect(self._update_actions)
        # 识别完直接全选，Ctrl+C 就能走，多数情况下不必再点按钮
        self._view.selectAll()
        self._view.setFocus()

    # ------------------------------------------------------------------ #
    # 外观
    # ------------------------------------------------------------------ #
    def _apply_view_style(self):
        t = self._theme
        bg = _color(t, "input_bg", "#ffffff")
        fg = _color(t, "text_primary", "#191919")
        border = _color(t, "input_border", "#e0e0e0")
        self._view.setStyleSheet(
            f"QTextEdit{{background:{bg};color:{fg};"
            f"border:1px solid {border};border-radius:6px;padding:6px;}}")
        # 字号跟随主题设置；QSS 的 font-size 在部分平台会被控件字体覆盖，
        # 所以直接用 QFont 设控件字体
        try:
            size = int(getattr(t, "font_size", 13) or 13)
        except (TypeError, ValueError):
            size = 13
        font = QFont()
        family = getattr(t, "font_family", "") or ""
        if family:
            font.setFamily(str(family))
        font.setPointSize(size)
        self._view.setFont(font)

    def set_theme(self, theme):
        super().set_theme(theme)
        self._apply_view_style()

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    @property
    def plain_text(self) -> str:
        return self._view.toPlainText()

    def _count_text(self) -> str:
        text = self.plain_text
        chars = len("".join(text.split()))
        lines = len([l for l in text.splitlines() if l.strip()])
        return f"{lines} 行 · {chars} 字"

    def _update_actions(self):
        has_text = bool(self.plain_text.strip())
        self._count.setText(self._count_text())
        self._copy_btn.setEnabled(has_text)
        self._insert_btn.setEnabled(has_text)

    def _on_copy(self, *_):
        text = self.plain_text.strip()
        if not text:
            return
        try:
            QApplication.clipboard().setText(text)
            self._status.set_variant("hint")
            self._status.setText("已复制到剪贴板")
        except Exception:  # noqa: BLE001 —— 槽内异常会终止进程
            log.exception("复制识别结果失败")

    def _on_insert(self, *_):
        text = self.plain_text.strip()
        if not text:
            return
        self.inserted_text = text
        self.accept()

    # 右上角关闭按钮是 clicked(bool) 直连；Cython 编译版必须兜住参数
    def accept(self, *_):
        super().accept()

    def reject(self, *_):
        super().reject()


def show_ocr_result(parent, theme, text: str,
                    stay_on_top: bool = False) -> Optional[str]:
    """模态展示识别结果；用户点「插入输入框」时返回文本，否则 None。"""
    dlg = OcrResultDialog(parent, theme, text=text, stay_on_top=stay_on_top)
    try:
        dlg.exec_()
        return dlg.inserted_text
    finally:
        dlg.deleteLater()
