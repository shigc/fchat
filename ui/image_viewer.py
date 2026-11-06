"""图片查看器：原图查看 + 前后翻页 / 滚轮缩放 / 拖动 / 旋转 / 另存为。

半透明黑色遮罩铺满屏幕，图片居中显示并保持比例。取图顺序不变：
磁盘缓存原图优先（服务器会清理历史文件，重拉 404），未命中才垫缩略图
并走网络。左右方向键或顶部按钮在同房间图片间翻页；滚轮缩放后按住左键
拖动平移，点图片以外的遮罩区域或 Esc 关闭。
"""
from __future__ import annotations

import logging
import os

from PyQt5.QtCore import QPoint, QPointF, QRect, Qt, QUrl, pyqtSignal
from PyQt5.QtGui import QColor, QKeyEvent, QPainter, QPixmap, QTransform
from PyQt5.QtWidgets import (QApplication, QDialog, QFileDialog, QHBoxLayout,
                             QLabel, QPushButton, QVBoxLayout, QWidget)
from PyQt5.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from core.url_guard import can_carry_credentials

log = logging.getLogger(__name__)

_ZOOM_MIN = 0.1
_ZOOM_MAX = 8.0
_ZOOM_STEP = 1.15

_BTN_QSS = (
    "QPushButton{background:rgba(255,255,255,0.14);color:#eee;border:none;"
    "border-radius:6px;padding:5px 10px;font-size:13px;}"
    "QPushButton:hover{background:rgba(255,255,255,0.28);}"
    "QPushButton:disabled{color:#777;background:rgba(255,255,255,0.06);}")


