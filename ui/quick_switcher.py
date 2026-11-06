"""Ctrl+K 快速切换：模糊搜索本地会话，并异步补充目录结果。

本地会话同步过滤（名称/用户名/最后消息），保证零延迟；输入停顿后
并行查询 directory 的频道与用户（服务端搜索，中文可用），未加入的
公开频道可以预览、用户可直接开私聊。结果按命中质量排序，回车/单击
打开，上下键选择，Esc 关闭。
"""
from __future__ import annotations

import logging

from PyQt5.QtCore import QEvent, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QVBoxLayout, QWidget,
)

from .api_workers import run_async
from .dialog_base import FramelessCardDialog
from .icons import icon
from .widgets import (AvatarWidget, WxLabel, WxLineEdit, _base_size, _color,
                      room_type_label)

log = logging.getLogger(__name__)

_SEARCH_DEBOUNCE_MS = 250
_LOCAL_LIMIT = 30
_MORE_ROOMS = 6
_MORE_USERS = 4


def fuzzy_score(query: str, text: str):
    """模糊匹配得分：子串优先（越靠前越高），否则要求按序子序列。

    返回 None 表示不匹配。只做轻量打分，不引入拼音依赖。
    """
    q = (query or "").strip().lower()
    if not q:
        return 0
    t = (text or "").lower()
    if not t:
        return None
    pos = t.find(q)
    if pos >= 0:
        return 1000 - pos + (300 if pos == 0 else 0)
    # 子序列：逐字符向后找，间隔越小得分越高
    idx = 0
    gaps = 0
    last = -1
    for ch in q:
        found = t.find(ch, idx)
        if found < 0:
            return None
        if last >= 0:
            gaps += found - last - 1
        last = found
        idx = found + 1
    return max(1, 500 - gaps)


def conversation_score(query: str, conv) -> int:
    """会话对搜索词的最佳得分（名称 > 用户名 > 最后消息）。"""
    if not query:
        return 0
    best = 0
    for text, weight in ((conv.name, 3), (conv.username, 2),
                         (conv.last_message_text, 1)):
        s = fuzzy_score(query, text or "")
        if s is not None:
            best = max(best, s * weight)
    return best


