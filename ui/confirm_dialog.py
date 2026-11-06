"""微信式无边框确认弹窗：统一替换 QMessageBox.question。

全程序确认/询问类弹窗走模块级 confirm()：外观骨架（圆角卡片/标题头/
关闭按钮）由 ui.dialog_base.FramelessCardDialog 提供，问号图标由
icons.py 现画，确认键 danger=True 时为红色（WxPushButton danger
变体），用于删除房间、清缓存等不可恢复操作。

默认按钮固定给「取消」：与原 QMessageBox.question(default=No) 的安全语义
一致——回车/ESC 都不会执行危险操作，必须显式点确认键。
"""
from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QDialog, QHBoxLayout, QLabel

from . import icons
from .dialog_base import FramelessCardDialog
from .widgets import WxLabel, WxPushButton


class ConfirmDialog(FramelessCardDialog):
    """无边框圆角确认弹窗。theme 由调用方注入（一次性弹窗无需订阅切换）。"""

    def __init__(self, parent=None, theme=None, title="", text="",
                 danger=False, ok_text="确定", cancel_text="取消",
                 show_cancel=True, icon_name="question"):
        super().__init__(title, theme, parent)
        self._danger = danger

        body = QHBoxLayout()
        body.setContentsMargins(4, 0, 4, 0)
        body.setSpacing(16)
        if icon_name:
            self._icon = QLabel(self)
            self._icon.setPixmap(icons.icon(icon_name, 40).pixmap(40, 40))
            body.addWidget(self._icon, 0, Qt.AlignTop)
        self._text = WxLabel(text, theme, self)
        self._text.setWordWrap(True)
        body.addWidget(self._text, 1)
        self._layout.addLayout(body, 1)

        foot = QHBoxLayout()
        foot.addStretch(1)
        foot.setSpacing(10)
        theme_widgets = [self._text]
        if show_cancel:
            # 取消键只在需要时创建：以 self 为父控件却不加进布局的
            # 孤儿按钮会默认显示在左上角（notify 无取消键时漏过这一层）
            self._cancel_btn = WxPushButton(cancel_text, theme, parent=self)
            self._cancel_btn.clicked.connect(self.reject)
            foot.addWidget(self._cancel_btn)
            theme_widgets.append(self._cancel_btn)
        self._ok_btn = WxPushButton(ok_text, theme, danger=danger, parent=self)
        self._ok_btn.clicked.connect(self.accept)
        foot.addWidget(self._ok_btn)
        self._layout.addLayout(foot)

        self._theme_widgets.extend(theme_widgets)
        if show_cancel:
            # 安全默认：回车等价于取消，危险操作必须显式确认
            self._cancel_btn.setDefault(True)
            self._cancel_btn.setFocus()
        else:
            self._ok_btn.setDefault(True)
            self._ok_btn.setFocus()


def confirm(parent, theme, title, text, *, danger=False,
            ok_text="确定", cancel_text="取消") -> bool:
    """模态弹一次确认框；返回用户是否点了确认键。"""
    dlg = ConfirmDialog(parent, theme, title, text,
                        danger=danger, ok_text=ok_text, cancel_text=cancel_text)
    try:
        return dlg.exec_() == QDialog.Accepted
    finally:
        dlg.deleteLater()


def notify(parent, theme, title, text, ok_text="确定", stay_on_top=False):
    """单按钮提示弹窗（无图标无取消键），只告知信息、无询问语义。

    stay_on_top：存在 WindowStaysOnTopHint 全屏窗（如图片查看器）时，
    普通模态窗会被压在其后不可见，此时必须置顶。"""
    dlg = ConfirmDialog(parent, theme, title, text,
                        show_cancel=False, icon_name="", ok_text=ok_text)
    if stay_on_top:
        dlg.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    try:
        dlg.exec_()
    finally:
        dlg.deleteLater()
