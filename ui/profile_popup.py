"""用户资料弹卡：点消息头像唤起，点击其它位置自动关闭。

用 Qt.Popup 语义（与 emoji_picker 一致）：显示时抓取鼠标，
点到卡片外任意位置即关闭，不需要自己装全局事件过滤器。
先用消息里已有的 User 字段立即渲染，再异步补服务器上的资料。
"""
from __future__ import annotations

import logging

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QHBoxLayout, QFrame, QPushButton,
)

from core.models import User
from .user_status import normalize_status_key, status_tooltip
from .widgets import AvatarWidget

log = logging.getLogger(__name__)

# 兼容保留：旧调用方可能仍查这张表；业务统一走 user_status 模块
_STATUS_CN = {
    "online": "在线", "online_mobile": "在线",
    "away": "离开", "away_mobile": "离开",
    "busy": "忙碌", "busy_mobile": "忙碌",
    "dnd": "勿扰", "offline": "离线", "offline_mobile": "离线",
    "none": "",
}


def _label(text: str, color: str, size: int, bold: bool = False) -> QLabel:
    lbl = QLabel(text)
    weight = "bold" if bold else "normal"
    lbl.setStyleSheet(f"color:{color};font-size:{size}px;font-weight:{weight};"
                      "background:transparent;border:none;")
    lbl.setWordWrap(True)
    return lbl


class ProfileCard(QWidget):
    """浮在消息列表上的用户基本信息卡片。"""

    dm_requested = pyqtSignal(str)  # username

    def __init__(self, user: User, parent=None):
        super().__init__(parent, Qt.Popup)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._username = user.username or ""
        self.setFixedSize(260, 250)
        self.setObjectName("ProfileCard")
        self.setStyleSheet(
            "#ProfileCard { background:#ffffff; border:1px solid #e0e0e0;"
            "border-radius:8px; }")

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(10)
        self.avatar = AvatarWidget(48)
        self.avatar.set_name(user.display_name)
        head.addWidget(self.avatar, 0, Qt.AlignTop)
        title = QVBoxLayout()
        title.setSpacing(2)
        self.name_label = _label(user.display_name, "#191919", 15, bold=True)
        self.user_label = _label("@" + (user.username or "?"), "#999999", 12)
        title.addWidget(self.name_label)
        title.addWidget(self.user_label)
        title.addStretch()
        head.addLayout(title, 1)
        root.addLayout(head)

        root.addWidget(self._divider())
        self.status_label = _label("", "#666666", 12)
        self.roles_label = _label("", "#666666", 12)
        self.department_label = _label("", "#666666", 12)
        self.email_label = _label("", "#999999", 12)
        for lbl in (self.status_label, self.roles_label,
                    self.department_label, self.email_label):
            lbl.hide()
            root.addWidget(lbl)
        root.addStretch()

        dm_btn = QPushButton("发送消息")
        dm_btn.setFixedHeight(30)
        dm_btn.setStyleSheet(
            "QPushButton { background:#07C160; color:white; border:none;"
            "border-radius:4px; font-size:13px; }"
            "QPushButton:hover { background:#06ad56; }"
            "QPushButton:pressed { background:#059a4c; }")
        dm_btn.clicked.connect(self._on_dm_clicked)
        root.addWidget(dm_btn)

    def _on_dm_clicked(self, *_):
        self.dm_requested.emit(self._username)
        self.close()

    @staticmethod
    def _divider() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color:#eeeeee;background:#eeeeee;border:none;"
                           "max-height:1px;")
        return line

    # ------------------------------------------------------------------ #
    def enrich(self, client):
        """异步拉 users.info 补齐状态/角色/邮箱，失败就保持现状。"""
        if not client or not self._username:
            return
        from .api_workers import run_async

        def _done(info):
            if info:
                self._apply_info(info)

        def _fail(err):
            log.debug("补用户资料失败 %s（非致命）：%s", self._username, err)

        run_async(lambda: client.fetch_user_info(self._username),
                  on_done=_done, on_fail=_fail, parent=self)

    def _apply_info(self, info: dict):
        name = info.get("name", "")
        if name:
            self.name_label.setText(name)
        username = info.get("username", "")
        if username:
            self.user_label.setText("@" + username)
        status_text = info.get("status_text", "")
        # 与列表/标题栏同一套判定：department 含「停用」→ 已停用。
        # enrich 前的首次渲染 info 可能没有 status 字段，此时不显示状态行，
        # 避免把"未知"误画成"离线"
        raw_status = info.get("status", "")
        key = normalize_status_key(raw_status, info.get("department", ""))
        if raw_status or key == "departed":
            self.status_label.setText(status_tooltip(key, status_text))
            self.status_label.show()
        roles = [r for r in (info.get("roles") or []) if r not in ("user", "bot")]
        if roles:
            self.roles_label.setText("角色：" + "、".join(roles))
            self.roles_label.show()
        if info.get("department"):
            self.department_label.setText("部门：" + info["department"])
            self.department_label.show()
        if info.get("email"):
            self.email_label.setText(info["email"])
            self.email_label.show()