class QuickSwitcher(FramelessCardDialog):
    """快速切换弹窗：open_requested 携带 Conversation 或 DirectoryEntry。"""

    open_requested = pyqtSignal(object)
    global_search_requested = pyqtSignal(str)   # 关键词 → 聊天记录全局搜索

    def __init__(self, conversations, client=None, theme=None, parent=None):
        super().__init__("快速切换", theme, parent, width=560)
        self._convs = list(conversations or [])
        self._client = client
        self._theme = theme
        self._rows = []
        self._directory_token = 0
        self._global_item = None
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(_SEARCH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(self._search_directory)

        self._search = WxLineEdit(theme, self)
        self._search.setPlaceholderText("搜索会话、频道或用户…")
        self._search.installEventFilter(self)
        self._search.textChanged.connect(self._on_text_changed)
        self._layout.addWidget(self._search)

        self._list = QListWidget(self)
        self._list.setObjectName("QuickSwitchList")
        self._list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._list.setMaximumHeight(380)
        self._list.itemClicked.connect(
            lambda item, _col=0: self._activate_item(item))
        self._list.itemActivated.connect(self._activate_item)
        self._layout.addWidget(self._list)

        self._status = WxLabel("", theme, self)
        self._status.set_variant("hint")
        self._layout.addWidget(self._status)
        self._theme_widgets.extend([self._search, self._status])
        self._apply_list_style()
        self._populate()
        self.setMinimumHeight(220)

    # ------------------------------------------------------------------ #
    def _apply_list_style(self):
        t = self._theme
        bg = _color(t, "conv_bg", "#ffffff")
        border = _color(t, "border", "#e7e7e7")
        hover = _color(t, "conv_hover", "#f2f2f2")
        text = _color(t, "text_primary", "#191919")
        self._list.setStyleSheet(
            f"QListWidget{{background:{bg};border:1px solid {border};"
            f"border-radius:6px;outline:none;color:{text};}}"
            f"QListWidget::item{{border-bottom:1px solid {border};}}"
            f"QListWidget::item:selected{{background:{hover};color:{text};}}")

    def set_theme(self, theme):
        super().set_theme(theme)
        self._theme = theme
        self._apply_list_style()

    def showEvent(self, event):
        super().showEvent(event)
        self._search.setFocus()

    # ------------------------------------------------------------------ #
    def _on_text_changed(self, text: str):
        self._directory_token += 1
        self._populate(text)
        self._search_timer.stop()
        if text.strip() and self._client is not None:
            self._search_timer.start()

    def _populate(self, query: str = ""):
        query = (query or "").strip()
        self._list.clear()
        self._rows = []
        scored = []
        for conv in self._convs:
            s = conversation_score(query, conv)
            if query and s <= 0:
                continue
            scored.append((s, conv))
        scored.sort(key=lambda pair: (
            -pair[0], not pair[1].is_favorite,
            -pair[1].last_message_time))
        for _s, conv in scored[:_LOCAL_LIMIT]:
            self._add_conversation(conv)
        if query:
            self._append_global_row(query)
        self._select_first()
        self._update_status(query)

    def _update_status(self, query: str):
        if not query:
            tip = "输入名称模糊搜索"
            if self._client is not None:
                tip += "；目录结果会在停顿后补上"
            self._status.setText(tip)
            self._status.show()
        elif self._list.count():
            self._status.hide()
        else:
            self._status.setText("没有匹配的会话，正在搜索目录…"
                                 if self._client is not None
                                 else "没有匹配的会话")
            self._status.show()

    def _add_conversation(self, conv):
        if conv.room_type == "d":
            sub = "@" + (conv.username or "")
        else:
            sub = room_type_label(conv)
        preview = " ".join((conv.last_message_text or "").split())
        if preview:
            sub += " · " + preview
        if conv.unread:
            sub = f"未读 {conv.unread} · " + sub
        self._append_row(
            {"kind": "conv", "obj": conv},
            conv.name or conv.username or "?",
            sub,
            avatar_name=conv.name,
            icon_name="" if conv.room_type == "d" else "hash")

    def _append_row(self, data, name: str, sub: str,
                    avatar_name: str = "", icon_name: str = ""):
        item = QListWidgetItem()
        item.setData(Qt.UserRole, data)
        item.setToolTip(sub)
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(10)
        if icon_name:
            glyph = QLabel(row)
            glyph.setFixedSize(30, 30)
            glyph.setAlignment(Qt.AlignCenter)
            glyph.setPixmap(icon(icon_name, 18).pixmap(QSize(18, 18)))
            lay.addWidget(glyph, 0, Qt.AlignVCenter)
        else:
            avatar = AvatarWidget(30, row)
            avatar.set_name(avatar_name or name)
            lay.addWidget(avatar, 0, Qt.AlignVCenter)
        text_lay = QVBoxLayout()
        text_lay.setSpacing(1)
        name_lbl = WxLabel(name, self._theme, row)
        name_lbl.set_variant("bold")
        name_lbl.set_size(f"{_base_size(self._theme) + 1}px")
        sub_lbl = WxLabel(sub, self._theme, row)
        sub_lbl.set_variant("hint")
        text_lay.addWidget(name_lbl)
        text_lay.addWidget(sub_lbl)
        lay.addLayout(text_lay, 1)
        item.setSizeHint(QSize(0, row.sizeHint().height() + 6))
        self._list.addItem(item)
        self._list.setItemWidget(item, row)
        self._rows.append(row)
        return item

    def _select_first(self):
        if self._list.count():
            self._list.setCurrentRow(0)

    # ------------------------------------------------------------------ #
    # 目录搜索
    # ------------------------------------------------------------------ #
    def _search_directory(self):
        if self._client is None:
            return
        query = self._search.text().strip()
        if not query:
            return
        client = self._client
        self._directory_token += 1
        token = self._directory_token

        def work():
            # 同一任务内串行调用：共享 Session 非线程安全
            rooms = client.search_directory_rooms(query, count=20)
            users = client.search_directory_users(query, count=20)
            return rooms, users

        def on_done(result):
            try:
                if not self.isVisible() or token != self._directory_token:
                    return
                rooms, users = result
                self._append_directory(rooms.get("entries") or [],
                                       users.get("entries") or [], query)
            except Exception:  # noqa: BLE001
                log.exception("快速切换目录结果渲染失败")

        def on_fail(err):
            try:
                if self.isVisible() and token == self._directory_token:
                    self._status.setText(f"目录搜索失败：{err}")
                    self._status.show()
            except Exception:  # noqa: BLE001
                log.exception("快速切换目录搜索错误提示失败")

        run_async(work, on_done, on_fail, parent=self)

    def _append_global_row(self, query: str):
        """结果底部固定项：关键词交给聊天记录全局搜索（目录结果插入后仍保持在底部）。"""
        if self._global_item is not None:
            row = self._list.row(self._global_item)
            if row >= 0:
                self._list.takeItem(row)
        self._global_item = self._append_row(
            {"kind": "global", "query": query, "obj": None},
            f"在聊天记录中搜索「{query}」",
            "跨房间扫描最近会话的消息正文",
            icon_name="search")

    def _append_directory(self, rooms, users, query: str):
        conv_rids = {c.id for c in self._convs}
        conv_users = {c.username for c in self._convs if c.username}
        added = 0
        for entry in rooms:
            if added >= _MORE_ROOMS:
                break
            if not entry.rid or entry.rid in conv_rids:
                continue
            sub = "目录 · " + ("私有频道" if entry.is_private else "公开频道")
            if entry.users_count >= 0:
                sub += f" · {entry.users_count}人"
            self._append_row({"kind": "entry", "obj": entry},
                             entry.name or entry.rid, sub,
                             icon_name="hash")
            conv_rids.add(entry.rid)
            added += 1
        user_added = 0
        for entry in users:
            if user_added >= _MORE_USERS:
                break
            if not entry.username or entry.username in conv_users:
                continue
            sub = "目录 · @" + entry.username
            if entry.department:
                sub += " · " + entry.department
            self._append_row({"kind": "entry", "obj": entry},
                             entry.name or entry.username, sub,
                             avatar_name=entry.name or entry.username)
            conv_users.add(entry.username)
            user_added += 1
        if self._list.count() == 0:
            self._status.setText("没有匹配的会话或目录条目")
            self._status.show()
        else:
            self._status.hide()
            if self._list.currentRow() < 0:
                self._select_first()
        if query and self._global_item is not None:
            # 目录结果插到了全局搜索项前面，把它挪回底部
            self._append_global_row(query)

    # ------------------------------------------------------------------ #
    # 键盘 / 打开
    # ------------------------------------------------------------------ #
    def eventFilter(self, obj, event):
        if obj is self._search and event.type() == QEvent.KeyPress:
            key = event.key()
            if key in (Qt.Key_Down, Qt.Key_Up):
                self._move_current(1 if key == Qt.Key_Down else -1)
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                self._activate_current()
                return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)

    def _move_current(self, delta: int):
        count = self._list.count()
        if not count:
            return
        row = self._list.currentRow()
        self._list.setCurrentRow(min(max(row + delta, 0), count - 1))

    def _activate_current(self):
        item = self._list.currentItem()
        if item is not None:
            self._activate_item(item)

    def _activate_item(self, item, *_):
        # itemClicked/itemActivated 在不同 Qt 绑定下可能多带一列参数
        data = item.data(Qt.UserRole) if item is not None else None
        if not isinstance(data, dict):
            return
        if data.get("kind") == "global":
            self.accept()
            self.global_search_requested.emit(data.get("query") or "")
            return
        if data.get("obj") is None:
            return
        obj = data["obj"]
        self.accept()
        self.open_requested.emit(obj)
