"""通用小部件与工具：圆形头像、时间格式化、气泡容器等。"""
from __future__ import annotations

import datetime

from PyQt5 import sip
from PyQt5.QtCore import Qt, pyqtSignal, QPoint, QRectF, QLineF
from PyQt5.QtGui import QColor, QFont, QPainter, QPolygon, QPixmap, QPen
from PyQt5.QtWidgets import (
    QComboBox, QLabel, QLineEdit, QPushButton, QWidget, QSizePolicy,
    QStyledItemDelegate,
)

from .theme import ThemeManager
from .icons import status_pixmap


# ---------------------------------------------------------------------- #
# 时间格式化（微信风格）
# ---------------------------------------------------------------------- #
# 周名（不要用 strftime("%A")，C locale 下会输出英文）
_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def format_timestamp(ts: float) -> str:
    """把 epoch 秒转成会话列表用的时间文本：今天/昨天/周几/日期。"""
    if not ts:
        return ""
    dt = datetime.datetime.fromtimestamp(ts)
    now = datetime.datetime.now()
    today = now.date()
    d = dt.date()
    if d == today:
        return dt.strftime("%H:%M")
    if d == today - datetime.timedelta(days=1):
        return "昨天"
    if (today - d).days < 7:
        return _WEEKDAY_CN[d.weekday()]
    if d.year == today.year:
        return d.strftime("%m-%d")
    return d.strftime("%Y-%m-%d")


def format_chat_time(ts: float) -> str:
    """聊天视图内的时间标签：今天/昨天 + 具体时间。"""
    if not ts:
        return ""
    dt = datetime.datetime.fromtimestamp(ts)
    now = datetime.datetime.now()
    today = now.date()
    d = dt.date()
    hm = dt.strftime("%H:%M")
    if d == today:
        return f"{hm}"
    if d == today - datetime.timedelta(days=1):
        return f"昨天 {hm}"
    if (today - d).days < 7:
        return f"{d.strftime('%m-%d')} {hm}"
    return f"{d.strftime('%Y-%m-%d')} {hm}"


def days_since(ts: float) -> int:
    if not ts:
        return 10 ** 9
    dt = datetime.datetime.fromtimestamp(ts)
    return (datetime.datetime.now().date() - dt.date()).days


def short_time_text(ts: float) -> str:
    """非常紧凑的时间：今天只显示时分秒。"""
    if not ts:
        return ""
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M")


def room_type_label(conv) -> str:
    """房间的官方术语类型标签，供角标/右键菜单统一使用。

    Rocket.Chat 语义：团队主房间（teamMain）的 t 仍是 c/p，要结合
    is_team 区分「频道/团队」；c=公开、p=私有。
    """
    if conv.room_type == "d":
        return "私聊"
    if getattr(conv, "is_team", False):
        return "私有团队" if conv.room_type == "p" else "公开团队"
    return "私有频道" if conv.room_type == "p" else "公开频道"


# ---------------------------------------------------------------------- #
# 圆形头像（无头像时用姓氏首字 + 固定背景色）
# ---------------------------------------------------------------------- #
_AVATAR_COLORS = [
    "#07C160", "#10AEFF", "#FA9D3B", "#FA5151", "#986EE5",
    "#00A86B", "#E64340", "#5B8FF9", "#F0A73A", "#576B95",
]


