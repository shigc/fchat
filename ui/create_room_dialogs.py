"""新建房间对话框：私聊 / 频道 / 团队 / 讨论串。

复用 ui/widgets.py 的 Wx 组件集取语义色（随亮/暗主题切换），对话框外
观由 main_window 在 _refresh_theme 时对本类控件的 set_theme(theme)
刷新。这里只做输入与结果收集，真正调用 ApiClient 建房间由 main_window
负责（网络操作不在 Qt 槽里同步执行）。
"""
from __future__ import annotations

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QHBoxLayout, QVBoxLayout, QListWidget, QListWidgetItem, QWidget,
)

from .api_workers import run_async
from .dialog_base import FramelessCardDialog
from .widgets import WxLabel, WxLineEdit, WxComboBox, WxPushButton


class MemberSelector(QWidget):
    """可复用成员选择控件：输入即搜索候选人，点选进入已选列表，可点选移除。

    传入 search_fn(query) -> List[User]，内部用 run_async 在线程里搜，回到
    主线程更新列表（遵守“跨线程回调转 Qt 信号”约定）。
    """

    def __init__(self, search_fn, theme=None, parent=None, single=False):
        super().__init__(parent)
        self._search_fn = search_fn
        self._theme = theme
        self._single = single
        self._theme_widgets: list = []
        # username -> 显示名（选中集合，保留顺序）
        self._selected: dict = {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        self._input = WxLineEdit(theme, self)
        # spotlight 服务端同时匹配用户名与真实姓名（姓名可检索）
        self._input.setPlaceholderText("输入用户名或姓名搜索，点击加入成员")
        lay.addWidget(self._input)
        self._theme_widgets.append(self._input)

        self._candidates = QListWidget(self)
        self._candidates.setMaximumHeight(130)
        lay.addWidget(self._candidates)

        sel_lbl = WxLabel("已选成员", theme, self)
        lay.addWidget(sel_lbl)

        self._selected_list = QListWidget(self)
        self._selected_list.setMaximumHeight(90)
        lay.addWidget(self._selected_list)
        self._theme_widgets.append(sel_lbl)

        # 输入防抖：350ms 后触发搜索，避免每敲一字打一次接口
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(350)
        self._timer.timeout.connect(self._do_search)
        self._input.textChanged.connect(self._schedule_search)
        self._candidates.itemClicked.connect(self._add_candidate)
        self._selected_list.itemClicked.connect(self._remove_selected)

    @property
    def selected_usernames(self) -> list:
        return list(self._selected.keys())

    def set_theme(self, theme) -> None:
        self._theme = theme
        for w in self._theme_widgets:
            try:
                w.set_theme(theme)
            except AttributeError:
                pass
        self._apply_list_style()

    def _apply_list_style(self):
        t = self._theme
        from .widgets import _color
        bg = _color(t, "input_bg", "#ffffff")
        border = _color(t, "input_border", "#e0e0e0")
        text = _color(t, "text_primary", "#191919")
        hover = _color(t, "conv_hover", "#f2f2f2")
        sel = _color(t, "conv_hover", "#f2f2f2")
        qss = (
            f"QListWidget{{background:{bg};border:1px solid {border};"
            f"border-radius:6px;color:{text};}} "
            f"QListWidget::item{{padding:4px 6px;border-radius:4px;}} "
            f"QListWidget::item:hover{{background:{hover};}} "
            f"QListWidget::item:selected{{background:{sel};color:{text};}}"
        )
        self._candidates.setStyleSheet(qss)
        self._selected_list.setStyleSheet(qss)

    def _schedule_search(self, text: str):
        self._timer.start()

    def _do_search(self):
        q = self._input.text().strip()
        if not q:
            self._candidates.clear()
            return

        def _done(users):
            # 忽略旧查询的晚到结果：只显示与最新输入匹配的
            if q != self._input.text().strip():
                return
            self._candidates.clear()
            seen = set()
            for u in users:
                # 服务端 spotlight 偶发重复返回同一用户，这里按用户名兜底去重
                if u.username in self._selected or u.username.casefold() in seen:
                    continue
                seen.add(u.username.casefold())
                name = u.name or u.username or u.id
                display = f"{name} @{u.username}" if u.username and u.username != name \
                    else (name or u.username)
                item = QListWidgetItem(display)
                item.setData(Qt.UserRole, u.username)
                self._candidates.addItem(item)

        def _fail(err):
            self._candidates.clear()
            item = QListWidgetItem(f"搜索失败：{err}")
            item.setFlags(Qt.NoItemFlags)
            self._candidates.addItem(item)

        run_async(lambda: self._search_fn(q), on_done=_done, on_fail=_fail,
                  parent=self)

    def _add_candidate(self, item, *_):
        username = item.data(Qt.UserRole)
        if not username or username in self._selected:
            return
        if self._single and self._selected:
            # 单选模式：新选中替换旧选中（私聊只允许一个人）
            self._selected.clear()
        self._selected[username] = item.text()
        self._refresh_selected()
        # 已选中用户不再出现在候选人里
        row = self._candidates.row(item)
        self._candidates.takeItem(row)

    def _remove_selected(self, item, *_):
        username = item.data(Qt.UserRole)
        if username in self._selected:
            del self._selected[username]
        self._refresh_selected()

    def _refresh_selected(self):
        self._selected_list.clear()
        for username, display in self._selected.items():
            item = QListWidgetItem(f"{display}  ✕")
            item.setData(Qt.UserRole, username)
            self._selected_list.addItem(item)


class _BaseCreateDialog(FramelessCardDialog):
    """对话框公共骨架：标题头部（基类提供）+ 字段区 + 取消/确定按钮。"""

    def __init__(self, title: str, theme=None, parent=None):
        super().__init__(title, theme, parent)

    def _add_field(self, label_text: str, widget) -> None:
        row = QVBoxLayout()
        lbl = WxLabel(label_text, self._theme, self)
        row.addWidget(lbl)
        row.addWidget(widget)
        self._layout.addLayout(row)
        self._theme_widgets.append(lbl)
        self._theme_widgets.append(widget)

    def _add_buttons(self, ok_text: str, cancel_text: str = "取消") -> None:
        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = WxPushButton(cancel_text, self._theme, self)
        cancel.clicked.connect(self.reject)
        ok = WxPushButton(ok_text, self._theme, self)
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        self._layout.addLayout(btns)
        self._ok_btn = ok
        self._cancel_btn = cancel
        self._theme_widgets.extend([cancel, ok])


class CreateRoomDialog(_BaseCreateDialog):
    """创建频道/团队：名称（必填）+ 可见性（公开/私有）+ 描述（可选）+ 可选成员。

    kind: "channel"=频道, "team"=团队。官方语义：频道公开走 channels.create、
    私有走 groups.create；团队统一走 teams.create（公开/私有）。默认
    频道公开、团队私有。API 选路由 main_window 按 result_private 决定。
    """

    def __init__(self, kind: str, theme=None, parent=None, search_fn=None):
        label = "频道" if kind == "channel" else "团队"
        super().__init__(f"新建{label}", theme, parent)
        self.kind = kind
        self.result_name = ""
        self.result_topic = ""
        self.result_members: list = []
        self.result_private = (kind == "team")
        self._name = WxLineEdit(theme, self)
        self._name.setPlaceholderText(f"请输入{label}名称")
        self._topic = WxLineEdit(theme, self)
        self._topic.setPlaceholderText("描述（可选）")
        self._visibility = WxComboBox(theme, self)
        self._visibility.addItem("公开（所有人可见，可自由加入）", False)
        self._visibility.addItem("私有（仅受邀成员可见）", True)
        self._visibility.setCurrentIndex(0 if kind == "channel" else 1)
        self._add_field(f"{label}名称", self._name)
        self._add_field("可见性", self._visibility)
        self._add_field("描述", self._topic)
        # 可选成员：一次性加入多人（search_fn None 时不显示成员选择）
        if search_fn is not None:
            self._members = MemberSelector(search_fn, theme, self)
            self._add_field("添加成员", self._members)
            self._theme_widgets.append(self._members)
        self._add_buttons("创建")
        self._name.setFocus()

    def accept(self, *_):
        name = self._name.text().strip()
        if not name:
            self._name.setFocus()
            return
        self.result_name = name
        self.result_topic = self._topic.text().strip()
        self.result_private = bool(self._visibility.currentData())
        if hasattr(self, "_members"):
            self.result_members = self._members.selected_usernames
        super().accept()


class CreateDmDialog(_BaseCreateDialog):
    """发起私聊：输入用户名或姓名搜索，从候选列表中单选一人。"""

    def __init__(self, theme=None, parent=None, search_fn=None):
        super().__init__("发起私聊", theme, parent)
        self.username = ""
        # 单选成员选择器（私聊只选一人），search_fn=None 时只保留输入框
        self._members = MemberSelector(search_fn or (lambda q: []), theme, self,
                                       single=True)
        self._add_field("选择用户", self._members)
        self._add_buttons("开始聊天")
        self._members._input.setFocus()

    def accept(self, *_):
        users = self._members.selected_usernames
        if not users:
            # 未选人就点确定：聚焦回搜索框，不关闭
            self._members._input.setFocus()
            return
        self.username = users[0]
        super().accept()


class AddMemberDialog(_BaseCreateDialog):
    """添加成员到房间：搜索单选一个用户，确定后由 main_window 调 API。"""

    def __init__(self, theme=None, parent=None, search_fn=None):
        super().__init__("添加成员", theme, parent)
        self.username = ""
        self._members = MemberSelector(search_fn or (lambda q: []), theme, self,
                                       single=True)
        self._add_field("选择用户", self._members)
        self._add_buttons("添加")
        self._members._input.setFocus()

    def accept(self, *_):
        users = self._members.selected_usernames
        if not users:
            self._members._input.setFocus()
            return
        self.username = users[0]
        super().accept()


class CreateDiscussionDialog(_BaseCreateDialog):
    """创建讨论串：讨论名 + 父房间下拉。"""

    def __init__(self, conversations, theme=None, parent=None):
        super().__init__("新建讨论串", theme, parent)
        self.result_name = ""
        self.result_parent_id = ""
        self._name = WxLineEdit(theme, self)
        self._name.setPlaceholderText("请输入讨论串名称")
        self._parent = WxComboBox(theme, self)
        # 只允许把频道/团队/私有频道作为父房间（讨论必须挂在某房间下）
        for conv in conversations:
            if conv.room_type in ("c", "p"):
                self._parent.addItem(conv.name or conv.id, conv.id)
        self._add_field("讨论串名称", self._name)
        self._add_field("父房间", self._parent)
        self._add_buttons("创建")
        self._name.setFocus()

    def accept(self, *_):
        name = self._name.text().strip()
        if not name:
            self._name.setFocus()
            return
        pid = self._parent.currentData()
        if not pid:
            return
        self.result_name = name
        self.result_parent_id = pid
        super().accept()
