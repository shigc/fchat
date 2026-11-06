"""目录面板：频道 / 用户 / 团队的查看与搜索。

会话列表「目录」按钮弹出（相对主窗口居中），三页签切换。数据全部走
`directory` 端点（真机抓包网页目录页同款，text/type/workspace 必须是
平级 URL 参数）：
- 浏览（搜索框为空）：频道 = channels.list + groups.list（本方言只
  返回已加入的）；用户 = users.list 分页；团队 = directory type=teams
  分页（公开 + 已加入私有团队全量可见，teams.list 只回已加入）。
- 搜索（输入词防抖后下推服务端，实测毫秒级、支持中文）：用户 =
  directory type=users（带 department/createdAt）；频道/团队 =
  directory type=channels（发现未加入公开频道的唯一途径，讨论串除外）。
  团队页搜索结果统一按团队条目渲染，点开 rooms.info 校准。
  不能用本地过滤+空页续拉：4.4 万用户翻页比网页慢一个量级。

不能用 Qt.Popup（IME），浮层行为继承 RoomPanelBase；数据走 run_async
（一次任务内只串行调 API，rocketchat_API 共享 Session 非线程安全）。
"""
from __future__ import annotations

import datetime
import logging

from PyQt5.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFontMetrics
from PyQt5.QtWidgets import (
    QDesktopWidget, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from .api_workers import run_async
from .icons import icon
from .room_panels import RoomPanelBase
from .user_status import normalize_status_key, status_tooltip
from .widgets import AvatarWidget, WxLabel, WxLineEdit, format_chat_time

log = logging.getLogger(__name__)

_DIR_PAGE = 50
_DIR_AVATAR_BATCH = 8
# 搜索时自动续拉的条数兜底：防大服务器把接口翻穿
_AUTO_CONT_CAP = 2000
# 搜索词过滤后可见命中少于该数且还有未拉取页时，自动续拉下一页
_FILTER_CONT_MIN = 30


def _date_text(ts: float) -> str:
    """epoch 秒 → 纯日期文本（创建/加入时间用，不是聊天时间）。"""
    if not ts:
        return "—"
    try:
        return datetime.date.fromtimestamp(ts).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return "—"


def _count_text(n: int, unit: str) -> str:
    return f"{n}{unit}" if n >= 0 else f"—{unit}"


class _DirectoryRow(QFrame):
    """目录行：图标 + 名称/副文本 + 右侧时间/已加入徽标。"""

    clicked = pyqtSignal(object)

    def __init__(self, entry, theme_fn, joined: bool, parent=None):
        super().__init__(parent)
        self._entry = entry
        self.setCursor(Qt.PointingHandCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(10)

        # 左侧图标位：频道/团队现画图标，用户复用头像（懒加载）
        if entry.kind == "user":
            self.avatar = AvatarWidget(36)
            self.avatar.set_name(entry.name or entry.username or "?")
            # 行内即时判：搜索结果带 department（离职即时可见），
            # users.list 浏览带 status（在线态即时可见）；缺的一半
            # 由 _DirPage 的状态补拉回填
            self._status_key = normalize_status_key(entry.status,
                                                    entry.department)
            self.avatar.set_status_key(self._status_key)
            self.avatar.set_status_tooltip(status_tooltip(
                self._status_key, getattr(entry, "_status_text", "")))
            self._icon = None
            lay.addWidget(self.avatar, 0, Qt.AlignVCenter)
        else:
            self.avatar = None
            self._icon = QLabel(self)
            self._icon.setFixedSize(36, 36)
            self._icon.setAlignment(Qt.AlignCenter)
            name = "hash" if entry.kind == "channel" else "team"
            self._icon.setPixmap(icon(name, 22).pixmap(QSize(22, 22)))
            lay.addWidget(self._icon, 0, Qt.AlignVCenter)

        text = QVBoxLayout()
        text.setSpacing(2)
        fm = QFontMetrics(self.font())
        name_lbl = QLabel(fm.elidedText(entry.name or entry.username or "?",
                                        Qt.ElideRight, 300))
        font = name_lbl.font()
        font.setPointSize(14)
        font.setBold(True)
        name_lbl.setFont(font)
        name_lbl.setStyleSheet(f"color:{theme_fn('text_primary')};")
        text.addWidget(name_lbl)
        sub = self._sub_text(entry)
        if sub:
            sub_lbl = QLabel(sub)
            font2 = sub_lbl.font()
            font2.setPointSize(12)
            sub_lbl.setFont(font2)
            sub_lbl.setStyleSheet(f"color:{theme_fn('text_secondary')};")
            text.addWidget(sub_lbl)
        lay.addLayout(text, 1)

        right = QVBoxLayout()
        right.setSpacing(2)
        right.addStretch(1)
        t = self._time_text(entry)
        if t:
            t_lbl = QLabel(t)
            tf = t_lbl.font()
            tf.setPointSize(11)
            t_lbl.setFont(tf)
            t_lbl.setStyleSheet(f"color:{theme_fn('time_label')};")
            t_lbl.setAlignment(Qt.AlignRight)
            right.addWidget(t_lbl)
        # 私聊行没有「已加入」语义（点开即建会话）
        if joined and entry.kind != "user":
            badge = QLabel("已加入")
            bf = badge.font()
            bf.setPointSize(9)
            badge.setFont(bf)
            badge.setStyleSheet(f"color:{theme_fn('status_read')};")
            badge.setAlignment(Qt.AlignRight)
            right.addWidget(badge)
        right.addStretch(1)
        lay.addLayout(right, 0)

    @staticmethod
    def _sub_text(entry) -> str:
        if entry.kind == "user":
            sub = "@" + (entry.username or "")
            if entry.department:
                sub += f" · {entry.department}"
            return sub
        date = _date_text(entry.created_ts)
        if entry.kind == "team":
            return f"{_count_text(entry.rooms_count, '个频道')} · 创建 {date}"
        head = "私有 · " if entry.is_private else ""
        return f"{head}{_count_text(entry.users_count, '人')} · 创建 {date}"

    @staticmethod
    def _time_text(entry) -> str:
        # 频道右侧显示上次消息时间，用户/团队显示创建（加入）日期
        if entry.kind == "channel":
            return format_chat_time(entry.last_ts)
        return _date_text(entry.created_ts)

    def apply_status_info(self, info):
        """状态补拉回填：用户行头像状态点 + tooltip。"""
        if not info or self.avatar is None:
            return
        key = info.get("status_key") or normalize_status_key(
            info.get("status", ""), info.get("department", ""))
        self._status_key = key
        self.avatar.set_status_key(key)
        self.avatar.set_status_tooltip(
            status_tooltip(key, info.get("status_text", "")))

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self._entry)
        super().mousePressEvent(e)


class _DirPage(QWidget):
    """单个页签：搜索框 + 列表 + 状态行，自带过滤/分批渲染/token 取消。"""

    load_more = pyqtSignal(object, int)   # (page, offset)
    row_activated = pyqtSignal(object)    # DirectoryEntry
    # 搜索词提交（防抖后）：(page, query)；空串=回到分页浏览
    search_committed = pyqtSignal(object, str)

    def __init__(self, kind: str, placeholder: str, panel, parent=None):
        super().__init__(parent)
        self.kind = kind
        self._panel = panel
        # 已提交给服务端的搜索词（与输入框比对，变化才重新请求）
        self._server_query = ""
        # 过滤前的已加载条目 / 当前过滤结果
        self.entries = []
        self.filtered = []
        self.rendered = 0
        self.rendering = False
        self.offset = 0
        self.total = 0
        self.loading = False
        self.loaded = False      # 是否已拉过数据（切页签懒加载）
        self.error = ""
        self.token = 0
        self._avatar_loaded = set()
        self._avatar_timer = QTimer(self)
        self._avatar_timer.setInterval(50)
        self._avatar_timer.timeout.connect(self._drain_avatars)
        # 用户行状态补拉（users.info：浏览模式补离职、搜索模式补实时状态）
        self._status_provider = None
        self._status_fetched = set()
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(100)
        self._status_timer.timeout.connect(self._drain_status)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self.search = WxLineEdit(panel._theme, self)
        self.search.setPlaceholderText(placeholder)
        self.search.setClearButtonEnabled(True)
        lay.addWidget(self.search)

        self.status = WxLabel("", panel._theme, self)
        self.status.set_variant("secondary")
        lay.addWidget(self.status)

        self.list = QListWidget(self)
        self.list.setFocusPolicy(Qt.NoFocus)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self.list.verticalScrollBar().valueChanged.connect(
            self._avatar_timer.start)
        self.list.verticalScrollBar().valueChanged.connect(
            self._status_timer.start)
        lay.addWidget(self.list, 1)

        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(250)
        self._filter_timer.timeout.connect(self._on_filter_timeout)
        self.search.textChanged.connect(lambda _: self._filter_timer.start())

    # ---------------- 生命周期 ---------------- #
    def reset(self):
        """打开/首次切换时清状态并首拉。"""
        self.token += 1
        self._filter_timer.stop()
        self.search.clear()
        self._server_query = ""
        self.entries = []
        self.filtered = []
        self.rendered = 0
        self.rendering = False
        self.offset = 0
        self.total = 0
        self.error = ""
        self._avatar_loaded.clear()
        self._avatar_timer.stop()
        self._status_fetched.clear()
        self._status_timer.stop()
        self.list.clear()
        self.loaded = True
        self.load_more.emit(self, 0)

    def invalidate(self):
        """关闭/切走时停后台活动：迟到回调比对 token 后丢弃。"""
        self.token += 1
        self.loading = False
        self._filter_timer.stop()
        self._avatar_timer.stop()
        self._status_timer.stop()

    # ---------------- 数据回灌 ---------------- #
    def feed(self, entries, total, offset, final, token):
        if token != self.token:
            return
        self.loading = False
        if self.offset == 0:
            self.entries = []
        self.offset = offset
        self.total = total
        # 频道页两个来源（channels.list + groups.list）合并去重
        seen = {e.rid for e in self.entries if e.rid}
        for e in entries:
            if e.rid and e.rid in seen:
                continue
            seen.add(e.rid)
            self.entries.append(e)
        self._apply_filter()
        self._maybe_auto_continue(final)

    def show_error(self, message, token):
        if token != self.token:
            return
        self.loading = False
        self.error = message
        self._update_status()
        if not self.entries:
            self.list.clear()

    # ---------------- 过滤与渲染 ---------------- #
    def _on_filter_timeout(self):
        self._apply_filter()
        # 搜索词全部下推服务端 spotlight（本地过滤+续拉太慢，公开频道/
        # 团队也只在 spotlight 可见）：防抖后词变化才重新请求；清空词
        # 也提交（空串 = 回到分页浏览）
        q = (self.search.text() or "").strip()
        if q != self._server_query:
            self._server_query = q
            self.search_committed.emit(self, q)
            return
        # 输入搜索词后命中太少，自动续拉未加载的页
        self._maybe_auto_continue(final=False)

    def _query(self) -> str:
        return (self.search.text() or "").strip().casefold()

    def _match(self, e, q: str) -> bool:
        return (q in (e.name or "").casefold()
                or q in (e.username or "").casefold()
                or q in (e.department or "").casefold())

    def _apply_filter(self):
        q = self._query()
        if q and not self._server_query:
            self.filtered = [e for e in self.entries if self._match(e, q)]
        else:
            # 服务端搜索结果不做本地复核：spotlight 按 name/username 等
            # 服务端字段匹配，本地子串过滤会把合法命中误删
            self.filtered = list(self.entries)
        self.list.clear()
        self.rendered = 0
        self.rendering = False
        self._append_page()
        self._update_status()

    def _append_page(self):
        if self.rendering:
            return
        self.rendering = True
        try:
            page = self.filtered[self.rendered:self.rendered + _DIR_PAGE]
            for entry in page:
                row = _DirectoryRow(entry, self._panel._color,
                                    self._panel._is_joined(entry))
                item = QListWidgetItem(self.list)
                item.setSizeHint(row.sizeHint())
                row.clicked.connect(self._on_row_clicked)
                self.list.addItem(item)
                self.list.setItemWidget(item, row)
                # 头像 url 暂存在行上，由 _drain_avatars 只对可见行发起
                if entry.kind == "user" and self._panel._client:
                    try:
                        row._avatar_url = self._panel._client.get_avatar_url(
                            username=entry.username)
                    except Exception:  # noqa: BLE001
                        row._avatar_url = ""
                    row._avatar_key = entry.username
                # 已补齐过状态（缓存复用）则直接回填
                if entry.kind == "user" and getattr(entry, "_status_key", ""):
                    row.apply_status_info({
                        "status_key": entry._status_key,
                        "status_text": getattr(entry, "_status_text", ""),
                    })
            self.rendered += len(page)
        finally:
            self.rendering = False
        if self._panel._avatar_loader:
            self._avatar_timer.start()
        if self._status_provider and self.kind == "user":
            self._status_timer.start()

    def _update_status(self):
        if self.loading:
            self.status.setText("加载中…")
            return
        if self.error:
            self.status.setText(f"加载失败：{self.error}")
            return
        q = self._query()
        if q:
            if not self.filtered:
                self.status.setText("未找到匹配项"
                                    if self.entries else "")
            elif self.rendered < len(self.filtered):
                self.status.setText(f"匹配 {len(self.filtered)} 项")
            else:
                self.status.setText(f"匹配 {len(self.filtered)} 项"
                                    f"（已扫描 {len(self.entries)}）")
        elif self.entries:
            suffix = (f"（仅显示前 {len(self.entries)} 条）"
                      if self.total and len(self.entries) < self.total else "")
            # 频道浏览态只能列出已加入的（本方言 channels.list 如此）；
            # 未加入的公开频道只能搜索名字发现，得告诉用户这个入口
            hint = ("（输入名称搜索未加入的公开频道）"
                    if self.kind == "channel" else "")
            self.status.setText(f"共 {self.total} 条{suffix}{hint}")
        else:
            self.status.setText("")

    def _maybe_auto_continue(self, final: bool):
        """搜索空页续拉：命中过少且还有未拉取页时继续加载。"""
        q = self._query()
        if not q or self.loading or final:
            return
        if len(self.entries) >= _AUTO_CONT_CAP:
            return
        if self.total and self.offset >= self.total:
            return
        if len(self.filtered) >= _FILTER_CONT_MIN:
            return
        self.load_more.emit(self, self.offset)

    # ---------------- 滚动 / 头像 / 点击 ---------------- #
    def _on_scroll(self, value):
        vb = self.list.verticalScrollBar()
        if value >= vb.maximum() - 40:
            if self.rendered < len(self.filtered):
                self._append_page()
            elif (not self.loading
                  and self._panel._client
                  and not (self.total and self.offset >= self.total)
                  and len(self.entries) < _AUTO_CONT_CAP):
                # 已渲染完过滤结果但服务端还有下一页
                self.load_more.emit(self, self.offset)

    def _drain_avatars(self):
        """照抄 MembersPanel 的可见行懒加载：每 tick 最多 8 个。"""
        loader = self._panel._avatar_loader
        if not loader:
            self._avatar_timer.stop()
            return
        vp = self.list.viewport()
        tl = self.list.indexAt(vp.rect().topLeft())
        bl = self.list.indexAt(vp.rect().bottomLeft())
        start = tl.row() if tl.isValid() else 0
        end = bl.row() if bl.isValid() else (self.list.count() - 1)
        loaded = 0
        for r in range(start, end + 1):
            if loaded >= _DIR_AVATAR_BATCH:
                break
            item = self.list.item(r)
            wid = self.list.itemWidget(item) if item is not None else None
            key = getattr(wid, "_avatar_key", "") if wid is not None else ""
            url = getattr(wid, "_avatar_url", "") if wid is not None else ""
            if not key or not url or key in self._avatar_loaded:
                continue
            self._avatar_loaded.add(key)
            try:
                loader.load(wid.avatar, url)
            except Exception:  # noqa: BLE001
                log.exception("加载目录头像失败 url=%s", url)
            loaded += 1
        if loaded == 0:
            self._avatar_timer.stop()

    def _drain_status(self):
        """用户行状态补拉：照抄可见行模式，每 tick 最多 4 个 users.info。"""
        if not self._status_provider or self.kind != "user":
            self._status_timer.stop()
            return
        vp = self.list.viewport()
        tl = self.list.indexAt(vp.rect().topLeft())
        bl = self.list.indexAt(vp.rect().bottomLeft())
        start = tl.row() if tl.isValid() else 0
        end = bl.row() if bl.isValid() else (self.list.count() - 1)
        requested = 0
        for r in range(start, end + 1):
            if requested >= 4:
                break
            item = self.list.item(r)
            wid = self.list.itemWidget(item) if item is not None else None
            uname = getattr(wid, "_avatar_key", "") if wid is not None else ""
            if not uname or uname in self._status_fetched:
                continue
            self._status_fetched.add(uname)
            requested += 1
            self._status_provider(
                uname, lambda info, u=uname: self._on_status_info(u, info))
        if requested == 0:
            self._status_timer.stop()

    def _on_status_info(self, username: str, info):
        """状态补拉回调：写回条目（重渲染保持）并刷新活行。"""
        if not info:
            return
        for e in self.entries:
            if e.kind == "user" and e.username == username:
                e._status_key = info.get("status_key") or getattr(
                    e, "_status_key", "")
                e._status_text = info.get("status_text", "")
        for i in range(self.list.count()):
            wid = self.list.itemWidget(self.list.item(i))
            if (wid is not None
                    and getattr(wid, "_avatar_key", "") == username):
                wid.apply_status_info(info)

    def _on_row_clicked(self, entry):
        self.row_activated.emit(entry)


class DirectoryPanel(RoomPanelBase):
    """目录面板：三页签（频道/用户/团队），不依赖当前房间。"""

    _W = 520
    _H = 640

    open_requested = pyqtSignal(object)   # DirectoryEntry（频道/团队）
    dm_requested = pyqtSignal(str)        # username（用户）

    _TABS = (
        ("channel", "频道", "搜索频道"),
        ("user", "用户", "搜索用户"),
        ("team", "团队", "搜索团队"),
    )

    def __init__(self, parent=None):
        super().__init__("directory", parent)
        self._joined_provider = None
        self._current = 0

        tabs = QHBoxLayout()
        tabs.setSpacing(6)
        self._tab_btns = []
        for _, text, _ph in self._TABS:
            btn = QPushButton(text, self)
            btn.setObjectName("DirTab")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.clicked.connect(self._make_tab_handler(len(self._tab_btns)))
            tabs.addWidget(btn)
            self._tab_btns.append(btn)
        tabs.addStretch(1)
        self._root.addLayout(tabs)

        self._stack = QStackedWidget(self)
        self._root.addWidget(self._stack, 1)
        self._pages = []
        for kind, _text, placeholder in self._TABS:
            page = _DirPage(kind, placeholder, self)
            page.load_more.connect(self._load_page)
            page.row_activated.connect(self._on_row_activated)
            page.search_committed.connect(self._on_search_committed)
            self._stack.addWidget(page)
            self._pages.append(page)
        self._theme_widgets.extend(
            w for page in self._pages for w in (page.search, page.status))
        self._tab_btns[0].setChecked(True)

    def _make_tab_handler(self, index):
        def handler():
            self._switch_tab(index)
        return handler

    def set_joined_provider(self, fn):
        """fn(entry) -> bool：该房间是否已加入（MainWindow 按 _conversations 判定）。"""
        self._joined_provider = fn

    def set_status_provider(self, fn):
        """注入 UserStatusProvider.get，分发给各页签的用户行补拉。"""
        for page in self._pages:
            page._status_provider = fn

    def refresh_status(self, username: str, info):
        """实时状态推送回填：转发给用户页签的 _on_status_info 刷活行。"""
        for page in self._pages:
            if getattr(page, "kind", "") == "user":
                page._on_status_info(username, info)

    def _is_joined(self, entry) -> bool:
        try:
            return bool(self._joined_provider and self._joined_provider(entry))
        except Exception:  # noqa: BLE001
            return False

    def _position(self, btn):
        """目录面板比房间浮层大得多，相对主窗口居中（而非锚在按钮下），
        贴屏时钳回屏内。"""
        win = btn.window()
        wg = win.geometry() if win else btn.mapToGlobal(btn.rect())
        screen = QDesktopWidget().availableGeometry(self)
        x = wg.x() + (wg.width() - self.width()) // 2
        y = wg.y() + (wg.height() - self.height()) // 2
        self.move(max(screen.left(), min(x, screen.right() - self.width())),
                  max(screen.top(), min(y, screen.bottom() - self.height())))

    # ---------------- 打开 / 关闭 / 切页签 ---------------- #
    def _on_open(self):
        # 每次打开都重拉当前页签；其余页签标记为未加载，切换时再拉
        for page in self._pages:
            page.invalidate()
            page.loaded = False
        self._switch_tab(0)

    def _on_closed(self):
        for page in self._pages:
            page.invalidate()
            page.loaded = False

    def _switch_tab(self, index):
        if index == self._current and self._pages[index].loaded \
                and self.isVisible():
            return
        self._pages[self._current].invalidate()
        self._current = index
        for i, btn in enumerate(self._tab_btns):
            btn.setChecked(i == index)
        self._stack.setCurrentIndex(index)
        page = self._pages[index]
        if not page.loaded:
            page.reset()

    # ---------------- 数据加载 ---------------- #
    def _apply_theme(self):
        super()._apply_theme()
        self.setStyleSheet(self.styleSheet() + (
            f"#DirTab {{ background:transparent;"
            f" border:1px solid {self._color('input_border')};"
            f" border-radius:6px; padding:4px 14px;"
            f" color:{self._color('text_secondary')}; }}"
            f"#DirTab:hover {{ background:{self._color('conv_hover')}; }}"
            f"#DirTab:checked {{ border-color:{self._color('status_read')};"
            f" color:{self._color('status_read')}; }}"
        ))

    def _load_page(self, page, offset):
        if not self._client or page.loading:
            return
        page.loading = True
        token = page.token
        page._update_status()
        client = self._client
        kind = page.kind

        def work():
            # 同一任务内串行调用（Session 非线程安全）
            if kind == "channel":
                r1 = client.fetch_directory_channels(offset=offset,
                                                     count=_DIR_PAGE)
                r2 = client.fetch_directory_groups(offset=offset,
                                                   count=_DIR_PAGE)
                return (r1["entries"] + r2["entries"],
                        max(r1["total"], r2["total"]),
                        offset,
                        not r1["entries"] and not r2["entries"])
            if kind == "user":
                r = client.fetch_directory_users(offset=offset,
                                                 count=_DIR_PAGE)
                return (r["entries"], r["total"], offset,
                        offset + len(r["entries"]) >= r["total"])
            r = client.fetch_directory_teams(offset=offset, count=_DIR_PAGE)
            return (r["entries"], r["total"], offset,
                    offset + len(r["entries"]) >= r["total"])

        def on_done(result):
            try:
                if not self.isVisible():
                    return
                entries, total, off, final = result
                page.feed(entries, total, off, final, token)
            except Exception:  # noqa: BLE001
                log.exception("目录数据渲染失败")

        def on_fail(err):
            try:
                page.show_error(str(err), token)
            except Exception:  # noqa: BLE001
                log.exception("目录错误提示失败")

        run_async(work, on_done, on_fail, parent=self)

    def _on_search_committed(self, page, query):
        """搜索下推服务端 spotlight：一次请求拿命中，不再逐页续拉。
        query 为空回到分页浏览；token 机制丢弃迟到的旧结果。"""
        if not self._client:
            return
        if not query:
            page.reset()
            return
        page.token += 1
        token = page.token
        page.loading = True
        page.error = ""
        page.entries = []
        page.filtered = []
        page.rendered = 0
        page.rendering = False
        page.offset = 0
        page._avatar_loaded.clear()
        page._avatar_timer.stop()
        page.list.clear()
        page._update_status()
        client = self._client
        kind = page.kind

        def work():
            # 同一任务内串行调用（Session 非线程安全）
            if kind == "user":
                return client.search_directory_users(query)
            if kind == "team":
                # type=teams 直接命中团队，与网页团队页签一致
                return client.search_directory_teams(query)
            return client.search_directory_rooms(query)

        def on_done(result):
            try:
                if not self.isVisible():
                    return
                entries = result["entries"]
                # offset 传成 len(entries)：feed 后 offset >= total，
                # 滚动续拉 / 空页自动续拉都不会再对搜索结果发起分页请求
                page.feed(entries, len(entries), len(entries), True, token)
            except Exception:  # noqa: BLE001
                log.exception("目录搜索渲染失败")

        def on_fail(err):
            try:
                page.show_error(str(err), token)
            except Exception:  # noqa: BLE001
                log.exception("目录搜索错误提示失败")

        run_async(work, on_done, on_fail, parent=self)

    # ---------------- 点击路由 ---------------- #
    def _on_row_activated(self, entry):
        if entry.kind == "user":
            if entry.username:
                self.dm_requested.emit(entry.username)
        elif entry.rid:
            self.open_requested.emit(entry)
        # rid 缺失（teams.list 字段异常）：置灰条目不响应
