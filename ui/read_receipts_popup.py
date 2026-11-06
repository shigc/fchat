"""已读/未读名单浮窗：点群聊消息的「N人未读」唤起，点击其它位置自动关闭。

Qt.Popup 语义（与 profile_popup 一致）：显示时抓取鼠标，点到卡片外
任意位置即关闭。先 show_loading 占位，REST 拉到回执与成员列表后
populate 填充。
"""
from __future__ import annotations

import logging

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QHBoxLayout, QFrame, QScrollArea,
)

from core.models import json_ts_to_epoch
from core.messages import mention_scope
from .widgets import AvatarWidget, format_chat_time

log = logging.getLogger(__name__)


def _label(text: str, color: str, size: int, bold: bool = False) -> QLabel:
    lbl = QLabel(text)
    weight = "bold" if bold else "normal"
    lbl.setStyleSheet(f"color:{color};font-size:{size}px;font-weight:{weight};"
                      "background:transparent;border:none;")
    lbl.setWordWrap(True)
    return lbl


class ReadReceiptsPopup(QWidget):
    """群聊消息的已读/未读人员名单卡片。"""

    def __init__(self, message, server_url: str, parent=None,
                 my_user_id: str = ""):
        super().__init__(parent, Qt.Popup)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._message = message
        self._my_id = my_user_id
        self._server_url = (server_url or "").rstrip("/")
        self.setFixedSize(280, 380)
        self.setObjectName("ReadReceiptsPopup")
        self.setStyleSheet(
            "#ReadReceiptsPopup { background:#ffffff; border:1px solid #e0e0e0;"
            "border-radius:8px; }")

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(6)
        self.title_label = _label("", "#191919", 14, bold=True)
        head.addWidget(self.title_label)
        # @所有人 / @在线 徽标
        if mention_scope(message.mentions) == "all":
            badge = _label("@所有人", "#fa9d3b", 11)
            badge.setStyleSheet(
                "color:#fa9d3b;font-size:11px;background:#fff4e6;"
                "border-radius:4px;padding:1px 6px;")
            head.addWidget(badge)
        head.addStretch()
        root.addLayout(head)

        root.addWidget(self._divider())

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setStyleSheet("QScrollArea{background:transparent;"
                                   "border:none;}")
        container = QWidget()
        container.setStyleSheet("background:transparent;")
        self._list = QVBoxLayout(container)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(2)
        self._scroll.setWidget(container)
        root.addWidget(self._scroll)

    @staticmethod
    def _divider() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color:#eeeeee;background:#eeeeee;border:none;"
                           "max-height:1px;")
        return line

    def _clear_list(self):
        while self._list.count():
            item = self._list.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def show_loading(self):
        self.title_label.setText("读取中…")
        self._clear_list()
        hint = _label("正在获取已读名单", "#999999", 12)
        self._list.addWidget(hint)
        self._list.addStretch()

    def populate(self, receipts: list, members: list, avatar_loader=None):
        """receipts: [{messageId, userId, ts}]（应已剔除登录者自己的回执）；
        members: [{_id, username, name}]。"""
        self._clear_list()
        # 排除登录者自己（不是消息发送者——点开别人消息的名单时 sender
        # 是别人，拿 sender 当自己会把发送者错排进/错出未读名单）

        # 同一人多次回执取最早一次（去重）；按已读时间先后排列。
        # 自己的回执在这里也兜底剔除（上游 _drop_own_receipt 漏掉时，
        # 已读会多 1、行内与浮窗数字对不上）
        first_ts: dict = {}
        for r in receipts or []:
            uid = r.get("userId", "")
            if not uid or uid == self._my_id:
                continue
            ts = json_ts_to_epoch(r.get("ts"))
            if uid not in first_ts or (ts and ts < first_ts[uid][1]):
                first_ts[uid] = (r, ts)
        by_id = {m.get("_id"): m for m in members or []}

        read_rows = sorted(first_ts.items(), key=lambda kv: kv[1][1])
        unread_rows = [m for _id, m in (by_id or {}).items()
                       if _id not in first_ts and _id != self._my_id]

        self.title_label.setText(
            f"已读 {len(read_rows)} 人 · 未读 {len(unread_rows)} 人")

        if not read_rows and not unread_rows:
            self._list.addWidget(_label("暂无成员数据", "#999999", 12))
            self._list.addStretch()
            return

        self._section("已读", "#07C160")
        if not read_rows:
            self._list.addWidget(_label("还没有人已读", "#bbbbbb", 12))
        for uid, (r, ts) in read_rows:
            self._member_row(by_id.get(uid) or {"_id": uid},
                             ts_text=format_chat_time(ts) if ts else "",
                             avatar_loader=avatar_loader)
        self._list.addSpacing(6)
        self._section("未读", "#999999")
        if not unread_rows:
            self._list.addWidget(_label("全部已读", "#bbbbbb", 12))
        for m in unread_rows:
            self._member_row(m, ts_text="", avatar_loader=avatar_loader)
        self._list.addStretch()

    def _section(self, text: str, color: str):
        lbl = _label(text, color, 12, bold=True)
        self._list.addWidget(lbl)

    def _member_row(self, member: dict, ts_text: str, avatar_loader=None):
        row = QWidget()
        row.setStyleSheet("background:transparent;")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(8)
        avatar = AvatarWidget(24)
        name = (member.get("name") or member.get("username")
                or member.get("_id") or "?")
        avatar.set_name(name)
        lay.addWidget(avatar, 0, Qt.AlignTop)
        name_lbl = _label(name, "#333333", 13)
        lay.addWidget(name_lbl, 1)
        if ts_text:
            lay.addWidget(_label(ts_text, "#bbbbbb", 11))
        self._list.addWidget(row)
        if avatar_loader and self._server_url and member.get("username"):
            avatar_loader.load(
                avatar, f"{self._server_url}/avatar/{member['username']}")