class AvatarWidget(QLabel):
    """头像 QLabel。未加载到图片时自绘方形圆角的姓氏首字占位。

    占位不能用样式表 background-color 画：WA_TranslucentBackground 会让
    它完全不绘制，且 border-radius 对背景的裁剪在不同平台表现不一。
    """

    clicked = pyqtSignal()

    def __init__(self, size: int = 40, parent=None):
        super().__init__(parent)
        self._size = size
        self._name = ""
        self._color = QColor(_AVATAR_COLORS[0])
        self._clickable = False
        self._hovered = False
        self._status_key = None    # 在线状态点 key（None 不画）
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignCenter)
        font = QFont()
        font.setPointSize(max(9, size // 2))
        font.setBold(True)
        self.setFont(font)

    def set_name(self, name: str):
        """设置显示名；取名字首字作为无头像时的占位文字。"""
        self._name = name or ""
        self._color = QColor(
            _AVATAR_COLORS[abs(hash(self._name or "?")) % len(_AVATAR_COLORS)])
        self.update()

    def set_pixmap_rounded(self, pixmap: QPixmap):
        """把图片裁切成圆形显示。"""
        size = self._size
        pix = pixmap.scaled(
            size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        # 居中裁剪成正方形
        x = (pix.width() - size) // 2
        y = (pix.height() - size) // 2
        pix = pix.copy(x, y, size, size)
        rounded = QPixmap(size, size)
        rounded.fill(Qt.transparent)
        painter = QPainter(rounded)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(230, 230, 230))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(0, 0, size, size)
        painter.setClipRect(0, 0, size, size)
        painter.drawPixmap(0, 0, pix)
        painter.end()
        self.setPixmap(rounded)

    def set_clickable(self, clickable: bool):
        """开启点击换头像交互：悬停画相机角标，点击发 clicked。"""
        self._clickable = clickable
        self.setCursor(Qt.PointingHandCursor if clickable else Qt.ArrowCursor)
        # 复用控件的场景里先 set_clickable(True) 悬停再关掉，残留的
        # _hovered 会让角标一直画着，所以关闭时顺手清掉
        if not clickable and self._hovered:
            self._hovered = False
            self.update()

    def enterEvent(self, event):
        if self._clickable:
            self._hovered = True
            self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        # 不在悬停态时不要触发重绘：leave 在窗口切换等场景会频繁到来
        if self._hovered:
            self._hovered = False
            self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if (self._clickable and event.button() == Qt.LeftButton
                and self.rect().contains(event.pos())):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def set_status_key(self, key):
        """头像右下角在线状态点；None/空串不画。key 为
        normalize_status_key 的产物（online/away/busy/dnd/offline/departed）。"""
        key = key or None
        if key == self._status_key:
            return
        self._status_key = key
        self.update()

    def set_status_tooltip(self, text: str):
        """状态点的悬停说明（点本身太小，tooltip 挂在头像上）。"""
        self.setToolTip(text or "")

    def _paint_status_dot(self):
        d = max(9, self._size // 4)
        pm = status_pixmap(self._status_key, d)
        cx = self._size - d // 2 - 2
        cy = self._size - d // 2 - 2
        painter = QPainter(self)
        painter.drawPixmap(cx - d // 2, cy - d // 2, pm)
        painter.end()

    def _paint_camera_badge(self):
        """右下角相机角标：暗圆底 + 白色机身 + 镜头点（现画，不用图片资源）。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        d = max(16, self._size // 3)
        cx = self._size - d // 2 - 2
        cy = self._size - d // 2 - 2
        painter.setPen(QPen(QColor("white"), 1.5))
        painter.setBrush(QColor(0, 0, 0, 170))
        painter.drawEllipse(QPoint(cx, cy), d // 2, d // 2)
        bw, bh = d * 0.62, d * 0.46
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("white"))
        painter.drawRoundedRect(QRectF(cx - bw / 2, cy - bh / 2, bw, bh),
                                1.5, 1.5)
        painter.setBrush(QColor(0, 0, 0, 170))
        painter.drawEllipse(QPoint(int(cx), int(cy)),
                            int(bh * 0.24), int(bh * 0.24))
        painter.end()

    def paintEvent(self, event):
        if self.pixmap() is None:
            # 无图占位：方形圆角色块 + 名字首字
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(Qt.NoPen)
            painter.setBrush(self._color)
            r = self._size // 5
            painter.drawRoundedRect(0, 0, self._size, self._size, r, r)
            painter.setPen(QColor("white"))
            painter.setFont(self.font())
            painter.drawText(self.rect(), Qt.AlignCenter,
                             (self._name[:1].upper()) or "?")
            painter.end()
        else:
            super().paintEvent(event)
        # 状态点与相机角标同在右下角：可点击头像悬停时只画相机角标
        if self._status_key and not (self._clickable and self._hovered):
            self._paint_status_dot()
        if self._clickable and self._hovered:
            self._paint_camera_badge()


# ---------------------------------------------------------------------- #
# 微信风格设置组件集（设置页统一复用）
#
# 所有控件不再写死十六进制色，而是从一只「取色器」取语义键颜色，这样随
# 亮/暗主题切换会自动变化（修复设置页暗色下不切换、文字看不清的根因）。
# 取色器可以是 ThemeManager（有 .color(key, default)），或任何提供
# `.color(key, default) -> str` 的对象；未注入时按亮色调板渲染。
# ---------------------------------------------------------------------- #
_DEFAULT_THEME = ThemeManager()


def _color(theme, key: str, default: str) -> str:
    """从取色器安全取色；取色器缺失或键不存在时回退默认。"""
    if theme is not None:
        try:
            return theme.color(key, default) or default
        except Exception:  # noqa: BLE001 —— 取色失败降级默认，不中断 UI
            pass
    return default


def _base_size(theme, default: int = 13) -> int:
    """取界面基础字号（设置页可调 10-24）；取不到时用亮色默认 13。

    Wx 组件与设置页/会话列表的字号都按它推算（默认值即 13px 基准时的
    原始像素），改字号设置后重刷样式即可跟随。
    """
    try:
        size = int(getattr(theme, "font_size", default))
    except (TypeError, ValueError):
        return default
    return size if size > 0 else default


class WxLabel(QLabel):
    """微信风格文本标签，按语义变体取色。

    variant: primary(正文) / secondary(次要说明) / hint(灰提示) / bold(加粗标题)
    / danger(错误提示，danger_fg 随主题)。
    """

    def __init__(self, text: str = "", theme=None, parent=None):
        super().__init__(text, parent)
        self._theme = theme
        self._size = None          # 非空时覆盖变体默认字号（含单位，如 "24px"）
        self.set_variant("primary")

    def set_theme(self, theme):
        self._theme = theme
        self._apply_style()

    def set_variant(self, variant: str):
        self._variant = variant
        self._apply_style()

    def set_size(self, size: str):
        """覆盖字号（如 "24px"），保留语义色与字重，避免用 setStyleSheet 冲掉主题色。"""
        self._size = size
        self._apply_style()

    def _apply_style(self):
        t = self._theme
        base = _base_size(t)
        if self._variant in ("primary", "bold"):
            color = _color(t, "text_primary", "#191919")
            size = base + 3 if self._variant == "bold" else base
        elif self._variant == "danger":
            color = _color(t, "danger_fg", "#e64340")
            size = base
        else:  # secondary / hint
            color = _color(t, "text_secondary", "#999999")
            size = base - 1 if self._variant == "hint" else base
        weight = "bold" if self._variant == "bold" else "normal"
        self.setStyleSheet(
            f"color:{color};font-size:{self._size or size}px;"
            f"font-weight:{weight};")


class WxLineEdit(QLineEdit):
    """微信风格输入框：浅底、圆角、聚焦时主题绿描边。"""

    def __init__(self, theme=None, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._apply_style()

    def set_theme(self, theme):
        self._theme = theme
        self._apply_style()

    def _apply_style(self):
        t = self._theme
        bg = _color(t, "input_bg", "#ffffff")
        border = _color(t, "input_border", "#e0e0e0")
        text = _color(t, "text_primary", "#191919")
        accent = _color(t, "accent", "#07C160")
        hover = _color(t, "conv_hover", "#f2f2f2")
        self.setStyleSheet(
            f"QLineEdit{{background:{bg};border:1px solid {border};"
            f"border-radius:6px;padding:6px 8px;color:{text};}} "
            f"QLineEdit:hover{{border:1px solid {hover};}} "
            f"QLineEdit:focus{{border:1px solid {accent};}}")


class _WxComboDelegate(QStyledItemDelegate):
    """下拉框选项委托：在选项之间画分割线。

    行距由 `WxComboBox` 通过 `view.setSpacing` 控制。委托**不能**重写
    `sizeHint`：combo 被 GC 回收时 Qt 会在析构期间回调它，此时委托的
    Python 包装可能已释放，异常逃出 C++ 虚函数会让 PyQt 直接 qFatal /
    访问冲突（真机与全量单测都踩过）。分割线颜色随主题亮/暗切换，由
    `WxComboBox._apply_style` 更新。
    """

    def __init__(self, pad: int = 6, line: str = "#e8e8e8",
                 last: bool = False, parent=None):
        super().__init__(parent)
        self._pad = pad
        self._line = line
        self._draw_last = last

    def paint(self, painter, option, index):
        try:
            if sip.isdeleted(self):
                return
            super().paint(painter, option, index)
            if not self._draw_last and index.row() == index.model().rowCount() - 1:
                return
            painter.save()
            pen = QPen(QColor(self._line))
            pen.setWidth(1)
            painter.setPen(pen)
            # 行距由 view.setSpacing 制造，分割线画在两行之间的空隙中线；
            # 用 QLineF 承载 .5 的半像素坐标，避免 drawLine(int,...) 拒绝 float
            y = option.rect.bottom() + self._pad + 0.5
            painter.drawLine(QLineF(option.rect.left(), y, option.rect.right(), y))
            painter.restore()
        except RuntimeError:
            return


class WxComboBox(QComboBox):
    """微信风格下拉框，从语义键取色。保留 addItem(cn,data)/currentIndexChanged/itemData 接口。"""

    def __init__(self, theme=None, parent=None):
        super().__init__(parent)
        self._theme = theme
        # 行距用 view.setSpacing 制造，不再由委托重写 sizeHint：
        # combo 被 GC 回收时（真机与全量单测都踩过）Qt 会在析构期间回调
        # sizeHint，此时委托的 Python 包装可能已失效，异常逃出 C++ 虚函数
        # 会让 PyQt 直接 qFatal/访问冲突。sizeHint 保持纯 C++ 实现后，
        # 析构期间的回调不再进入 Python。
        view = self.view()
        view.setSpacing(6)
        self._delegate = _WxComboDelegate(parent=view)
        self.setItemDelegate(self._delegate)
        self._apply_style()

    def set_theme(self, theme):
        self._theme = theme
        self._apply_style()

    def _apply_style(self):
        t = self._theme
        bg = _color(t, "input_bg", "#ffffff")
        border = _color(t, "input_border", "#e0e0e0")
        text = _color(t, "text_primary", "#191919")
        item_bg = _color(t, "conv_bg", "#ffffff")
        line = _color(t, "input_border", "#e0e0e0")
        self._delegate._line = line
        self.setStyleSheet(
            f"QComboBox{{background:{bg};border:1px solid {border};"
            f"border-radius:6px;padding:6px 8px;color:{text};}} "
            f"QComboBox:hover{{border:1px solid {_color(t, 'conv_hover', '#f2f2f2')};}} "
            f"QComboBox QAbstractItemView{{background:{item_bg};color:{text};"
            f"border:1px solid {border};selection-background-color:{_color(t, 'conv_hover', '#f2f2f2')}; "
            f"selection-color:{text};}}")


class WxPushButton(QPushButton):
    """微信风格按钮：浅底、圆角、hover 加深；danger 变体为红色提示按钮。"""

    def __init__(self, text: str = "", theme=None, danger: bool = False, parent=None):
        super().__init__(text, parent)
        self._theme = theme
        self._danger = danger
        self._apply_style()

    def set_theme(self, theme):
        self._theme = theme
        self._apply_style()

    def _apply_style(self):
        t = self._theme
        if self._danger:
            bg = _color(t, "danger_bg", "#fdeaea")
            fg = _color(t, "danger_fg", "#e64340")
            hover = _color(t, "danger_hover", "#f8d3d3")
        else:
            bg = _color(t, "conv_bg", "#ffffff")
            fg = _color(t, "text_primary", "#191919")
            hover = _color(t, "conv_hover", "#eef3f4")
        self.setStyleSheet(
            f"QPushButton{{background:{bg};color:{fg};border:none;"
            f"border-radius:6px;padding:8px 12px;font-size:{_base_size(t)}px;}} "
            f"QPushButton:hover{{background:{hover};}}")


# ---------------------------------------------------------------------- #
# 气泡圆角容器（供 chat_view 的 bubble 复用）
# ---------------------------------------------------------------------- #
class Bubble(QWidget):
    """带圆角背景的气泡容器。使用 QSS 样式表实现，与原始组件库视觉一致。

    color: 气泡背景色（mine 绿色 / other 白色）
    is_mine: 是否为自己的消息
    """

    def __init__(self, color: str, is_mine: bool, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setProperty("bubble_side", "mine" if is_mine else "other")
        # 直接使用内联样式（颜色因用户而异，无法全局预设）
        self.setStyleSheet(
            f"background-color: {color}; border-radius: 10px;"
        )
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Maximum)


class Triangle(QLabel):
    """气泡三角指示器。使用 QPolygon 绘制，与原始组件库一致。"""

    def __init__(self, color: str, is_send: bool, parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self._is_send = is_send
        self.setFixedSize(6, 45)

    def set_color(self, color: str):
        self._color = QColor(color)
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(self._color)
        painter.setBrush(self._color)
        tri = QPolygon()
        if self._is_send:
            tri.setPoints(0, 20, 0, 34, 6, 27)
        else:
            tri.setPoints(0, 27, 6, 20, 6, 34)
        painter.drawPolygon(tri)


# ---------------------------------------------------------------------- #
# 文本框中文右键菜单
# ---------------------------------------------------------------------- #
def install_text_menu(edit, editable: bool = None) -> None:
    """把 QTextEdit/QTextBrowser 的标准右键菜单换成自建中文菜单。

    PyQt5 附带的 qt_zh_CN.qm 是空壳（qtbase_zh_CN.qm 缺失，发行版里的
    PyQt5-Qt5 常常只有一份），QTranslator 救不了「Copy/Select All」，
    而项目不引入二进制资源，所以自建：动作直接调控件的标准槽，行为与
    原生菜单一致。editable 缺省按 isReadOnly() 判断。
    """
    if editable is None:
        editable = not edit.isReadOnly()
    edit.setContextMenuPolicy(Qt.CustomContextMenu)
    edit.customContextMenuRequested.connect(
        lambda pos: _open_text_menu(edit, pos, editable))


def _open_text_menu(edit, pos, editable: bool) -> None:
    from PyQt5.QtGui import QKeySequence
    from PyQt5.QtWidgets import QApplication, QMenu

    menu = QMenu(edit)

    def _add(text: str, slot, enabled: bool = True, key=None):
        act = menu.addAction(text)
        if key is not None:
            act.setShortcut(QKeySequence(key))
        act.setEnabled(enabled)
        act.triggered.connect(lambda _=False: slot())
        return act

    # 悬停链接时补「复制链接」（原生菜单的 Copy Link Location 同位）
    anchor = edit.anchorAt(pos) if hasattr(edit, "anchorAt") else ""
    if anchor:
        _add("复制链接", lambda: QApplication.clipboard().setText(anchor))
        menu.addSeparator()
    if editable:
        _add("撤销", edit.undo, edit.document().isUndoAvailable(),
             QKeySequence.Undo)
        _add("重做", edit.redo, edit.document().isRedoAvailable(),
             QKeySequence.Redo)
        menu.addSeparator()
        _add("剪切", edit.cut, edit.textCursor().hasSelection(),
             QKeySequence.Cut)
    _add("复制", edit.copy, edit.textCursor().hasSelection(),
         QKeySequence.Copy)
    if editable:
        _add("粘贴", edit.paste, edit.canPaste(), QKeySequence.Paste)
        menu.addSeparator()
    _add("全选", edit.selectAll, bool(edit.toPlainText()), QKeySequence.SelectAll)
    menu.exec_(edit.viewport().mapToGlobal(pos))
    menu.deleteLater()
