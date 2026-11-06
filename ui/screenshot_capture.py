"""矩形框选截图遮罩（带标注）。

点击截图按钮后，主窗口最小化并抓取主屏，再把本遮罩置顶全屏显示：
区域外压暗、区域内亮出，用鼠标拖拽框选矩形。松手后**进入标注态**——
选区下缘浮出工具条（矩形/椭圆/箭头/画笔/文字/撤销 | 复制 | 识别文字 |
完成 | 取消），各工具在选区画布上作画；「完成」才把成图发出去（保持
原分辨率，不压缩）。框选阶段仍支持 Esc / 右键取消。

注意：多屏只做主屏范围（与之前 primaryScreen() 行为一致）。
"""
from __future__ import annotations

import os
import tempfile

from PyQt5.QtCore import (Qt, QRect, QRectF, QPoint, QPointF, QLineF,
                          pyqtSignal)
from PyQt5.QtGui import (QImage, QPixmap, QPainter, QColor, QPen, QFont,
                         QFontMetrics)
from PyQt5.QtWidgets import (QWidget, QApplication, QToolButton, QFrame,
                             QHBoxLayout, QLineEdit)

from .icons import icon

_DRAW_COLOR = QColor("#e64340")   # 标注画笔色（红色，微信同款观感）
_DRAW_WIDTH = 2
_TEXT_SIZE = 14
_UNDO_LIMIT = 10


