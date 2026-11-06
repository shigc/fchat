"""房间三功能浮层面板：成员 / 文件 / 消息记录。

标题栏三个按钮各自在其正下方弹出独立面板（点外部自动关闭，互斥打开）。
不能用 Qt.Popup：Windows 上 IME 无法在 Popup 窗口合成输入（搜索框打不了
中文），因此用无边框 Qt.Tool 窗口 + changeEvent 失活自关模拟 popup 语义；
嵌套 Qt.Popup（ProfileCard/右键菜单）打开时面板失活要豁免。数据获取统一
走 ui/api_workers.run_async（回调回到主线程，外层 try/except +
log.exception）；颜色全部从 ThemeManager 语义色取。成员数据复用主窗口
_ensure_room_members 的缓存（面板自己不拉）；正则搜索在客户端逐页扫描
全部历史（服务端搜索接口不支持正则）。
"""
from __future__ import annotations

import logging
import re
import time

from PyQt5.QtCore import (QDate, QDateTime, QEvent, QObject, QPoint, QRect,
                          QSize, Qt, QTimer, pyqtSignal)
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QDateEdit, QDesktopWidget, QFrame, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QMenu, QPushButton, QVBoxLayout,
    QWidget,
)

from .api_workers import run_async
from .icons import icon
from .user_status import normalize_status_key, status_tooltip
from .widgets import (AvatarWidget, WxComboBox, WxLabel, WxLineEdit,
                      WxPushButton, _color, format_chat_time)

log = logging.getLogger(__name__)

# 成员渲染分页：一次性 setItemWidget 上千行会在主线程卡死。头像只加载列表
# 当前可见行的（滚到哪加载到哪），避免给整屋成员并发发请求造成洪流/卡死。
_MEMBER_PAGE = 50
_MEMBER_AVATAR_BATCH = 8
# 状态补拉（users.info 判离职/自定义状态）每 tick 最多发起的请求数
_MEMBER_STATUS_BATCH = 4
_FILE_PAGE = 50
_FILE_SEARCH_PAGE = 100
# 文件搜索全量拉取的兜底上限：防大房间把接口翻穿
_SEARCH_CAP = 2000
_SCAN_PAGE = 100
# 正则扫描结果渲染上限：超出只显示前 N 条（进度行注明），防列表失控
_MAX_RESULT_ROWS = 300
_RESULT_FLUSH = 30
# 「点已开面板的按钮=关闭」的判定窗口：面板失活自关后按钮 click 才到达，
# 回放到达时面板刚 hide（见 MainWindow._on_panel_requested）
_TOGGLE_GRACE = 0.3


def _label(text: str, size: int = 13, color: str = "", bold: bool = False) -> QLabel:
    lbl = QLabel(text)
    font = lbl.font()
    font.setPointSize(size)
    font.setBold(bold)
    lbl.setFont(font)
    if color:
        lbl.setStyleSheet(f"color:{color};")
    return lbl


