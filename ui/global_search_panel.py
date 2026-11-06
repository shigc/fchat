"""跨房间全局搜索：Ctrl+K 里选「在聊天记录中搜索」后弹出。

按会话列表的最后消息时间取前 50 个房间逐房扫描（客户端字面匹配，
服务端 chat.search 中文不分词搜不到），每房最多 200 条、全局结果
200 条封顶；token 取消（重输入/关窗即废）。结果行点击 → 打开房间
并定位原消息（主窗口接线）。
"""
from __future__ import annotations

import logging
import re

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout, QListWidget, QListWidgetItem, QVBoxLayout, QWidget,
)

from .api_workers import run_async
from .dialog_base import FramelessCardDialog
from .icons import icon
from .widgets import WxLabel, WxLineEdit, _color

log = logging.getLogger(__name__)

_SCAN_PAGE = 100        # 每页条数（与 get_history 分页一致）
_PER_ROOM_CAP = 200     # 每房扫描上限（2 页）
_MAX_ROOMS = 50         # 最多扫前 50 个最近活跃房间
_MAX_ROWS = 200         # 全局结果上限
_FLUSH_MS = 16

# 附件类消息没有正文，摘要用类型词占位
_EMPTY_HINT = {"image": "[图片]", "file": "[文件]", "video": "[视频]",
               "audio": "[语音]", "quote": "[引用]", "merge": "[转发记录]"}


class _ResultRow(QWidget):
    """结果行：房间名 + 发送人/摘要/时间，点击回传 (rid, msg)。"""

    clicked = pyqtSignal(str, object)

    def __init__(self, rid, msg, room_name, theme_fn, parent=None):
        super().__init__(parent)
        self._rid = rid
        self._msg = msg
        self._theme_fn = theme_fn
        self.setCursor(Qt.PointingHandCursor)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(2)

        head = QHBoxLayout()
        head.addWidget(self._label(room_name, 12, "text_secondary",
                                   bold=True), 1)
        head.addWidget(self._label(_fmt_time(msg.timestamp), 11,
                                   "text_secondary"))
        lay.addLayout(head)

        sender = msg.sender.display_name if msg.sender else ""
        text = (msg.text or msg.display_text or msg.file_name
                or _EMPTY_HINT.get(msg.msg_type, ""))
        preview = " ".join(text.split())
        if len(preview) > 80:
            preview = preview[:80] + "…"
        self._preview = self._label(
            f"{sender}：{preview}" if sender else preview, 13,
            "text_primary")
        self._preview.setWordWrap(True)
        lay.addWidget(self._preview)

    def _label(self, text, size, color_key, bold=False):
        lbl = WxLabel(text, None, self)
        lbl.set_size(f"{size}px")
        if bold:
            lbl.set_variant("bold")
        else:
            lbl.set_variant("hint" if size <= 12 else "normal")
        lbl.setStyleSheet(
            f"color:{self._theme_fn(color_key)};background:transparent;")
        return lbl

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self._rid, self._msg)
        super().mousePressEvent(e)


def _fmt_time(ts: float) -> str:
    from .widgets import format_chat_time
    return format_chat_time(ts)