class ScreenshotOverlay(QWidget):
    """全屏框选遮罩。构造时传入已抓取的主屏 QPixmap。"""

    selected = pyqtSignal(object)   # 携带裁剪出的 QImage（标注后为成图）
    cancelled = pyqtSignal()        # 用户取消（Esc / 右键）
    ocr_requested = pyqtSignal(str)  # 「识别文字」：成图已存临时 png 路径
    closed = pyqtSignal()           # 窗口销毁后发，供外层清引用

    _MIN_SIZE = 5   # 小于该边长视为误触，不结束，继续等下一次

    def __init__(self, base: QPixmap, parent=None):
        super().__init__(parent)
        self._base = base
        self._selecting = False
        self._start = QPoint()
        self._rect = QRect()
        # 标注态
        self._mode = "select"        # select | annotate
        self._canvas = None          # 选区 QImage 副本（设备像素 + DPR）
        self._tool = "rect"
        self._drawing = False
        self._draw_start = QPointF()
        self._draw_points = []       # 画笔自由轨迹
        self._undo_stack = []
        self._toolbar = None
        self._text_edit = None
        self._text_pos = None        # 文字锚点（选区内逻辑坐标）
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setCursor(Qt.CrossCursor)
        screen = QApplication.primaryScreen()
        if screen is not None:
            self.setGeometry(screen.geometry())
        self._emit_closed_on_close = True

    def show_fullscreen(self):
        self.showFullScreen()
        self.raise_()
        self.activateWindow()

    def _finish(self):
        """统一收尾：发 closed 并关闭窗口。"""
        self._emit_closed_on_close = False
        self.closed.emit()
        self.close()
        self.deleteLater()

    # ------------------------------------------------------------------ #
    # 事件处理
    # ------------------------------------------------------------------ #
    def closeEvent(self, event):
        if self._emit_closed_on_close:
            self.closed.emit()
            self._emit_closed_on_close = False
        super().closeEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.cancelled.emit()
            self._finish()
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        if self._mode == "annotate":
            if event.button() == Qt.RightButton:
                self.cancelled.emit()
                self._finish()
                return
            if event.button() != Qt.LeftButton:
                return
            local = self._local_in_rect(event.pos())
            if local is None:
                return
            if self._tool == "text":
                self._begin_text(local)
                return
            self._drawing = True
            self._draw_start = local
            self._draw_points = [local]
            self.update()
            return
        if event.button() == Qt.RightButton:
            self.cancelled.emit()
            self._finish()
            return
        if event.button() == Qt.LeftButton:
            self._selecting = True
            self._start = event.pos()
            self._rect = QRect(self._start, self._start)
            self.update()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._mode == "annotate":
            if not self._drawing:
                return
            local = self._local_in_rect(event.pos())
            if local is None:
                return
            if self._tool == "pen":
                self._draw_points.append(local)
            else:
                self._draw_start = local
            self.update()
            return
        if self._selecting:
            self._rect = QRect(self._start, event.pos()).normalized()
            self.update()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._mode == "annotate":
            if event.button() != Qt.LeftButton or not self._drawing:
                return
            self._drawing = False
            shape = self._current_shape()
            self._draw_points = []
            if shape is not None:
                self._commit_shape(shape)
            else:
                self.update()
            return
        if event.button() == Qt.LeftButton and self._selecting:
            self._selecting = False
            self._rect = QRect(self._start, event.pos()).normalized()
            if (self._rect.width() < self._MIN_SIZE
                    or self._rect.height() < self._MIN_SIZE):
                # 误触：清掉矩形继续等下一次
                self._rect = QRect()
                self.update()
                return
            self._enter_annotate()
            return
        super().mouseReleaseEvent(event)

    # ------------------------------------------------------------------ #
    # 标注态
    # ------------------------------------------------------------------ #
    def _enter_annotate(self):
        dpr = self._base.devicePixelRatio() or 1.0
        region = self._base.copy(self._device_rect(self._rect)).toImage()
        # 裁剪结果通常比逻辑矩形大（设备像素），保持原分辨率供发送
        region.setDevicePixelRatio(dpr)
        self._canvas = region
        self._mode = "annotate"
        self._build_toolbar()
        self._position_toolbar()
        self.update()

    def _local_in_rect(self, pos: QPoint):
        """全局逻辑坐标 → 选区内逻辑坐标；出界返回 None。"""
        if not self._rect.contains(pos):
            return None
        return QPointF(pos.x() - self._rect.x(), pos.y() - self._rect.y())

    def _current_shape(self):
        """正在拖的形状（选区内逻辑坐标）；太短视为误触返回 None。

        拖拽工具的锚点是 _draw_points[0]（按下位置），移动端是 _draw_start。
        """
        tool = self._tool
        if tool == "pen":
            pts = list(self._draw_points)
            return ("pen", pts) if len(pts) >= 2 else None
        if not self._draw_points:
            return None
        p1 = QPointF(self._draw_points[0])
        p2 = QPointF(self._draw_start)
        if QLineF(p1, p2).length() < 3:
            return None
        return (tool, p1, p2)

    def _push_undo(self):
        self._undo_stack.append(self._canvas.copy())
        if len(self._undo_stack) > _UNDO_LIMIT:
            self._undo_stack.pop(0)

    def _commit_shape(self, shape):
        self._push_undo()
        painter = QPainter(self._canvas)
        dpr = self._canvas.devicePixelRatio() or 1.0
        painter.scale(dpr, dpr)
        self._draw_shape(painter, shape)
        painter.end()
        self.update()

    @staticmethod
    def _setup_pen(painter: QPainter):
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(_DRAW_COLOR, _DRAW_WIDTH))
        painter.setBrush(Qt.NoBrush)

    @staticmethod
    def _draw_shape(painter: QPainter, shape):
        """形状绘制（选区内逻辑坐标系，(0,0)=选区左上）。"""
        ScreenshotOverlay._setup_pen(painter)
        kind = shape[0]
        if kind == "rect":
            painter.drawRect(QRectF(shape[1], shape[2]).normalized())
        elif kind == "ellipse":
            painter.drawEllipse(QRectF(shape[1], shape[2]).normalized())
        elif kind == "arrow":
            p1, p2 = shape[1], shape[2]
            line = QLineF(p1, p2)
            painter.drawLine(line)
            # 箭头两翼：从来路方向两侧各偏 30°
            import math
            angle = math.radians(line.angle())
            length = line.length()
            if length > 0.1:
                ux, uy = -math.cos(angle), math.sin(angle)
                for side in (1.0, -1.0):
                    wx = p2.x() - ux * 10 + (-uy) * 6 * side
                    wy = p2.y() - uy * 10 + ux * 6 * side
                    painter.drawLine(QLineF(p2, QPointF(wx, wy)))
        elif kind == "pen":
            pts = shape[1]
            painter.drawPolyline([QPointF(p) for p in pts])
        elif kind == "text":
            pos, text = shape[1], shape[2]
            font = QFont()
            font.setPointSize(_TEXT_SIZE)
            painter.setFont(font)
            painter.drawText(pos, text)

    def _begin_text(self, local: QPointF):
        if self._text_edit is not None:
            self._text_edit.deleteLater()
            self._text_edit = None
        self._text_pos = local
        edit = QLineEdit(self)
        edit.setPlaceholderText("输入文字，回车确认")
        edit.setFixedWidth(max(140, int(self._rect.width() * 0.5)))
        edit.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint)
        edit.move(int(self._rect.x() + local.x()),
                  int(self._rect.y() + local.y()))
        edit.returnPressed.connect(lambda: self._commit_text(edit))
        edit.show()
        edit.raise_()
        edit.setFocus()
        self._text_edit = edit

    def _commit_text(self, edit: QLineEdit):
        text = (edit.text() or "").strip()
        edit.deleteLater()
        self._text_edit = None
        if text and self._text_pos is not None:
            self._commit_shape(("text", QPointF(self._text_pos), text))

    def _undo(self, *_):
        if not self._undo_stack:
            return
        self._canvas = self._undo_stack.pop()
        self.update()

    def _copy_to_clipboard(self, *_):
        if self._canvas is not None:
            QApplication.clipboard().setPixmap(
                QPixmap.fromImage(self._canvas))

    def _ocr(self, *_):
        """成图落临时 png 交主窗口走离线识别；本遮罩随即收尾。"""
        if self._canvas is None:
            return
        fd, path = tempfile.mkstemp(prefix="fchat_shot_", suffix=".png")
        os.close(fd)
        if not self._canvas.save(path, "PNG"):
            try:
                os.remove(path)
            except OSError:
                pass
            return
        self._emit_closed_on_close = False
        self.ocr_requested.emit(path)
        self.closed.emit()
        self.close()
        self.deleteLater()

    def _done(self, *_):
        if self._canvas is not None:
            self._emit_closed_on_close = False
            self.selected.emit(self._canvas)
            self.closed.emit()
            self.close()
            self.deleteLater()

    def _cancel(self, *_):
        self.cancelled.emit()
        self._finish()

    # ------------------------------------------------------------------ #
    # 工具条
    # ------------------------------------------------------------------ #
    def _build_toolbar(self):
        bar = QFrame(self)
        bar.setObjectName("ShotToolbar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(2)
        self._tool_btns = {}
        for name in ("rect", "ellipse", "arrow", "pen", "text"):
            btn = self._tool_button(bar, name)
            btn.setCheckable(True)
            btn.setChecked(name == self._tool)
            btn.clicked.connect(lambda _=False, n=name: self._set_tool(n))
            lay.addWidget(btn)
            self._tool_btns[name] = btn
        undo_btn = self._tool_button(bar, "undo")
        undo_btn.clicked.connect(self._undo)
        lay.addWidget(undo_btn)
        lay.addSpacing(6)
        copy_btn = self._tool_button(bar, "copy")
        copy_btn.setToolTip("复制到剪贴板")
        copy_btn.clicked.connect(self._copy_to_clipboard)
        lay.addWidget(copy_btn)
        ocr_btn = self._tool_button(bar, "ocr")
        ocr_btn.setToolTip("识别文字")
        ocr_btn.clicked.connect(self._ocr)
        lay.addWidget(ocr_btn)
        lay.addSpacing(6)
        done_btn = self._tool_button(bar, "check")
        done_btn.setToolTip("完成")
        done_btn.clicked.connect(self._done)
        lay.addWidget(done_btn)
        cancel_btn = self._tool_button(bar, "close")
        cancel_btn.setToolTip("取消")
        cancel_btn.clicked.connect(self._cancel)
        lay.addWidget(cancel_btn)
        self._toolbar = bar
        bar.adjustSize()

    def _tool_button(self, parent, name: str) -> QToolButton:
        btn = QToolButton(parent)
        btn.setIcon(icon(name))
        btn.setAutoRaise(True)
        btn.setFixedSize(28, 28)
        btn.setIconSize(btn.size() * 0.6)
        return btn

    def _set_tool(self, name: str):
        self._tool = name
        for n, btn in self._tool_btns.items():
            btn.setChecked(n == name)

    def _position_toolbar(self):
        bar = self._toolbar
        if bar is None:
            return
        x = self._rect.x() + self._rect.width() - bar.width()
        x = max(4, min(x, self.width() - bar.width() - 4))
        y = self._rect.y() + self._rect.height() + 4
        if y + bar.height() > self.height() - 4:
            y = max(4, self._rect.y() - bar.height() - 4)
        bar.move(x, y)
        bar.show()
        bar.raise_()

    # ------------------------------------------------------------------ #
    # 坐标换算
    # ------------------------------------------------------------------ #
    def _device_rect(self, logical: QRect) -> QRect:
        """把逻辑坐标矩形换算成 _base 的设备像素矩形。

        grabWindow 在 Windows 高分屏缩放（DPR>1）时返回设备像素尺寸的
        pixmap，而鼠标坐标是逻辑像素；直接 copy(逻辑rect) 会裁剪错位，
        必须按 devicePixelRatio 放大后再取。
        """
        dpr = self._base.devicePixelRatio() or 1.0
        return QRect(
            int(logical.x() * dpr),
            int(logical.y() * dpr),
            int(logical.width() * dpr),
            int(logical.height() * dpr),
        )

    # ------------------------------------------------------------------ #
    # 绘制
    # ------------------------------------------------------------------ #
    def paintEvent(self, event):
        painter = QPainter(self)
        # 全屏基图
        painter.drawPixmap(self.rect(), self._base)
        # 区域外压暗
        painter.fillRect(self.rect(), QColor(0, 0, 0, 120))
        if self._rect.isNull() or self._rect.isEmpty():
            painter.end()
            return
        if self._mode == "annotate" and self._canvas is not None:
            # 标注后的成图（设备像素 → 逻辑矩形）
            painter.drawPixmap(self._rect,
                               QPixmap.fromImage(self._canvas))
        else:
            src = self._rect.intersected(self.rect())
            if not src.isNull() and not src.isEmpty():
                sub = self._base.copy(self._device_rect(src))
                painter.drawPixmap(src, sub)
        # 高亮边框
        pen = QPen(QColor(0, 160, 255, 230), 2)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(self._rect)
        # 进行中的形状预览
        if self._mode == "annotate" and self._drawing:
            shape = self._current_shape()
            if shape is not None:
                painter.save()
                painter.translate(self._rect.topLeft())
                self._draw_shape(painter, shape)
                painter.restore()
        painter.end()