class RoomPanelBase(QWidget):
    """浮层面板基类：无边框 Tool 窗、锚定按钮下方、主题 QSS、关闭回调。"""

    _W = 300
    _H = 420

    panel_closed = pyqtSignal(object)   # self（控制器转成 closed(kind)）

    def __init__(self, kind: str, parent=None):
        # 不用 Qt.Popup：Windows 上 IME 无法在 Popup 窗口合成输入（搜索框打
        # 不了中文）。改用无边框 Tool 窗，失活时自关（见 changeEvent）模拟
        # 「点外部关闭」；WindowStaysOnTopHint 保证盖在主窗口上，面板失活
        # 即关所以不会常驻置顶。
        super().__init__(
            parent,
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.kind = kind
        self._theme = None
        self._client = None
        self._avatar_loader = None
        self._rid = None
        self._room_type = "c"
        self._room_name = ""
        self._closed_at = 0.0
        self._theme_widgets: list = []
        self.setObjectName("RoomPanel")
        self.setFixedSize(self._W, self._H)
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(10, 10, 10, 10)
        self._root.setSpacing(8)
        self.hide()

    # ---------------- 外部注入 ---------------- #
    def set_theme(self, theme):
        self._theme = theme
        for w in self._theme_widgets:
            try:
                w.set_theme(theme)
            except AttributeError:
                pass
        self._apply_theme()

    def set_session(self, client, avatar_loader):
        self._client = client
        self._avatar_loader = avatar_loader

    # ---------------- 打开 / 关闭 ---------------- #
    def open_for(self, rid: str, room_type: str, name: str, anchor_btn):
        self._rid = rid
        self._room_type = room_type
        self._room_name = name
        self._position(anchor_btn)
        self.show()
        self.raise_()
        # Tool 窗不会像 popup 那样自动抓键盘，必须显式激活才能输入
        self.activateWindow()
        self._on_open()

    def _position(self, btn):
        """面板右缘与按钮右缘对齐、悬在按钮正下方；贴屏幕边时钳回屏内。"""
        g = btn.mapToGlobal(QPoint(0, btn.height()))
        screen = QDesktopWidget().availableGeometry(self)
        x = min(max(screen.left(), g.x() + btn.width() - self.width()),
                screen.right() - self.width())
        y = g.y() + 4
        if y + self.height() > screen.bottom():
            y = max(screen.top(), g.y() - self.height() - 4)
        self.move(x, max(screen.top(), y))

    def was_recently_closed(self) -> bool:
        return (time.monotonic() - self._closed_at) < _TOGGLE_GRACE

    def changeEvent(self, e):
        # 模拟 popup 的「点外部关闭」：面板失活即关。豁免嵌套 Qt.Popup
        # （ProfileCard / 文件右键菜单 / MentionPicker）打开导致的失活，
        # 否则点成员弹卡片时面板会先被关掉。
        super().changeEvent(e)
        if (e.type() == QEvent.ActivationChange
                and not self.isActiveWindow()
                and QApplication.activePopupWidget() is None):
            self.close()

    def hideEvent(self, e):
        self._closed_at = time.monotonic()
        self._on_closed()
        self.panel_closed.emit(self)
        super().hideEvent(e)

    # ---------------- 子类钩子 ---------------- #
    def _on_open(self):
        """每次打开时重置状态并首拉数据。"""

    def _on_closed(self):
        """面板被关闭（含点外部）时停后台活动。"""

    # ---------------- 主题 ---------------- #
    def _color(self, key: str, default: str = "") -> str:
        return _color(self._theme, key, default or "#191919")

    def _apply_theme(self):
        bg = self._color("conv_bg")
        border = self._color("border")
        text = self._color("text_primary")
        self.setStyleSheet(
            f"#RoomPanel {{ background:{bg}; border:1px solid {border};"
            f" border-radius:8px; }}"
            f"#RoomPanel QListWidget {{ background:{bg}; border:none;"
            f" color:{text}; }}"
            f"#RoomPanel QScrollBar:vertical {{ background:transparent;"
            f" width:6px; }}"
            f"#RoomPanel QScrollBar::handle:vertical {{"
            f" background:{self._color('scrollbar')}; border-radius:3px; }}"
            f"#RoomPanel QScrollBar::add-line:vertical,"
            f"#RoomPanel QScrollBar::sub-line:vertical {{ height:0px; }}"
        )

    def _make_list(self) -> QListWidget:
        lst = QListWidget(self)
        lst.setFocusPolicy(Qt.NoFocus)
        lst.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        return lst


# ---------------------------------------------------------------------- #
# 成员面板
# ---------------------------------------------------------------------- #
class _MemberRow(QFrame):
    """成员行：头像 + 姓名 + @username，点击弹资料卡。"""

    clicked = pyqtSignal(dict)

    def __init__(self, member: dict, theme_fn, parent=None):
        super().__init__(parent)
        self._member = member
        self.setCursor(Qt.PointingHandCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(10)

        self.avatar = AvatarWidget(36)
        self.avatar.set_name(member.get("name") or member.get("username") or "?")
        # members 端点自带实时 status，先按它画点；离职/自定义状态
        # 由面板经 UserStatusProvider 补 users.info 后回填
        self._status_key = normalize_status_key(member.get("status", ""))
        self.avatar.set_status_key(self._status_key)
        lay.addWidget(self.avatar, 0, Qt.AlignVCenter)

        text = QVBoxLayout()
        text.setSpacing(2)
        name = member.get("name") or member.get("username") or "?"
        text.addWidget(_label(name, 14, theme_fn("text_primary"), bold=True))
        uname = "@" + (member.get("username") or "")
        text.addWidget(_label(uname, 12, theme_fn("text_secondary")))
        lay.addLayout(text, 1)

    def apply_status_info(self, info):
        """users.info 补齐后的状态回填（离职判定 + 自定义状态 tooltip）。"""
        if not info:
            return
        key = info.get("status_key") or normalize_status_key(
            info.get("status", ""), info.get("department", ""))
        self._status_key = key
        self.avatar.set_status_key(key)
        self.avatar.set_status_tooltip(
            status_tooltip(key, info.get("status_text", "")))

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self._member)
        super().mousePressEvent(e)


class MembersPanel(RoomPanelBase):
    _H = 420

    member_clicked = pyqtSignal(dict, object)   # (member, 行全局 rect)
    add_member_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("members", parent)
        self._provider = None           # (rid, room_type, on_ready) -> list|None
        self._all_members = []          # 过滤前的完整列表（不含自己，按名排序）
        self._shown_members = []        # 当前过滤结果
        self._rendered = 0
        self._rendering = False
        self._avatar_loaded = set()
        self._avatar_timer = QTimer(self)
        self._avatar_timer.setInterval(50)
        self._avatar_timer.timeout.connect(self._drain_avatars)
        # 状态补拉定时器：同头像的可见行模式，离职/自定义状态按需补
        self._status_provider = None
        self._status_fetched = set()
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(100)
        self._status_timer.timeout.connect(self._drain_status)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.search = WxLineEdit(self._theme, self)
        self.search.setPlaceholderText("搜索成员")
        self.search.setClearButtonEnabled(True)
        row.addWidget(self.search, 1)
        self.add_btn = QPushButton(self)
        self.add_btn.setObjectName("PanelAddBtn")
        self.add_btn.setIcon(icon("plus", 16))
        self.add_btn.setFixedSize(28, 28)
        self.add_btn.setCursor(Qt.PointingHandCursor)
        self.add_btn.setFocusPolicy(Qt.NoFocus)
        self.add_btn.setToolTip("添加成员")
        self.add_btn.clicked.connect(self.add_member_requested.emit)
        row.addWidget(self.add_btn)
        self._root.addLayout(row)

        self.count_label = WxLabel("", self._theme, self)
        self.count_label.set_variant("secondary")
        self._root.addWidget(self.count_label)

        self.member_list = self._make_list()
        self._root.addWidget(self.member_list, 1)
        self._theme_widgets.extend([self.search, self.count_label])

        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(250)
        self._filter_timer.timeout.connect(self._apply_filter)
        self.search.textChanged.connect(lambda _: self._filter_timer.start())
        self.member_list.verticalScrollBar().valueChanged.connect(
            self._on_member_scroll)
        # 成员列表滚动时重启头像定时器，加载新露出的可见行头像
        self.member_list.verticalScrollBar().valueChanged.connect(
            self._avatar_timer.start)
        self.member_list.verticalScrollBar().valueChanged.connect(
            self._status_timer.start)

    def set_provider(self, fn):
        self._provider = fn

    def set_status_provider(self, fn):
        """注入 UserStatusProvider.get（username, callback) -> None。"""
        self._status_provider = fn

    def refresh_status(self, username: str, info):
        """实时状态推送回填：与 _on_status_info 同路，但不经可见行补拉调度。"""
        self._on_status_info(username, info)

    def set_theme(self, theme):
        super().set_theme(theme)
        self.add_btn.setIcon(icon("plus", 16))
        self._apply_add_btn_style()

    def _apply_theme(self):
        super()._apply_theme()
        self._apply_add_btn_style()

    def _apply_add_btn_style(self):
        self.add_btn.setStyleSheet(
            f"#PanelAddBtn {{ background:transparent;"
            f" border:1px solid {self._color('input_border')};"
            f" border-radius:6px; }}"
            f"#PanelAddBtn:hover {{ background:{self._color('conv_hover')}; }}")

    def _on_open(self):
        self._filter_timer.stop()
        self.search.clear()
        self.member_list.clear()
        self._all_members = []
        self._shown_members = []
        self._rendered = 0
        self._avatar_loaded.clear()
        self._avatar_timer.stop()
        self._status_fetched.clear()
        self._status_timer.stop()
        # d 私聊没有「加成员」语义
        self.add_btn.setVisible(self._room_type in ("c", "p"))
        self.count_label.setText("成员" if self._room_type != "d" else "对方")
        self.search.setFocus()
        if self._provider and self._rid:
            cached = self._provider(self._rid, self._room_type,
                                    self._on_members_ready)
            if cached is not None:
                self._build_members(cached)

    def _on_closed(self):
        self._avatar_timer.stop()

    def _on_members_ready(self, members):
        # provider 的补拉回调来自 _ensure_room_members 的主线程回调
        if self._rid is None or not self.isVisible():
            return
        self._build_members(members or [])

    def _build_members(self, members):
        uid = (self._client.user_id or "") if self._client else ""
        shown = [m for m in members if m.get("_id") != uid]
        shown.sort(key=lambda m: (m.get("name") or m.get("username") or "").lower())
        self._all_members = shown
        self._avatar_loaded.clear()
        self._avatar_timer.stop()
        self._status_fetched.clear()
        self._status_timer.stop()
        if self._room_type != "d":
            self.count_label.setText(f"成员（{len(shown)}）")
        else:
            self.count_label.setText("对方")
        self._apply_filter()

    def _apply_filter(self):
        q = (self.search.text() or "").strip().casefold()
        if q:
            self._shown_members = [
                m for m in self._all_members
                if q in (m.get("name") or "").casefold()
                or q in (m.get("username") or "").casefold()]
        else:
            self._shown_members = list(self._all_members)
        self.member_list.clear()
        self._rendered = 0
        self._rendering = False
        if not self._shown_members and q:
            self.member_list.addItem(QListWidgetItem("未找到匹配的成员"))
        self._append_page()

    def _append_page(self):
        if self._rendering:
            return
        self._rendering = True
        try:
            page = self._shown_members[self._rendered:
                                       self._rendered + _MEMBER_PAGE]
            for m in page:
                row = _MemberRow(m, self._color)
                item = QListWidgetItem(self.member_list)
                item.setSizeHint(row.sizeHint())
                row.clicked.connect(self._on_row_clicked)
                self.member_list.addItem(item)
                self.member_list.setItemWidget(item, row)
                # 头像 url 与成员键暂存在行上，不立即请求；由 _drain_avatars
                # 只对当前可见行发起加载（滚到哪加载到哪）
                row._avatar_url = self._avatar_url_for(m)
                row._avatar_key = m.get("username") or m.get("_id") or ""
                row._status_name = m.get("username") or ""
                # 成员 dict 上已补齐过状态（缓存复用）则直接回填
                if m.get("_status_key"):
                    row.apply_status_info({
                        "status_key": m["_status_key"],
                        "status_text": m.get("_status_text", ""),
                    })
            self._rendered += len(page)
        finally:
            self._rendering = False
        if self._avatar_loader:
            self._avatar_timer.start()
        if self._status_provider:
            self._status_timer.start()

    def _on_member_scroll(self, value):
        vb = self.member_list.verticalScrollBar()
        if (value >= vb.maximum() - 40
                and self._rendered < len(self._shown_members)):
            self._append_page()

    def _drain_avatars(self):
        """每 tick 只对成员列表「可见行」发起最多 _MEMBER_AVATAR_BATCH 个头像
        加载；可见行加载完后停表，滚动时由 valueChanged 重启。"""
        guard = self._avatar_loader
        if not guard:
            self._avatar_timer.stop()
            return
        vp = self.member_list.viewport()
        tl = self.member_list.indexAt(vp.rect().topLeft())
        bl = self.member_list.indexAt(vp.rect().bottomLeft())
        start = tl.row() if tl.isValid() else 0
        end = bl.row() if bl.isValid() else (self.member_list.count() - 1)
        loaded = 0
        for r in range(start, end + 1):
            if loaded >= _MEMBER_AVATAR_BATCH:
                break
            item = self.member_list.item(r)
            wid = self.member_list.itemWidget(item) if item is not None else None
            key = getattr(wid, "_avatar_key", "") if wid is not None else ""
            url = getattr(wid, "_avatar_url", "") if wid is not None else ""
            if not key or not url or key in self._avatar_loaded:
                continue
            self._avatar_loaded.add(key)
            try:
                guard.load(wid.avatar, url)
            except Exception:  # noqa: BLE001
                log.exception("加载成员头像失败 url=%s", url)
            loaded += 1
        if loaded == 0:
            self._avatar_timer.stop()

    def _drain_status(self):
        """每 tick 只对成员列表「可见行」发起最多 _MEMBER_STATUS_BATCH 个
        users.info 状态补拉（离职判定/自定义状态）；补完停表，滚动再重启。"""
        if not self._status_provider:
            self._status_timer.stop()
            return
        vp = self.member_list.viewport()
        tl = self.member_list.indexAt(vp.rect().topLeft())
        bl = self.member_list.indexAt(vp.rect().bottomLeft())
        start = tl.row() if tl.isValid() else 0
        end = bl.row() if bl.isValid() else (self.member_list.count() - 1)
        requested = 0
        for r in range(start, end + 1):
            if requested >= _MEMBER_STATUS_BATCH:
                break
            item = self.member_list.item(r)
            wid = self.member_list.itemWidget(item) if item is not None else None
            uname = getattr(wid, "_status_name", "") if wid is not None else ""
            if not uname or uname in self._status_fetched:
                continue
            self._status_fetched.add(uname)
            requested += 1
            self._status_provider(
                uname, lambda info, u=uname: self._on_status_info(u, info))
        if requested == 0:
            self._status_timer.stop()

    def _on_status_info(self, username: str, info):
        """状态补拉回调：写回成员 dict（行重建时保持）并刷新活行。"""
        if not info:
            return
        for m in self._all_members:
            if m.get("username") == username:
                m["_status_key"] = info.get("status_key") or m.get("_status_key")
                m["_status_text"] = info.get("status_text", "")
        for i in range(self.member_list.count()):
            wid = self.member_list.itemWidget(self.member_list.item(i))
            if wid is not None and getattr(wid, "_status_name", "") == username:
                wid.apply_status_info(info)

    def _avatar_url_for(self, member: dict) -> str:
        if not self._client:
            return ""
        try:
            return self._client.get_avatar_url(username=member.get("username"))
        except Exception:  # noqa: BLE001
            return ""

    def _on_row_clicked(self, member: dict):
        self.member_clicked.emit(member, QRect(QPoint(0, 0), QSize(0, 0)))


# ---------------------------------------------------------------------- #
# 文件面板
# ---------------------------------------------------------------------- #
class _FileRow(QFrame):
    """文件行：文件名 + 大小 · 上传者 · 时间，点击下载并打开。"""

    clicked = pyqtSignal(dict)

    def __init__(self, f: dict, theme_fn, members_by_uid, parent=None):
        super().__init__(parent)
        self._file = f
        self.setCursor(Qt.PointingHandCursor)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(2)

        name = f.get("name") or "附件"
        if len(name) > 48:
            name = name[:48] + "…"
        lay.addWidget(_label(name, 14, theme_fn("text_primary"), bold=True))

        uid = f.get("userId") or ""
        uploader = ""
        if uid:
            m = (members_by_uid or {}).get(uid) or {}
            uploader = m.get("name") or m.get("username") or ""
        size = self._human_size(f.get("size"))
        when = self._human_time(f.get("uploadedAt") or "")
        meta = _label(" · ".join(x for x in (size, uploader, when) if x),
                      12, theme_fn("text_secondary"))
        lay.addWidget(meta)

    @staticmethod
    def _human_size(size) -> str:
        try:
            n = float(size)
        except (TypeError, ValueError):
            return ""
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} TB"

    @staticmethod
    def _human_time(t: str) -> str:
        if not t:
            return ""
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
            return dt.astimezone().strftime("%Y-%m-%d %H:%M")
        except Exception:  # noqa: BLE001
            return ""

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self._file)
        super().mousePressEvent(e)


