"""系统托盘：关闭到托盘、未读闪烁、悬停未读预览卡片。

设计约束（Windows / PyQt5 5.15）：

- 卡片不用 ``Qt.Popup``：popup 会抓鼠标，光标从图标移向卡片途中就会被
  关掉；用普通 Tool 顶层窗 + ``WA_ShowWithoutActivating``，不抢焦点。
- 悬停检测轮询 ``QSystemTrayIcon.geometry()``（Windows 由
  ``Shell_NotifyIconGetRect`` 实现）。Win11 图标收进「^」溢出区时该
  geometry 为空，此时卡片不可用，降级为 tooltip + 点击 + 右键菜单。
- 托盘图标作为 ``QApplication`` 的 windowIcon，红点用 QPainter 现画，
  不引入图片资源（项目约定）。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Iterable, List, Sequence, Tuple

from PyQt5.QtCore import QObject, QPointF, QRect, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import (
    QColor, QFont, QFontMetrics, QIcon, QPainter, QPen, QPixmap,
)
from PyQt5.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMenu, QSystemTrayIcon,
    QVBoxLayout, QWidget,
)

from .widgets import format_timestamp

_CARD_WIDTH = 320
_ROW_HEIGHT = 54
_MAX_ROWS = 6
_DOT_SIZES = (16, 24, 32, 48, 64)


@dataclass
class TrayRow:
    """卡片中的一行：单条未读消息，或某房间的未读汇总。"""

    rid: str = ""
    msg_id: str = ""
    room: str = ""
    sender: str = ""
    text: str = ""
    ts: float = 0.0
    count: int = 1


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())


def _alert_get(alert, key: str, default=""):
    if isinstance(alert, dict):
        return alert.get(key, default)
    return getattr(alert, key, default)


def _conv_get(conv, key: str, default=""):
    if isinstance(conv, dict):
        return conv.get(key, default)
    return getattr(conv, key, default)


def build_tray_rows(alerts: Iterable, conversations: Iterable,
                    active_rid: str = "",
                    limit: int = _MAX_ROWS) -> Tuple[List[TrayRow], int]:
    """把实时提醒 + 会话未读数整理成卡片行。

    实时提醒逐条展示（按时间倒序、按 msg id 去重）；未被提醒覆盖但
    unread > 0 的会话补一条汇总行。当前正在看的会话不展示。
    返回 ``(rows, overflow)``，overflow 是没放进卡片的未读数。
    """
    active_rid = active_rid or ""
    alerts = list(alerts or ())[::-1]      # 入队顺序旧->新，倒过来让同秒新消息在前
    alerts.sort(key=lambda a: float(_alert_get(a, "ts", 0) or 0), reverse=True)
    conversations = list(conversations or ())
    # 免打扰房间既不进卡片也不计入未读合计（unread 本身仍在主窗口累加）
    muted = set()
    for conv in conversations:
        rid = str(_conv_get(conv, "id", "") or "")
        if rid and bool(_conv_get(conv, "muted", False)):
            muted.add(rid)

    unique: List = []
    seen_ids = set()
    alert_counts = {}
    for a in alerts:
        rid = str(_alert_get(a, "rid", "") or "")
        if not rid or rid == active_rid or rid in muted:
            continue
        msg_id = str(_alert_get(a, "msg_id", "") or "")
        if msg_id:
            if msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
        unique.append(a)
        alert_counts[rid] = alert_counts.get(rid, 0) + 1

    rows: List[TrayRow] = []
    covered = set()
    for a in unique:
        if len(rows) >= limit:
            break
        rid = str(_alert_get(a, "rid", "") or "")
        rows.append(TrayRow(
            rid=rid,
            msg_id=str(_alert_get(a, "msg_id", "") or ""),
            room=_one_line(_alert_get(a, "room", "") or ""),
            sender=_one_line(_alert_get(a, "sender", "") or ""),
            text=_one_line(_alert_get(a, "text", "") or ""),
            ts=float(_alert_get(a, "ts", 0) or 0),
            count=1,
        ))
        covered.add(rid)

    # 房间未读数：会话里的 unread 与实时提醒条数取大者（重启后只有
    # unread、没有消息体，只能靠 last_message_text 汇总）
    unread = {}
    fallbacks = []
    for conv in conversations:
        rid = str(_conv_get(conv, "id", "") or "")
        if not rid or rid == active_rid or rid in muted:
            continue
        n = int(_conv_get(conv, "unread", 0) or 0)
        if n <= 0:
            continue
        unread[rid] = max(unread.get(rid, 0), n)
        if rid not in covered:
            fallbacks.append(conv)
    for rid, n in alert_counts.items():
        unread[rid] = max(unread.get(rid, 0), n)

    fallbacks.sort(
        key=lambda c: float(_conv_get(c, "last_message_time", 0) or 0),
        reverse=True)
    for conv in fallbacks:
        if len(rows) >= limit:
            break
        rid = str(_conv_get(conv, "id", "") or "")
        rows.append(TrayRow(
            rid=rid,
            msg_id="",
            room=_one_line(_conv_get(conv, "name", "") or ""),
            sender="",
            text=_one_line(_conv_get(conv, "last_message_text", "") or ""),
            ts=float(_conv_get(conv, "last_message_time", 0) or 0),
            count=max(1, int(_conv_get(conv, "unread", 0) or 0)),
        ))
        covered.add(rid)

    shown = sum(max(1, r.count) for r in rows)
    overflow = max(0, sum(unread.values()) - shown)
    return rows, overflow


class _TrayRowWidget(QWidget):
    """卡片里可点击的一行；所有 QLabel 鼠标穿透，避免首击被吞。"""

    activated = pyqtSignal(str, str)

    def __init__(self, row: TrayRow, theme, parent=None):
        super().__init__(parent)
        self._row = row
        self._theme = theme
        self._hovered = False
        self.setFixedHeight(_ROW_HEIGHT)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(row.text)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(2)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        self._title = QLabel(self)
        self._time = QLabel(self)
        self._preview = QLabel(self)
        for lb in (self._title, self._time, self._preview):
            lb.setTextFormat(Qt.PlainText)
            lb.setWordWrap(False)
            lb.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._title.setFont(self._bold_font(self._title.font()))
        tfont = QFont(self._time.font())
        tfont.setPointSizeF(max(7.5, tfont.pointSizeF() - 1))
        self._time.setFont(tfont)
        top.addWidget(self._title, 1)
        top.addWidget(self._time, 0)
        lay.addLayout(top)
        lay.addWidget(self._preview)

        title = row.room or "新会话"
        if row.sender:
            title += f" · {row.sender}"
        if row.count > 1:
            title += f" ({row.count})"
        # 卡片只有 320px 宽，用会话列表那套紧凑格式（今天/昨天/周几），
        # format_chat_time 带完整日期会把房间名挤到省略
        time_text = format_timestamp(row.ts)
        preview = row.text or "新消息"

        # 手动省略：QLabel 不会自己截断，超长消息会撑破卡片
        avail_title = (_CARD_WIDTH - 40) - QFontMetrics(self._time.font()) \
            .horizontalAdvance(time_text) - 8
        self._title.setText(QFontMetrics(self._title.font()).elidedText(
            title, Qt.ElideRight, max(40, avail_title)))
        self._time.setText(time_text)
        self._preview.setText(QFontMetrics(self._preview.font()).elidedText(
            preview, Qt.ElideRight, _CARD_WIDTH - 40))
        self.set_theme(theme)

    @staticmethod
    def _bold_font(font: QFont) -> QFont:
        f = QFont(font)
        f.setBold(True)
        return f

    def set_theme(self, theme):
        self._theme = theme
        if theme is None:
            return
        self._title.setStyleSheet(
            f"color:{theme.color('text_primary', '#191919')};")
        self._time.setStyleSheet(
            f"color:{theme.color('text_tertiary', '#666666')};")
        self._preview.setStyleSheet(
            f"color:{theme.color('text_secondary', '#999999')};")
        self.update()

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        if self._hovered and self._theme is not None:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(self._theme.color("conv_hover", "#eef3f4")))
            p.drawRoundedRect(self.rect(), 6, 6)
        super().paintEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton \
                and self.rect().contains(event.pos()):
            self.activated.emit(self._row.rid, self._row.msg_id)
        super().mouseReleaseEvent(event)


class TrayPreviewCard(QWidget):
    """悬停托盘图标时弹出的未读预览卡片（不抢焦点、不失活自关）。"""

    row_activated = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(
            None,
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setObjectName("TrayPreviewCard")
        self.setFixedWidth(_CARD_WIDTH)
        self._theme = None
        self._rows: List[TrayRow] = []
        self._overflow = 0
        self._row_widgets: List[_TrayRowWidget] = []

        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(14, 12, 14, 10)
        self._root.setSpacing(2)
        self._header = QLabel("未读消息", self)
        self._header.setTextFormat(Qt.PlainText)
        self._header.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._header.setFont(self._bold(self._header.font()))
        self._root.addWidget(self._header)
        self._body = QVBoxLayout()
        self._body.setContentsMargins(0, 4, 0, 0)
        self._body.setSpacing(0)
        self._root.addLayout(self._body)
        self._footer = QLabel("", self)
        self._footer.setTextFormat(Qt.PlainText)
        self._footer.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        font = QFont(self._footer.font())
        font.setPointSizeF(max(7.5, font.pointSizeF() - 1))
        self._footer.setFont(font)
        self._footer.hide()
        self._root.addWidget(self._footer)
        self.hide()

    @staticmethod
    def _bold(font: QFont) -> QFont:
        f = QFont(font)
        f.setBold(True)
        return f

    # ---------------- 主题 ---------------- #
    def set_theme(self, theme):
        self._theme = theme
        if theme is None:
            return
        self._header.setStyleSheet(
            f"color:{theme.color('text_primary', '#191919')};")
        self._footer.setStyleSheet(
            f"color:{theme.color('text_secondary', '#999999')};")
        for w in self._row_widgets:
            w.set_theme(theme)
        self.update()

    # ---------------- 内容 ---------------- #
    def set_rows(self, rows: Sequence[TrayRow], overflow: int = 0):
        self._rows = list(rows or ())
        self._overflow = int(overflow or 0)
        self._rebuild()
        if not self._rows:
            self.hide()

    def _rebuild(self):
        for w in self._row_widgets:
            self._body.removeWidget(w)
            w.deleteLater()
        self._row_widgets = []
        for row in self._rows:
            w = _TrayRowWidget(row, self._theme, self)
            w.activated.connect(self._on_row_activated)
            self._body.addWidget(w)
            self._row_widgets.append(w)
        if self._overflow > 0:
            self._footer.setText(f"还有 {self._overflow} 条未读")
            self._footer.show()
        else:
            self._footer.hide()
            self._footer.setText("")
        self.adjustSize()

    def _on_row_activated(self, rid: str, msg_id: str):
        self.hide()
        self.row_activated.emit(rid, msg_id)

    # ---------------- 定位 / 显隐 ---------------- #
    def show_near(self, anchor: QRect):
        """优先显示在图标正上方，空间不足翻到下方；钳在图标所在屏幕内。"""
        if not self._rows:
            self.hide()
            return
        self.adjustSize()
        app = QApplication.instance()
        screen = app.screenAt(anchor.center()) if app is not None else None
        if screen is None and app is not None:
            screen = app.primaryScreen()
        geo = screen.availableGeometry() if screen is not None \
            else QRect(0, 0, 1920, 1080)
        w, h = self.width(), self.height()
        x = anchor.center().x() - w // 2
        y = anchor.top() - h - 8
        if y < geo.top():
            y = anchor.bottom() + 8
        x = min(max(geo.left(), x), geo.right() - w + 1)
        y = min(max(geo.top(), y), geo.bottom() - h + 1)
        self.move(x, y)
        self.show()
        self.raise_()

    def paintEvent(self, event):
        if self._theme is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(1, 1, -1, -1)
        p.setBrush(QColor(self._theme.color("input_bg", "#ffffff")))
        p.setPen(QPen(QColor(self._theme.color("border", "#e7e7e7")), 1))
        p.drawRoundedRect(rect, 8, 8)


class TrayController(QObject):
    """托盘图标控制器：闪烁、悬停卡片、右键菜单、信号转发。"""

    show_main_requested = pyqtSignal()
    open_settings_requested = pyqtSignal()
    reminders_requested = pyqtSignal()
    quit_requested = pyqtSignal()
    message_activated = pyqtSignal(str, str)

    # 400ms 整枚图标变色的闪烁比原来 550ms 的小红点交替更醒目
    FLASH_MS = 400
    HOVER_MS = 120
    HOVER_SHOW_TICKS = 3
    HOVER_HIDE_TICKS = 2
    MENU_COOLDOWN = 0.4

    def __init__(self, theme, parent=None, *, tray_icon=None, card=None,
                 menu=None):
        super().__init__(parent)
        self._theme = theme
        self._has_unread = False
        self._count = 0
        self._rows: List[TrayRow] = []
        self._overflow = 0
        self._alerting = False
        self._flash_on_alert = False
        self._shutdown = False
        self._hover_in = 0
        self._hover_out = 0
        self._menu_open = False
        self._suppress_hover_until = 0.0
        self._last_activate = 0.0
        # 最近一条 toast 指向的 (rid, msg_id)；系统通知点击回调不带载荷
        self._toast_target = ("", "")

        self._tray = tray_icon if tray_icon is not None \
            else QSystemTrayIcon(self)
        self._card = card if card is not None else TrayPreviewCard()
        self._menu = menu if menu is not None else QMenu()

        self._act_open = self._menu.addAction("打开主程序")
        self._act_settings = self._menu.addAction("打开配置")
        self._act_reminders = self._menu.addAction("消息提醒")
        self._menu.addSeparator()
        self._act_quit = self._menu.addAction("关闭程序")
        # triggered 带 bool：Cython 编译版不会自动截断，lambda 必须显式收下
        self._act_open.triggered.connect(
            lambda _=False: self._on_menu_open_main())
        self._act_settings.triggered.connect(
            lambda _=False: self._on_menu_settings())
        self._act_reminders.triggered.connect(
            lambda _=False: self._on_menu_reminders())
        self._act_quit.triggered.connect(
            lambda _=False: self.quit_requested.emit())

        self._menu.aboutToShow.connect(self._on_menu_about_to_show)
        self._menu.aboutToHide.connect(self._on_menu_about_to_hide)

        self._normal_icon = self._build_normal_icon()
        self._dot_icon = self._build_dot_icon()
        self._alert_icon = self._build_alert_icon()
        self._tray.setIcon(self._normal_icon)
        self._tray.setToolTip("Fchat")
        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_activated)
        # Windows toast 由系统渲染，点击回调不带消息载荷，只能记住最近一条
        if hasattr(self._tray, "messageClicked"):
            self._tray.messageClicked.connect(self._on_toast_clicked)

        self._flash_timer = QTimer(self)
        self._flash_timer.setInterval(self.FLASH_MS)
        self._flash_timer.timeout.connect(self._flash_tick)

        self._hover_timer = QTimer(self)
        self._hover_timer.setInterval(self.HOVER_MS)
        self._hover_timer.timeout.connect(self._hover_tick)

        if hasattr(self._card, "row_activated"):
            self._card.row_activated.connect(self._on_card_row_activated)

        app = QApplication.instance()
        if app is not None:
            # 更新/重启路径直接 QApplication.quit()，不走主窗 closeEvent；
            # 不在这里收托盘会留下幽灵图标
            app.aboutToQuit.connect(self.shutdown)

    # ---------------- 图标 ---------------- #
    def _build_normal_icon(self) -> QIcon:
        icon = QIcon()
        app = QApplication.instance()
        if app is not None:
            icon = app.windowIcon()
        if icon.isNull():
            path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "resources", "fchat.ico")
            if os.path.exists(path):
                icon = QIcon(path)
        if icon.isNull():
            icon = QIcon(self._placeholder_pixmap())
        return icon

    @staticmethod
    def _placeholder_pixmap() -> QPixmap:
        pm = QPixmap(64, 64)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#07C160"))
        p.drawRoundedRect(4, 4, 56, 56, 12, 12)
        f = QFont()
        f.setBold(True)
        f.setPixelSize(34)
        p.setFont(f)
        p.setPen(QColor("#ffffff"))
        p.drawText(pm.rect(), Qt.AlignCenter, "F")
        p.end()
        return pm

    def _build_dot_icon(self) -> QIcon:
        color = self._theme.color("status_failed", "#fa5151") \
            if self._theme is not None else "#fa5151"
        icon = QIcon()
        for size in _DOT_SIZES:
            base = self._normal_icon.pixmap(size, size)
            canvas = QPixmap(size, size)
            canvas.fill(Qt.transparent)
            p = QPainter(canvas)
            p.setRenderHint(QPainter.Antialiasing)
            p.drawPixmap(0, 0, base)
            d = max(8.0, size * 0.5)
            r = d / 2
            border = max(1.0, d * 0.16)
            cx = size - r - 0.5
            cy = r + 0.5
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#ffffff"))
            p.drawEllipse(QPointF(cx, cy), r, r)
            p.setBrush(QColor(color))
            p.drawEllipse(QPointF(cx, cy), r - border, r - border)
            p.end()
            icon.addPixmap(canvas)
        return icon

    def _build_alert_icon(self) -> QIcon:
        """闪烁用的高对比图标：整枚红色圆角块 + 白色感叹号。

        16px 托盘里普通图标上叠小红点太不显眼，闪烁时换成整块红色，
        和普通图标交替才有明显的明暗跳变。
        """
        color = self._theme.color("status_failed", "#fa5151") \
            if self._theme is not None else "#fa5151"
        icon = QIcon()
        for size in _DOT_SIZES:
            canvas = QPixmap(size, size)
            canvas.fill(Qt.transparent)
            p = QPainter(canvas)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(color))
            p.drawRoundedRect(0, 0, size, size, size * 0.22, size * 0.22)
            f = QFont()
            f.setBold(True)
            f.setPixelSize(max(8, int(size * 0.72)))
            p.setFont(f)
            p.setPen(QColor("#ffffff"))
            p.drawText(canvas.rect(), Qt.AlignCenter, "!")
            p.end()
            icon.addPixmap(canvas)
        return icon

    def _apply_icon(self):
        if self._shutdown:
            return
        if self._has_unread:
            self._tray.setIcon(self._dot_icon)
        else:
            self._tray.setIcon(self._normal_icon)

    # ---------------- 外部接口 ---------------- #
    def show(self):
        self._tray.show()

    def set_theme(self, theme):
        self._theme = theme
        self._dot_icon = self._build_dot_icon()
        self._alert_icon = self._build_alert_icon()
        if hasattr(self._card, "set_theme"):
            self._card.set_theme(theme)
        self._apply_icon()

    def show_message_toast(self, title: str, text: str,
                           rid: str, msg_id: str):
        """弹 Windows 通知；点击后经 message_activated 回到该消息。

        fake tray / 非 Windows 平台没有 showMessage 时静默跳过。
        """
        if self._shutdown or not hasattr(self._tray, "showMessage"):
            return
        self._toast_target = (rid or "", msg_id or "")
        self._tray.showMessage(title, text, QSystemTrayIcon.Information, 5000)

    def _on_toast_clicked(self):
        rid, msg_id = self._toast_target
        if not rid:
            return
        self.message_activated.emit(rid, msg_id)

    def update_state(self, has_unread: bool, count: int,
                     rows: Sequence[TrayRow], overflow: int = 0):
        self._has_unread = bool(has_unread)
        self._count = int(count or 0)
        self._rows = list(rows or ())
        self._overflow = int(overflow or 0)
        if hasattr(self._card, "set_rows"):
            self._card.set_rows(self._rows, self._overflow)
        if not self._has_unread:
            self.stop_alert()
            self.hide_card()
        else:
            self._apply_icon()
        self._update_tooltip()
        self._sync_hover_timer()

    def start_alert(self):
        if not self._has_unread or self._shutdown:
            return
        self._alerting = True
        self._flash_on_alert = True
        self._tray.setIcon(self._alert_icon)
        self._flash_timer.start()

    def stop_alert(self):
        self._flash_timer.stop()
        self._alerting = False
        self._flash_on_alert = False
        self._apply_icon()

    def hide_card(self):
        try:
            self._card.hide()
        except AttributeError:
            pass
        self._hover_in = 0
        self._hover_out = 0

    def shutdown(self):
        if self._shutdown:
            return
        self._shutdown = True
        self._flash_timer.stop()
        self._hover_timer.stop()
        self._alerting = False
        self._toast_target = ("", "")
        try:
            self._card.hide()
        except AttributeError:
            pass
        try:
            self._tray.hide()
        except AttributeError:
            pass

    # ---------------- 状态内部 ---------------- #
    def _update_tooltip(self):
        rect = self._tray.geometry()
        valid = rect.isValid() and rect.width() > 0 and rect.height() > 0
        if valid:
            # 卡片能弹时 tooltip 让位，避免和卡片重叠
            self._tray.setToolTip("")
            return
        if not self._has_unread:
            self._tray.setToolTip("Fchat")
            return
        text = f"Fchat · {self._count} 条未读"
        if self._rows:
            first = self._rows[0]
            label = first.room or "新会话"
            preview = first.text[:40]
            text += f"\n{label}：{preview}" if preview else f"\n{label}"
        self._tray.setToolTip(text)

    def _sync_hover_timer(self):
        if self._has_unread and self._rows and not self._shutdown:
            if not self._hover_timer.isActive():
                self._hover_timer.start()
        else:
            self._hover_timer.stop()
            self._hover_in = 0
            self._hover_out = 0

    # ---------------- 闪烁 ---------------- #
    def _flash_tick(self):
        if not self._alerting or not self._has_unread:
            self._flash_timer.stop()
            return
        self._flash_on_alert = not self._flash_on_alert
        self._tray.setIcon(
            self._alert_icon if self._flash_on_alert else self._normal_icon)

    # ---------------- 悬停 ---------------- #
    def _hover_tick(self):
        if self._menu_open or time.monotonic() < self._suppress_hover_until:
            self._hover_in = 0
            self._hover_out = 0
            return
        if not self._has_unread or not self._rows:
            self.hide_card()
            return
        rect = self._tray.geometry()
        if not rect.isValid() or rect.width() <= 0:
            # Win11 溢出区拿不到图标位置，无法判断悬停
            self._hover_in = 0
            self._hover_out = 0
            if self._card.isVisible():
                self._card.hide()
            return
        pos = self._cursor_pos()
        tray_hit = rect.adjusted(-6, -6, 6, 6).contains(pos)
        card_visible = self._card.isVisible()
        card_hit = False
        if card_visible:
            card_hit = self._card.frameGeometry().adjusted(
                -6, -6, 6, 6).contains(pos)
        if tray_hit:
            self._hover_out = 0
            if card_visible:
                self._hover_in = 0
                return
            self._hover_in += 1
            if self._hover_in >= self.HOVER_SHOW_TICKS:
                self._hover_in = 0
                self._card.set_rows(self._rows, self._overflow)
                self._card.show_near(rect)
        elif card_hit:
            self._hover_in = 0
            self._hover_out = 0
        elif card_visible:
            self._hover_in = 0
            self._hover_out += 1
            if self._hover_out >= self.HOVER_HIDE_TICKS:
                self._card.hide()
                self._hover_out = 0
        else:
            self._hover_in = 0
            self._hover_out = 0

    @staticmethod
    def _cursor_pos():
        from PyQt5.QtGui import QCursor
        return QCursor.pos()

    # ---------------- 菜单 / 激活 ---------------- #
    def _on_menu_open_main(self):
        self.stop_alert()
        self.hide_card()
        self.show_main_requested.emit()

    def _on_menu_settings(self):
        self.stop_alert()
        self.hide_card()
        self.open_settings_requested.emit()

    def _on_menu_reminders(self):
        self.stop_alert()
        self.hide_card()
        self.reminders_requested.emit()

    def _on_menu_about_to_show(self):
        self._menu_open = True
        self.hide_card()

    def _on_menu_about_to_hide(self):
        self._menu_open = False
        self._suppress_hover_until = time.monotonic() + self.MENU_COOLDOWN

    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.Context:
            self.hide_card()
            return
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.stop_alert()
            self.hide_card()
            # Trigger 与 DoubleClick 会连续到达，300ms 内只响应一次
            now = time.monotonic()
            if now - self._last_activate < 0.3:
                return
            self._last_activate = now
            self.show_main_requested.emit()

    def _on_card_row_activated(self, rid: str, msg_id: str):
        self.hide_card()
        self.message_activated.emit(rid, msg_id)
