"""会话列表：微信风格，头像 + 昵称 + 最后消息预览 + 时间 + 未读红点。"""
from __future__ import annotations

import logging

from PyQt5.QtCore import Qt, QSize, pyqtSignal
from PyQt5.QtWidgets import (
    QListWidget, QListWidgetItem, QHBoxLayout, QVBoxLayout,
    QLabel, QFrame, QMenu, QAbstractItemView, QSizePolicy,
)

from core.models import Conversation
import config
from .icons import icon
from .user_status import normalize_status_key, status_tooltip
from .widgets import (AvatarWidget, format_timestamp, _color,
                      room_type_label)

log = logging.getLogger(__name__)


class ConversationItemWidget(QFrame):
    """单个会话条目。支持右键菜单：收藏 / 删除。"""

    # 信号：由 ConversationList 捕获并冒泡到 MainWindow
    favorite_requested = pyqtSignal(str)   # room_id
    delete_requested = pyqtSignal(str)   # room_id
    mute_requested = pyqtSignal(str)     # room_id — 切换消息免打扰
    mark_unread_requested = pyqtSignal(str)   # room_id — 标为未读

    def __init__(self, conv: Conversation, avatar_loader=None, theme=None,
                 parent=None):
        super().__init__(parent)
        self.conv = conv
        self._avatar_loader = avatar_loader
        self._theme = theme
        # 草稿标识：有未发送草稿的会话在预览前加「[草稿]」前缀
        self._has_draft = False
        self.setObjectName("ConversationItem")
        self.setCursor(Qt.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(12)

        # 头像
        self.avatar = AvatarWidget(44)
        self.avatar.set_name(conv.name)
        self._load_avatar()
        layout.addWidget(self.avatar, 0, Qt.AlignTop)

        # 中间：名称行（名称 + 免打扰 + @徽标） + 最后消息
        mid = QVBoxLayout()
        mid.setSpacing(4)
        name_text = conv.name
        if conv.is_favorite:
            name_text = f"⭐ {name_text}"
        name_row = QHBoxLayout()
        name_row.setSpacing(4)
        self.name_label = QLabel(name_text)
        # 这两个要挂 parent：创建后紧跟 setVisible(True)，而此时 mid 子
        # 布局尚未被加进 self 的布局，QLabel 若无 parent 会先成为独立
        # 顶层窗口，在屏幕上闪现（启动时小窗闪现的根因）
        self.mute_label = QLabel("免打扰", self)
        self.mention_badge = QLabel("@", self)
        self.mention_badge.setAlignment(Qt.AlignCenter)
        self.mention_badge.setFixedSize(14, 14)
        name_row.addWidget(self.name_label, 1)
        name_row.addWidget(self.mute_label, 0, Qt.AlignVCenter)
        name_row.addWidget(self.mention_badge, 0, Qt.AlignVCenter)
        mid.addLayout(name_row)
        self.mute_label.setVisible(bool(conv.muted))
        self.mention_badge.setVisible(bool(conv.mention_count))
        self.preview_label = QLabel(self._preview_text(conv))
        self.preview_label.setWordWrap(False)
        self._apply_colors()
        # 单行 + 不参与撑开行宽，超出部分由 QLabel 省略号截断
        for lbl in (self.name_label, self.preview_label):
            lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        mid.addWidget(self.preview_label)
        layout.addLayout(mid, 1)

        # 右侧：时间 + 未读
        right = QVBoxLayout()
        right.setSpacing(6)
        self.time_label = QLabel(format_timestamp(conv.last_message_time))
        self.time_label.setObjectName("TimeLabel")
        self.time_label.setAlignment(Qt.AlignRight | Qt.AlignTop)
        self.badge = QLabel("")
        self.badge.setAlignment(Qt.AlignCenter)
        self.badge.setStyleSheet(
            "background:#fa5151;color:white;border-radius:9px;"
            "min-width:18px;max-width:18px;min-height:18px;max-height:18px;"
            "font-size:11px;"
        )
        self.badge.hide()
        right.addWidget(self.time_label)
        right.addStretch()
        right.addWidget(self.badge, 0, Qt.AlignRight | Qt.AlignBottom)
        layout.addLayout(right)

        self.setFixedHeight(64)
        self.update_badge()

    def _load_avatar(self):
        if self._avatar_loader and self.conv.avatar_url:
            self._avatar_loader.load(self.avatar, self.conv.avatar_url)

    def set_theme(self, theme):
        self._theme = theme
        self._apply_colors()

    def _apply_colors(self):
        # 名称/预览用语义色并按设置的基础字号推算，随亮/暗主题与字号设置切换
        # （原硬编码 #191919/15px 在暗色或大字号下都不对）
        from .widgets import _base_size
        base = _base_size(self._theme)
        self.name_label.setStyleSheet(
            "color:"
            f"{_color(self._theme, 'text_primary', '#191919')};"
            f"font-size:{base + 2}px;font-weight:bold;")
        self.preview_label.setStyleSheet(
            "color:"
            f"{_color(self._theme, 'text_secondary', '#999999')};"
            f"font-size:{base}px;")
        self.mute_label.setStyleSheet(
            "color:"
            f"{_color(self._theme, 'text_tertiary', '#999999')};"
            f"font-size:{max(9, base - 2)}px;")
        self.mention_badge.setStyleSheet(
            "background:"
            f"{_color(self._theme, 'danger_fg', '#e64340')};"
            "color:#ffffff;border-radius:7px;font-size:9px;font-weight:bold;")

    def _preview_text(self, conv: Conversation) -> str:
        # 预览只允许单行：代码块等带换行的文本会把 QLabel 撑高，
        # 行的 sizeHint 跟着变大而控件固定 64 高，行里多出一截空白
        text = " ".join((conv.last_message_text or "").split())
        if self._has_draft:
            return f"[草稿] {text}"
        return text

    def set_has_draft(self, has_draft: bool):
        """标注该会话是否有未发送草稿（预览前加 [草稿] 前缀）。"""
        has_draft = bool(has_draft)
        if has_draft != self._has_draft:
            self._has_draft = has_draft
            self.preview_label.setText(self._preview_text(self.conv))

    def update_badge(self):
        if self.conv.unread and self.conv.unread > 0:
            n = self.conv.unread if self.conv.unread < 100 else "99+"
            self.badge.setText(str(n))
            self.badge.show()
        else:
            self.badge.hide()

    # ------------------------------------------------------------------ #
    # 右键菜单
    # ------------------------------------------------------------------ #
    def contextMenuEvent(self, event):
        menu = QMenu(self)
        conv = self.conv

        # 收藏 / 取消收藏
        if conv.is_favorite:
            pin_action = menu.addAction(icon("unfavorite"), "取消收藏")
        else:
            pin_action = menu.addAction(icon("favorite"), "收藏")
        pin_action.triggered.connect(
            lambda: self.favorite_requested.emit(conv.id))

        # 消息免打扰：服务器 rooms.saveNotification 同步（两态）
        mute_action = menu.addAction(
            "取消免打扰" if conv.muted else "消息免打扰")
        mute_action.triggered.connect(
            lambda _=False: self.mute_requested.emit(conv.id))

        # 标为未读：本地红点 +1，不真正动服务器已读（真机确认端点后接）
        unread_action = menu.addAction(
            icon("mark_unread"), "标为未读")
        unread_action.triggered.connect(
            lambda: self.mark_unread_requested.emit(conv.id))

        menu.addSeparator()

        # 删除 / 退出
        if conv.room_type == "d":
            del_action = menu.addAction(icon("delete"), "删除会话")
        else:
            del_action = menu.addAction(
                icon("leave"), f"退出{room_type_label(conv)}")
        del_action.triggered.connect(
            lambda: self.delete_requested.emit(conv.id))

        menu.exec_(event.globalPos())

    def refresh(self, conv: Conversation):
        """数据刷新后更新显示。"""
        self.conv = conv
        name_text = conv.name
        if conv.is_favorite:
            name_text = f"⭐ {name_text}"
        self.name_label.setText(name_text)
        self.preview_label.setText(self._preview_text(conv))
        self.time_label.setText(format_timestamp(conv.last_message_time))
        self.avatar.set_name(conv.name)
        self.mute_label.setVisible(bool(conv.muted))
        self.mention_badge.setVisible(bool(conv.mention_count))
        self._load_avatar()
        self.update_badge()

    def set_user_status(self, info):
        """私聊对方的在线状态点 + 悬停说明。info 为 None 时维持现状。"""
        if not info or self.conv.room_type != "d":
            return
        key = info.get("status_key") or normalize_status_key(
            info.get("status", ""), info.get("department", ""))
        self.avatar.set_status_key(key)
        self.avatar.set_status_tooltip(
            status_tooltip(key, info.get("status_text", "")))


class _GroupHeader(QFrame):
    """会话分组头行（收藏区折叠开关）。行高 32，整行可点。"""

    toggled = pyqtSignal()

    def __init__(self, theme=None, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._collapsed = False
        self._count = 0
        self.setObjectName("ConvGroupHeader")
        self.setFixedHeight(32)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 12, 0)
        self.label = QLabel("", self)
        lay.addWidget(self.label, 1)
        self._apply_colors()

    def update_state(self, count: int, collapsed: bool):
        self._count = count
        self._collapsed = collapsed
        self.label.setText(
            f"⭐ 收藏 ({count})  {'▸' if collapsed else '▾'}")

    def set_theme(self, theme):
        self._theme = theme
        self._apply_colors()

    def _apply_colors(self):
        from .widgets import _base_size
        base = _base_size(self._theme)
        self.label.setStyleSheet(
            "color:"
            f"{_color(self._theme, 'text_secondary', '#999999')};"
            f"font-size:{max(9, base - 1)}px;font-weight:bold;")

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.toggled.emit()
        super().mouseReleaseEvent(event)


class ConversationList(QListWidget):
    """会话列表控件。固定按「收藏 → 最后消息时间倒序」排列，不支持手动拖拽。"""

    conversation_selected = pyqtSignal(object)  # Conversation
    favorite_requested = pyqtSignal(str)        # room_id
    delete_requested = pyqtSignal(str)          # room_id
    room_mute_requested = pyqtSignal(str)       # room_id — 切换免打扰
    mark_unread_requested = pyqtSignal(str)     # room_id — 标为未读

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ConversationList")
        self.setSpacing(1)
        self.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        # 横向一律不出条：行宽跟随视口，超长文本由 label 自行省略
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.currentItemChanged.connect(self._on_current_changed)
        self._conv_by_id = {}
        self._convs = {}             # rid -> Conversation，排序依据
        # 私聊对方 username -> 状态 info。`_render` 重建行会销毁行 widget，
        # 状态挂在这里而不是行上，重建行时回填
        self._status_cache = {}
        self._filter = ""            # 搜索关键字：空=显示全部
        # rid -> 该会话已加载消息的全文索引（小写拼接），供顶部搜索过滤聊天记录
        self._content_index = {}
        self._resorting = False      # 重排期间屏蔽选中变化，避免会话被反复重开
        self.avatar_loader = None
        self._theme = None           # 主题切换时刷到所有已建行
        # 有未发送草稿的房间 id 集合（main_window 注入）；预览前加 [草稿] 前缀
        self._draft_ids: set = set()
        # 启动时一次全量建行会卡住主线程（几百个 widget 同步创建）。
        # 这里按页惰性补行：只建首屏，滚动接近底部再增量补建。
        self._page_size = 20
        self._fill_all = True        # 全部行是否都已建出
        # 已建出的会话行数（分组头行不计入）——_fill_more 的显式数据游标。
        # 行里混着分组头后不能再拿 self.count() 当数据索引
        self._built_rows = 0
        self._fav_header_added = False
        try:
            self._fav_collapsed = bool(
                config.load_config().get("collapsed_favorites", False))
        except Exception:  # noqa: BLE001  测试环境配置被换走时按默认展开
            self._fav_collapsed = False
        self.verticalScrollBar().valueChanged.connect(self._on_scroll_fill)

    # ------------------------------------------------------------------ #
    def set_draft_rooms(self, draft_ids: set):
        """注入有草稿的房间 id 集合，并刷新已建行的草稿标识。"""
        self._draft_ids = set(draft_ids or [])
        for rid, item in self._conv_by_id.items():
            w = self.itemWidget(item)
            if w is not None and hasattr(w, "set_has_draft"):
                w.set_has_draft(rid in self._draft_ids)

    def set_avatar_loader(self, loader):
        self.avatar_loader = loader

    def set_theme(self, theme):
        """注入取色源并刷新所有已建行（未建的惰性行创建时再取色）。"""
        self._theme = theme
        for i in range(self.count()):
            w = self.itemWidget(self.item(i))
            if w is not None:
                w.set_theme(theme)

    def clear_conversations(self):
        self.clear()
        self._conv_by_id.clear()
        self._convs.clear()
        self._status_cache.clear()

    def apply_status(self, username: str, info):
        """私聊对方状态更新：回填缓存并刷新所有匹配行。

        返回匹配到的已建行数；0 命中说明行匹配链路出了问题（缓存与
        行的 username 对不上 / 行还没建），打 INFO 便于生产定位。
        """
        if not username:
            return 0
        if info is not None:
            self._status_cache[username] = info
        info = info or self._status_cache.get(username)
        if not info:
            return 0
        matched = 0
        for conv in self._convs.values():
            if conv.room_type == "d" and conv.username == username:
                item = self._conv_by_id.get(conv.id)
                widget = self.itemWidget(item) if item else None
                if widget is not None:
                    widget.set_user_status(info)
                    matched += 1
        if matched == 0:
            # 未命中多半是懒分页没建到的行（首屏只建前 20 行），属预期；
            # 真异常（可见行匹配失败）靠前 20 行的用户名对照才能分辨，
            # 所以只记 debug 级别
            log.debug("状态回填未命中已建行：username=%s 会话=%d 行=%d",
                      username, len(self._convs), self.count())
        return matched

    def set_conversations(self, conversations):
        """全量刷新会话列表，保持当前选中项。"""
        self._convs = {c.id: c for c in conversations}
        self._render()

    def upsert_conversation(self, conv: Conversation, select: bool = False):
        """插入或更新单个会话。顺序没变只刷那一行，变了才重建。"""
        self._convs[conv.id] = conv
        item = self._conv_by_id.get(conv.id)
        if item is not None and self._row_ids() == self._expected_row_ids():
            self.itemWidget(item).refresh(conv)
        else:
            self._render()
        if select:
            self.select_conversation(conv.id)

    def remove_conversation(self, conv_id: str):
        self._convs.pop(conv_id, None)
        item = self._conv_by_id.pop(conv_id, None)
        if item:
            self.takeItem(self.row(item))

    def select_conversation(self, conv_id: str):
        item = self._conv_by_id.get(conv_id)
        if item:
            self.setCurrentItem(item)

    def set_search_filter(self, text: str, content_index: dict = None):
        """按关键字过滤会话列表：匹配会话名 / 对方 username / 显示名，
        以及该会话**已加载消息**的全文内容（策略见 `_matches`）。空=显示全部。
        `content_index`：rid -> 该会话消息文本的小写拼接，由上层（MainWindow）
        根据消息缓存传入，用于按聊天记录内容筛出对应会话。
        """
        self._filter = (text or "").strip().lower()
        self._content_index = content_index or {}
        self._render()

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    def _matches(self, conv: Conversation) -> bool:
        if not self._filter:
            return True
        # 匹配源分两层：
        # 1) 会话名 / 对方用户名 / 最后消息预览 —— 稳定、始终可用；
        # 2) 该会话**已加载**消息的全文索引（来自上层传入的 content_index），
        #    覆盖聊天记录里的历史关键词（不一定是最新一条）。两者做或匹配。
        hay = " ".join((conv.name or "", conv.username or "",
                        conv.last_message_text or "",
                        self._content_index.get(conv.id, ""))).lower()
        return self._filter in hay

    def _sorted_ids(self) -> list:
        """排序规则：收藏优先，其余按最后消息时间倒序；先按关键字过滤。"""
        convs = [c for c in self._convs.values() if self._matches(c)]
        convs = sorted(convs,
                       key=lambda c: (not c.is_favorite, -c.last_message_time))
        return [c.id for c in convs]

    def _expected_row_ids(self) -> list:
        """当前折叠状态下应显示的会话 id 序列（分组头不含在内）。

        收藏区折叠时收藏会话整组隐藏，但头行仍要显示（点击展开）。
        搜索过滤时不分组不折叠：用户要的是跨组平铺命中结果。
        """
        ids = self._sorted_ids()
        if self._filter or not self._fav_collapsed:
            return ids
        return [rid for rid in ids if not self._convs[rid].is_favorite]

    def _row_ids(self) -> list:
        out = []
        for i in range(self.count()):
            rid = self.item(i).data(Qt.UserRole)
            if rid:
                out.append(rid)
        return out

    def _render(self):
        """按排序结果重建行。

        行 widget 无法在行间搬移（takeItem 会连带销毁 widget），
        所以顺序变化时只能整体重建；头像走 ImageLoader 缓存，不会重复请求。
        这里只重建首屏一页（惰性），其余待滚动时补建，避免启动/重排卡住主线程。
        """
        current_id = None
        if self.currentItem():
            current_id = self.currentItem().data(Qt.UserRole)
        self.clear()
        self._conv_by_id.clear()
        vis = self._expected_row_ids()
        self._fill_all = len(vis) <= self._page_size
        batch = vis if self._fill_all else vis[:self._page_size]
        self._built_rows = 0
        self._fav_header_added = False
        # 折叠态下收藏行不进 batch，头行必须在建行前先插到最顶上
        if (not self._filter and self._fav_collapsed
                and any(c.is_favorite and self._matches(c)
                        for c in self._convs.values())):
            self._append_fav_header()
        for rid in batch:
            self._append_conv(self._convs[rid])
        # 只是回到原选中项时不触发 conversation_selected，否则每条新消息
        # 都会把当前会话重开一遍（历史与滚动位置被重置）
        restored = bool(current_id) and current_id in self._conv_by_id
        self._resorting = restored
        try:
            if restored:
                self.select_conversation(current_id)
            else:
                # 默认选中第一条会话行，跳过分组头行
                for i in range(self.count()):
                    if self.item(i).data(Qt.UserRole):
                        self.setCurrentRow(i)
                        break
        finally:
            self._resorting = False

    def _on_scroll_fill(self, value: int):
        """滚动接近底部时补建下一批会话行（惰性渲染）。"""
        bar = self.verticalScrollBar()
        if bar.value() >= bar.maximum() - 100:
            self._fill_more()

    def _fill_more(self):
        """增量补建下一批行，直到全部建出为止。

        行里混着分组头行，self.count() 不再等于已建数据行数，
        游标用 _built_rows（仅会话行）对 _expected_row_ids() 索引。
        """
        if self._fill_all:
            return
        vis = self._expected_row_ids()
        for rid in vis[self._built_rows:self._built_rows + self._page_size]:
            self._append_conv(self._convs[rid])
        self._fill_all = self._built_rows >= len(vis)

    def _append_conv(self, conv: Conversation):
        # 搜索态不分组；非搜索态首批收藏行前插可折叠分组头
        if (not self._filter and conv.is_favorite
                and not self._fav_header_added):
            self._append_fav_header()
        item = QListWidgetItem()
        item.setData(Qt.UserRole, conv.id)
        widget = ConversationItemWidget(conv, self.avatar_loader, self._theme)
        widget.set_has_draft(conv.id in self._draft_ids)
        # 连接信号冒泡
        widget.favorite_requested.connect(self.favorite_requested.emit)
        widget.delete_requested.connect(self.delete_requested.emit)
        widget.mute_requested.connect(self.room_mute_requested.emit)
        widget.mark_unread_requested.connect(self.mark_unread_requested.emit)
        # 宽度给 0：行宽跟随视口，长文本由 label 省略号截断；
        # 高度钳到固定行高——预览文本再异常也只截断，不撑高行
        item.setSizeHint(QSize(0, widget.minimumHeight()))
        self.addItem(item)
        self.setItemWidget(item, widget)
        self._conv_by_id[conv.id] = item
        self._built_rows += 1
        # 重建行回填已拉到的私聊状态（_render 重建会销毁旧行 widget）
        if conv.room_type == "d" and conv.username:
            cached = self._status_cache.get(conv.username)
            if cached:
                widget.set_user_status(cached)

    def _append_fav_header(self):
        self._fav_header_added = True
        count = sum(1 for c in self._convs.values()
                    if c.is_favorite and self._matches(c))
        item = QListWidgetItem()
        # 不可选中：分组头不是会话，选中态与 currentChanged 都不应命中
        item.setFlags(item.flags() & ~Qt.ItemIsSelectable)
        header = _GroupHeader(self._theme)
        header.update_state(count, self._fav_collapsed)
        header.toggled.connect(self._toggle_fav_group)
        item.setSizeHint(QSize(0, 32))
        self.addItem(item)
        self.setItemWidget(item, header)

    def _toggle_fav_group(self, *_):
        self._fav_collapsed = not self._fav_collapsed
        try:
            config.save_ui_prefs(collapsed_favorites=self._fav_collapsed)
        except Exception:  # noqa: BLE001  落盘失败不影响本次折叠
            log.exception("保存会话分组折叠状态失败")
        self._render()

    def _on_current_changed(self, current: QListWidgetItem, _previous):
        if current is None or self._resorting:
            return
        widget = self.itemWidget(current)
        # 分组头行不可选中但可能被键盘/程序置为 current，无 conv 可发
        if widget is not None and getattr(widget, "conv", None) is not None:
            self.conversation_selected.emit(widget.conv)