class ImageViewer(QDialog):
    """全屏半透明遮罩图片查看器。"""

    # 查看器自己不做识别（core.ocr 要在后台线程跑），交给主窗口接线
    ocr_requested = pyqtSignal(str)      # 当前图片 URL

    def __init__(self, url: str, image_loader=None, parent=None, *,
                 urls=None, index: int = 0):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground)
        # 每次查看都新建一个铺满屏幕的顶层窗口，关闭即销毁避免常驻
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._pixmap = None
        self._scaled = None
        self._hint = ""
        self._image_loader = image_loader
        self._nam = None
        self._reply = None
        self._placeholder = False
        self._zoom = 1.0
        self._rotation = 0
        self._pan = QPointF(0, 0)
        self._drag_from = None
        # 同房间全部图片 URL：前后键翻页；单图调用方只传 url 也能工作
        self._urls = [u for u in (urls or []) if u]
        if not self._urls:
            self._urls = [url] if url else []
        self._index = max(0, min(int(index), len(self._urls) - 1))
        self._url = self._urls[self._index] if self._urls else ""

        self._label = QLabel()
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setStyleSheet("color:#ccc;font-size:14px;")
        self._label.setText("图片加载中…")

        self._toolbar = self._build_toolbar()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._toolbar)
        layout.addWidget(self._label, 1)

        self._load_current()

    # ------------------------------------------------------------------ #
    # 顶部工具条
    # ------------------------------------------------------------------ #
    def _button(self, text: str, slot) -> QPushButton:
        btn = QPushButton(text, self._toolbar)
        btn.setStyleSheet(_BTN_QSS)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFocusPolicy(Qt.NoFocus)
        # clicked(bool) 连零参方法在 Cython 编译版会 TypeError，统一吞参数
        btn.clicked.connect(lambda _=False: slot())
        return btn

    def _build_toolbar(self) -> QWidget:
        bar = QWidget(self)
        self._toolbar = bar
        bar.setObjectName("ViewerToolbar")
        bar.setAttribute(Qt.WA_StyledBackground, True)
        bar.setStyleSheet(
            "QWidget#ViewerToolbar{background:rgba(0,0,0,110);}")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(6)
        self._prev_btn = self._button("‹", self.show_prev)
        self._next_btn = self._button("›", self.show_next)
        self._index_label = QLabel("", bar)
        self._index_label.setStyleSheet("color:#ddd;font-size:13px;")
        self._zoom_label = QLabel("100%", bar)
        self._zoom_label.setStyleSheet("color:#ddd;font-size:13px;")
        self._zoom_out_btn = self._button("－", self.zoom_out)
        self._zoom_in_btn = self._button("＋", self.zoom_in)
        self._fit_btn = self._button("适应窗口", self.reset_view)
        self._rotate_btn = self._button("旋转", self.rotate_image)
        self._ocr_btn = self._button("提取文字", self._request_ocr)
        self._save_btn = self._button("另存为", self.save_as)
        self._close_btn = self._button("✕", self.close)
        lay.addWidget(self._prev_btn)
        lay.addWidget(self._index_label)
        lay.addWidget(self._next_btn)
        lay.addStretch(1)
        lay.addWidget(self._zoom_out_btn)
        lay.addWidget(self._zoom_label)
        lay.addWidget(self._zoom_in_btn)
        lay.addWidget(self._fit_btn)
        lay.addWidget(self._rotate_btn)
        lay.addWidget(self._ocr_btn)
        lay.addWidget(self._save_btn)
        lay.addWidget(self._close_btn)
        self._sync_toolbar()
        return bar

    def _request_ocr(self):
        if not self._url:
            self._show_hint("没有可识别的图片地址")
            return
        self._show_hint("正在本机识别文字…")
        self.ocr_requested.emit(self._url)

    def clear_ocr_hint(self):
        """识别流程收尾时撤掉「正在识别」提示：结果窗弹在查看器之上，
        底下这句旧提示不撤会一直挂着（真机表现为识别完仍显示识别中）。"""
        if self._hint:
            self._hint = ""
            self.update()

    def _sync_toolbar(self):
        multi = len(self._urls) > 1
        for w in (self._prev_btn, self._next_btn, self._index_label):
            w.setVisible(multi)
        if multi:
            self._index_label.setText(
                f"{self._index + 1} / {len(self._urls)}")
        self._zoom_label.setText(f"{int(round(self._zoom * 100))}%")

    # ------------------------------------------------------------------ #
    # 加载
    # ------------------------------------------------------------------ #
    def _reset_view_state(self):
        self._cancel_load()
        self._pixmap = None
        self._scaled = None
        self._hint = ""
        self._placeholder = False
        self._zoom = 1.0
        self._rotation = 0
        self._pan = QPointF(0, 0)
        self._label.setText("图片加载中…")
        self._label.show()
        self.update()

    def _cancel_load(self):
        reply = self._reply
        self._reply = None
        if reply is not None:
            try:
                # 先断开再 abort：否则取消会触发 finished，被当成加载失败
                reply.finished.disconnect(self._on_finished)
            except (TypeError, RuntimeError):
                pass
            try:
                reply.abort()
                reply.deleteLater()
            except RuntimeError:
                # 请求已随窗口/管理器销毁
                pass

    def _load_current(self):
        self._reset_view_state()
        self._sync_toolbar()
        # 磁盘缓存里的原图优先：存的就是当年下载的原始字节（服务器后来
        # 会清理历史文件，带认证重拉 404 实测），命中即原图，且不发请求。
        # 未命中才垫缩略图 + 网络拉原图。
        original = None
        if self._image_loader:
            original = self._image_loader.cached_original(self._url)
        if original is not None:
            self._set_pixmap(original)
            return
        placeholder = None
        if self._image_loader:
            placeholder = self._image_loader.cached_pixmap(self._url)
        if placeholder is not None:
            self._set_pixmap(placeholder, placeholder=True)
        self._start_load()

    def _start_load(self):
        """加载原图：气泡缓存里只有缩小版 pixmap，直接走网络拿原图。"""
        url = self._url
        if url.startswith("/") and self._image_loader:
            url = self._image_loader.resolve(url)
        # 翻页会反复加载：复用同一个 manager，避免每翻一次积累一个
        if self._nam is None:
            self._nam = QNetworkAccessManager(self)
            self._nam.finished.connect(self._on_finished)
        req = QNetworkRequest(QUrl(url))
        req.setTransferTimeout(15000)
        # 认证头只给同源地址：图片 URL 来自消息/服务器，可以是任意主机
        server = getattr(self._image_loader, "server_url", "") \
            if self._image_loader else ""
        if self._image_loader and can_carry_credentials(url, server):
            if getattr(self._image_loader, "auth_token", ""):
                req.setRawHeader(b"X-Auth-Token",
                                 self._image_loader.auth_token.encode())
            if getattr(self._image_loader, "auth_uid", ""):
                req.setRawHeader(b"X-User-Id",
                                 self._image_loader.auth_uid.encode())
        self._reply = self._nam.get(req)

    def _on_finished(self, reply: QNetworkReply):
        # 翻页后旧请求才回来：丢弃，不能把当前图片覆盖成上一张
        if self._reply is not None and reply is not self._reply:
            reply.deleteLater()
            return
        self._reply = None
        err = reply.error()
        if err != QNetworkReply.NoError:
            reply.deleteLater()
            log.warning("原图加载失败 %s：%s", self._url, reply.errorString())
            # 已垫了缩略图时不再整屏覆盖成"失败"，但必须在画面上标一句——
            # 缩略图放大后的模糊会被当成"原图就这质量"，无从排障
            if self._pixmap is None:
                self._label.setText("图片加载失败")
            else:
                self._show_hint("原图加载失败（服务器文件可能已被清理）")
            return
        data = reply.readAll()
        reply.deleteLater()
        pm = QPixmap()
        if pm.loadFromData(bytes(data)) and not pm.isNull():
            self._set_pixmap(pm)
        elif self._pixmap is None:
            self._label.setText("图片加载失败")
        else:
            self._show_hint("原图加载失败（数据无法解码）")

    # ------------------------------------------------------------------ #
    # 显示尺寸 / 缩放 / 旋转
    # ------------------------------------------------------------------ #
    def _set_pixmap(self, pm: QPixmap, placeholder: bool = False):
        """设置当前图并按缩放状态重算显示尺寸（含 HiDPI，保证清晰）。"""
        self._pixmap = pm
        self._placeholder = placeholder
        self._hint = ""
        # 一旦有图可看就撤掉"加载中/失败"的文字，避免盖在图上
        self._label.hide()
        self._rebuild_scaled()
        # QDialog 默认居中于父窗口，必须显式铺满屏幕，否则遮罩和图片都会偏
        self.setGeometry(QApplication.primaryScreen().availableGeometry())
        self.update()

    def _rebuild_scaled(self):
        if self._pixmap is None or self._pixmap.isNull():
            self._scaled = None
            return
        pm = self._pixmap
        if self._rotation:
            pm = pm.transformed(QTransform().rotate(self._rotation),
                                Qt.SmoothTransformation)
        screen = QApplication.primaryScreen().availableGeometry()
        max_w = max(1, int(screen.width() * 0.85))
        max_h = max(1, int(screen.height() * 0.85))
        # 适应窗口：小图也放大到 85% 屏（沿用原有观感）
        k = min(max_w / pm.width(), max_h / pm.height())
        fit_w = max(1, int(pm.width() * k))
        fit_h = max(1, int(pm.height() * k))
        target_w = max(1, int(fit_w * self._zoom))
        target_h = max(1, int(fit_h * self._zoom))
        dpr = self.devicePixelRatioF() or 1.0
        scaled = pm.scaled(int(target_w * dpr), int(target_h * dpr),
                           Qt.KeepAspectRatio, Qt.SmoothTransformation)
        scaled.setDevicePixelRatio(dpr)
        self._scaled = scaled

    def _clamped_pan(self):
        """限制拖动范围：图比视口小的方向永远居中，大的方向不拖出空白。"""
        if not self._scaled:
            return QPoint(0, 0)
        dpr = self._scaled.devicePixelRatio() or 1.0
        w = int(self._scaled.width() / dpr)
        h = int(self._scaled.height() / dpr)
        max_dx = max(0, (w - self.width()) // 2)
        max_dy = max(0, (h - self.height()) // 2)
        dx = int(max(-max_dx, min(self._pan.x(), max_dx)))
        dy = int(max(-max_dy, min(self._pan.y(), max_dy)))
        return QPoint(dx, dy)

    def _image_draw_rect(self) -> QRect:
        if not self._scaled or self._scaled.isNull():
            return QRect()
        dpr = self._scaled.devicePixelRatio() or 1.0
        w = int(self._scaled.width() / dpr)
        h = int(self._scaled.height() / dpr)
        offset = self._clamped_pan()
        x = (self.width() - w) // 2 + offset.x()
        y = (self.height() - h) // 2 + offset.y()
        return QRect(x, y, w, h)

    def zoom_in(self):
        self._zoom_by(_ZOOM_STEP)

    def zoom_out(self):
        self._zoom_by(1 / _ZOOM_STEP)

    def _zoom_by(self, factor: float):
        if self._pixmap is None:
            return
        new_zoom = max(_ZOOM_MIN, min(_ZOOM_MAX, self._zoom * factor))
        if abs(new_zoom - self._zoom) < 1e-6:
            return
        self._zoom = new_zoom
        self._rebuild_scaled()
        self._sync_toolbar()
        self.update()

    def reset_view(self):
        self._zoom = 1.0
        self._rotation = 0
        self._pan = QPointF(0, 0)
        self._rebuild_scaled()
        self._sync_toolbar()
        self.update()

    def rotate_image(self):
        if self._pixmap is None:
            return
        self._rotation = (self._rotation + 90) % 360
        self._pan = QPointF(0, 0)
        self._rebuild_scaled()
        self.update()

    # ------------------------------------------------------------------ #
    # 翻页
    # ------------------------------------------------------------------ #
    def show_prev(self):
        self._switch_to(self._index - 1)

    def show_next(self):
        self._switch_to(self._index + 1)

    def _switch_to(self, index: int):
        if len(self._urls) < 2:
            return
        self._index = index % len(self._urls)
        self._url = self._urls[self._index]
        self._load_current()

    # ------------------------------------------------------------------ #
    # 另存为
    # ------------------------------------------------------------------ #
    def _default_save_name(self) -> str:
        path = QUrl(self._url).path() if self._url else ""
        name = os.path.basename(path).split("?")[0].strip()
        if not name:
            name = "image.png"
        for ch in '\\/:*?"<>|':
            name = name.replace(ch, "_")
        if not os.path.splitext(name)[1]:
            name += ".png"
        return name

    def _best_save_pixmap(self):
        """保存优先用磁盘原图；仍在用缩略图垫底时返回 None 避免存成糊图。"""
        if self._image_loader and self._url:
            original = self._image_loader.cached_original(self._url)
            if original is not None and not original.isNull():
                return original
        if self._placeholder:
            return None
        return self._pixmap

    def save_as(self):
        pm = self._best_save_pixmap()
        if pm is None or pm.isNull():
            self._show_hint("图片尚未加载完成，暂时无法保存")
            self.update()
            return
        pictures = os.path.join(os.path.expanduser("~"), "Pictures")
        start_dir = pictures if os.path.isdir(pictures) else os.path.expanduser("~")
        path, _sel = QFileDialog.getSaveFileName(
            self, "图片另存为", os.path.join(start_dir, self._default_save_name()),
            "图片 (*.png *.jpg *.jpeg *.bmp *.webp);;所有文件 (*)")
        if not path:
            return
        if pm.save(path):
            self._show_hint(f"已保存到 {path}")
        else:
            self._show_hint("保存失败：无法写入该文件")
        self.update()

    # ------------------------------------------------------------------ #
    # 绘制与交互
    # ------------------------------------------------------------------ #
    def showEvent(self, event):
        super().showEvent(event)
        screen = QApplication.primaryScreen().availableGeometry()
        self.setGeometry(screen)
        self.setFocus()

    def _show_hint(self, text: str):
        """底部红字提示，不遮挡画面主体（viewer 失败曾经完全静默）。"""
        self._hint = text
        self.update()

    def paintEvent(self, event):
        """绘制半透明黑色背景 + 居中图片。"""
        painter = QPainter(self)
        # 遮罩别太重：180 的纯黑会显得整屏暗沉，140 仍足以隔开背景
        painter.fillRect(self.rect(), QColor(0, 0, 0, 140))
        if self._scaled and not self._scaled.isNull():
            painter.drawPixmap(self._image_draw_rect().topLeft(), self._scaled)
        if self._hint:
            painter.setPen(QColor(255, 130, 130))
            painter.drawText(QRect(0, self.height() - 44, self.width(), 30),
                             Qt.AlignCenter, self._hint)
        painter.end()

    def wheelEvent(self, event):
        if self._pixmap is None:
            event.ignore()
            return
        delta = event.angleDelta().y()
        if delta:
            self._zoom_by(_ZOOM_STEP if delta > 0 else 1 / _ZOOM_STEP)
        event.accept()

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        # 放大后按住图片拖动平移；点图片以外的遮罩关闭
        if self._zoom > 1.0 and self._image_draw_rect().contains(event.pos()):
            self._drag_from = (event.globalPos(), QPointF(self._pan))
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        self.close()

    def mouseMoveEvent(self, event):
        if self._drag_from is not None and event.buttons() & Qt.LeftButton:
            origin, pan0 = self._drag_from
            delta = event.globalPos() - origin
            self._pan = QPointF(pan0.x() + delta.x(), pan0.y() + delta.y())
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_from is not None:
            self._drag_from = None
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        if key == Qt.Key_Escape:
            self.close()
            return
        if key in (Qt.Key_Left, Qt.Key_Up):
            self.show_prev()
            return
        if key in (Qt.Key_Right, Qt.Key_Down):
            self.show_next()
            return
        if key == Qt.Key_R:
            self.rotate_image()
            return
        if key in (Qt.Key_Plus, Qt.Key_Equal):
            self.zoom_in()
            return
        if key == Qt.Key_Minus:
            self.zoom_out()
            return
        if key == Qt.Key_0:
            self.reset_view()
            return
        if key == Qt.Key_S and event.modifiers() & Qt.ControlModifier:
            self.save_as()
            return
        super().keyPressEvent(event)