class FilesPanel(RoomPanelBase):
    _W = 340    # 搜索文件名输入框占满整行，面板略加宽留出余地
    _H = 460

    file_open_requested = pyqtSignal(str, str)      # (url, name)
    file_save_as_requested = pyqtSignal(str, str, int)   # (url, name, size)

    def __init__(self, parent=None):
        super().__init__("files", parent)
        self._by_uid_provider = None    # rid -> {uid: member dict}
        self._all_files = []
        self._files_offset = 0
        self._files_total = 0
        self._files_loading = False
        self._filter = ""
        self._search_token = 0

        self.file_search = WxLineEdit(self._theme, self)
        self.file_search.setPlaceholderText("搜索文件名")
        self.file_search.setClearButtonEnabled(True)
        self.file_search.setMinimumHeight(34)
        self._root.addWidget(self.file_search)
        self._theme_widgets.append(self.file_search)

        self.hint_label = WxLabel("", self._theme, self)
        self.hint_label.set_variant("secondary")
        self._root.addWidget(self.hint_label)

        self.file_list = self._make_list()
        self._root.addWidget(self.file_list, 1)

        self.file_list.verticalScrollBar().valueChanged.connect(
            self._on_file_scroll)
        self.file_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.file_list.customContextMenuRequested.connect(self._show_menu)

        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(350)
        self._filter_timer.timeout.connect(self._run_filter)
        self.file_search.textChanged.connect(self._on_filter_changed)

    def set_by_uid_provider(self, fn):
        self._by_uid_provider = fn

    def _on_open(self):
        self._search_token += 1
        self._filter_timer.stop()
        self.file_search.clear()
        self._filter = ""
        self._all_files = []
        self._files_offset = 0
        self._files_total = 0
        self._files_loading = False
        self.file_list.clear()
        self.hint_label.setText("")
        self.file_search.setFocus()
        self._load_files(0, append=False)

    def _members_by_uid(self) -> dict:
        if self._by_uid_provider and self._rid:
            try:
                return self._by_uid_provider(self._rid) or {}
            except Exception:  # noqa: BLE001
                log.exception("取成员映射失败 rid=%s", self._rid)
        return {}

    # ---------------- 浏览模式（分页） ---------------- #
    def _load_files(self, offset: int, append: bool):
        rid = self._rid
        if not rid or not self._client or self._files_loading:
            return
        self._files_loading = True
        client = self._client
        room_type = self._room_type

        def _done(result):
            if rid != self._rid:
                self._files_loading = False
                return
            self._files_loading = False
            files = result.get("files") or []
            if append:
                self._all_files.extend(files)
            else:
                self._all_files = list(files)
            self._files_total = int(result.get("total") or 0)
            self._files_offset = offset + len(files)
            self._build_files()
            self._update_hint()

        def _fail(err):
            if rid == self._rid:
                self._files_loading = False
            log.warning("拉取房间文件失败 rid=%s：%s", rid, err)

        def _proc():
            return client.fetch_room_files(rid, room_type, offset=offset,
                                           count=_FILE_PAGE)

        run_async(_proc, on_done=_done, on_fail=_fail, parent=self)

    def _on_file_scroll(self, value):
        vb = self.file_list.verticalScrollBar()
        if (not self._filter
                and self._files_total
                and self._files_offset < self._files_total
                and value >= vb.maximum() - 40):
            self._load_files(self._files_offset, append=True)

    # ---------------- 搜索模式（全量拉取 + 客户端过滤） ---------------- #
    def _on_filter_changed(self, text):
        self._filter = (text or "").strip()
        self._search_token += 1
        self._filter_timer.start()

    def _run_filter(self):
        if not self._filter:
            self._build_files()
            self._update_hint()
            return
        token = self._search_token
        self._build_files()
        self._update_hint()
        if self._files_offset < self._files_total or not self._files_total:
            self._search_more(token)

    def _search_more(self, token):
        if token != self._search_token:
            return
        rid = self._rid
        if not rid or not self._client:
            return
        client = self._client
        room_type = self._room_type
        offset = self._files_offset

        def _proc():
            return client.fetch_room_files(rid, room_type, offset=offset,
                                           count=_FILE_SEARCH_PAGE)

        def _done(result):
            if token != self._search_token or rid != self._rid:
                return
            files = result.get("files") or []
            self._all_files.extend(files)
            self._files_total = int(result.get("total") or 0)
            self._files_offset = offset + len(files)
            self._build_files()
            scanned = len(self._all_files)
            done = (not files
                    or len(files) < _FILE_SEARCH_PAGE
                    or scanned >= self._files_total
                    or scanned >= _SEARCH_CAP)
            self._update_hint(scanned=scanned, capped=not done and scanned >= _SEARCH_CAP)
            if not done:
                QTimer.singleShot(0, lambda: self._search_more(token))

        def _fail(err):
            if token == self._search_token and rid == self._rid:
                self.hint_label.setText(f"搜索失败：{err}")
            log.warning("搜索房间文件失败 rid=%s：%s", rid, err)

        run_async(_proc, on_done=_done, on_fail=_fail, parent=self)

    def _build_files(self):
        self.file_list.clear()
        items = self._all_files
        if self._filter:
            q = self._filter.casefold()
            items = [f for f in items
                     if q in (f.get("name") or "").casefold()]
        by_uid = self._members_by_uid()
        for f in items:
            row = _FileRow(f, self._color, by_uid)
            item = QListWidgetItem(self.file_list)
            item.setSizeHint(row.sizeHint())
            row.clicked.connect(self._on_file_clicked)
            self.file_list.addItem(item)
            self.file_list.setItemWidget(item, row)
        if not items:
            text = "未找到匹配的文件" if self._filter else "暂无文件"
            self.file_list.addItem(QListWidgetItem(text))

    def _update_hint(self, scanned: int = None, capped: bool = False):
        if self._filter:
            hits = self.file_list.count()
            if scanned is None:
                self.hint_label.setText(f"命中 {hits} 个文件")
            else:
                text = f"正在搜索… {scanned}/{self._files_total} · 命中 {hits}"
                if capped:
                    text += "（已达扫描上限）"
                self.hint_label.setText(text)
        else:
            total = self._files_total
            loaded = len(self._all_files)
            if total and loaded < total:
                self.hint_label.setText(f"共 {total} 个文件 · 已加载 {loaded}")
            elif total:
                self.hint_label.setText(f"共 {total} 个文件")
            else:
                self.hint_label.setText("")

    def _on_file_clicked(self, f: dict):
        url = f.get("url") or ""
        if url:
            self.file_open_requested.emit(url, f.get("name") or "附件")

    def _show_menu(self, pos):
        item = self.file_list.itemAt(pos)
        row = self.file_list.itemWidget(item) if item is not None else None
        f = getattr(row, "_file", None) if row is not None else None
        if not f:
            return
        menu = QMenu(self)
        act_open = menu.addAction("打开")
        act_save = menu.addAction(icon("save_file", 16), "另存为…")
        act = menu.exec_(self.file_list.mapToGlobal(pos))
        if act is act_open:
            self._on_file_clicked(f)
        elif act is act_save:
            # 原生文件对话框不能在 popup 鼠标抓取下打开，先关面板再交主窗口
            self.close()
            self.file_save_as_requested.emit(f.get("url") or "",
                                             f.get("name") or "附件",
                                             int(f.get("size") or 0))


