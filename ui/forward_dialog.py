"""转发消息时选择目标会话的对话框。"""
from __future__ import annotations

from PyQt5.QtCore import Qt, QSize, QRect, QRectF
from PyQt5.QtGui import QColor, QFontMetrics, QPainter, QPen
from PyQt5.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QStyle, QStyledItemDelegate, QVBoxLayout,
)

from core.models import Message
from core.messages import MERGE_HEAD, FORWARD_HEAD
from .widgets import _color, format_chat_time, room_type_label

# 会话列表行的固定尺寸（委托绘制与行高统一用这组常量）
_ROW_SIZE = QSize(360, 52)
_AVATAR_SIZE = 36


class _ConversationDelegate(QStyledItemDelegate):
    """会话列表行绘制：圆形头像（首字占位）+ 名称 + 右侧类型角标。

    颜色从取色器取语义色，随亮/暗主题切换；头像首字色沿用微信绿色盘。
    """

    def __init__(self, parent, theme=None):
        super().__init__(parent)
        self._theme = theme
        self._fc = lambda key, default: QColor(_color(theme, key, default))

    def _avatar_color(self, name: str) -> QColor:
        colors = ["#07C160", "#10AEFF", "#FA9D3B", "#FA5151", "#986EE5",
                  "#00A86B", "#E64340", "#5B8FF9", "#F0A73A", "#576B95"]
        return QColor(colors[abs(hash(name or "?")) % len(colors)])

    def paint(self, painter: QPainter, option, index):
        conv = index.data(Qt.UserRole)
        if conv is None:
            super().paint(painter, option, index)
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        rect = option.rect
        # 悬停 / 选中背景
        if option.state & QStyle.State_Selected:
            painter.fillRect(rect, self._fc("conv_hover", "#eef3f4"))
        elif option.state & QStyle.State_MouseOver:
            hover = self._fc("conv_hover", "#eef3f4")
            hover.setAlpha(max(0, hover.alpha() - 40))
            painter.fillRect(rect, hover)

        label = conv.name or conv.username or conv.id or ""
        name = conv.name or conv.username or ""
        type_label = room_type_label(conv)

        # 圆形头像：色块 + 首字
        av = _AVATAR_SIZE
        av_rect = QRect(rect.left() + 10,
                        rect.top() + (rect.height() - av) // 2, av, av)
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._avatar_color(name or "?"))
        painter.drawEllipse(QRectF(av_rect))
        painter.setPen(QColor("white"))
        fm = QFontMetrics(painter.font())
        painter.drawText(av_rect, Qt.AlignCenter,
                         (name[:1].upper() or "?"))

        # 名称 + 类型角标
        text_x = av_rect.right() + 12
        painter.setPen(self._fc("text_primary", "#191919"))
        type_w = fm.horizontalAdvance(type_label) + 16 if type_label else 0
        max_width = rect.right() - text_x - 8 - (type_w if type_w else 0)
        shown = fm.elidedText(label, Qt.ElideRight, max_width)
        painter.drawText(QRect(text_x, rect.top(),
                               max_width, rect.height()),
                         Qt.AlignLeft | Qt.AlignVCenter, shown)
        if type_label:
            tag_rect = QRect(rect.right() - type_w - 10,
                             rect.top() + (rect.height() - 22) // 2,
                             type_w, 22)
            painter.setPen(QPen(self._fc("text_tertiary", "#888888"), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(tag_rect, 11, 11)
            fm2 = QFontMetrics(painter.font())
            painter.setPen(self._fc("text_tertiary", "#888888"))
            painter.drawText(tag_rect, Qt.AlignCenter, type_label)
        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        return _ROW_SIZE


def _forward_head(message: Message) -> str:
    """逐条转发首行标记「⟳[转发消息] 原发送人」，接收端据此加转发表识。"""
    sender = (message.sender.name or message.sender.username
              if message.sender else "")
    return f"{FORWARD_HEAD} {sender}".rstrip()


def _base_payload(message: Message, resolve_url):
    """组装转发的 (文本, 附件)，不含转发标记。附件沿用原始地址，不做重传。"""
    att = message.attachments[0] if message.attachments else None
    if att and isinstance(att, dict):
        payload = dict(att)
        for key in ("image_url", "video_url", "audio_url", "title_link"):
            v = payload.get(key)
            if isinstance(v, str) and v.startswith("/"):
                payload[key] = resolve_url(v)
        text = message.text or ""
        if message.msg_type == "image" and text == "图片":
            # 这是解析时给无标题图片补的占位文案，不该跟着转发
            text = ""
        return text, [payload]
    if message.msg_type == "quote":
        sender_name = (message.sender.name or message.sender.username
                       if message.sender else "")
        payload = {
            "author_name": sender_name,
            "text": message.description or "",
        }
        # 带上原始 permalink，收方（网页版/本应用）仍可点击跳转
        if message.quote_link:
            payload["message_link"] = message.quote_link
        return message.text or "", [payload]
    return message.text or message.display_text or "", None


def build_forward_payload(message: Message, resolve_url):
    """组装逐条转发的 (文本, 附件)，首行带「转发的消息」标记。

    接收端 parse_message 识别 FORWARD_HEAD 后剥离标记行并设 is_forwarded，
    气泡内容仍是原文。
    """
    text, atts = _base_payload(message, resolve_url)
    head = _forward_head(message)
    if atts:
        return (f"{head}\n{text}".strip() if text else head), atts
    return (f"{head}\n{text}".strip() if text else head), None


def _merge_file_ref(atts: list) -> str:
    """把附件编码成 markdown 文件引用「[文件名](完整URL)」。

    其它客户端（网页版）据此可点击跳转文件；本应用渲染端把 content 走 markdown
    渲染成可点击锚点。URL 取各 url 字段（_base_payload 已用 resolve_url 补全为
    完整地址），取附件 title 为文件名。
    """
    att = atts[0] if atts else {}
    if not isinstance(att, dict):
        return ""
    url = (att.get("title_link") or att.get("image_url")
           or att.get("video_url") or att.get("audio_url"))
    if not url:
        return ""
    name = (att.get("title") or "").strip()
    return f"[{name}]({url})" if name else url


def build_forward_payload_many(messages, resolve_url, merge=True):
    """多条消息转发的载荷，返回 [(text, attachments), ...]。

    merge=False：逐条返回每条消息的载荷（自带转发表记）。
    merge=True：合成「合并转发」方案二——正文 text 只留「⟳[聊天记录]共N条」
    （首行 MERGE_HEAD 用于接收端 parse_message 识别成 merge），每条子消息一张
    Rocket.Chat 附件卡片 {color, title, text, collapsed}，title=「@发送人 · 时间」。

    图片子消息（msg_type="image"）的卡片额外带 image_url（图片真实地址，web 端
    直接内联渲染图片），text 只放「[右键打开链接查看原图](图片url)」链接作为不
    能力内联渲染场景的兜底；接收端（本应用 parse_message）据此把条目标记成图片，
    渲染时只画图片、不再显示 text。其余子消息维持 markdown 链接 [文件名](URL)。
    """
    if not merge:
        return [build_forward_payload(m, resolve_url) for m in messages]

    attachments = []
    for m in messages:
        text, atts = _base_payload(m, resolve_url)
        sender = (m.sender.name or m.sender.username or "" if m.sender else "")
        ts_text = format_chat_time(m.timestamp) if m.timestamp else "0"
        if m.msg_type == "image" and atts and isinstance(atts[0], dict):
            # 图片子消息：卡片携带 image_url，text 只留「右键打开链接查看原图」，
            # 不再拼入配文（渲染端对图片条目只画图、不显示文本）。
            image_url = atts[0].get("image_url") or ""
            attachments.append({
                "color": "#1d74f5",
                "title": f"@{sender or '未知'} · {ts_text}",
                "text": f"[右键打开链接查看原图]({image_url})" if image_url else "",
                "collapsed": False,
                "image_url": image_url,
            })
        elif atts and isinstance(atts[0], dict):
            # 其它附件条目：配文(可选) + 空格 + markdown 文件引用，单行不换行。
            # 附件统一走 markdown 链接 [name](url)。附件信息不再往正文里塞
            # JSON——那份 JSON 本应用渲染用不着，却会把 `{"image_url":...,...}`
            # 原样暴露给网页版。
            ref = _merge_file_ref(atts)
            content = (f"{text} {ref}".strip() if text else ref)
            attachments.append({
                "color": "#1d74f5",
                "title": f"@{sender or '未知'} · {ts_text}",
                "text": content or "",
                "collapsed": False,
            })
        else:
            attachments.append({
                "color": "#1d74f5",
                "title": f"@{sender or '未知'} · {ts_text}",
                "text": text or "",
                "collapsed": False,
            })

    body = f"{MERGE_HEAD}共{len(attachments)}条"
    return [(body, attachments)]


class ForwardDialog(QDialog):
    """微信风格转发对话框：顶部搜索 + 头像是话列表 + 底部操作条。

    支持多选目标（ExtendedSelection），按名称/用户名过滤；底部实时显示
    「已选 N 个会话」，转发按钮未选时置灰，选中后高亮「转发(N)」。
    双击单条仍视为单选即转发。mode 用于区分对话框标题（合并/逐条）。
    """

    def __init__(self, conversations, parent=None, mode: str = "each",
                 theme=None):
        super().__init__(parent)
        if mode == "merge":
            title = "合并转发到"
        elif mode == "each":
            title = "逐条转发到"
        else:
            title = "转发到"
        self.setWindowTitle(title)
        self.setMinimumSize(400, 500)
        self._conversations = list(conversations)
        self._theme = theme
        self.selected_conversations: list = []
        self._fc = lambda key, default: _color(theme, key, default)

        bg = self._fc("bg", "#f7f7f7")
        panel = self._fc("input_bg", "#ffffff")
        border = self._fc("border", "#e7e7e7")
        text = self._fc("text_primary", "#191919")
        text2 = self._fc("text_tertiary", "#888888")
        accent = self._fc("accent", "#07C160")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(12)
        self.setStyleSheet(
            f"QDialog{{background:{bg};}}"
            f"QLineEdit{{background:{panel};border:1px solid {border};"
            f"border-radius:8px;padding:8px 12px;font-size:14px;color:{text};}}"
            f"QLineEdit:focus{{border-color:{accent};}}")

        # 顶部放大搜索框
        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._refill)
        layout.addWidget(self._search)

        # 会话列表：自定义委托绘制头像 + 名称 + 类型角标
        self._list = QListWidget()
        self._list.setSelectionMode(QListWidget.ExtendedSelection)
        self._list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self._list.setSpacing(2)
        self._list.setStyleSheet(
            f"QListWidget{{background:transparent;border:none;}}"
            f"QListWidget::item{{border-radius:8px;color:{text};}}"
            f"QListWidget::item:selected{{background:{self._fc('conv_hover', '#eef3f4')};}}")
        self._list.setItemDelegate(_ConversationDelegate(self, theme))
        self._list.itemDoubleClicked.connect(lambda _item: self.accept())
        self._list.itemSelectionChanged.connect(self._update_action_bar)
        layout.addWidget(self._list, 1)

        # 底部操作条
        action_row = QHBoxLayout()
        action_row.setContentsMargins(2, 0, 2, 0)
        self._count_label = QLabel("已选 0 个会话")
        self._count_label.setStyleSheet(
            f"color:{text2};font-size:13px;padding-left:4px;")
        action_row.addWidget(self._count_label)
        action_row.addStretch(1)
        self._forward_btn = QPushButton("转发")
        self._forward_btn.clicked.connect(self.accept)
        self._forward_btn.setDefault(True)
        self._forward_btn.setEnabled(False)
        self._forward_btn.setStyleSheet(
            f"QPushButton{{background:{accent};color:#ffffff;border:none;"
            f"border-radius:16px;padding:7px 22px;font-size:14px;}}"
            f"QPushButton:disabled{{background:{self._fc('border','#e7e7e7')};"
            f"color:{text2};}}")
        action_row.addWidget(self._forward_btn)
        layout.addLayout(action_row)

        self._refill("")

    def _update_action_bar(self):
        n = len(self._list.selectedItems())
        self._count_label.setText(f"已选 {n} 个会话")
        self._forward_btn.setEnabled(n > 0)
        self._forward_btn.setText("转发" if not n else f"转发({n})")

    def _refill(self, query: str):
        self._list.clear()
        q = (query or "").strip().lower()
        for conv in self._conversations:
            label = conv.name or conv.username or conv.id
            if q and q not in label.lower() and q not in (conv.username or "").lower():
                continue
            item = QListWidgetItem()
            item.setData(Qt.UserRole, conv)
            item.setSizeHint(_ROW_SIZE)
            self._list.addItem(item)
        if self._list.count():
            self._list.setCurrentRow(0)
        self._update_action_bar()

    def accept(self, *_):
        if self._list.selectedItems():
            self.selected_conversations = [
                it.data(Qt.UserRole) for it in self._list.selectedItems()]
        else:
            item = self._list.currentItem()
            if item is None:
                return
            self.selected_conversations = [item.data(Qt.UserRole)]
        if not self.selected_conversations:
            return
        super().accept()
