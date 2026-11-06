"""房间信息浮卡：频道/团队「查看房间信息」菜单项唤起。

Qt.Popup 语义照 ProfileCard：点卡片外任意位置即关闭。纯展示无输入框，
不触 Windows 上 Popup 无法合成输入的 IME 限制（那只针对含输入框的弹窗）。
先用会话已有字段立即渲染，再异步拉 rooms.info 补齐——rooms.info 带真实
成员数/创建时间/创建者/encrypted，顺带把加密状态回写会话对象校准菜单文案
（服务端没有房间设置变更的实时推送，这是唯一的新鲜度校准点）。
"""
from __future__ import annotations

import logging
import time

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QFrame

from core.models import Conversation
from .widgets import AvatarWidget, WxLabel, _color, room_type_label

log = logging.getLogger(__name__)


def _fmt_ts(epoch: float) -> str:
    if not epoch:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


class RoomInfoCard(QWidget):
    """浮在聊天区上的房间基本信息卡片（频道/团队共用）。"""

    def __init__(self, conv: Conversation, theme=None, parent=None):
        super().__init__(parent, Qt.Popup)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._conv = conv
        self._theme = theme
        self.setFixedWidth(300)
        self.setObjectName("RoomInfoCard")
        self.setStyleSheet(
            f"#RoomInfoCard {{ background:{_color(theme, 'conv_bg', '#ffffff')};"
            f" border:1px solid {_color(theme, 'border', '#e0e0e0')};"
            f"border-radius:8px; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(10)
        self.avatar = AvatarWidget(48)
        self.avatar.set_name(conv.name or "?")
        head.addWidget(self.avatar, 0, Qt.AlignTop)
        title = QVBoxLayout()
        title.setSpacing(2)
        self.name_label = WxLabel(conv.name or "未命名", theme)
        self.name_label.set_variant("bold")
        self.type_label = WxLabel(room_type_label(conv), theme)
        self.type_label.set_variant("secondary")
        title.addWidget(self.name_label)
        title.addWidget(self.type_label)
        title.addStretch()
        head.addLayout(title, 1)
        root.addLayout(head)

        root.addWidget(self._divider())

        # 主题/描述：没有就整行隐藏
        self.topic_label = WxLabel("", theme)
        self.topic_label.set_variant("secondary")
        self.topic_label.setWordWrap(True)
        self.topic_label.hide()
        root.addWidget(self.topic_label)

        # 明细行：标签 + 值，取不到数据的行隐藏
        self._rows = {}
        for key, caption in (("members", "成员"), ("created", "创建"),
                             ("creator", "创建者"), ("e2e", "端到端加密")):
            row = QHBoxLayout()
            row.setSpacing(8)
            cap = WxLabel(caption, theme)
            cap.set_variant("hint")
            val = WxLabel("", theme)
            val.set_variant("primary")
            row.addWidget(cap, 0)
            row.addWidget(val, 1)
            root.addLayout(row)
            self._rows[key] = val
            for w in (cap, val):
                w.hide()
            # caption 与 value 同步显隐：记下 caption 以便 _set_row 一并处理
            self._rows[key + "_cap"] = cap

        root.addStretch()

        self._apply_conv()

    # ------------------------------------------------------------------ #
    def _set_row(self, key: str, value: str):
        """填充明细行；值为空隐藏整行（标签 + 值一起）。"""
        val = self._rows.get(key)
        cap = self._rows.get(key + "_cap")
        if val is None or cap is None:
            return
        val.setText(value)
        visible = bool(value)
        val.setVisible(visible)
        cap.setVisible(visible)

    def _apply_conv(self):
        """用会话已有字段先填一版（成员数/创建信息拉到后覆盖）。"""
        conv = self._conv
        self._set_row("e2e", "已开启" if conv.encrypted else "未开启")

    def _divider(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet(
            f"color:{_color(self._theme, 'border', '#eeeeee')};"
            f"background:{_color(self._theme, 'border', '#eeeeee')};"
            "border:none;max-height:1px;")
        return line

    # ------------------------------------------------------------------ #
    def enrich(self, client, room_id: str):
        """异步拉 rooms.info 补齐成员数/创建时间/创建者，失败保持现状。"""
        if not client or not room_id:
            return
        from .api_workers import run_async

        def _done(info):
            if info:
                self._apply(info)

        def _fail(err):
            log.debug("补房间信息失败 %s（非致命）：%s", room_id, err)

        run_async(lambda: client.fetch_room_info(room_id),
                  on_done=_done, on_fail=_fail, parent=self)

    def _apply(self, info: dict):
        conv = self._conv
        name = str(info.get("fname") or info.get("name") or "").strip()
        if name:
            self.name_label.setText(name)
            self.avatar.set_name(name)
        topic = str(info.get("topic") or info.get("description") or "").strip()
        if topic:
            self.topic_label.setText(topic)
            self.topic_label.show()
        users = info.get("usersCount")
        if users is not None:
            try:
                self._set_row("members", f"{int(users)} 人")
            except (TypeError, ValueError):
                pass
        created = info.get("ts")
        if created:
            from core.models import ts_to_epoch
            self._set_row("created", _fmt_ts(ts_to_epoch(created)))
        creator = info.get("u") or {}
        creator_text = str(creator.get("name") or creator.get("username") or "").strip()
        if creator_text:
            self._set_row("creator", creator_text)
        # 校准加密状态：rooms.info 的 encrypted 是权威值，回写会话对象
        # 让下次菜单文案直接正确（无实时推送时的唯一校准点）
        if "encrypted" in info:
            conv.encrypted = bool(info.get("encrypted"))
            self._set_row("e2e", "已开启" if conv.encrypted else "未开启")
        self.adjustSize()