# ---------------------------------------------------------------------- #
# 消息记录面板
# ---------------------------------------------------------------------- #
class _ResultRow(QFrame):
    """搜索结果行：发送者 + 摘要 + 时间，点击定位原消息。"""

    clicked = pyqtSignal(object)    # Message

    def __init__(self, msg, theme_fn, parent=None):
        super().__init__(parent)
        self._msg = msg
        self.setCursor(Qt.PointingHandCursor)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(2)

        head = QHBoxLayout()
        sender = msg.sender.display_name if msg.sender else ""
        head.addWidget(_label(sender, 12, theme_fn("text_secondary")), 1)
        head.addWidget(_label(format_chat_time(msg.timestamp), 11,
                              theme_fn("text_secondary")))
        lay.addLayout(head)

        text = msg.text or msg.display_text or ""
        preview = " ".join(text.split())
        if len(preview) > 60:
            preview = preview[:60] + "…"
        preview_lbl = _label(preview or "（无文本内容）", 13,
                             theme_fn("text_primary"))
        preview_lbl.setWordWrap(True)
        lay.addWidget(preview_lbl)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self._msg)
        super().mousePressEvent(e)


class MessagesPanel(RoomPanelBase):
    _W = 360    # 搜索框独占一行 + 工具栏一行，宽 300 放不下日期框
    _H = 460

    locate_requested = pyqtSignal(object)   # Message
    date_jump_requested = pyqtSignal(float)  # 目标日 0 点 epoch 秒
    ai_search_requested = pyqtSignal(str)   # 自然语言查询（只上传查询词）

    def __init__(self, parent=None):
        super().__init__("messages", parent)
        self._scan_token = 0
        self._scan_scanned = 0
        self._scan_hits = 0
        self._render_count = 0
        self._pending_rows: list = []
        self._ai_keywords: list = []
        self._ai_busy = False

        # 搜索框独占一行：面板宽 300，四个控件挤一行会把输入框压到
        # 只剩几十像素（真机截图）。
        row = QHBoxLayout()
        row.setSpacing(6)
        self.search_input = WxLineEdit(self._theme, self)
        self.search_input.setPlaceholderText("搜索聊天记录")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setMinimumHeight(34)
        row.addWidget(self.search_input, 1)
        self._root.addLayout(row)

        tools = QHBoxLayout()
        tools.setSpacing(6)
        self.rx_box = QCheckBox("正则", self)
        tools.addWidget(self.rx_box)
        self.ai_search_btn = WxPushButton("AI 搜索", self._theme, self)
        self.ai_search_btn.setToolTip(
            "用 AI 把自然语言查询扩写成关键词（只上传查询词，不上传聊天记录）")
        self.ai_search_btn.clicked.connect(self._on_ai_search_clicked)
        tools.addWidget(self.ai_search_btn)
        tools.addStretch(1)
        # 按日期跳转：近 7 天直选 + 自定义日期
        self.date_combo = WxComboBox(self._theme, self)
        self.date_combo.addItem("跳转日期", userData=None)
        for i in range(7):
            day = time.localtime(time.time() - i * 86400)
            label = "今天" if i == 0 else ("昨天" if i == 1 else
                     time.strftime("%m-%d", day))
            zero = time.mktime((day.tm_year, day.tm_mon, day.tm_mday,
                                0, 0, 0, 0, 0, -1))
            self.date_combo.addItem(label, userData=float(zero))
        self.date_combo.addItem("选择日期…", userData="pick")
        self.date_combo.setCurrentIndex(0)
        self.date_combo.setToolTip("跳到指定日期的聊天记录")
        tools.addWidget(self.date_combo)
        self.date_edit = QDateEdit(self)
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setDate(QDate.currentDate())
        self.date_edit.setVisible(False)
        self.date_edit.setFixedWidth(130)
        self.date_edit.setMinimumHeight(30)
        tools.addWidget(self.date_edit)
        self._root.addLayout(tools)
        self._theme_widgets.extend([self.search_input, self.ai_search_btn,
                                    self.date_combo])
        self.date_combo.currentIndexChanged.connect(self._on_date_combo)
        self.date_edit.dateChanged.connect(self._on_date_picked)

        prog = QHBoxLayout()
        self.hint_label = WxLabel("", self._theme, self)
        self.hint_label.set_variant("secondary")
        prog.addWidget(self.hint_label, 1)
        self.stop_btn = WxPushButton("停止", self._theme, self)
        self.stop_btn.setVisible(False)
        prog.addWidget(self.stop_btn)
        self._root.addLayout(prog)
        self._theme_widgets.extend([self.hint_label, self.stop_btn])

        self.error_label = QLabel("", self)
        self.error_label.setObjectName("PanelError")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        self._root.addWidget(self.error_label)

        self.search_list = self._make_list()
        self._root.addWidget(self.search_list, 1)

        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(16)
        self._flush_timer.timeout.connect(self._flush_rows)

        self.search_input.returnPressed.connect(self._run_search)
        self.rx_box.toggled.connect(lambda _: self._run_search())
        self.stop_btn.clicked.connect(self._stop_scan)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(350)
        self._debounce.timeout.connect(self._run_search)
        self.search_input.textChanged.connect(self._on_search_text_changed)

    def _apply_theme(self):
        super()._apply_theme()
        self.error_label.setStyleSheet(
            f"#PanelError {{ color:{self._color('status_failed')}; }}")

    def _on_open(self):
        self._scan_token += 1
        self._ai_keywords = []
        self.set_ai_search_busy(False)
        self._debounce.stop()
        self._flush_timer.stop()
        self.search_input.clear()
        self.search_list.clear()
        self._pending_rows = []
        self._render_count = 0
        self.hint_label.setText("")
        self.error_label.setVisible(False)
        self.stop_btn.setVisible(False)
        self.date_combo.blockSignals(True)
        self.date_combo.setCurrentIndex(0)
        self.date_combo.blockSignals(False)
        self.date_combo.setVisible(True)
        self.date_edit.setVisible(False)
        self.search_input.setFocus()

    # ---------------- 按日期跳转 ---------------- #
    def _on_date_combo(self, index: int):
        data = self.date_combo.itemData(index)
        if not data:
            return
        if data == "pick":
            # 日期框与下拉互斥：两者同时出现会让这一行超宽，日期框被
            # 面板右缘裁掉（真机「控件显示不全」）
            self.date_combo.setVisible(False)
            self.date_edit.setVisible(True)
            self.date_edit.setFocus()
            return
        self.date_edit.setVisible(False)
        self.date_jump_requested.emit(float(data))

    def _on_date_picked(self, qdate, *_):
        """自定义日期确认后跳转；目标日按本地时区取 0 点。"""
        d = qdate.toPyDate()
        zero = time.mktime((d.year, d.month, d.day, 0, 0, 0, 0, 0, -1))
        self.date_edit.setVisible(False)
        self.date_combo.blockSignals(True)
        self.date_combo.setCurrentIndex(0)
        self.date_combo.blockSignals(False)
        self.date_combo.setVisible(True)
        self.date_jump_requested.emit(float(zero))

    def _on_closed(self):
        self._scan_token += 1
        self._ai_keywords = []
        self.set_ai_search_busy(False)
        self._flush_timer.stop()

    def _on_search_text_changed(self, *_):
        # 手动改查询即退出 AI 关键词模式，避免旧关键词继续过滤
        self._ai_keywords = []
        self._debounce.start()

    # ---------------- AI 自然语言搜索 ---------------- #
    def _on_ai_search_clicked(self, *_):
        if self._ai_busy:
            return
        text = (self.search_input.text() or "").strip()
        if not text:
            self.hint_label.setText("请先输入要搜索的内容")
            return
        self.set_ai_search_busy(True)
        self.hint_label.setText("正在理解查询…")
        self.ai_search_requested.emit(text)

    def set_ai_search_busy(self, busy: bool):
        self._ai_busy = bool(busy)
        self.ai_search_btn.setEnabled(not self._ai_busy)
        self.ai_search_btn.setText("理解中…" if self._ai_busy else "AI 搜索")

    def apply_ai_keywords(self, keywords):
        """主窗口拿到关键词后：以 OR 正则跑现有客户端全历史扫描。"""
        self.set_ai_search_busy(False)
        kws = [k for k in (keywords or []) if k]
        if not kws:
            self.hint_label.setText("AI 未返回可用关键词")
            return
        self._ai_keywords = kws
        self._scan_token += 1
        self.error_label.setVisible(False)
        self.stop_btn.setVisible(False)
        self.search_list.clear()
        self._pending_rows = []
        self._render_count = 0
        self._flush_timer.stop()
        self._regex_search("|".join(re.escape(k) for k in kws))

    def fail_ai_search(self, err: str):
        self.set_ai_search_busy(False)
        self.hint_label.setText(f"AI 搜索失败：{err}")

    def cancel_ai_search(self):
        self.set_ai_search_busy(False)
        if "已扫描" not in self.hint_label.text():
            self.hint_label.setText("")

    # ---------------- 搜索入口 ---------------- #
    def _run_search(self):
        self._scan_token += 1
        self._ai_keywords = []
        self.stop_btn.setVisible(False)
        self.error_label.setVisible(False)
        self.search_list.clear()
        self._pending_rows = []
        self._render_count = 0
        self._flush_timer.stop()
        text = (self.search_input.text() or "").strip()
        if not text or not self._rid or not self._client:
            self.hint_label.setText("")
            return
        if self.rx_box.isChecked():
            self._regex_search(text)
        else:
            # 服务端 chat.search 基于 MongoDB 文本索引，中文不分词基本搜
            # 不到（真机验证），普通搜索同样走客户端全历史扫描，按原文
            # 字面匹配（re.escape 等价于整段子串）。
            self._regex_search(re.escape(text))

    # ---------------- 正则/普通共用：客户端全历史扫描 ---------------- #
    def _regex_search(self, pattern):
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            self.error_label.setText(f"正则表达式无效：{e}")
            self.error_label.setVisible(True)
            return
        token = self._scan_token
        self._scan_scanned = 0
        self._scan_hits = 0
        self.stop_btn.setVisible(True)
        self._update_scan_hint(finished=False)
        self._scan_page(token, rx, 0)

    def _scan_page(self, token, rx, offset):
        rid = self._rid
        client = self._client
        room_type = self._room_type

        def _proc():
            # 后台线程：纯计算 + HTTP，不碰 widget。文本截断是防灾难性回溯的兜底
            msgs = client.get_history(rid, count=_SCAN_PAGE, offset=offset,
                                      room_type=room_type)
            hits = [m for m in msgs
                    if rx.search((m.text or m.display_text or "")[:5000])
                    or (m.file_name and rx.search(m.file_name[:200]))]
            return hits, len(msgs)

        def _done(res):
            if token != self._scan_token or rid != self._rid:
                return
            hits, page_len = res
            self._scan_scanned += page_len
            self._scan_hits += len(hits)
            self._queue_rows(hits)
            finished = (page_len < _SCAN_PAGE
                        or self._render_count >= _MAX_RESULT_ROWS)
            self._update_scan_hint(finished=finished)
            if finished:
                self.stop_btn.setVisible(False)
                if self._scan_hits == 0 and self._render_count == 0:
                    self.search_list.addItem(
                        QListWidgetItem("未找到匹配的消息"))
            else:
                QTimer.singleShot(0, lambda: self._scan_page(token, rx,
                                                             offset + page_len))

        def _fail(err):
            if token == self._scan_token and rid == self._rid:
                self.stop_btn.setVisible(False)
                self.hint_label.setText(f"扫描失败：{err}")
            log.warning("正则扫描历史失败 rid=%s：%s", rid, err)

        run_async(_proc, on_done=_done, on_fail=_fail, parent=self)

    def _update_scan_hint(self, finished: bool):
        text = f"已扫描 {self._scan_scanned} 条 · 命中 {self._scan_hits} 条"
        if self._render_count >= _MAX_RESULT_ROWS:
            text += f"（仅显示前 {_MAX_RESULT_ROWS} 条）"
        elif finished:
            text = f"已扫描全部 {self._scan_scanned} 条 · 命中 {self._scan_hits} 条"
        if self._ai_keywords:
            text = f"AI 关键词：{'、'.join(self._ai_keywords)} · {text}"
        self.hint_label.setText(text)

    def _stop_scan(self, *_):
        self._scan_token += 1
        self.stop_btn.setVisible(False)
        text = self.hint_label.text()
        if text and "已停止" not in text:
            self.hint_label.setText(text + "（已停止）")

    # ---------------- 结果渲染（分批插入） ---------------- #
    def _queue_rows(self, msgs):
        self._pending_rows.extend(msgs)
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def _flush_rows(self):
        batch = self._pending_rows[:_RESULT_FLUSH]
        del self._pending_rows[:_RESULT_FLUSH]
        for msg in batch:
            if self._render_count >= _MAX_RESULT_ROWS:
                self._pending_rows.clear()
                break
            row = _ResultRow(msg, self._color)
            item = QListWidgetItem(self.search_list)
            item.setSizeHint(row.sizeHint())
            row.clicked.connect(self.locate_requested.emit)
            self.search_list.addItem(item)
            self.search_list.setItemWidget(item, row)
            self._render_count += 1
        if not self._pending_rows:
            self._flush_timer.stop()