class GlobalSearchPanel(FramelessCardDialog):
    """全局搜索结果面板：locate_requested(rid, msg) 交给主窗口跳转。"""

    locate_requested = pyqtSignal(str, object)
    restart_requested = pyqtSignal(str)   # 面板内改词重扫（主窗口注入会话表）

    def __init__(self, theme=None, parent=None):
        super().__init__("全局搜索", theme, parent, width=560)
        self.setModal(False)

        self._client = None
        self._token = 0
        self._rx = None
        self._queue = []
        self._room_names = {}
        self._room_types = {}
        self._current_rid = None
        self._room_offset = 0
        self._room_scanned = 0
        self._scanned_rooms = 0
        self._scanned_msgs = 0
        self._hits = 0
        self._rows = 0
        self._pending = []

        search_row = QHBoxLayout()
        self.search = WxLineEdit(theme, self)
        self.search.setPlaceholderText("输入关键词，回车重新扫描…")
        self.search.returnPressed.connect(self._on_search)
        search_row.addWidget(self.search, 1)
        self.stop_btn = WxLabel("停止", theme, self)
        self.stop_btn.setCursor(Qt.PointingHandCursor)
        self.stop_btn.setStyleSheet("color:#d9534f;")
        self.stop_btn.setVisible(False)
        self.stop_btn.mousePressEvent = lambda e: self._stop()
        search_row.addWidget(self.stop_btn)
        self._layout.addLayout(search_row)

        self.list = QListWidget(self)
        self.list.setObjectName("GlobalSearchList")
        self.list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setMinimumHeight(380)
        self._layout.addWidget(self.list)

        self.status = WxLabel("", theme, self)
        self.status.set_variant("hint")
        self._layout.addWidget(self.status)
        self._theme_widgets.extend([self.search, self.status])
        self._apply_list_style()

        # 结果分批插入：扫描回调可能一次带回几十条，逐条塞列表会卡帧
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(_FLUSH_MS)
        self._flush_timer.timeout.connect(self._flush_rows)

    def _apply_list_style(self):
        bg = _color(self._theme, "conv_bg", "#ffffff")
        border = _color(self._theme, "border", "#e7e7e7")
        hover = _color(self._theme, "conv_hover", "#f2f2f2")
        text = _color(self._theme, "text_primary", "#191919")
        self.list.setStyleSheet(
            f"QListWidget{{background:{bg};border:1px solid {border};"
            f"border-radius:6px;outline:none;color:{text};}}"
            f"QListWidget::item{{border-bottom:1px solid {border};}}"
            f"QListWidget::item:selected{{background:{hover};}}")

    def set_theme(self, theme):
        super().set_theme(theme)
        self._theme = theme
        self._apply_list_style()

    # ------------------------------------------------------------------ #
    # 扫描
    # ------------------------------------------------------------------ #
    def start_scan(self, keyword: str, conversations, client):
        self._client = client
        self.search.setText(keyword)
        try:
            self._rx = re.compile(re.escape((keyword or "").strip()),
                                  re.IGNORECASE)
        except re.error:
            self._rx = None
        convs = [c for c in (conversations or []) if c.id]
        convs.sort(key=lambda c: -c.last_message_time)
        convs = convs[:_MAX_ROOMS]
        self._queue = [c.id for c in convs]
        self._room_names = {c.id: (c.name or c.username or "?")
                            for c in convs}
        self._room_types = {c.id: c.room_type for c in convs}
        self._token += 1
        self._current_rid = None
        self._scanned_rooms = 0
        self._scanned_msgs = 0
        self._hits = 0
        self._rows = 0
        self._pending = []
        self.list.clear()
        self._flush_timer.stop()
        if not self._rx or not self._queue or client is None:
            self.status.setText("没有可扫描的会话" if not self._queue
                                else "请输入关键词")
            return
        self.stop_btn.setVisible(True)
        self._update_status(finished=False)
        self._scan_next_room()

    def _on_search(self):
        # start_scan 需要会话列表（主窗口持有），交回主窗口重跑
        self.restart_requested.emit(self.search.text().strip())

    def _stop(self, *_, **__):
        self._token += 1
        self.stop_btn.setVisible(False)
        text = self.status.text()
        if text and "已停止" not in text:
            self.status.setText(text + "（已停止）")

    def closeEvent(self, event):
        self._token += 1   # 关窗即废：迟到回调一律丢弃
        self._flush_timer.stop()
        super().closeEvent(event)

    def _scan_next_room(self):
        token = self._token
        if not self._queue:
            self.stop_btn.setVisible(False)
            self._update_status(finished=True)
            if self._rows == 0 and not self._pending:
                self.list.addItem(QListWidgetItem("未找到匹配的消息"))
            return
        rid = self._queue.pop(0)
        self._current_rid = rid
        self._room_offset = 0
        self._room_scanned = 0
        self._scan_page()

    def _scan_page(self):
        token = self._token
        rid = self._current_rid
        client = self._client
        if rid is None or client is None or token != self._token:
            return
        offset = self._room_offset
        room_type = self._room_types.get(rid)
        rx = self._rx

        def _proc():
            msgs = client.get_history(rid, count=_SCAN_PAGE, offset=offset,
                                      room_type=room_type)
            hits = [m for m in msgs if _match(rx, m)]
            return hits, len(msgs)

        def _done(res):
            try:
                if token != self._token or rid != self._current_rid:
                    return
                hits, page_len = res
                self._scanned_msgs += page_len
                self._room_scanned += page_len
                self._hits += len(hits)
                for m in hits:
                    if self._rows >= _MAX_ROWS:
                        break
                    self._pending.append((rid, m))
                if not self._flush_timer.isActive():
                    self._flush_timer.start()
                self._update_status(finished=False)
                cap_reached = (page_len < _SCAN_PAGE
                               or self._room_scanned >= _PER_ROOM_CAP
                               or self._rows >= _MAX_ROWS)
                if cap_reached:
                    self._scanned_rooms += 1
                    self._current_rid = None
                    if self._rows >= _MAX_ROWS:
                        self._queue = []
                    self._scan_next_room()
                else:
                    self._room_offset += page_len
                    QTimer.singleShot(0, self._scan_page)
            except Exception:  # noqa: BLE001
                log.exception("全局扫描回调失败")

        def _fail(err):
            if token != self._token or rid != self._current_rid:
                return
            log.warning("全局扫描房间 %s 失败：%s", rid, err)
            self._scanned_rooms += 1
            self._current_rid = None
            self._scan_next_room()

        run_async(_proc, on_done=_done, on_fail=_fail, parent=self)

    def _update_status(self, finished: bool):
        text = f"已扫描 {self._scanned_rooms}/{self._scanned_rooms + len(self._queue)} 房 · 命中 {self._hits} 条"
        if self._rows >= _MAX_ROWS:
            text += f"（仅显示前 {_MAX_ROWS} 条）"
        elif finished:
            text = (f"已扫描全部 {self._scanned_rooms} 房 · "
                    f"命中 {self._hits} 条")
        self.status.setText(text)

    # ------------------------------------------------------------------ #
    # 结果渲染
    # ------------------------------------------------------------------ #
    def _flush_rows(self):
        batch = self._pending[:30]
        del self._pending[:30]
        theme_fn = lambda key: _color(self._theme, key, "#666666")
        for rid, msg in batch:
            row = _ResultRow(rid, msg, self._room_names.get(rid, "?"),
                             theme_fn)
            row.clicked.connect(self._on_row)
            item = QListWidgetItem(self.list)
            item.setSizeHint(row.sizeHint())
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
            self._rows += 1
        if not self._pending:
            self._flush_timer.stop()

    def _on_row(self, rid, msg):
        self.accept()
        self.locate_requested.emit(rid, msg)


def _match(rx, msg):
    if rx is None:
        return False
    text = (msg.text or msg.display_text or "")
    if rx.search(text[:5000]):
        return True
    return bool(msg.file_name and rx.search(msg.file_name[:200]))
