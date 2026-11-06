"""无边框圆角卡片对话框基类：确认弹窗与新建房间类弹窗共用。

微信式外观：无系统标题栏，自绘圆角卡片底 + 右上角关闭按钮，标题加粗，
颜色全部走 ThemeManager 语义色随亮/暗切换。子类把字段加进 self._layout，
按钮区放底部。不用 Qt.Popup（Windows 上 Popup 无法合成 IME 输入）。
"""
from __future__ import annotations

from PyQt5.QtCore import QEvent, QRectF, Qt
from PyQt5.QtGui import QColor, QCursor, QPainter, QPainterPath
from PyQt5.QtWidgets import (QApplication, QDialog, QHBoxLayout, QSizeGrip,
                             QToolButton, QVBoxLayout, QWidget)

from . import icons
from .widgets import WxLabel, _color

_RADIUS = 10.0
# 卡片固定宽度：与微信弹窗观感一致
_CARD_WIDTH = 440
# 无边框窗口没有系统边框可拖，自己给边沿留的缩放热区（逻辑像素）
_RESIZE_MARGIN = 6
_RESIZE_CURSORS = {
    int(Qt.LeftEdge | Qt.TopEdge): Qt.SizeFDiagCursor,
    int(Qt.RightEdge | Qt.BottomEdge): Qt.SizeFDiagCursor,
    int(Qt.RightEdge | Qt.TopEdge): Qt.SizeBDiagCursor,
    int(Qt.LeftEdge | Qt.BottomEdge): Qt.SizeBDiagCursor,
    int(Qt.LeftEdge): Qt.SizeHorCursor,
    int(Qt.RightEdge): Qt.SizeHorCursor,
    int(Qt.TopEdge): Qt.SizeVerCursor,
    int(Qt.BottomEdge): Qt.SizeVerCursor,
}