# ---------------------------------------------------------------------- #
# 控制器：互斥开合、信号汇聚
# ---------------------------------------------------------------------- #
class RoomPanels(QObject):
    """三个面板的统一控制器：同时最多开一个面板，信号汇聚给主窗口。"""

    closed = pyqtSignal(str)                    # kind（含点外部关闭）
    member_clicked = pyqtSignal(dict, object)   # (member, 行全局 rect)
    add_member_requested = pyqtSignal()
    file_open_requested = pyqtSignal(str, str)
    file_save_as_requested = pyqtSignal(str, str, int)   # (url, name, size)
    locate_requested = pyqtSignal(object)       # Message
    date_jump_requested = pyqtSignal(float)     # 目标日 0 点 epoch 秒

    def __init__(self, parent=None):
        super().__init__(parent)
        self.members = MembersPanel(parent)
        self.files = FilesPanel(parent)
        self.messages = MessagesPanel(parent)
        # 延迟 import：directory_panel / ai_panel 反向 import 本模块的
        # RoomPanelBase，顶层互相引会循环
        from .directory_panel import DirectoryPanel
        from .ai_panel import AIPanel
        self.directory = DirectoryPanel(parent)
        self.ai = AIPanel(parent)
        self._panels = {"members": self.members, "files": self.files,
                        "messages": self.messages,
                        "directory": self.directory, "ai": self.ai}
        for panel in self._panels.values():
            panel.panel_closed.connect(self._on_panel_closed)
        self.members.member_clicked.connect(self.member_clicked)
        self.members.add_member_requested.connect(self.add_member_requested)
        self.files.file_open_requested.connect(self.file_open_requested)
        self.files.file_save_as_requested.connect(self.file_save_as_requested)
        self.messages.locate_requested.connect(self.locate_requested)
        self.messages.date_jump_requested.connect(self.date_jump_requested)

    def _on_panel_closed(self, panel):
        self.closed.emit(panel.kind)

    def panel(self, kind: str) -> RoomPanelBase:
        return self._panels[kind]

    def set_theme(self, theme):
        for p in self._panels.values():
            p.set_theme(theme)

    def set_session(self, client, avatar_loader):
        for p in self._panels.values():
            p.set_session(client, avatar_loader)

    def set_members_provider(self, fn):
        self.members.set_provider(fn)

    def set_members_by_uid_provider(self, fn):
        self.files.set_by_uid_provider(fn)

    def is_open(self, kind: str) -> bool:
        return self._panels[kind].isVisible()

    def toggle(self, kind: str, anchor_btn, rid: str, room_type: str,
               name: str) -> bool:
        """打开目标面板（先关其它）；已开则关闭。返回打开后的开合态。"""
        panel = self._panels[kind]
        for k, p in self._panels.items():
            if k != kind and p.isVisible():
                p.close()
        if panel.isVisible():
            panel.close()
            return False
        panel.open_for(rid, room_type, name, anchor_btn)
        return True

    def close_all(self):
        for p in self._panels.values():
            if p.isVisible():
                p.close()
