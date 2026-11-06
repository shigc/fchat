"""置顶 / 星标消息列表弹窗。

列表数据按当前房间现拉（上限 50 条，暂不做分页/搜索）；点击一行发
message_selected，由 MainWindow 复用 _locate_message 跳转并高亮。
异步任务挂在弹窗上（parent=self），弹窗被销毁时任务自动解绑收尾。
"""
from __future__ import annotations

from PyQt5.QtCore import QSize, Qt, pyqtSignal
from PyQt5.QtWidgets import (QHBoxLayout, QListWidget, QListWidgetItem,
                             QVBoxLayout, QWidget)

from .api_workers import run_async
from .dialog_base import FramelessCardDialog
from .widgets import WxLabel, WxPushButton, _color, format_chat_time

# 正文单行省略的可用宽度：弹窗 520 - 左右边距 24*2 - 滚动条与行内边距
_ROW_TEXT_W = 430


class MessageListDialog(FramelessCardDialog):
    """「置顶消息」/「星标消息」列表。"""

    message_selected = pyqtSignal(object)

    def __init__(self, client, room_id: str, kind: str = "pinned",
                 theme=None, parent=None):
        self._kind = kind
        self._client = client
        self._room_id = room_id
        title = "置顶消息" if kind == "pinned" else "星标消息"
        super().__init__(title, theme, parent, width=520)
        self.setFixedHeight(480)

        self._status = WxLabel("正在加载…", theme)
        self._status.set_variant("hint")
        self._retry_btn = WxPushButton("重试", theme)
        self._retry_btn.clicked.connect(lambda _=False: self._load())
        self._retry_btn.hide()
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self._status, 1)
        row.addWidget(self._retry_btn)
        self._layout.addLayout(row)

        self._list = QListWidget(self)
        self._list.setObjectName("MessageList")
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._list.itemClicked.connect(self._on_item_clicked)
        self._list.hide()
        self._layout.addWidget(self._list, 1)

        self._theme_widgets.extend([self._status, self._retry_btn])
        self._row_labels: list = []
        self._apply_list_style()
        self._load()

    # ------------------------------------------------------------------ #
    def _apply_list_style(self):
        t = self._theme
        self._list.setStyleSheet(
            "QListWidget#MessageList{background:transparent;border:none;"
            "color:%s;font-size:13px;}"
            "QListWidget#MessageList::item{padding:0px;"
            "border-bottom:1px solid %s;}"
            "QListWidget#MessageList::item:hover{background:%s;}"
            "QListWidget#MessageList::item:selected{background:%s;color:%s;}"
            % (_color(t, "text_primary", "#191919"),
               _color(t, "border", "#e7e7e7"),
               _color(t, "conv_hover", "#eef3f4"),
               _color(t, "conv_hover", "#eef3f4"),
               _color(t, "text_primary", "#191919")))

    def set_theme(self, theme):
        super().set_theme(theme)
        self._apply_list_style()
        for lbl in self._row_labels:
            lbl.set_theme(theme)

    # ------------------------------------------------------------------ #
    def _load(self):
        if not self._client:
            self._set_error("尚未登录")
            return
        self._status.setText("正在加载…")
        self._status.show()
        self._retry_btn.hide()
        self._list.clear()
        self._row_labels = []
        self._list.hide()
        client = self._client
        room_id = self._room_id
        kind = self._kind

        def _task():
            if kind == "pinned":
                return client.get_pinned_messages(room_id, 50)
            return client.get_starred_messages(room_id, 50)

        def _done(msgs):
            msgs = list(msgs or [])
            if not msgs:
                self._status.setText(
                    "暂无置顶消息" if kind == "pinned" else "暂无星标消息")
                self._status.show()
                return
            self._status.hide()
            for msg in msgs[:50]:
                item, row, labels = self._make_item(msg)
                self._list.addItem(item)
                self._list.setItemWidget(item, row)
                self._row_labels.extend(labels)
            self._list.show()

        def _fail(err):
            self._set_error(f"加载失败：{err}")

        run_async(_task, on_done=_done, on_fail=_fail, parent=self)

    def _set_error(self, text: str):
        self._status.setText(text)
        self._status.show()
        self._retry_btn.show()

    def _make_item(self, msg):
        sender = ""
        if msg.sender is not None:
            sender = msg.sender.name or msg.sender.username or ""
        when = format_chat_time(msg.timestamp)
        body = (msg.text or "").replace("\n", " ").strip()
        if len(body) > 80:
            body = body[:80] + "…"
        if not body:
            body = f"[{msg.msg_type}]"
        header = f"{sender or '?'} · {when}"
        # item 文本留空：默认委托仍会把它画在行部件下面（部件透明），
        # 与自定义 label 叠成重影。文字只由行部件负责。
        item = QListWidgetItem()
        item.setData(Qt.UserRole, msg)
        item.setData(Qt.AccessibleTextRole, f"{header} {body}")
        item.setToolTip(body)
        row, labels = self._make_row_widget(header, body)
        item.setSizeHint(QSize(0, row.sizeHint().height()))
        return item, row, labels

    def _make_row_widget(self, header: str, body: str):
        """两行行部件；QListWidget 默认委托不开 wordWrap，只设多行文本
        会把正文整体省略掉——只能自己放 QLabel 才能直接看到内容。"""
        row = QWidget(self._list)
        lay = QVBoxLayout(row)
        lay.setContentsMargins(6, 5, 6, 5)
        lay.setSpacing(2)
        head = WxLabel(header, self._theme, row)
        head.set_variant("hint")
        body_lbl = WxLabel("", self._theme, row)
        body_lbl.ensurePolished()
        body_lbl.setText(body_lbl.fontMetrics().elidedText(
            body, Qt.ElideRight, _ROW_TEXT_W))
        lay.addWidget(head)
        lay.addWidget(body_lbl)
        return row, [head, body_lbl]

    def _on_item_clicked(self, item, *_):
        msg = item.data(Qt.UserRole)
        if msg is None:
            return
        self.message_selected.emit(msg)
        self.accept()