class FramelessCardDialog(QDialog):
    """无边框圆角对话框骨架：标题头部（含关闭按钮）+ 子类内容区。"""

    def __init__(self, title: str, theme=None, parent=None, *,
                 width: int = _CARD_WIDTH, resizable: bool = False):
        super().__init__(parent)
        self._theme = theme
        self._theme_widgets: list = []
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        if resizable:
            # 可伸缩弹窗（识别结果等长文本）：宽度仍以下限起步，高度放开；
            # 除右下角 QSizeGrip 外，四条边/四角也可直接拖拽缩放
            # ——无边框窗口在 Windows 上没有系统边框，只给 grip 时用户
            # 按习惯去拖边沿会以为「不支持调整大小」（真机反馈）
            self.setMinimumWidth(width)
            self.setMinimumHeight(280)
            self.setMouseTracking(True)
        else:
            self.setFixedWidth(width)

        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)

        head = QHBoxLayout()
        head.setContentsMargins(24, 16, 10, 0)
        self._title = WxLabel(title, theme, self)
        self._title.set_variant("bold")
        # 标题不接收鼠标：按下时落到对话框自身，才能从标题处拖动卡片
        self._title.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        head.addWidget(self._title)
        head.addStretch(1)
        self._close_btn = QToolButton(self)
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.clicked.connect(self.reject)
        head.addWidget(self._close_btn)
        root.addLayout(head)

        # 子类字段区（保留 _layout 命名：_BaseCreateDialog 历史接口）
        self._layout = QVBoxLayout()
        self._layout.setContentsMargins(24, 16, 24, 20)
        self._layout.setSpacing(12)
        root.addLayout(self._layout)

        if resizable:
            grip_row = QHBoxLayout()
            grip_row.setContentsMargins(0, 0, 2, 4)
            grip_row.addStretch(1)
            grip_row.addWidget(QSizeGrip(self))
            root.addLayout(grip_row)

        self._theme_widgets.append(self._title)
        self._resizable = resizable
        self._drag_offset = None
        self._resize_edges = 0
        self._resize_start = None      # (起始 geometry, 起始 globalPos)
        self._resize_cursor = None
        self._apply_chrome()

    # ------------------------------------------------------------------ #
    # 无边框卡片拖动：只认标题带（y 到标题下缘附近），内容区各自交互
    # ------------------------------------------------------------------ #
    def _in_drag_band(self, pos) -> bool:
        title = self._title.geometry()
        return bool(title.height()) and pos.y() <= title.bottom() + 10

    def _edge_at(self, pos) -> int:
        """指针落在哪条边/角（0 表示不在缩放热区）。"""
        if not self._resizable:
            return 0
        edges = 0
        if pos.x() <= _RESIZE_MARGIN:
            edges |= int(Qt.LeftEdge)
        elif pos.x() >= self.width() - _RESIZE_MARGIN - 1:
            edges |= int(Qt.RightEdge)
        if pos.y() <= _RESIZE_MARGIN:
            edges |= int(Qt.TopEdge)
        elif pos.y() >= self.height() - _RESIZE_MARGIN - 1:
            edges |= int(Qt.BottomEdge)
        return edges

    def _sync_resize_cursor(self, pos) -> None:
        shape = _RESIZE_CURSORS.get(self._edge_at(pos))
        if shape == self._resize_cursor:
            return
        self._resize_cursor = shape
        if shape is None:
            self.unsetCursor()
        else:
            self.setCursor(shape)

    def _resize_to(self, global_pos) -> None:
        geo, origin = self._resize_start
        dx = global_pos.x() - origin.x()
        dy = global_pos.y() - origin.y()
        left, top, width, height = (geo.x(), geo.y(),
                                    geo.width(), geo.height())
        if self._resize_edges & Qt.LeftEdge:
            width = max(self.minimumWidth(), width - dx)
            left = geo.right() - width + 1     # 拖左缘时右缘钉住不动
        elif self._resize_edges & Qt.RightEdge:
            width = max(self.minimumWidth(), width + dx)
        if self._resize_edges & Qt.TopEdge:
            height = max(self.minimumHeight(), height - dy)
            top = geo.bottom() - height + 1
        elif self._resize_edges & Qt.BottomEdge:
            height = max(self.minimumHeight(), height + dy)
        self.setGeometry(left, top, width, height)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            # 缩放热区优先于标题拖动：上边沿与拖动带重叠
            edges = self._edge_at(event.pos())
            if edges:
                self._resize_edges = edges
                self._resize_start = (self.geometry(), event.globalPos())
                event.accept()
                return
            if self._in_drag_band(event.pos()):
                self._drag_offset = (event.globalPos()
                                     - self.frameGeometry().topLeft())
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resize_edges and event.buttons() & Qt.LeftButton:
            self._resize_to(event.globalPos())
            event.accept()
            return
        if (self._drag_offset is not None
                and event.buttons() & Qt.LeftButton):
            self.move(event.globalPos() - self._drag_offset)
            event.accept()
            return
        self._sync_resize_cursor(event.pos())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._resize_edges = 0
        self._resize_start = None
        self._drag_offset = None
        self._sync_resize_cursor(self.mapFromGlobal(event.globalPos()))
        super().mouseReleaseEvent(event)

    def eventFilter(self, obj, event):
        if not self._resizable:
            return super().eventFilter(obj, event)
        etype = event.type()
        if etype == QEvent.MouseButtonPress \
                and event.button() == Qt.LeftButton:
            # 子控件会截走边沿上的按下：拦下热区内的按下才能缩放
            pos = self.mapFromGlobal(event.globalPos())
            edges = self._edge_at(pos)
            if edges:
                self._resize_edges = edges
                self._resize_start = (self.geometry(), event.globalPos())
                return True
        elif etype == QEvent.MouseMove:
            # 缩放拖动：按下发生在子控件上，后续移动也发给子控件，
            # mouseMoveEvent 收不到，这里代为执行 _resize_to（同主窗口）
            if self._resize_edges and event.buttons() & Qt.LeftButton:
                self._resize_to(event.globalPos())
                return True
            # 用事件位置刷新光标（QCursor.pos() 在快速移动时滞后）
            self._sync_resize_cursor(self.mapFromGlobal(event.globalPos()))
        elif etype == QEvent.Enter:
            # Enter 事件不携带位置，退回全局鼠标位置
            self._sync_resize_cursor(self.mapFromGlobal(QCursor.pos()))
        return super().eventFilter(obj, event)

    def _apply_chrome(self):
        self._close_btn.setIcon(icons.icon("close", 16))
        hover = _color(self._theme, "conv_hover", "#f2f2f2")
        self._close_btn.setStyleSheet(
            "QToolButton{border:none;border-radius:4px;padding:4px;"
            f"background:transparent;}} QToolButton:hover{{background:{hover};}}")

    def set_theme(self, theme) -> None:
        """主题切换时刷新所有已登记控件（由 main_window 注入）。"""
        self._theme = theme
        for w in self._theme_widgets:
            try:
                w.set_theme(theme)
            except AttributeError:
                pass
        self._apply_chrome()
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
                            _RADIUS, _RADIUS)
        p.fillPath(path, QColor(_color(self._theme, "conv_bg", "#ffffff")))
        p.setPen(QColor(_color(self._theme, "border", "#e7e7e7")))
        p.drawPath(path)

    def showEvent(self, event):
        super().showEvent(event)
        if self._resizable:
            # 子控件是构造期建的，show 时才挂过滤器；重复 install 会被 Qt 忽略
            for w in self.findChildren(QWidget):
                w.installEventFilter(self)
        parent = self.parentWidget()
        if parent is not None and parent.isVisible():
            geo = self.frameGeometry()
            geo.moveCenter(parent.frameGeometry().center())
            self.move(geo.topLeft())
        else:
            screen = QApplication.primaryScreen().availableGeometry()
            self.move(screen.center() - self.rect().center())
