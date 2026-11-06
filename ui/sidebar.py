"""最左侧图标导航栏（微信风格深色竖向图标列）。

提供可扩展的导航按钮列表，追踪点击历史，
支持通过 add_nav_button() 动态添加新导航项。
"""
from __future__ import annotations

from PyQt5.QtCore import QSize, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QVBoxLayout, QPushButton, QButtonGroup, QFrame,
)

from . import icons
from .widgets import _color

_NAV_ITEMS = [
    ("chats", "会话", "chats"),
    ("settings", "设置", "settings"),
]

_MAX_HISTORY = 20


class Sidebar(QFrame):
    """竖向图标导航栏。支持历史追踪和动态扩展。"""

    show_chats = pyqtSignal()
    show_settings = pyqtSignal()
    nav_clicked = pyqtSignal(str)  # 通用导航信号，参数为 nav_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Sidebar")
        # QFrame 子类必须显式开这个，#Sidebar 的 QSS 深色背景才会画出来
        # （否则透出窗口底色，浅色图标几乎看不见）
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedWidth(56)
        self._theme = None
        self._icon_names: dict[QPushButton, str] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QPushButton] = {}
        self._nav_history: list[str] = []

        for icon_name, tip, nav_id in _NAV_ITEMS:
            self._add_nav_button(icon_name, tip, nav_id, layout)

        layout.addStretch()
        self._group.buttonClicked.connect(self._on_clicked)

    def _add_nav_button(self, icon_name: str, tip: str, nav_id: str,
                        layout: QVBoxLayout):
        btn = QPushButton()
        btn.setObjectName("SidebarButton")
        btn.setCheckable(True)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedSize(40, 40)
        btn.setToolTip(tip)
        btn.setProperty("nav_id", nav_id)
        btn.setIconSize(QSize(22, 22))
        btn.setFocusPolicy(Qt.NoFocus)
        self._icon_names[btn] = icon_name
        self._apply_btn_icon(btn)
        # 选中态换白icon：toggled 在 setChecked(True)（含程序化切换）时也会触发
        btn.toggled.connect(lambda _checked, b=btn: self._apply_btn_icon(b))
        self._group.addButton(btn)
        self._buttons[nav_id] = btn
        layout.addWidget(btn)
        if nav_id == "chats":
            btn.setChecked(True)

    def add_nav_button(self, icon_name: str, tip: str, nav_id: str):
        """动态添加导航按钮（现画图标名），插入到设置按钮之前。"""
        layout = self.layout()
        stretch_idx = None
        for i in range(layout.count()):
            if layout.itemAt(i).spacerItem():
                stretch_idx = i
                break
        btn = QPushButton()
        btn.setObjectName("SidebarButton")
        btn.setCheckable(True)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedSize(40, 40)
        btn.setToolTip(tip)
        btn.setProperty("nav_id", nav_id)
        btn.setIconSize(QSize(22, 22))
        btn.setFocusPolicy(Qt.NoFocus)
        self._icon_names[btn] = icon_name
        self._apply_btn_icon(btn)
        btn.toggled.connect(lambda _checked, b=btn: self._apply_btn_icon(b))
        self._group.addButton(btn)
        self._buttons[nav_id] = btn
        if stretch_idx is not None:
            layout.insertWidget(stretch_idx, btn)
        else:
            layout.addWidget(btn)

    def _apply_btn_icon(self, btn: QPushButton) -> None:
        """未选中浅灰、选中白（配 accent 底）；随主题取语义色。"""
        name = self._icon_names.get(btn)
        if name is None:
            return
        if btn.isChecked():
            btn.setIcon(icons.icon(name, 22, color="#ffffff"))
        else:
            btn.setIcon(icons.icon(
                name, 22, _color(self._theme, "text_secondary", "#9a9a9a")))

    def set_theme(self, theme) -> None:
        """注入取色源并刷新所有导航图标颜色（随亮/暗主题切换）。"""
        self._theme = theme
        for btn in self._icon_names:
            self._apply_btn_icon(btn)

    def _on_clicked(self, btn: QPushButton):
        nav_id = btn.property("nav_id")
        if not nav_id:
            return
        self._nav_history.append(nav_id)
        if len(self._nav_history) > _MAX_HISTORY:
            self._nav_history = self._nav_history[-_MAX_HISTORY:]
        self.nav_clicked.emit(nav_id)
        if nav_id == "chats":
            self.show_chats.emit()
        elif nav_id == "settings":
            self.show_settings.emit()

    def set_active(self, nav_id: str):
        """程序化切换页签时同步按钮选中态（不触发 nav_clicked）。

        托盘菜单等入口直接切内容区，不走按钮点击；只切右侧会让左侧
        高亮停在旧页签，两边对不上。
        """
        btn = self._buttons.get(nav_id)
        if btn is not None and not btn.isChecked():
            btn.setChecked(True)

    @property
    def nav_history(self) -> list[str]:
        return list(self._nav_history)

    def go_back(self) -> str | None:
        """回退到上一个导航页面，返回回退到的 nav_id；无历史则返回 None。"""
        if len(self._nav_history) < 2:
            return None
        self._nav_history.pop()
        prev_id = self._nav_history[-1]
        btn = self._buttons.get(prev_id)
        if btn:
            btn.setChecked(True)
            self.nav_clicked.emit(prev_id)
            if prev_id == "chats":
                self.show_chats.emit()
            elif prev_id == "settings":
                self.show_settings.emit()
        return prev_id
