"""消息提醒列表：查看未到期提醒、单条取消、点击跳到原消息。

入口在托盘右键「消息提醒」。数据由 main_window 注入（权威在 config
的 reminders 落盘），对话框只展示与转发意图：open_requested 交回
main_window 复用托盘 toast 的同一条跳转链路。
"""
from __future__ import annotations

import time

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QHBoxLayout, QScrollArea,
                             QVBoxLayout, QWidget)

from .dialog_base import FramelessCardDialog
from .widgets import WxLabel, WxPushButton


def format_due(due: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(due))


class _RemindRow(QWidget):
    """单条提醒：上行房间名 + 到期时间，下行内容摘要；整行可点跳原消息。"""

    open_requested = pyqtSignal(str, str)     # (rid, msg_id)
    cancel_requested = pyqtSignal(str, str)   # (rid, msg_id)

    def __init__(self, row: dict, theme, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._rid = str(row.get("rid", "") or "")
        self._msg_id = str(row.get("msg_id", "") or "")
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._restyle(False)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 8, 8)
        lay.setSpacing(10)
        left = QVBoxLayout()
        left.setSpacing(3)
        top = QHBoxLayout()
        top.setSpacing(8)
        name = WxLabel(row.get("room_name") or "未知房间", theme, self)
        name.set_variant("bold")
        top.addWidget(name)
        due = WxLabel(format_due(float(row.get("due", 0) or 0)), theme, self)
        due.set_variant("hint")
        top.addStretch(1)
        top.addWidget(due)
        left.addLayout(top)
        snippet = WxLabel(row.get("text") or "（无内容摘要）", theme, self)
        snippet.set_variant("hint")
        snippet.setWordWrap(False)
        fm = snippet.fontMetrics()
        snippet.setText(fm.elidedText(snippet.text(), Qt.ElideRight, 300))
        left.addWidget(snippet)
        lay.addLayout(left, 1)
        btn = WxPushButton("取消", theme, self)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(
            lambda _=False: self.cancel_requested.emit(self._rid, self._msg_id))
        lay.addWidget(btn, 0, Qt.AlignVCenter)

    def _restyle(self, hovered: bool):
        from .widgets import _color
        bg = _color(self._theme, "conv_hover", "#f5f5f5") if hovered else "transparent"
        self.setStyleSheet(
            f"QWidget{{background:{bg};border-radius:6px;}}")

    def enterEvent(self, event):
        self._restyle(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._restyle(False)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.open_requested.emit(self._rid, self._msg_id)
        super().mouseReleaseEvent(event)


class RemindersDialog(FramelessCardDialog):
    """未到期提醒列表；非模态（托盘入口打开时主窗口可能还在后台）。"""

    open_requested = pyqtSignal(str, str)
    cancel_requested = pyqtSignal(str, str)

    def __init__(self, theme=None, parent=None):
        super().__init__("消息提醒", theme, parent, width=460)
        self.setModal(False)

        self._empty = WxLabel("暂无未到期的提醒", theme, self)
        self._empty.set_variant("hint")
        self._empty.setAlignment(Qt.AlignCenter)
        self._layout.addWidget(self._empty)

        self._rows_widget = QWidget(self)
        self._rows_lay = QVBoxLayout(self._rows_widget)
        self._rows_lay.setContentsMargins(0, 0, 0, 0)
        self._rows_lay.setSpacing(2)
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.NoFrame)
        self._scroll.setStyleSheet("background:transparent;")
        self._scroll.viewport().setAutoFillBackground(False)
        self._scroll.setWidget(self._rows_widget)
        self._scroll.setMinimumHeight(140)
        self._layout.addWidget(self._scroll, 1)
        self.setMaximumHeight(480)

    def set_reminders(self, rows: list):
        while self._rows_lay.count():
            item = self._rows_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        if not rows:
            self._empty.show()
            self._scroll.hide()
            return
        self._empty.hide()
        self._scroll.show()
        for r in rows:
            row = _RemindRow(r, self._theme, self._rows_widget)
            row.open_requested.connect(self.open_requested)
            row.cancel_requested.connect(self.cancel_requested)
            self._rows_lay.addWidget(row)
        self._rows_lay.addStretch(1)
