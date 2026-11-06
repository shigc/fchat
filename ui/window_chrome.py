"""主窗口自绘窗口按钮：最小化 / 最大化(还原) / 关闭。

无边框主窗口（main_window）没有系统标题栏，三按钮分别嵌在聊天页标题行
右端与设置页顶部右上角，随页面切换始终保持在窗口右上角。样式随
ThemeManager 语义色切换；关闭按钮 hover 红底（微信同款）。构造必须挂
parent——无 parent 的控件 setVisible 会闪出一个独立小窗而非嵌进布局。
"""
from __future__ import annotations

from PyQt5.QtCore import QSize, Qt, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QToolButton, QWidget

from . import icons
from .widgets import _color


class WindowControls(QWidget):
    """三按钮排；MainWindow 统一接线到 showMinimized/_toggle_maximized/close。"""

    minimize_clicked = pyqtSignal()
    maximize_clicked = pyqtSignal()
    close_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._theme = None
        self._maximized = False

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        self._min_btn = self._make_btn("最小化")
        self._max_btn = self._make_btn("最大化")
        self._close_btn = self._make_btn("关闭")
        # clicked(bool) → 零参信号：lambda 兜住多余参数（Cython 编译版
        # 多参信号直连少参方法会 TypeError，lambda 是既有验证过的写法）
        self._min_btn.clicked.connect(lambda _=False: self.minimize_clicked.emit())
        self._max_btn.clicked.connect(lambda _=False: self.maximize_clicked.emit())
        self._close_btn.clicked.connect(lambda _=False: self.close_clicked.emit())
        lay.addWidget(self._min_btn)
        lay.addWidget(self._max_btn)
        lay.addWidget(self._close_btn)

        self._apply_chrome()

    def _make_btn(self, tip: str) -> QToolButton:
        btn = QToolButton(self)
        btn.setObjectName("WindowCtrlBtn")
        btn.setFixedSize(30, 30)
        btn.setIconSize(QSize(16, 16))
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFocusPolicy(Qt.NoFocus)
        btn.setToolTip(tip)
        return btn

    def _apply_chrome(self) -> None:
        self._min_btn.setIcon(icons.icon("min", 16))
        self._max_btn.setIcon(
            icons.icon("restore" if self._maximized else "max", 16))
        self._close_btn.setIcon(icons.icon("close", 16))
        hover = _color(self._theme, "conv_hover", "#f2f2f2")
        danger = _color(self._theme, "danger_bg", "#e64545")
        base = ("QToolButton{border:none;border-radius:4px;padding:4px;"
                "background:transparent;}")
        self._min_btn.setStyleSheet(
            f"{base} QToolButton:hover{{background:{hover};}}")
        self._max_btn.setStyleSheet(
            f"{base} QToolButton:hover{{background:{hover};}}")
        self._close_btn.setStyleSheet(
            f"{base} QToolButton:hover{{background:{danger};}}")

    def set_maximized(self, maximized: bool, *_) -> None:
        """最大化状态切换（还原态显示 max，最大化态显示 restore）。"""
        if maximized == self._maximized:
            return
        self._maximized = maximized
        self._max_btn.setIcon(
            icons.icon("restore" if maximized else "max", 16))
        self._max_btn.setToolTip("还原" if maximized else "最大化")

    def set_theme(self, theme) -> None:
        self._theme = theme
        self._apply_chrome()
