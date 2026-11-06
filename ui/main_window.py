"""主窗口：微信式三栏布局 + 登录流程 + 实时消息整合。

布局：
    [Sidebar] [ConversationList] [ChatArea(ChatTitle + ChatView + ChatInput)]
"""
from __future__ import annotations

import logging
import os
import time
from collections import deque
from typing import Optional

from PyQt5.QtCore import (Qt, QEvent, QPoint, QRect, QRectF, QSize,
                          pyqtSignal, QTimer)
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLabel, QSplitter,
    QStackedWidget, QFrame, QDesktopWidget, QDialog,
    QApplication, QLineEdit, QPushButton, QMenu, QFileDialog,
    QSystemTrayIcon, QScrollArea,
)
from PyQt5.QtGui import (QCloseEvent, QGuiApplication, QPixmap, QCursor,
                         QColor, QPainter, QPainterPath, QRegion)

from core.api_client import ApiClient, ReceiptsUnsupportedError
from core.drafts import get_draft, set_draft
from core.messages import (E2E_PLACEHOLDER_TEXT, format_system_text,
                           preview_text, preview_with_sender, rc_shortcode)
from core.models import Conversation, Message
from .widgets import room_type_label, WxLabel, WxPushButton, _color as _c

from .sidebar import Sidebar
from .conversation_list import ConversationList
from .chat_view import ChatView
from .chat_input import ChatInput
from .screenshot_capture import ScreenshotOverlay
from .login_dialog import LoginDialog, LoginRunner, normalize_server
from .realtime_bridge import RealtimeBridge
from .api_workers import run_async
from .message_model import UNREAD_COUNT_LIMIT
from .icons import icon, status_pixmap
from .user_status import (UserStatusProvider, status_label, status_tooltip)
from .confirm_dialog import confirm, notify
from .message_list_dialog import MessageListDialog
from .upload_dialog import FileUploadDialog
from .room_panels import RoomPanels
from .settings_page import SettingsPage
from .theme import ThemeManager
from .tray import TrayController, build_tray_rows
from .ai_dialog import run_ai_dialog
from .window_chrome import WindowControls
# 无边框缩放热区常量与光标表直接复用弹窗骨架（少量共享，不另抽公共基类）
from .dialog_base import _RESIZE_CURSORS

log = logging.getLogger(__name__)

# 退出时等在途后台线程收尾的上限（秒）：网络卡死时不能把退出无限挂住
CLOSE_DRAIN_TIMEOUT = 3.0

# 无边框主窗口圆角半径（最大化时 0）
_WIN_RADIUS = 10.0
# 主窗口缩放热区：必须大于 _WIN_RADIUS。
# _update_window_mask 用 setMask 把四角裁成圆角，mask 外的区域收不到鼠标事件；
# 若热区(6)小于圆角半径(10)，角落 x<=6 且 y<=6 的点全被裁掉，_edge_at 只能
# 命中单条边，四角就只能上下或左右拖、不能斜着缩放。取 14 让圆角弧线外的
# 可见区域仍能同时命中两条边。
_WIN_RESIZE_MARGIN = 14
# 设置页顶部拖动带高度（右上角三按钮区域就落在其中）
_SETTINGS_BAND_H = 40


class ChatArea(QWidget):
    # kind: "members" / "files" / "messages"
    panel_requested = pyqtSignal(str)
    # 预览模式横幅上的「加入」按钮
    join_clicked = pyqtSignal()
    # 标题栏「更多」菜单按钮（开关房间 E2E / 查看房间信息）
    menu_requested = pyqtSignal()
    # 历史加载状态条上的「重试」按钮
    history_retry_clicked = pyqtSignal()
    # 连接横幅上的「立即重连」
    reconnect_clicked = pyqtSignal()
    # 「N 条新消息」提示条点击
    new_messages_clicked = pyqtSignal()

    _PANEL_BUTTONS = (
        ("members", "members", "成员"),
        ("files", "files", "文件"),
        ("messages", "search", "聊天记录"),
        ("ai", "ai", "AI 划重点"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ChatArea")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # 标题行：左侧房间名，右侧三个面板按钮（成员/文件/聊天记录）
        header = QHBoxLayout()
        header.setContentsMargins(16, 0, 8, 0)
        header.setSpacing(8)
        self.title = QLabel("")
        self.title.setObjectName("ChatTitle")
        self.title.setFixedHeight(52)
        header.addWidget(self.title, 1)
        # 私聊对方在线状态：房间名右侧的状态点 + 自定义状态文字（仅 d 房显示）
        self._dm_status_key = None
        self.status_icon = QLabel(self)
        self.status_icon.setFixedSize(14, 14)
        self.status_icon.hide()
        self.status_text = WxLabel("", None, self)
        self.status_text.set_variant("secondary")
        self.status_text.hide()
        header.addWidget(self.status_icon, 0, Qt.AlignVCenter)
        header.addWidget(self.status_text, 0, Qt.AlignVCenter)
        self.panel_buttons = {}
        for kind, icon_name, tip in self._PANEL_BUTTONS:
            btn = QPushButton(self)
            btn.setObjectName("ChatHeaderBtn")
            btn.setIcon(icon(icon_name, 18))
            btn.setIconSize(QSize(18, 18))
            btn.setFixedSize(30, 30)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.setToolTip(tip)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _=False, k=kind: self.panel_requested.emit(k))
            header.addWidget(btn, 0, Qt.AlignVCenter)
            self.panel_buttons[kind] = btn
        # 「更多」菜单按钮：不参与面板互斥（非 checkable），单独弹 QMenu
        self.menu_btn = QPushButton(self)
        self.menu_btn.setObjectName("ChatHeaderBtn")
        self.menu_btn.setIcon(icon("more", 18))
        self.menu_btn.setIconSize(QSize(18, 18))
        self.menu_btn.setFixedSize(30, 30)
        self.menu_btn.setCursor(Qt.PointingHandCursor)
        self.menu_btn.setFocusPolicy(Qt.NoFocus)
        self.menu_btn.setToolTip("更多")
        self.menu_btn.clicked.connect(self.menu_requested.emit)
        header.addWidget(self.menu_btn, 0, Qt.AlignVCenter)
        # 无边框主窗口的自绘三按钮：嵌在标题行右端，窗口级信号由 MainWindow 接线
        self.window_controls = WindowControls(self)
        header.addWidget(self.window_controls, 0, Qt.AlignVCenter)
        lay.addLayout(header)

        # 连接状态横幅：断线/重连中常驻，恢复且订阅就绪后隐藏。原先只有
        # 状态栏 3 秒提示，用户看不到「现在其实收不到消息」，也无法手动催
        # 重连（长轮询退避无上限时尤其难受）
        self._realtime_was_ready = False
        self._new_msg_count = 0
        self.conn_banner = QFrame()
        self.conn_banner.setObjectName("ConnBanner")
        cb_lay = QHBoxLayout(self.conn_banner)
        cb_lay.setContentsMargins(16, 6, 16, 6)
        cb_lay.setSpacing(8)
        self.conn_label = WxLabel("", None, self.conn_banner)
        self.conn_label.set_variant("secondary")
        cb_lay.addWidget(self.conn_label, 1)
        self.conn_btn = WxPushButton("立即重连", None,
                                     parent=self.conn_banner)
        # clicked 带 bool 参数：Cython 编译版必须显式吃掉
        self.conn_btn.clicked.connect(
            lambda _=False: self.reconnect_clicked.emit())
        cb_lay.addWidget(self.conn_btn, 0, Qt.AlignVCenter)
        self.conn_banner.hide()
        lay.addWidget(self.conn_banner)

        self.chat_view = ChatView()
        lay.addWidget(self.chat_view, 1)

        # 历史加载状态条：视图空白时给出「正在加载 / 加载失败·重试」的可见
        # 反馈。慢网络下（冷连接实测能抖到 20 秒）纯白屏会被用户当成程序
        # 死了、反复点击又静默无效（_load_history 有在途去重），必须让状态
        # 上界面。
        self.history_notice = QFrame()
        self.history_notice.setObjectName("HistoryNotice")
        notice_lay = QHBoxLayout(self.history_notice)
        notice_lay.setContentsMargins(16, 6, 16, 6)
        notice_lay.setSpacing(8)
        self.history_label = WxLabel("", None, self.history_notice)
        self.history_label.set_variant("secondary")
        notice_lay.addWidget(self.history_label, 1)
        self.history_retry_btn = WxPushButton("重试", None,
                                              parent=self.history_notice)
        # clicked 带 bool 参数：Cython 编译版不会自动截断，必须显式吃掉
        self.history_retry_btn.clicked.connect(
            lambda _=False: self.history_retry_clicked.emit())
        self.history_retry_btn.hide()
        notice_lay.addWidget(self.history_retry_btn, 0, Qt.AlignVCenter)
        self.history_notice.hide()
        lay.addWidget(self.history_notice)

        # 新消息提示条：用户向上翻历史时新消息照常插入，但不再把视图
        # 拽到底部（微信式），改为显示「N 条新消息」，点击或滚到底部清除
        self.new_msg_bar = QFrame()
        self.new_msg_bar.setObjectName("NewMsgBar")
        nm_lay = QHBoxLayout(self.new_msg_bar)
        nm_lay.setContentsMargins(0, 4, 0, 4)
        nm_lay.setSpacing(0)
        nm_lay.addStretch(1)
        self.new_msg_btn = WxPushButton("", None, parent=self.new_msg_bar)
        self.new_msg_btn.setCursor(Qt.PointingHandCursor)
        self.new_msg_btn.clicked.connect(
            lambda _=False: self.new_messages_clicked.emit())
        nm_lay.addWidget(self.new_msg_btn, 0, Qt.AlignVCenter)
        nm_lay.addStretch(1)
        self.new_msg_bar.hide()
        lay.addWidget(self.new_msg_bar)

        # 预览横幅：未加入的公开频道/团队以预览模式打开时替换输入框显示，
        # 提供加入入口（加入成功后 MainWindow 切回正常会话，横幅自动隐藏）
        self.preview_banner = QFrame()
        self.preview_banner.setObjectName("PreviewBanner")
        banner_lay = QHBoxLayout(self.preview_banner)
        banner_lay.setContentsMargins(16, 10, 16, 10)
        banner_lay.setSpacing(12)
        self.preview_label = WxLabel(
            "预览模式：加入后可发言并接收新消息", None, self.preview_banner)
        self.preview_label.set_variant("secondary")
        banner_lay.addWidget(self.preview_label, 1)
        self.join_btn = WxPushButton("加入频道", None, parent=self.preview_banner)
        self.join_btn.clicked.connect(self.join_clicked.emit)
        banner_lay.addWidget(self.join_btn, 0, Qt.AlignVCenter)
        self.preview_banner.hide()
        lay.addWidget(self.preview_banner)

        self.input = ChatInput()
        lay.addWidget(self.input)

    def set_preview_mode(self, on: bool, is_team: bool = False):
        """预览模式：横幅替换输入框；正常会话恢复输入框。"""
        self.preview_banner.setVisible(on)
        self.join_btn.setText("加入团队" if is_team else "加入频道")
        self.input.setVisible(not on)
        # 预览会话不在 _conversations 里，菜单拿不到 conv，直接隐藏
        self.menu_btn.setVisible(not on)

    def set_connection_state(self, state: str):
        """实时连接横幅四态：ready / connected / reconnecting / disconnected。

        首次连接（还没 ready 过）的 connected 不弹横幅，避免启动时闪一下；
        断线后重连成功要先显示「同步会话中」，直到 ready 才收起。
        """
        if state == "ready":
            self._realtime_was_ready = True
            self.conn_banner.hide()
            return
        if state == "connected":
            if not self._realtime_was_ready and not self.conn_banner.isVisible():
                return
            text, enabled = "已连接，正在同步会话…", False
        elif state == "reconnecting":
            text, enabled = "正在重新连接实时服务…", True
        elif state == "disconnected":
            text, enabled = "实时连接已断开，消息可能延迟", True
        else:
            return
        self.conn_label.setText(text)
        self.conn_btn.setEnabled(enabled)
        self.conn_banner.show()

    def reset_connection_state(self):
        """登出/换账号时复位（不能沿用上一个账号的「曾经 ready」）。"""
        self._realtime_was_ready = False
        self.conn_banner.hide()

    def show_new_messages(self, count: int):
        """累加并显示「N 条新消息」提示条（count 为本批新增条数）。"""
        if count <= 0:
            return
        self._new_msg_count += count
        self.new_msg_btn.setText(f"{self._new_msg_count} 条新消息 ↓")
        self.new_msg_bar.show()

    def clear_new_messages(self):
        self._new_msg_count = 0
        self.new_msg_bar.hide()

    def set_history_state(self, kind: str, text: str = ""):
        """历史加载状态条三态：hide / loading / error。

        只在「视图当前没有任何可渲染内容」时由 MainWindow 决定显示，
        命中缓存或磁盘快照时不该占一条横幅。
        """
        if kind == "hide":
            self.history_notice.hide()
            return
        self.history_label.setText(text)
        if kind == "error":
            # 失败态用 danger 变体（danger_fg 随主题），不新增色板键
            self.history_label.set_variant("danger")
            self.history_retry_btn.setVisible(True)
        else:
            self.history_label.set_variant("secondary")
            self.history_retry_btn.setVisible(False)
        self.history_notice.setVisible(True)

    def set_preview_theme(self, theme):
        """横幅配色随主题切换（QSS 全局模板不含该对象，单独刷）。"""
        if theme is None:
            return
        self.preview_banner.setStyleSheet(
            f"#PreviewBanner {{ background:{_c(theme, 'system_bubble_bg', '')};"
            f" border-top:1px solid {_c(theme, 'border', '')}; }}")
        for w in (self.preview_label, self.join_btn):
            try:
                w.set_theme(theme)
            except AttributeError:
                pass
        # 历史加载状态条同一条细横幅，取同样的语义色
        self.history_notice.setStyleSheet(
            f"#HistoryNotice {{ background:{_c(theme, 'system_bubble_bg', '')};"
            f" border-top:1px solid {_c(theme, 'border', '')}; }}")
        for w in (self.history_label, self.history_retry_btn):
            try:
                w.set_theme(theme)
            except AttributeError:
                pass
        # 连接横幅同一条细横幅
        self.conn_banner.setStyleSheet(
            f"#ConnBanner {{ background:{_c(theme, 'system_bubble_bg', '')};"
            f" border-bottom:1px solid {_c(theme, 'border', '')}; }}")
        for w in (self.conn_label, self.conn_btn):
            try:
                w.set_theme(theme)
            except AttributeError:
                pass
        # 新消息提示条：透明底，只让按钮本身取主题色
        self.new_msg_bar.setStyleSheet("#NewMsgBar { background: transparent; }")
        try:
            self.new_msg_btn.set_theme(theme)
        except AttributeError:
            pass

    def set_panel_active(self, kind):
        """同步面板按钮的勾选态：打开的面板按钮 checked，其余取消。"""
        for k, btn in self.panel_buttons.items():
            btn.setChecked(k == kind)

    def set_ai_available(self, available: bool):
        """AI 功能启用后才显示「AI 划重点」按钮。"""
        self.panel_buttons["ai"].setVisible(bool(available))

    def refresh_icons(self):
        """主题切换后重画图标（icons 按 (名,尺寸,颜色) 缓存，QSS 刷新不会
        自动更换已挂到按钮上的 QIcon）。"""
        for kind, icon_name, _tip in self._PANEL_BUTTONS:
            self.panel_buttons[kind].setIcon(icon(icon_name, 18))
        self.menu_btn.setIcon(icon("more", 18))

    def set_title(self, text: str):
        self.title.setText(text)

    def set_dm_status(self, key: str, status_text: str = ""):
        """私聊标题栏的对方状态（图标 + 文字，自定义状态优先显示）。"""
        if not key:
            self.clear_dm_status()
            return
        self._dm_status_key = key
        self.status_icon.setPixmap(status_pixmap(key, 14))
        self.status_icon.setToolTip(status_tooltip(key, status_text))
        self.status_text.setText(status_label(key, status_text))
        self.status_icon.show()
        self.status_text.show()

    def clear_dm_status(self):
        self._dm_status_key = None
        self.status_icon.hide()
        self.status_text.clear()
        self.status_text.hide()

    def refresh_status_icon(self):
        """主题切换后重画状态点（icons 的 pixmap 缓存已随主题清空）。"""
        if self._dm_status_key:
            self.status_icon.setPixmap(status_pixmap(self._dm_status_key, 14))


class MainWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("MainWindow")
        self.setWindowTitle("Fchat")
        # 无边框：系统标题栏换成自绘三按钮（chat/settings 两处 WindowControls）。
        # 透明背景 + paintEvent 自绘圆角底（同 FramelessCardDialog）；不透明
        # 子部件（侧栏/聊天视口）会盖出直角，用窗口 mask 钳到圆角形内。
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        # 无系统边框后缩放没有 OS 最小尺寸兜底，自己设一个
        self.setMinimumSize(720, 480)
        # 拖动/缩放状态（同 dialog_base 的手写方案；system 调用失败时兜底）
        self._drag_offset = None
        self._resize_edges = 0
        self._resize_start = None
        self._resize_cursor = None

        self.client: ApiClient = None
        self.realtime: RealtimeBridge = None
        self.avatar_loader = None
        self.image_loader = None
        # 当前账号的头像 URL：上传成功后按它清缓存；未登录时保持空串兜底
        self._avatar_url = ""
        # 头像上传防重入：run_async 发出后立即返回，模态选图框关闭后再点头像
        # 会叠出第二个并发上传（状态提示交错、双重清缓存），必须用标志挡住
        self._avatar_busy = False

        self._conversations = {}          # rid -> Conversation
        # rid -> (最后消息时间, Message)：客户端实际见过的最新尾部。
        # 服务端 rooms.get 的 lm/lastMessage 会滞后，会话列表整表重拉时
        # 用它兜底，不能被过期摘要顶回去。存 Message 而不是现成的预览
        # 文本：兜底落地时才知道目标会话是私聊还是频道，前缀规则不同。
        self._latest_by_room = {}
        self._room_messages = {}          # rid -> list[Message]（时间正序，旧->新）
        self._history_offset = {}         # rid -> 已加载条数（也作加载标志）
        self._history_more = {}           # rid -> 是否还有更早消息
        # rid 集合：当前窗口是 around 拉出的锚定页（日期跳转/搜索定位），
        # 不是从最新页开始的完整历史。滚动向上改走锚定分页；重进房间
        # 必须丢弃它重拉最新首屏，否则 offset>0 会让房间永远停在那一页
        self._history_anchored = set()
        # rid 集合：锚定窗口已向下补到最新（滚到底是触发点，补过一次
        # 就不用再请求；新消息此后走实时推送追加）
        self._history_forward_done = set()
        # 重建锚定页期间滚动条会瞬时归零/滚动到落点，别当成用户滚到底
        self._suppress_bottom_load = False
        # rid -> 锚定窗口代数：换锚定页/重进房间后，在途的向下续载结果作废
        self._history_anchor_gen = {}
        # rid -> 锚定窗口最新的连续时间戳（向下续载游标）。不能直接取
        # _room_messages 尾条：锚定期间自己发的乐观消息、实时推送都会
        # 追加到尾部，拿它当游标会跳过「锚定日~现在」的间隔
        self._history_anchor_ts = {}
        self._history_loading = set()     # 正在加载首屏历史的房间 rid
        # 首屏失败时待重试的会话（状态条「重试」按钮点它重新加载）
        self._history_retry_conv: Optional[Conversation] = None
        self._load_gen = {}               # rid -> 加载代数（切换房间时使旧回调失效）
        # 本轮首屏用了磁盘快照的房间 rid：网络首屏回来后要整体 rebuild
        # （prepend 去重保不住编辑/撤回后的新内容）
        self._snapshot_shown = set()
        self._loading_older = False
        self._loading_newer = False
        self._loading_conversations = False
        self._active_room_id = None
        self._room_type = "c"
        # rid -> (本地已读时间戳, 服务端未读数)：打开会话时采集，用于
        # 标出「以下为新消息」；切房即丢弃，不跨房间复用
        self._pending_unread = {}
        # 预览模式中的房间：rid -> DirectoryEntry（加入时要 team_id 等信息）
        self._preview_entries = {}
        self._page_size = 30
        self._quote_fetching = set()      # 正在补抓原消息的引用 msg id（防重复）
        self._quote_enriched = set()      # 已补抓过原消息的引用消息 id（每会话一次）
        self._login_runner = None           # 自动登录用的后台管道
        # 在途上传进度弹窗（无父独立顶层窗口）：持强引用防 GC，主窗口关闭时
        # 统一 force_close，否则会被计入 quitOnLastWindowClosed 卡住退出
        self._upload_dialogs = set()
        self.theme_manager = ThemeManager()  # 主题管理器

        # 系统托盘：关闭到托盘、未读闪烁与预览卡片。托盘不可用（含
        # offscreen）时 _tray 保持 None，所有行为退回旧版直接退出。
        self._tray = None
        # 未读提醒（实时消息）环形队列：重启后没有消息体，卡片回退用
        # 会话的 last_message_text 汇总
        self._tray_alerts = deque(maxlen=50)
        self._force_quit = False            # 托盘「关闭程序」置位，close 不再拦截
        self._closing = False               # 退出清理防重入
        # 登录成功到认证 gate 返回之间 client 还没挂上；登出流程据此
        # 判断「用户正在重新登录」而不是关闭应用
        self._login_pending = False

        # 已读回执（私聊双勾 / 群聊 N人未读）：权威缓存与探测降级标记
        self._receipts = {}               # rid -> {msg_id: [receipt dict]}
        self._receipts_fetching = set()   # 在途去重（同房间一次一个请求）
        self._receipts_degraded = False   # 服务端明确拒绝后置 True（仅内存，重启重新探测）
        self._members_cache = {}          # rid -> (拉取时刻, [member dict])
        self._members_fetching = set()
        self._members_ready_cbs = {}      # rid -> [回调]（成员面板等补拉完成后建表）
        self._receipts_timer = QTimer(self)
        # 已读回执当前靠轮询（服务端不推回执事件）。活跃房间 8s 一次，配合
        # 新消息/发送/窗口激活后的立即触发，把"对方已读"的可见延迟从最长
        # ~30s 压到秒级；请求只发当前活动房间，代价可接受。
        self._receipts_timer.setInterval(8000)
        self._receipts_timer.timeout.connect(self._fetch_receipts_for_active_room)
        # 消息定时提醒：30s 轮询到期项，经托盘 toast 通知（重启后仍生效）
        self._reminders_timer = QTimer(self)
        self._reminders_timer.setInterval(30000)
        self._reminders_timer.timeout.connect(self._check_reminders)

        # 实时消息节流：缓冲区内消息合并后批量渲染
        # 元素是 (Message, is_update)：同 id 重推（编辑/表情回应等）不能
        # 当新增处理，否则会滚动到底部
        self._pending_realtime_messages = []
        self._realtime_flush_timer = QTimer()
        self._realtime_flush_timer.setInterval(100)  # 100ms 合并窗口
        self._realtime_flush_timer.setSingleShot(True)
        self._realtime_flush_timer.timeout.connect(self._flush_realtime_messages)

        # 正在编辑的消息 id（"" = 普通发送）；发送走 _on_send_text 分流
        self._editing_msg_id = ""
        # 有未发送草稿的房间 id 集合（驱动会话列表 [草稿] 标识）
        self._draft_rooms: set = set()
        # local- 假 id -> 发送载荷：失败后重发原样再用一次，成功即删
        self._retry_payloads = {}
        self._reaction_picker = None
        self._reaction_target = None
        self._message_list_dialog = None
        # 每会话草稿：输入变化 500ms 后落盘（切房/关闭/登出另有强制保存）
        self._draft_timer = QTimer(self)
        self._draft_timer.setSingleShot(True)
        self._draft_timer.setInterval(500)
        # 定时器不能直连 _save_current_draft：编辑态下那个函数会 cancel_edit
        # 恢复原文（切房/登出需要），打字停顿 500ms 就把正在编辑的内容吞掉
        self._draft_timer.timeout.connect(self._on_draft_timeout)

        self._setup_ui()
        self._setup_tray()

        # 启动：开了自动登录则后台直接登录，否则弹登录框
        self._try_auto_login()

    # ------------------------------------------------------------------ #
    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.sidebar = Sidebar()
        root.addWidget(self.sidebar)

        # 右半部分：会话列表 | 内容区
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        # 默认 4px 手柄透底色像一条空白缝，收成 1px 细分割线（色走 QSS）
        splitter.setHandleWidth(1)

        # 会话列表上方加 header：搜索框 + 新建(+)
        self.conversation_header = QWidget()
        self.conversation_header.setObjectName("ConversationHeader")
        header_lay = QVBoxLayout(self.conversation_header)
        # 右 margin 必须是 0：列表滚动条要贴住分割线，否则滚动条右侧
        # 多出一条与列表同色的死白带（真机截图反馈过）
        header_lay.setContentsMargins(10, 8, 0, 4)
        header_lay.setSpacing(6)

        # 搜索 + 新建放在同一行：搜索框占满，+ 按钮在最右
        top_row = QHBoxLayout()
        top_row.setSpacing(4)
        top_row.setContentsMargins(0, 0, 10, 0)
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("HeaderSearch")
        self.search_edit.setPlaceholderText("搜索")
        self.search_edit.setClearButtonEnabled(True)
        from .icons import icon
        self.new_room_btn = QPushButton(icon("plus", 16), "")
        self.new_room_btn.setObjectName("NewRoomButton")
        self.new_room_btn.setToolTip("新建")
        self.new_room_btn.setFixedSize(28, 28)
        self.directory_btn = QPushButton(icon("directory", 16), "")
        self.directory_btn.setObjectName("DirectoryButton")
        self.directory_btn.setToolTip("目录")
        self.directory_btn.setFixedSize(28, 28)
        self.directory_btn.setCheckable(True)
        self.directory_btn.setFocusPolicy(Qt.NoFocus)
        top_row.addWidget(self.search_edit, 1)
        top_row.addWidget(self.new_room_btn, 0)
        top_row.addWidget(self.directory_btn, 0)
        header_lay.addLayout(top_row)

        self.conversation_list = ConversationList()
        header_lay.addWidget(self.conversation_list, 1)

        # 会话列表容器
        self.conversation_panel = QWidget()
        panel_lay = QVBoxLayout(self.conversation_panel)
        panel_lay.setContentsMargins(0, 0, 0, 0)
        panel_lay.setSpacing(0)
        panel_lay.addWidget(self.conversation_header)
        splitter.addWidget(self.conversation_panel)
        splitter.setStretchFactor(0, 2)

        self.content_stack = QStackedWidget()
        self.chat_area = ChatArea()
        self.settings_page = SettingsPage()
        self.content_stack.addWidget(self.chat_area)       # index 0
        self.content_stack.addWidget(self.settings_page)   # index 1
        splitter.addWidget(self.content_stack)
        splitter.setStretchFactor(1, 5)
        splitter.setSizes([280, 760])

        root.addWidget(splitter, 1)

        # 无边框窗口三按钮：两处实例统一接线（close 走原 closeEvent，
        # 已登录时仍是「关到托盘」语义）
        self._window_controls = [self.chat_area.window_controls,
                                 self.settings_page.window_controls]
        for wc in self._window_controls:
            wc.minimize_clicked.connect(self.showMinimized)
            wc.maximize_clicked.connect(self._toggle_maximized)
            wc.close_clicked.connect(self.close)
        # 可拖动带：会话列表头部（搜索+新建行）、聊天页标题、设置页左导航。
        # 这些区域的非交互空白处按下即拖动窗口；按钮/输入框各自消费事件不受影响
        self._drag_bands = [self.conversation_header, self.chat_area.title,
                            self.settings_page.nav]
        # 不透明子部件会吞掉靠近边沿的鼠标移动，过滤器里按指针位置复位
        # 缩放光标（同 dialog_base），并拦截落在缩放热区里的按下事件。
        # 子部件必须开 mouse tracking：未按下时只有开了 tracking 才会持续
        # 产生 MouseMove，否则光标只在鼠标 Enter 进子部件的瞬间刷一次，
        # 移到边沿热区时拖拽图标不跟随（顶部/底部图标出现不及时的根因）。
        # 滚动条（聊天/会话列表的垂直条）不是 viewport、也不在过滤器里，
        # 会盖住窗口右/左边缘，鼠标搁在滚动条上既不刷新光标也无法缩放，
        # 一并纳入过滤器并开 tracking。
        self._resize_tracked = []
        for w in (self.sidebar, self.conversation_list.viewport(),
                  self.chat_area.chat_view.viewport(), self.settings_page.nav):
            w.setMouseTracking(True)
            w.installEventFilter(self)
            self._resize_tracked.append(w)
        for sb in (self.chat_area.chat_view.verticalScrollBar(),
                   self.conversation_list.verticalScrollBar()):
            sb.setMouseTracking(True)
            sb.installEventFilter(self)
            self._resize_tracked.append(sb)
        # 设置页各页是 QScrollArea，视口同样盖住窗口右边缘，一并纳入
        for i in range(self.settings_page._stack.count()):
            page = self.settings_page._stack.widget(i)
            if isinstance(page, QScrollArea):
                page.viewport().setMouseTracking(True)
                page.viewport().installEventFilter(self)
                self._resize_tracked.append(page.viewport())

        # 连接信号
        self.sidebar.show_chats.connect(self._show_chat_tab)
        self.sidebar.show_settings.connect(self._show_settings_tab)
        # 会话列表顶部：搜索框实时过滤列表（匹配会话名 + 聊天记录内容）；
        # 新建按钮弹出选房间菜单
        self.search_edit.textChanged.connect(self._on_search_changed)
        # 搜索框回车：在**当前打开的会话**内搜索消息并跳转高亮
        self.search_edit.returnPressed.connect(self._search_messages)
        self.new_room_btn.clicked.connect(self._show_create_menu)
        self.conversation_list.conversation_selected.connect(self._on_conversation_selected)
        self.conversation_list.favorite_requested.connect(self._toggle_favorite_room)
        self.conversation_list.room_mute_requested.connect(self._on_toggle_mute)
        self.conversation_list.mark_unread_requested.connect(self._on_mark_unread)
        self.conversation_list.delete_requested.connect(self._on_delete_room)
        self.chat_area.input.send_text.connect(self._on_send_text)
        self.chat_area.input.send_file.connect(self._on_send_file)
        self.chat_area.input.sticker_selected.connect(self._on_send_file)
        self.chat_area.input.send_mixed.connect(self._on_send_mixed)
        self.chat_area.input.screenshot_requested.connect(self._on_screenshot)
        # 消息右键菜单信号
        self.chat_area.chat_view.recall_requested.connect(
            self._on_recall_message)
        self.chat_area.chat_view.quote_requested.connect(
            self._on_quote_message)
        self.chat_area.chat_view.retry_requested.connect(
            self._on_retry_message)
        self.chat_area.chat_view.reaction_toggle_requested.connect(
            self._on_reaction_toggle)
        self.chat_area.chat_view.reaction_add_requested.connect(
            self._on_reaction_add)
        self.chat_area.chat_view.edit_requested.connect(
            self._on_edit_message)
        self.chat_area.chat_view.pin_requested.connect(
            self._on_pin_message)
        self.chat_area.chat_view.star_requested.connect(
            self._on_star_message)
        self.chat_area.chat_view.remind_requested.connect(
            self._on_remind_requested)
        self.chat_area.chat_view.remind_cancel_requested.connect(
            self._on_remind_cancel_requested)
        self.chat_area.chat_view.forward_requested.connect(
            self._on_forward_message)
        self.chat_area.chat_view.forward_many_requested.connect(
            self._on_forward_many)
        self.chat_area.chat_view.profile_requested.connect(
            self._on_profile_requested)
        self.chat_area.chat_view.mention_clicked.connect(
            self._on_mention_clicked)
        # @ 提及：候选缺成员时请求补拉当前房间成员
        self.chat_area.input.mention_wanted.connect(
            self._on_mention_wanted)
        # 每会话草稿：输入停顿 500ms 落盘；取消编辑要同步清发送分流标记
        self.chat_area.input.input.textChanged.connect(
            lambda: self._draft_timer.start())
        self.chat_area.input.edit_cancelled.connect(
            self._on_edit_cancelled)
        # AI：输入框写作助手 + 消息右键子菜单（只回填，不自动发送）
        self.chat_area.input.ai_assist_requested.connect(self._on_ai_assist)
        self.chat_area.chat_view.ai_action_requested.connect(
            self._on_ai_message_action)
        self.chat_area.chat_view.image_view_requested.connect(
            self._on_image_view_requested)
        self.chat_area.chat_view.inline_image_view_requested.connect(
            self._on_inline_image_view_requested)
        # 离线文字识别：本机推理，结果可编辑后插入输入框
        self._ocr_task = None
        self._ocr_token = 0
        self.chat_area.chat_view.ocr_requested.connect(self._on_ocr_requested)
        self.chat_area.chat_view.quote_jump_requested.connect(
            self._on_quote_jump)
        # 群聊「N人未读」点击 → 已读/未读名单浮窗
        self.chat_area.chat_view.read_receipts_requested.connect(
            self._on_read_receipts_requested)
        # 拖入本地文件到消息列表：弹确认后整批发给当前会话
        self.chat_area.chat_view.drop_files_requested.connect(
            self._on_chat_view_drop)
        # 聊天视图状态提示转发到主窗口状态栏
        # 状态栏只在我有消息要提示时短暂出现：空 QStatusBar 常驻底部会在
        # 整个窗口下沿横出一条空白带，割裂左侧深色栏（真机反馈）。无边框
        # 窗口自绘缩放热区，原生 size grip 也不需要。
        sb = self.statusBar()
        sb.setSizeGripEnabled(False)
        sb.messageChanged.connect(lambda text: sb.setVisible(bool(text)))
        sb.hide()
        self.chat_area.chat_view.status_message.connect(
            self.statusBar().showMessage)
        # 向上滚动到顶部时加载更早的历史
        self.chat_area.chat_view.verticalScrollBar().valueChanged.connect(
            self._on_scroll)
        # 新消息提示条：点击跳到底部；手动滚到底部也清除
        self.chat_area.new_messages_clicked.connect(
            self._on_new_messages_clicked)
        self.chat_area.chat_view.scrolled_to_bottom.connect(
            self.chat_area.clear_new_messages)
        # 锚定窗口滚到底：向下补齐锚定日期之后的新消息
        self.chat_area.chat_view.scrolled_to_bottom.connect(
            self._on_view_bottom)

        # 头像 / 图片加载器
        from .avatar_loader import AvatarLoader, ImageLoader
        self.avatar_loader = AvatarLoader(self)
        self.conversation_list.set_avatar_loader(self.avatar_loader)
        self.chat_area.chat_view.set_avatar_loader(self.avatar_loader)
        self.image_loader = ImageLoader(server_url="", parent=self)
        self.chat_area.chat_view.set_image_loader(self.image_loader)
        self.chat_area.input.set_image_loader(self.image_loader)

        # 房间三功能浮层面板（成员/文件/聊天记录），按钮正下方弹出、互斥打开
        self._room_panels = RoomPanels(self)
        self._room_panels.set_theme(self.theme_manager)
        self.chat_area.panel_requested.connect(self._on_panel_requested)
        self.chat_area.menu_requested.connect(self._on_room_menu)
        self._room_panels.closed.connect(self._on_panel_closed)
        # AI 划重点：面板只发意图，门禁/采集/模型调用统一在主窗口
        self._ai_panel_task = None
        self._ai_panel_token = 0
        # 在途任务发起时的房间 id：换房间后结果无处安放，须取消
        self._ai_panel_rid = None
        self._room_panels.ai.generate_requested.connect(
            self._on_ai_panel_generate)
        self._room_panels.ai.stop_requested.connect(self._on_ai_panel_stop)
        self._room_panels.ai.save_requested.connect(self._on_ai_panel_save)
        # 自然语言历史搜索：只上传查询词，关键词在本地扫描
        self._ai_search_task = None
        self._ai_search_token = 0
        self._room_panels.messages.ai_search_requested.connect(
            self._on_ai_search_requested)
        self._room_panels.file_open_requested.connect(
            self.chat_area.chat_view.open_room_file)
        self._room_panels.file_save_as_requested.connect(
            self.chat_area.chat_view.save_file_as)
        self._room_panels.member_clicked.connect(self._on_panel_member_clicked)
        self._room_panels.add_member_requested.connect(
            self._on_add_member_requested)
        self._room_panels.locate_requested.connect(self._locate_message)
        self._room_panels.date_jump_requested.connect(self._on_date_jump)
        self._room_panels.set_members_provider(self._members_provider)
        self._room_panels.set_members_by_uid_provider(
            lambda rid: {m.get("_id") or "": m
                         for m in (self._members_cache.get(rid, (0, []))[1])})
        # 目录面板：点行开私聊 / 开会话（未加入的进预览）
        self._room_panels.directory.dm_requested.connect(
            self._on_dm_requested)
        self._room_panels.directory.open_requested.connect(
            self._on_directory_entry_open)
        self._room_panels.directory.set_joined_provider(
            lambda entry: entry.rid in self._conversations)
        # 用户状态按需拉取器：会话列表/成员面板/目录/私聊标题栏共用
        self._status_provider = UserStatusProvider(self)
        # username -> 状态拉取失败后的重试次数（成功即清；防失败风暴）
        self._status_attempts = {}
        # subscriptions-changed inserted 的防抖重拉定时器（延迟创建）
        self._conv_reload_timer: Optional[QTimer] = None
        self._room_panels.members.set_status_provider(
            self._status_provider.get)
        self._room_panels.directory.set_status_provider(
            self._status_provider.get)
        # 目录按钮 + 预览横幅的加入按钮
        self.directory_btn.clicked.connect(self._on_directory_requested)
        self.chat_area.join_clicked.connect(self._join_preview_room)
        self.chat_area.history_retry_clicked.connect(
            self._on_history_retry_clicked)
        self.chat_area.reconnect_clicked.connect(
            self._on_reconnect_clicked)
        self.chat_area.set_preview_theme(self.theme_manager)

        # 设置页面信号
        self.settings_page.theme_changed.connect(self._on_theme_changed)
        self.settings_page.accent_changed.connect(self._on_accent_changed)
        self.settings_page.font_family_changed.connect(self._on_font_family_changed)
        self.settings_page.font_size_changed.connect(self._on_font_size_changed)
        self.settings_page.wallpaper_changed.connect(self._on_wallpaper_changed)
        self.settings_page.auto_login_toggled.connect(self._on_auto_login_toggled)
        self.settings_page.logout_requested.connect(self._on_logout_requested)
        self.settings_page.status_changed.connect(self._on_status_changed)
        self.settings_page.shortcuts_changed.connect(self._on_shortcuts_changed)
        # 存储页的 (文本, 毫秒) 提示与 QStatusBar.showMessage 签名一致，直接连
        self.settings_page.storage_message.connect(self.statusBar().showMessage)
        self.settings_page.avatar_clicked.connect(self._on_change_avatar)
        self.settings_page.server_saved.connect(self._on_server_saved)
        self.settings_page.set_avatar_loader(self.avatar_loader)
        self.settings_page.upload_service_changed.connect(
            self._on_upload_service_edited)
        self.settings_page.upload_service_toggled.connect(
            self._on_upload_service_toggled)
        self.settings_page.update_auto_check_toggled.connect(
            self._on_update_auto_check_toggled)
        self.settings_page.notify_toggled.connect(self._on_notify_toggled)
        self.settings_page.notify_mentions_toggled.connect(
            self._on_notify_mentions_toggled)
        self.settings_page.send_mode_changed.connect(self._on_send_mode_changed)
        self.settings_page.check_update_requested.connect(
            lambda: self._check_for_update(manual=True))
        # E2E 密码：输入完成（回车或焦点移开）时落盘保存
        self.settings_page.e2e_input.editingFinished.connect(
            self._on_e2e_password_edited)
        # AI 配置：开关即时落盘，保存/测试连接交给主窗口
        self.settings_page.llm_enabled_toggled.connect(
            self._on_llm_enabled_toggled)
        self.settings_page.llm_config_changed.connect(
            self._on_llm_config_changed)
        self.settings_page.llm_test_requested.connect(
            self._on_llm_test_requested)
        self.settings_page.llm_refresh_requested.connect(self._push_llm_config)

        # 加载并应用主题
        self._load_and_apply_theme()
        # 主题应用会清图标缓存并换色，构造期挂上的图标要按新色重挂
        self._refresh_chrome_icons()
        # 恢复登录相关设置的界面初值
        from config import load_config
        cfg = load_config()
        self.settings_page.set_auto_login(cfg["auto_login"])
        # 恢复服务器地址到通用页输入框
        self.settings_page.set_server_url(cfg["server_url"])
        # 恢复快捷键：应用到主窗口，并回写到设置页输入框（否则编辑框始终显示默认值）
        saved_shortcuts = cfg.get("shortcuts") or {}
        self._apply_shortcuts(saved_shortcuts)
        self.settings_page.set_shortcuts(saved_shortcuts)
        self.settings_page.set_upload_service(cfg.get("upload_service_url", ""))
        self.settings_page.set_upload_service_token(cfg.get("upload_service_token", ""))
        self.settings_page.set_upload_service_enabled(
            cfg.get("upload_service_enabled", False))
        # E2E 密码已配置时要回写输入框（之前只存不显，输入框一直是空的）
        self.settings_page.set_e2e_password(cfg.get("e2e_password", ""))
        self.settings_page.set_update_auto_check(cfg.get("update_auto_check", True))
        # 通知与发送方式回写（_notify_* 供 _add_tray_alert 热路径直接读，避免每次 load_config）
        self._notify_enabled = bool(cfg.get("notify_enabled", True))
        self._notify_mentions_only = bool(cfg.get("notify_mentions_only", False))
        self.settings_page.set_notify(self._notify_enabled)
        self.settings_page.set_notify_mentions(
            cfg.get("notify_mentions_only", False))
        self.settings_page.set_send_mode(cfg.get("send_mode", "enter"))
        self._apply_send_mode(cfg.get("send_mode", "enter"))
        self._push_llm_config()
        # 打包版启动后延时检查更新（避开登录/订阅高峰；dev 模式内部会跳过）
        self._update_busy = False
        QTimer.singleShot(3000, lambda: self._check_for_update(manual=False))

    def _on_auto_login_toggled(self, enabled: bool):
        from config import save_auto_login, load_config
        save_auto_login(enabled)
        cfg = load_config()
        if enabled and not (cfg["remember_password"] and cfg["password"]):
            self.statusBar().showMessage(
                "当前未保存密码，自动登录要在下次登录时勾选「记住密码」才会生效", 8000)

    def _on_e2e_password_edited(self, *_):
        """E2E 密码编辑完成：落盘并即时生效（重新解私钥），成功后自动
        重拉含占位消息的加密房间；失败在输入框下方红字提示。"""
        from config import save_e2e_password
        pw = self.settings_page.e2e_input.text().strip()
        save_e2e_password(pw)
        client = self.client
        if not client or not client.connected:
            self.settings_page.set_e2e_hint("已保存，登录后生效")
            return
        if not pw:
            client.e2e = None
            self.settings_page.set_e2e_hint("已清除 E2E 密码，加密消息将显示占位")
            return
        if client.e2e is not None and client.e2e.enabled \
                and client.e2e.password == pw:
            return  # 密码没变（仅焦点移开），不重复发起验证
        if client.e2e is None:
            client.create_e2e(pw)
        else:
            # 房间密钥虽与密码无关，但私钥对象必须重建，缓存一并作废
            client.e2e.password = pw
            client.e2e.enabled = False
            client.e2e._priv_key = None
            with client.e2e._lock:
                client.e2e._room_keys.clear()
        self.settings_page.set_e2e_hint("正在验证 E2E 密码…")

        def _ok(ok):
            if ok:
                self.settings_page.set_e2e_hint("解密已启用")
                self._refresh_encrypted_rooms()
            else:
                self.settings_page.set_e2e_hint(
                    "E2E 密码不正确，加密消息无法解密", error=True)

        def _fail(err):
            log.warning("E2E 密码验证失败：%s", err)
            self.settings_page.set_e2e_hint(
                f"E2E 密码验证失败：{err}", error=True)

        run_async(client.e2e.init_keys, on_done=_ok, on_fail=_fail, parent=self)

    def _refresh_encrypted_rooms(self):
        """E2E 密码生效后重拉缓存里含占位消息的房间首屏，替换占位显示。"""
        client = self.client
        if client is None:
            return
        for rid, msgs in list(self._room_messages.items()):
            if not any(m.msg_type == "system"
                       and m.text == E2E_PLACEHOLDER_TEXT for m in msgs):
                continue
            conv = self._conversations.get(rid)
            if conv is None:
                continue
            gen = self._load_gen.get(rid, 0)

            def _done(msgs, rid=rid, gen=gen):
                if self._load_gen.get(rid, 0) != gen:
                    return
                self._room_messages[rid] = list(reversed(msgs))
                self._history_offset[rid] = len(msgs)
                self._history_more[rid] = len(msgs) >= self._page_size
                if rid == self._active_room_id:
                    view = self.chat_area.chat_view
                    view.rebuild_messages(self._room_messages[rid])
                    self._apply_receipts_state_to_view(rid)

            def _fail(err, rid=rid):
                log.warning("加密房间消息刷新失败 rid=%s：%s", rid, err)

            run_async(
                lambda rid=rid, conv=conv: client.get_history(
                    rid, count=self._page_size, offset=0,
                    room_type=conv.room_type),
                on_done=_done, on_fail=_fail, parent=self)

    # ------------------------------------------------------------------ #
    # AI 助手：配置读写、连通性测试、隐私门禁
    # ------------------------------------------------------------------ #
    def _push_llm_config(self):
        """把配置文件里的 AI 配置回读到设置页（不含日志，token 只在内存）。"""
        from config import load_config
        cfg = load_config()
        self.settings_page.set_llm_config(
            cfg.get("llm_url", ""), cfg.get("llm_model", ""),
            cfg.get("llm_token", ""), bool(cfg.get("llm_enabled", False)))
        self._refresh_ai_entry_visibility()

    def _on_llm_enabled_toggled(self, enabled: bool):
        from config import save_llm
        save_llm(enabled=enabled)
        self._refresh_ai_entry_visibility()
        self.settings_page.set_llm_hint(
            "AI 功能已启用" if enabled else "AI 功能已关闭")
        log.info("AI 功能开关：%s", "on" if enabled else "off")

    def _on_llm_config_changed(self, url: str, model: str, token: str):
        from config import load_config, save_llm
        url = (url or "").strip()
        model = (model or "").strip()
        token = (token or "").strip()
        old = load_config()
        # 换服务地址等于换了数据接收方，之前的隐私同意自动失效
        url_changed = bool(old.get("llm_consent")) \
            and url != (old.get("llm_url") or "").strip()
        save_llm(url=url, model=model, token=token,
                 consent=False if url_changed else None)
        self._push_llm_config()
        if url_changed:
            self.settings_page.set_llm_hint(
                "已保存。服务地址已变更，首次使用需重新确认隐私说明")
        else:
            self.settings_page.set_llm_hint("已保存")
        log.info("AI 配置已更新 url=%s model=%s token=%s",
                 bool(url), model or "-", "set" if token else "empty")

    def _on_llm_test_requested(self, url: str, model: str, token: str):
        """用当前输入（未保存也可）发一次极短请求测试连通性。"""
        from core.ai_client import AIClient
        self.settings_page.set_llm_test_busy(True)

        def _proc():
            client = AIClient(url, token, model, timeout=(10, 60))
            try:
                return client.test()
            finally:
                client.close()

        def _done(reply):
            self.settings_page.set_llm_test_busy(False)
            text = " ".join((reply or "").split())[:60]
            self.settings_page.set_llm_hint(
                f"连接正常：{text}" if text else "连接正常")

        def _fail(err):
            self.settings_page.set_llm_test_busy(False)
            self.settings_page.set_llm_hint(f"连接失败：{err}", error=True)

        run_async(_proc, on_done=_done, on_fail=_fail, parent=self)

    def _open_settings_ai_page(self):
        self._show_settings_tab()
        self.settings_page.show_ai_page()

    def _ai_client(self):
        """按当前配置创建一次性客户端；未配置时抛 AINotConfigured。"""
        from config import load_config
        from core.ai_client import AIClient
        return AIClient.from_config(load_config())

    def _ensure_ai_ready(self, encrypted: bool = False) -> bool:
        """AI 调用前的统一门禁：启用/配置/首次同意/E2E 二次确认。"""
        from config import load_config, save_llm
        cfg = load_config()
        if not cfg.get("llm_enabled"):
            notify(self, self.theme_manager, "AI 功能未启用",
                   "请先在「设置 → AI」中启用并配置 AI 服务。")
            self._open_settings_ai_page()
            return False
        if not ((cfg.get("llm_url") or "").strip()
                and (cfg.get("llm_model") or "").strip()):
            notify(self, self.theme_manager, "AI 服务未配置",
                   "请先在「设置 → AI」中填写服务地址和模型名称。")
            self._open_settings_ai_page()
            return False
        if not cfg.get("llm_consent"):
            ok = confirm(
                self, self.theme_manager, "使用 AI 功能",
                "使用 AI 功能时，所选聊天文字或输入框内容会发送到配置的"
                " AI 服务用于生成结果；不会上传图片、文件或附件。\n\n"
                "请确认已知悉并同意上述数据处理方式。",
                ok_text="同意并继续")
            if not ok:
                return False
            save_llm(consent=True)
        if encrypted:
            ok = confirm(
                self, self.theme_manager, "端到端加密房间",
                "当前会话开启了端到端加密。使用 AI 功能会把解密后的聊天"
                "明文发送到模型服务器，端到端加密的保密性将不再成立。\n\n"
                "是否仍要继续？",
                danger=True, ok_text="仍要继续")
            if not ok:
                return False
        return True

    def _refresh_ai_entry_visibility(self):
        """AI 开关变化后同步消息右键与输入框按钮的显隐。"""
        from config import load_config
        enabled = bool(load_config().get("llm_enabled"))
        self.chat_area.chat_view.set_ai_available(enabled)
        self.chat_area.input.set_ai_available(enabled)
        self.chat_area.set_ai_available(enabled)

    def _ai_job(self, messages: list):
        """构造 AIDialog 任务：重试时整条链路（客户端/请求）重新创建。"""
        def _job(worker):
            client = self._ai_client()
            try:
                worker.emit_status("正在请求模型…")
                return client.chat(messages, cancel=worker.cancel_event,
                                   on_delta=worker.emit_delta)
            finally:
                client.close()
        return _job

    def _ai_username(self) -> str:
        me = self.client.me if self.client else None
        return (me.username or "") if me else ""

    def _on_ai_message_action(self, message, action):
        """单条消息 AI：翻译/解释/待办/纠错/起草回复，结果可复制或插入。"""
        try:
            if message is None or not message:
                return
            encrypted = bool(self.chat_area.chat_view._room_encrypted)
            if not self._ensure_ai_ready(encrypted=encrypted):
                return
            text = (message.text or "").strip()
            if not text:
                notify(self, self.theme_manager, "无法处理",
                       "这条消息没有可发送给 AI 的文字内容。")
                return
            from core.ai_context import message_action_prompt
            messages = message_action_prompt(
                action, text, self._ai_username())
            titles = {"translate": "AI 翻译", "explain": "AI 解释",
                      "todos": "AI 提取待办", "proofread": "AI 校对纠错",
                      "reply": "AI 起草回复"}
            sender = (message.sender.display_name
                      if message.sender else "未知用户")
            log.info("AI 消息操作 action=%s room=%s", action,
                     message.room_id or "-")
            result = run_ai_dialog(
                self, self.theme_manager, titles.get(action, "AI 助手"),
                lambda: self._ai_job(messages),
                context_note=f"来源：{sender} 的一条消息；内容仅用于本次请求。")
            if result:
                self.chat_area.input.append_text(result)
                self.statusBar().showMessage(
                    "AI 结果已插入输入框，请确认后发送", 4000)
        except Exception:  # noqa: BLE001 —— 槽内异常会终止进程
            log.exception("单条消息 AI 操作失败")

    def _on_ai_assist(self, action):
        """输入框写作助手：润色/改写/精简替换原文，起草回复追加到光标。"""
        try:
            if action == "reply_draft":
                self._ai_draft_reply()
                return
            encrypted = bool(self.chat_area.chat_view._room_encrypted)
            if not self._ensure_ai_ready(encrypted=encrypted):
                return
            input_widget = self.chat_area.input
            if input_widget.has_media_content():
                notify(self, self.theme_manager, "暂不支持",
                       "写作助手目前只处理文字，请先移除输入框中的图片或文件。")
                return
            text = input_widget.input.toPlainText().strip()
            if not text:
                notify(self, self.theme_manager, "输入为空",
                       "请先在输入框中输入内容，再使用写作助手。")
                return
            from core.ai_context import writing_assist_prompt
            messages = writing_assist_prompt(action, text)
            titles = {"polish": "AI 润色", "rewrite": "AI 改写",
                      "shorten": "AI 精简"}
            log.info("AI 写作助手 action=%s chars=%d", action, len(text))
            result = run_ai_dialog(
                self, self.theme_manager, titles.get(action, "AI 写作助手"),
                lambda: self._ai_job(messages),
                context_note="仅发送输入框中的这段文字，用于本次改写。")
            if result:
                input_widget.replace_text(result)
                self.statusBar().showMessage("已回填输入框，请确认后发送", 4000)
        except Exception:  # noqa: BLE001
            log.exception("AI 写作助手失败")

    def _ai_draft_reply(self):
        """根据当前会话最近消息起草回复，结果追加到输入框（不发送）。"""
        rid = self._active_room_id
        client = self.client
        if not rid or client is None:
            notify(self, self.theme_manager, "请先打开会话",
                   "起草回复需要读取当前会话的最近聊天记录。")
            return
        encrypted = bool(self.chat_area.chat_view._room_encrypted)
        if not self._ensure_ai_ready(encrypted=encrypted):
            return
        conv = self._conversations.get(rid)
        room_type = conv.room_type if conv else self._room_type
        username = self._ai_username()

        def _job(worker):
            from core.ai_context import collect_room_context, reply_draft_prompt
            from core.ai_client import AIError
            ctx = collect_room_context(
                client, rid, room_type, limit=80, max_chars=12000,
                cancel=worker.cancel_event, on_status=worker.emit_status)
            if not ctx.messages:
                raise AIError("当前会话没有可用于起草回复的聊天记录")
            messages = reply_draft_prompt(ctx.text, username)
            ai = self._ai_client()
            try:
                worker.emit_status("正在请求模型…")
                return ai.chat(messages, cancel=worker.cancel_event,
                               on_delta=worker.emit_delta)
            finally:
                ai.close()

        log.info("AI 起草回复 room=%s", rid)
        # run_ai_dialog 收的是 job_factory（零参 → job(worker)），重试要靠它
        # 重新采集上下文；直接传 _job 会被当成工厂调用，报缺 worker 参数
        result = run_ai_dialog(
            self, self.theme_manager, "AI 起草回复", lambda: _job,
            context_note="将读取当前会话最近约 80 条消息，用于起草本次回复。")
        if result:
            self.chat_area.input.append_text(result)
            self.statusBar().showMessage("已回填输入框，请确认后发送", 4000)

    # ------------------------------------------------------------------ #
    # 离线文字识别（OCR）
    # ------------------------------------------------------------------ #
    def _warmup_ocr(self):
        """登录后定时触发的引擎预热：失败静默，首次识别时再走既有报错路径。"""
        try:
            from core import ocr
            # 局部导入：让测试能按 ui.api_workers.run_async 拦截，不至于
            # 在单测里真去加载 ONNX 模型
            from .api_workers import run_async
            if self.client is None or not ocr.is_available():
                return
            run_async(ocr.warmup,
                      on_done=lambda ok: log.info("OCR 引擎预热%s",
                                                  "完成" if ok else "跳过"),
                      on_fail=lambda err: log.info("OCR 引擎预热失败：%s", err),
                      parent=self)
        except Exception:  # noqa: BLE001 —— 槽内异常会终止进程
            log.exception("OCR 预热调度失败")

    @staticmethod
    def _viewer_on_top() -> bool:
        """图片查看器是否开着。它是 WindowStaysOnTopHint 全屏窗，普通
        模态弹窗会被压在其后不可见（真机实测「提取文字」后结果窗消失），
        识别流程里所有弹窗都要据此置顶。"""
        from .image_viewer import ImageViewer
        return any(isinstance(w, ImageViewer) and w.isVisible()
                   for w in QApplication.topLevelWidgets())

    @staticmethod
    def _clear_viewer_ocr_hint():
        """识别流程收尾（结果窗已弹/已失败）后撤掉查看器的「正在识别」
        提示——弹窗盖在查看器上面，底下这句不撤会一直挂着。"""
        from .image_viewer import ImageViewer
        for w in QApplication.topLevelWidgets():
            if isinstance(w, ImageViewer) and w.isVisible():
                w.clear_ocr_hint()

    def _on_ocr_requested(self, url: str):
        """识别图片里的文字：先拿到本机文件路径，再后台推理，结果弹窗。

        全程在本机（core.ocr 不联网），所以没有 AI 那样的隐私门禁；
        取图可能触发下载（磁盘缓存没命中时），所以是两段异步。
        """
        try:
            from core import ocr
            if self._ocr_task is not None:
                self.statusBar().showMessage("上一条识别还没结束，请稍候…", 3000)
                return
            if not ocr.is_available():
                notify(self, self.theme_manager, "缺少识别组件",
                       "离线文字识别需要可选依赖 rapidocr-onnxruntime"
                       "（该包只支持到 Python 3.12）：\n"
                       "pip install -r requirements-ocr.txt",
                       stay_on_top=self._viewer_on_top())
                return
            # 新一轮开始：旧回调按 token 判迟到，切房/重复点击都不会串台
            self._ocr_token += 1
            token = self._ocr_token
            # 模型首次加载要 1~2 秒，只有这时才提示「稍慢」；预热（登录后
            # 15s）或前一次识别已把引擎建好时不该再吓用户
            hint = ("正在本机识别文字…" if ocr.is_loaded()
                    else "正在本机识别文字（首次加载模型稍慢）…")
            self.statusBar().showMessage(hint, 8000)

            def _ready(path):
                self._run_ocr(path, token)

            def _fail(err):
                if self._ocr_token != token:
                    return
                self._ocr_task = None
                self._clear_viewer_ocr_hint()
                notify(self, self.theme_manager, "取图失败",
                       str(err or "图片获取失败，请稍后再试"),
                       stay_on_top=self._viewer_on_top())

            self.chat_area.chat_view.local_image_path(url, _ready, _fail)
        except Exception:  # noqa: BLE001 —— 槽内异常会终止进程
            log.exception("离线文字识别启动失败")

    def _run_ocr(self, path: str, token: int):
        from core import ocr
        from .api_workers import run_async

        def _done(result):
            # 先判 token 再清引用：迟到的旧回调不能把新一轮的任务标记抹掉
            if self._ocr_token != token:
                return
            self._ocr_task = None
            self._clear_viewer_ocr_hint()
            try:
                text = (result.text if result is not None else "").strip()
                if not text:
                    notify(self, self.theme_manager, "没识别到文字",
                           "这张图片里没有可提取的文字"
                           "（图片太小或太糊也会这样）。",
                           stay_on_top=self._viewer_on_top())
                    return
                from .ocr_dialog import show_ocr_result
                inserted = show_ocr_result(self, self.theme_manager, text,
                                           stay_on_top=self._viewer_on_top())
                if inserted:
                    self.chat_area.input.append_text(inserted)
                    self.statusBar().showMessage(
                        "识别文字已插入输入框，请确认后发送", 4000)
            except Exception:  # noqa: BLE001
                log.exception("识别结果处理异常")

        def _fail(err):
            if self._ocr_token != token:
                return
            self._ocr_task = None
            self._clear_viewer_ocr_hint()
            notify(self, self.theme_manager, "识别失败",
                   str(err or "识别过程出错"),
                   stay_on_top=self._viewer_on_top())

        self._ocr_task = run_async(lambda: ocr.recognize(path),
                                   _done, _fail, parent=self)

    # ------------------------------------------------------------------ #
    # AI 划重点面板
    # ------------------------------------------------------------------ #
    def _cancel_ai_panel_task(self):
        task = self._ai_panel_task
        self._ai_panel_task = None
        if task is not None:
            try:
                task.cancel()
            except RuntimeError:
                pass

    def _cancel_ai_task_if_room_changed(self, rid: str):
        """AI 划重点任务绑定发起时的房间：切走后结果无处安放，取消之。

        关面板不取消（用户要求后台继续，见 _on_panel_closed）；这里
        token 未递增，worker 自己的 cancelled 回调会照常把面板落定。
        """
        if (self._ai_panel_task is not None
                and self._ai_panel_rid not in (None, rid)):
            self._cancel_ai_panel_task()

    def _on_ai_panel_generate(self, kind: str, limit: int):
        """面板请求生成：门禁 → 后台采集+模型 → 增量回填面板。"""
        panel = self._room_panels.panel("ai")
        try:
            rid = self._active_room_id
            client = self.client
            if not rid or client is None:
                return
            encrypted = bool(self.chat_area.chat_view._room_encrypted)
            if not self._ensure_ai_ready(encrypted=encrypted):
                return
            # 新一轮开始：旧的迟到信号靠 token 失效丢弃
            self._cancel_ai_panel_task()
            self._ai_panel_token += 1
            token = self._ai_panel_token
            panel.begin_generating(limit)

            conv = self._conversations.get(rid)
            room_type = conv.room_type if conv else self._room_type
            username = self._ai_username()
            from core.ai_context import start_of_day, period_start
            since_ts = 0.0
            period = ""
            if kind == "daily":
                since_ts = start_of_day()
                period = "daily"
            elif kind == "weekly":
                since_ts = period_start("weekly")
                period = "weekly"

            def _job(worker):
                from core.ai_context import (
                    collect_room_context, report_prompt, summary_prompt)
                from core.ai_client import AIError
                ctx = collect_room_context(
                    client, rid, room_type, limit=limit, since_ts=since_ts,
                    cancel=worker.cancel_event, on_status=worker.emit_status)
                if not ctx.messages:
                    raise AIError("所选范围内没有可发送给模型的文本消息")
                if period:
                    messages = report_prompt(ctx, period, username)
                else:
                    messages = summary_prompt(ctx, username)
                ai = self._ai_client()
                try:
                    worker.emit_status("正在生成…")
                    text = ai.chat(messages, cancel=worker.cancel_event,
                                   on_delta=worker.emit_delta)
                    return {"text": text, "coverage": ctx.coverage}
                finally:
                    ai.close()

            log.info("AI 划重点 kind=%s limit=%s room=%s", kind, limit, rid)
            self._ai_panel_rid = rid
            from .ai_worker import AITask
            task = AITask(_job, parent=self)
            self._ai_panel_task = task
            panel.set_task_running(True)
            task.delta.connect(
                lambda text: panel.append_delta(text)
                if token == self._ai_panel_token else None)
            task.status.connect(
                lambda text: panel.append_status(text)
                if token == self._ai_panel_token else None)
            task.done.connect(
                lambda result: self._on_ai_panel_done(result, token))
            task.failed.connect(
                lambda err: self._on_ai_panel_failed(err, token))
            task.cancelled.connect(
                lambda: self._on_ai_panel_cancelled(token))
        except Exception as e:  # noqa: BLE001
            log.exception("AI 划重点启动失败")
            panel.fail(str(e) or "启动失败")

    def _on_ai_panel_done(self, result, token: int):
        if token != self._ai_panel_token:
            return
        self._ai_panel_task = None
        self._ai_panel_rid = None
        text, coverage = "", ""
        if isinstance(result, dict):
            text = str(result.get("text") or "")
            coverage = str(result.get("coverage") or "")
        elif result is not None:
            text = str(result)
        panel = self._room_panels.panel("ai")
        panel.set_task_running(False)
        panel.finish(text, coverage)

    def _on_ai_panel_failed(self, err, token: int):
        if token != self._ai_panel_token:
            return
        self._ai_panel_task = None
        self._ai_panel_rid = None
        panel = self._room_panels.panel("ai")
        panel.set_task_running(False)
        panel.fail(err)

    def _on_ai_panel_cancelled(self, token: int):
        if token != self._ai_panel_token:
            return
        self._ai_panel_task = None
        self._ai_panel_rid = None
        panel = self._room_panels.panel("ai")
        panel.set_task_running(False)
        panel.cancelled()

    def _on_ai_panel_stop(self):
        task = self._ai_panel_task
        if task is not None:
            task.cancel()

    def _on_ai_panel_save(self, text: str):
        """AI 结果另存为 Markdown/文本（UTF-8）。"""
        text = text or ""
        if not text.strip():
            return
        conv = self._conversations.get(self._active_room_id or "")
        base = ((conv.name if conv else "") or "AI").strip()
        base = "".join(ch for ch in base if ch not in '\\/:*?"<>|').strip() or "AI"
        default = os.path.join(
            os.path.expanduser("~"),
            f"{base}_AI_{time.strftime('%Y%m%d_%H%M')}.md")
        path, _sel = QFileDialog.getSaveFileName(
            self, "保存 AI 结果", default,
            "Markdown (*.md);;文本文件 (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            notify(self, self.theme_manager, "保存失败", str(e))
            return
        self.statusBar().showMessage(f"已保存：{path}", 5000)

    # ------------------------------------------------------------------ #
    # AI 自然语言历史搜索
    # ------------------------------------------------------------------ #
    def _cancel_ai_search_task(self):
        task = self._ai_search_task
        self._ai_search_task = None
        if task is not None:
            try:
                task.cancel()
            except RuntimeError:
                pass

    def _on_ai_search_requested(self, query: str):
        """只把查询词发给模型扩写成关键词；聊天正文留在本机扫描。"""
        panel = self._room_panels.messages
        try:
            query = (query or "").strip()
            if not query:
                panel.cancel_ai_search()
                return
            if not self._active_room_id or self.client is None:
                panel.cancel_ai_search()
                return
            encrypted = bool(self.chat_area.chat_view._room_encrypted)
            if not self._ensure_ai_ready(encrypted=encrypted):
                panel.cancel_ai_search()
                return
            self._cancel_ai_search_task()
            self._ai_search_token += 1
            token = self._ai_search_token
            panel.set_ai_search_busy(True)

            def _job(worker):
                from core.ai_context import search_keywords_prompt
                ai = self._ai_client()
                try:
                    worker.emit_status("正在理解查询…")
                    return ai.chat(search_keywords_prompt(query),
                                   temperature=0.2, max_tokens=200,
                                   cancel=worker.cancel_event)
                finally:
                    ai.close()

            log.info("AI 搜索查询 chars=%d room=%s",
                     len(query), self._active_room_id)
            from .ai_worker import AITask
            task = AITask(_job, parent=self)
            self._ai_search_task = task
            task.done.connect(
                lambda reply: self._on_ai_search_done(reply, query, token))
            task.failed.connect(
                lambda err: self._on_ai_search_failed(err, token))
            task.cancelled.connect(
                lambda: self._on_ai_search_cancelled(token))
        except Exception as e:  # noqa: BLE001
            log.exception("AI 搜索启动失败")
            panel.fail_ai_search(str(e) or "启动失败")

    def _on_ai_search_done(self, reply, query: str, token: int):
        if token != self._ai_search_token:
            return
        self._ai_search_task = None
        from core.ai_context import parse_search_keywords
        keywords = parse_search_keywords(str(reply or ""), fallback=query)
        self._room_panels.messages.apply_ai_keywords(keywords)

    def _on_ai_search_failed(self, err, token: int):
        if token != self._ai_search_token:
            return
        self._ai_search_task = None
        self._room_panels.messages.fail_ai_search(err)

    def _on_ai_search_cancelled(self, token: int):
        if token != self._ai_search_token:
            return
        self._ai_search_task = None
        self._room_panels.messages.cancel_ai_search()

    def _on_upload_service_edited(self, *_):
        """图片上传服务配置编辑完成：落盘保存，并立即应用到当前会话的客户端。

        客户端（REST/图文混排）直接读 client.upload_service_url / _token，所以
        存盘后即时改写 client 两个字段让配置当场生效，避免"要重新登录才生效"。
        开关关闭时地址/Token 仅存盘不生效（client 保持空走服务器默认上传）。
        """
        from config import save_upload_service
        url = self.settings_page.upload_service_input.text().strip()
        token = self.settings_page.upload_service_token_input.text().strip()
        enabled = self.settings_page.upload_custom_switch.isChecked()
        save_upload_service(url, token, enabled=enabled)
        self._apply_upload_service_to_client(
            url if enabled else "", token if enabled else "")
        self.statusBar().showMessage("上传服务已保存，立即生效", 4000)

    def _on_upload_service_toggled(self, enabled: bool):
        """「使用自定义服务器」开关：关闭后图片回落到聊天服务器默认上传，
        地址/Token 配置保留，重新开启即恢复。"""
        from config import save_upload_service
        url = self.settings_page.upload_service_input.text().strip()
        token = self.settings_page.upload_service_token_input.text().strip()
        save_upload_service(url, token, enabled=enabled)
        self._apply_upload_service_to_client(
            url if enabled else "", token if enabled else "")
        self.statusBar().showMessage(
            "图片将经自定义上传服务上传" if enabled
            else "图片将上传到聊天服务器（自定义服务器已关闭）", 4000)

    def _apply_upload_service_to_client(self, url: str, token: str):
        if self.client:
            self.client.upload_service_url = url
            self.client.upload_service_token = token

    def _on_update_auto_check_toggled(self, enabled: bool):
        from config import save_update_auto_check
        save_update_auto_check(enabled)
        self.statusBar().showMessage(
            "启动时自动检查更新已开启" if enabled
            else "启动时自动检查更新已关闭", 4000)

    def _on_notify_toggled(self, enabled: bool):
        """通知总开关：即时落盘，只影响提醒不影响未读。"""
        from config import save_notify
        self._notify_enabled = bool(enabled)
        save_notify(enabled=enabled)
        self.statusBar().showMessage(
            "消息提醒已开启" if enabled else "消息提醒已关闭（未读仍会累计）", 4000)

    def _on_notify_mentions_toggled(self, enabled: bool):
        """仅 @我/私聊提醒开关：即时落盘。"""
        from config import save_notify
        self._notify_mentions_only = bool(enabled)
        save_notify(mentions_only=enabled)
        self.statusBar().showMessage(
            "仅 @我 或私聊时提醒已开启" if enabled
            else "所有消息都会提醒", 4000)

    def _on_send_mode_changed(self, mode: str):
        """发送方式变化：落盘并应用到输入框。"""
        from config import save_send_mode
        save_send_mode(mode)
        self._apply_send_mode(mode)
        self.statusBar().showMessage(
            "回车发送" if mode == "enter" else "Ctrl+回车发送", 4000)

    def _apply_send_mode(self, mode: str):
        """把发送方式应用到聊天输入框（动态切换快捷键）。"""
        inp = getattr(self.chat_area, "input", None)
        if inp is not None and hasattr(inp, "set_send_mode"):
            inp.set_send_mode(mode or "enter")

    def _check_for_update(self, manual: bool = False):
        """后台拉取更新清单；有新版本询问下载，静默安装后退出。

        自动检查仅打包版（sys.frozen）且开关开启时执行；手动检查
        （关于页按钮）不受这两个条件限制。任何网络/清单异常都降级为
        状态栏提示或日志，不中断主流程。
        """
        import sys as _sys
        from config import load_config
        from core import updater
        from core.version import APP_VERSION

        if self._update_busy:
            return
        if not manual:
            if not getattr(_sys, "frozen", False):
                return
            if not load_config().get("update_auto_check", True):
                return
        cfg = updater.load_update_config()
        if not cfg.get("base_url"):
            if manual:
                self.statusBar().showMessage("未配置更新服务器", 4000)
            return
        self._update_busy = True

        def _proc():
            return updater.fetch_manifest(cfg["base_url"], cfg["manifest"])

        def _done(manifest):
            self._update_busy = False
            self._on_manifest(manifest, cfg, manual)

        def _fail(err):
            self._update_busy = False
            log.info("检查更新失败：%s", err)
            if manual:
                self.statusBar().showMessage(f"检查更新失败：{err}", 6000)

        run_async(_proc, on_done=_done, on_fail=_fail, parent=self)

    def _on_manifest(self, manifest: dict, cfg: dict, manual: bool):
        from core import updater
        from core.version import APP_VERSION

        remote = str(manifest.get("version", ""))
        if not updater.is_new_version(remote, APP_VERSION):
            if manual:
                self.statusBar().showMessage("当前已是最新版本", 4000)
            return
        text = (f"检测到新版本 V{remote}（当前 V{APP_VERSION}）")
        notes = manifest.get("notes")
        if isinstance(notes, list) and notes:
            lines = [str(n).strip() for n in notes if str(n).strip()]
            if lines:
                text += "\n\n更新内容：\n" + "\n".join(f"· {ln}" for ln in lines)
        text += "\n\n是否下载并安装更新？"
        if not confirm(self, self.theme_manager, "发现新版本", text):
            return
        url = str(manifest.get("url", ""))
        if not url.lower().startswith(("http://", "https://")):
            url = cfg["base_url"].rstrip("/") + "/" + url.lstrip("/")
        # 函数内导入：便于测试对弹窗打补丁
        from .updater_dialog import UpdateProgressDialog
        dlg = UpdateProgressDialog(url, self.theme_manager, self)
        dlg.exec_()
        if dlg.result() != UpdateProgressDialog.Accepted or not dlg.file_path:
            if dlg.error and not dlg._user_cancelled:
                self.statusBar().showMessage(f"更新下载失败：{dlg.error}", 8000)
            return
        if not updater.verify_sha256(dlg.file_path,
                                     str(manifest.get("sha256", ""))):
            try:
                os.remove(dlg.file_path)
            except OSError:
                pass
            self.statusBar().showMessage("安装包校验失败，已放弃本次更新", 8000)
            return
        self._launch_installer(dlg.file_path)

    def _launch_installer(self, path: str):
        """启动安装器（/SILENT 有进度界面）并退出当前进程，装完自动重启。

        本进程必须先退出（运行中的 exe 被锁，安装器无法覆盖）；重启不能
        指望 Inno 的 /RESTARTAPPLICATIONS（只对注册了 Restart Manager 的
        程序生效），所以用 cmd 批处理串"等本进程退出 → 启动安装器 → 再
        拉起本程序"。批处理必须轮询 PID 确认本进程真正退出后才动安装器：
        提前启动会让复制阶段撞上未解锁的 fchat.exe，留下"旧版已卸载、
        新版没装上"的半卸载状态（1.0.2 真机踩坑）。
        """
        import sys
        from PyQt5.QtCore import QProcess
        from PyQt5.QtWidgets import QApplication
        args = ["/SILENT", "/SUPPRESSMSGBOXES", "/CLOSEAPPLICATIONS"]
        if not getattr(sys, "frozen", False):
            # 开发态没有真实安装目录，重启批处理无从谈起，只起安装器
            ok = QProcess.startDetached(path, args)
            if not ok:
                self.statusBar().showMessage(
                    "安装程序启动失败，请手动运行：\n" + path, 10000)
            return
        app_exe = os.path.abspath(sys.executable)
        bat = os.path.join(os.path.dirname(path), "restart_update.cmd")
        log_path = os.path.join(os.path.dirname(path), "update_install.log")
        # cmd.exe 按 ANSI 读批处理，编码用 mbcs 才能容忍中文路径；
        # tasklist 输出经 find 判断 PID 是否还在，INFO 行不含数字所以
        # 无任务时 find 必失败（errorlevel 1）跳出循环
        with open(bat, "w", encoding="mbcs") as f:
            f.write('@echo off\r\n'
                    ':waitpid\r\n'
                    f'tasklist /FI "PID eq {os.getpid()}" '
                    f'| find "{os.getpid()}" >nul 2>&1\r\n'
                    'if not errorlevel 1 (\r\n'
                    '  ping -n 2 127.0.0.1 >nul\r\n'
                    '  goto waitpid\r\n'
                    ')\r\n'
                    f'"{path}" {" ".join(args)} /LOG="{log_path}"\r\n'
                    f'start "" "{app_exe}"\r\n'
                    'del "%~f0"\r\n')
        # 工作目录指到安装包所在目录：cmd 默认继承本进程 CWD（打包态=安装
        # 目录），其常驻句柄会让安装器的目录改名占用探测必然失败
        ok, _pid = QProcess.startDetached(
            "cmd.exe", ["/c", bat], os.path.dirname(path))
        if not ok:
            self.statusBar().showMessage("安装程序启动失败，请手动运行：\n" + path,
                                         10000)
            return
        self.statusBar().showMessage("正在安装更新，程序即将退出…", 3000)
        # 给 detached 进程留出启动时间再退出，避免安装器立即抢锁
        QTimer.singleShot(800, lambda: QApplication.instance().quit())

    def _on_server_saved(self, raw: str):
        """通用页保存服务器地址：归一化后落盘，并询问是否重启应用生效。"""
        from config import load_config, save_server_url
        from .login_dialog import normalize_server
        text = (raw or "").strip()
        if not text:
            self.statusBar().showMessage("服务器地址不能为空", 4000)
            return
        new_server = normalize_server(text)
        old_server = load_config().get("server_url", "")
        if new_server == old_server:
            self.statusBar().showMessage("服务器地址未变化，无需重启", 3000)
            return
        save_server_url(new_server)
        if confirm(self, self.theme_manager, "重启应用",
                   f"服务器地址已修改为：\n{new_server}\n\n"
                   f"需要重启应用后才能生效，是否立即重启？"):
            self._restart_app()

    def _restart_app(self):
        """重启应用：重新拉起一个新进程并退出当前进程。"""
        import os
        import sys
        import subprocess
        from PyQt5.QtWidgets import QApplication
        from config import ROOT
        script = os.path.join(ROOT, "main.py")
        subprocess.Popen([sys.executable, script])
        QApplication.instance().quit()

    def _on_change_avatar(self):
        """设置页点击头像：选图上传服务器，成功后清缓存并刷新显示。"""
        if not (self.client and getattr(self.client, "connected", False)):
            self.statusBar().showMessage("未登录，无法更换头像", 4000)
            return
        # 与存储页 _storage_busy 同款：忙时在打开文件对话框之前就拦下，
        # 避免上传中再叠一个选图框出来
        if self._avatar_busy:
            self.statusBar().showMessage("头像正在上传，请稍候…", 4000)
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "选择头像图片", "",
            "图片 (*.png *.jpg *.jpeg *.bmp *.gif)")
        if not path:
            return
        self._avatar_busy = True
        self.statusBar().showMessage("正在上传头像…")
        # 函数内导入：便于测试对 ui.api_workers.run_async 打补丁
        from .api_workers import run_async

        def _proc():
            self.client.set_avatar(path)
            return True

        def _done(_ok):
            # 回调里最先复位防重入标志，任何提前 return 都不能把上传卡死
            self._avatar_busy = False
            # 上传期间可能已退出登录，client 已置 None，不能再取 me
            if not self.client:
                return
            self.statusBar().showMessage("头像已更新", 4000)
            # purge 只清缓存不刷新控件，重新 load 才会从服务器拉新头像
            self.avatar_loader.purge(self._avatar_url)
            me = self.client.me
            self.settings_page.show_account(
                me.username, me.name or "", self._avatar_url,
                getattr(me, "department", ""))
            # show_account 先设置 _pending_avatar_url，再注入加载器触发重载
            self.settings_page.set_avatar_loader(self.avatar_loader)

        def _fail(err):
            self._avatar_busy = False
            self.statusBar().showMessage(f"头像上传失败：{err}", 6000)

        run_async(_proc, on_done=_done, on_fail=_fail, parent=self)

    # ------------------------------------------------------------------ #
    # 系统托盘
    # ------------------------------------------------------------------ #
    def _tray_available(self) -> bool:
        """可覆盖的探测点：offscreen / 无托盘桌面返回 False。"""
        try:
            return bool(QSystemTrayIcon.isSystemTrayAvailable())
        except Exception:  # noqa: BLE001
            return False

    def _create_tray_controller(self):
        """可覆盖的构造点（测试注入 fake tray/card/menu）。"""
        return TrayController(self.theme_manager, self)

    def _setup_tray(self):
        if not self._tray_available():
            log.info("系统托盘不可用，关闭按钮保持直接退出")
            return
        try:
            tray = self._create_tray_controller()
        except Exception:  # noqa: BLE001
            # 托盘失败不能影响主功能，退回旧版退出行为
            log.exception("系统托盘初始化失败，关闭按钮保持直接退出")
            return
        self._tray = tray
        app = QApplication.instance()
        if app is not None:
            # closeEvent 会把窗口 hide 掉；不关这个开关，隐藏最后一个
            # 窗口会直接退出进程
            app.setQuitOnLastWindowClosed(False)
        tray.show_main_requested.connect(self._on_tray_open_main)
        tray.open_settings_requested.connect(self._on_tray_open_settings)
        tray.quit_requested.connect(self._quit_app)
        tray.message_activated.connect(self._on_tray_message_activated)
        tray.reminders_requested.connect(self._show_reminders_dialog)
        tray.set_theme(self.theme_manager)
        tray.update_state(False, 0, [], 0)
        tray.show()

    def _restore_from_tray(self):
        """从托盘恢复主窗口（最小化则还原，隐藏则 show）。"""
        tray = self._tray
        if tray is not None:
            tray.stop_alert()
            tray.hide_card()
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def _on_tray_open_main(self):
        self._restore_from_tray()
        self._show_chat_tab()

    def bring_to_front(self):
        """第二个实例启动时调用：把已有窗口（含模态弹窗）唤到前台。"""
        modal = QApplication.activeModalWidget()
        if modal is not None and modal.isVisible():
            modal.raise_()
            modal.activateWindow()
            return
        self._restore_from_tray()

    def _on_tray_open_settings(self):
        self._restore_from_tray()
        self._show_settings_tab()

    def _quit_app(self):
        """真正的退出入口（托盘菜单/认证失败/放弃登录）。"""
        self._force_quit = True
        self.close()

    def _request_quit(self):
        """托盘存在时 quitOnLastWindowClosed=False，必须显式结束事件循环。"""
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _on_tray_message_activated(self, rid: str, msg_id: str):
        """点击预览卡片某一行：恢复窗口、打开会话并高亮该消息。"""
        self._restore_from_tray()
        self._show_chat_tab()
        self._drop_tray_alerts(rid)
        conv = self._conversations.get(rid)
        if conv is not None:
            self._open_conversation(conv)
            if msg_id:
                self._flash_when_ready(rid, msg_id)
        else:
            # 新会话（实时提醒先于列表到达）：重拉列表后再开，定位由
            # _reload_and_open 在真正打开房间后调度
            self._reload_and_open(rid, flash_msg_id=msg_id)
        self._refresh_tray_state()

    def _flash_when_ready(self, rid: str, msg_id: str, attempt: int = 0):
        """等消息模型就绪后高亮；必要时重拉历史定位。"""
        if rid != self._active_room_id or not msg_id:
            return
        view = self.chat_area.chat_view
        if view.model.find_message_row(msg_id) >= 0:
            view.flash_row(msg_id)
            return
        if attempt < 3:
            QTimer.singleShot(
                120, lambda: self._flash_when_ready(rid, msg_id, attempt + 1))
        else:
            self.load_around(msg_id, rid)

    # ---------------- 未读提醒 ---------------- #
    def _add_tray_alert(self, message: Message, room_name: str) -> bool:
        if self._tray is None or message is None:
            return False
        if message.is_mine or message.msg_type == "system":
            return False
        if message.room_id == self._active_room_id \
                and self._is_chat_foreground():
            return False
        conv = self._conversations.get(message.room_id or "")
        if conv is not None and conv.muted:
            # 免打扰只压提醒：未读数照常在 _on_realtime_message 累加
            return False
        # 通知设置：总开关关 → 不提醒；仅 @我/私聊 → 非私聊且非 @我 不提醒。
        # 与免打扰一致，只压提醒，未读计数照旧。
        if not getattr(self, "_notify_enabled", True):
            return False
        if getattr(self, "_notify_mentions_only", False):
            is_mention = bool(getattr(message, "mentions_me", False)
                              or getattr(message, "mention_all", False))
            is_dm = bool(conv is not None and conv.room_type == "d")
            if not is_mention and not is_dm:
                return False
        text = self._preview_from_message(message)
        if not (text or "").strip():
            return False
        msg_id = message.id or ""
        if msg_id:
            for alert in self._tray_alerts:
                if alert.get("msg_id") == msg_id:
                    return False
        sender = ""
        if message.sender is not None:
            sender = getattr(message.sender, "display_name", "") or ""
        self._tray_alerts.append({
            "rid": message.room_id or "",
            "msg_id": msg_id,
            "room": room_name or "",
            "sender": sender,
            "text": text,
            "ts": message.timestamp or time.time(),
        })
        # Windows toast：主窗口不在前台时由系统通知中心展示，点击回跳。
        # fake tray / 非 Windows 无该方法时静默跳过
        toast = getattr(self._tray, "show_message_toast", None)
        if toast:
            prefix = "[@你] " if (
                getattr(message, "mentions_me", False)
                or getattr(message, "mention_all", False)) else ""
            toast(room_name or "新会话",
                  prefix + f"{sender}：{text}" if sender else prefix + text,
                  message.room_id or "", msg_id)
        return True

    def _is_main_active(self) -> bool:
        return (self.isVisible() and not self.isMinimized()
                and self.isActiveWindow())

    def _is_chat_foreground(self) -> bool:
        return self._is_main_active() and self.content_stack.currentIndex() == 0

    def _refresh_tray_state(self, flash: bool = False):
        tray = self._tray
        if tray is None:
            return
        # 窗口在设置页或托盘里时，当前会话也照常出提醒；只有真正停在
        # 聊天页看着该会话时才过滤
        active = self._active_room_id if self._is_chat_foreground() else ""
        try:
            rows, overflow = build_tray_rows(
                self._tray_alerts, list(self._conversations.values()), active)
        except Exception:  # noqa: BLE001
            log.exception("构建托盘未读预览失败")
            return
        total = sum(max(1, row.count) for row in rows) + overflow
        has_unread = total > 0
        tray.update_state(has_unread, total, rows, overflow)
        if flash and has_unread and not self._is_main_active():
            tray.start_alert()

    def _drop_tray_alerts(self, rid: str):
        if not rid or not self._tray_alerts:
            return
        self._tray_alerts = deque(
            (a for a in self._tray_alerts if a.get("rid") != rid),
            maxlen=self._tray_alerts.maxlen or 50)

    def _clear_tray_alerts(self):
        self._tray_alerts.clear()

    # ------------------------------------------------------------------ #
    def _try_auto_login(self):
        """开了自动登录则用已存凭据后台登录，失败回落到登录对话框。"""
        from config import load_config
        cfg = load_config()
        if not (cfg["auto_login"] and cfg["server_url"] and cfg["username"]
                and cfg["remember_password"] and cfg["password"]):
            self._show_login()
            return
        self.statusBar().showMessage("正在自动登录…")
        # 登录还在后台跑，先把上次会话快照渲染出来（微信式首屏）。
        # 此时没有登录态、头像加载器不会发请求（显示占位），登录成功后
        # _load_conversations 会拿同一份快照重渲染并补载头像
        from core.conv_cache import load_conversations_snapshot
        server = normalize_server(cfg["server_url"])
        cached = load_conversations_snapshot(server, cfg["username"])
        if cached:
            # 登录前就把磁盘状态缓存预载进 provider（无需 client）：
            # 快照行的状态点随列表一起显示，不等登录+网络往返
            self._status_provider.preload(server, cfg["username"])
            self.conversation_list.set_conversations(cached)
            self._queue_conversation_statuses(cached)
            log.info("登录前先用快照渲染 %d 个会话", len(cached))
        self._login_runner = LoginRunner(self)
        self._login_runner.success.connect(self._on_login_success)
        self._login_runner.failed.connect(self._on_auto_login_failed)
        self._login_runner.start(cfg["server_url"], cfg["username"],
                                 cfg["password"], cfg.get("e2e_password", ""),
                                 upload_service_url=(
                                     cfg.get("upload_service_url", "")
                                     if cfg.get("upload_service_enabled")
                                     else ""),
                                 upload_service_token=(
                                     cfg.get("upload_service_token", "")
                                     if cfg.get("upload_service_enabled")
                                     else ""))

    def _on_auto_login_failed(self, err: str):
        log.warning("自动登录失败，转手工登录：%s", err)
        # 预渲染的快照属于失败的那个账号，不能残留在界面上
        self.conversation_list.clear_conversations()
        self.statusBar().showMessage("自动登录失败，请重新登录", 5000)
        self._show_login(err)

    def _show_login(self, error_text: str = "", standalone: bool = False):
        # standalone：微信式独立登录窗（退出登录后主界面已隐藏），不挂
        # parent 才能在任务栏保有自己的图标，否则主窗隐藏后找不到登录窗
        dialog = LoginDialog(None if standalone else self)
        dialog.login_ok.connect(self._on_login_success)
        if error_text:
            dialog.set_error(error_text)
        dialog.exec_()

    def _on_login_success(self, client: ApiClient):
        # 登出后重新登录是异步的：_show_login 的 exec_ 已返回时 client
        # 还没挂上，登出流程靠这个标记区分「正在登录」与「放弃登录」。
        self._login_pending = True
        self._complete_login(client)

    def _complete_login(self, client: ApiClient):
        self.client = client
        self.setWindowTitle("Fchat")
        # 账号信息在「账号」页展示，通用页只保留可配置的服务器地址
        self.settings_page.set_server_url(client.server_url)
        # 账号卡：显示头像 / 用户名 / 姓名 / 部门
        me = client.me
        # 表情回应要按用户名判断「自己是否点过」，登录后注入；登出复位
        self.chat_area.chat_view.set_my_username(me.username or "")
        avatar_url = me.avatar_url or f"{client.server_url}/avatar/{me.username or ''}"
        self._avatar_url = avatar_url
        # 头像 URL 不随内容变化：磁盘缓存即时显示（load 磁盘优先），
        # 后台 refresh_if_changed 拉远端与本地比对，不一致才更新缓存并刷新。
        # 服务端 /avatar/ 只认 Cookie 认证（X-Auth-Token 头会被降级成字母
        # SVG），重拉前必须先注入登录态
        self.avatar_loader.set_auth(client.user_id, client.auth_token,
                                    client.server_url)
        self.avatar_loader.refresh_if_changed(avatar_url)
        self.settings_page.show_account(
            me.username, me.name or "", avatar_url, getattr(me, "department", ""))
        # show_account 先设置 _pending_avatar_url，再注入加载器触发重载；
        # _setup_ui 时的注入在登录前（还没有 pending URL）是空操作，
        # 漏了这一步账号页头像会永远停在占位图
        self.settings_page.set_avatar_loader(self.avatar_loader)
        # 恢复上次保存的在线状态
        from config import load_config
        self.settings_page.set_status(load_config().get("status", "online"))
        # E2E 密钥初始化走网络，放后台与会话拉取并行；就绪前到达的加密
        # 消息按既有降级显示占位。失败不阻断，仅记日志
        if client.e2e:
            run_async(
                client.e2e.init_keys,
                on_done=lambda ok: log.info(
                    "[计时] E2E 密钥初始化%s",
                    "成功" if ok else "失败（加密房间将显示占位文本）"),
                on_fail=lambda err: log.warning("E2E 密钥初始化失败：%s", err),
                parent=self)
        # 部门信息只用于账号页展示，异步补齐（login 已不再串行查它）
        def _dept_done(department: str):
            if department:
                self.settings_page.set_department(department)

        run_async(lambda: client.fetch_department(me.username or ""),
                  on_done=_dept_done, parent=self)
        # 图片加载器绑定服务器地址（用于附件相对路径）
        self.image_loader.server_url = client.server_url
        # 附件 /file-upload/ 需要认证头，否则图片加载 401 显示空白
        self.image_loader.set_auth(client.user_id, client.auth_token)
        self.image_loader.ssl_verify = getattr(client, "ssl_verify", True)
        # 面板的取数与头像
        self._room_panels.set_session(self.client, self.avatar_loader)
        # 状态缓存换账号即清
        self._status_provider.set_session(self.client, self)
        # 启动实时
        self._start_realtime()
        self._load_conversations()
        # 已读回执 30s 周期刷新（仅当前会话）
        self._receipts_timer.start()
        self._reminders_timer.start()
        # 已设提醒的消息时间行画表盘标记（重启后提醒仍在，标记要恢复）
        self._sync_remind_map()
        # OCR 引擎后台预热：模型加载要 1~2 秒，趁启动空闲期在后台线程
        # 提前做好，首次点「识别文字」免等待。登录后再定时（此时才确定
        # 真要用程序），延迟错开启动瞬间的网络/渲染高峰
        QTimer.singleShot(15000, self._warmup_ocr)
        # 登录前快照渲染自动选中的会话当时开不了，这里补开
        pending = getattr(self, "_pending_open_conv", None)
        if pending is not None:
            self._pending_open_conv = None
            self._open_conversation(pending)
        self._login_pending = False
        # 登出后重新登录：登录框是独立窗口，它关闭后主窗口若还是隐藏的，
        # 这里恢复；自动登录/启动路径窗口本来可见，不打扰焦点
        if not self.isVisible():
            self.show()
            self.raise_()
            self.activateWindow()
        self._refresh_tray_state()

    # ------------------------------------------------------------------ #
    def _start_realtime(self):
        if not self.client:
            return
        self.realtime = RealtimeBridge(
            self.client.server_url,
            self.client.user_id,
            self.client.auth_token,
            ssl_verify=True,
            e2e_manager=self.client.e2e,
        )
        self.realtime.message_received.connect(self._on_realtime_message)
        self.realtime.status_changed.connect(self._on_realtime_status)
        self.realtime.user_status_changed.connect(self._on_pushed_user_status)
        self.realtime.subscription_changed.connect(self._on_pushed_subscription)
        self.realtime.start()

    def _on_realtime_status(self, state: str):
        # 横幅常驻显示；状态栏短提示保留（状态栏会被下一条消息顶掉，
        # 断线时用户需要一直看得见的入口）
        self.chat_area.set_connection_state(state)
        if state == "connected":
            self.statusBar().showMessage("实时连接已建立", 2000)
        elif state == "ready":
            # 订阅全部就绪：此后新消息才开始实时推送（ready 之前不补发）
            self.statusBar().showMessage("实时消息已就绪", 2000)
        elif state == "disconnected":
            self.statusBar().showMessage("实时连接已断开", 3000)
        elif state == "reconnecting":
            self.statusBar().showMessage("正在重新连接实时服务…", 3000)

    def _on_reconnect_clicked(self):
        """横幅「立即重连」：催 DDP 立刻重连（不重建 client/订阅）。"""
        if self.realtime is None:
            return
        self.chat_area.set_connection_state("reconnecting")
        self.realtime.reconnect_now()

    # ------------------------------------------------------------------ #
    def _load_conversations(self):
        """异步拉取会话列表，避免登录后首屏阻塞。"""
        if not self.client or self._loading_conversations:
            return
        self._loading_conversations = True
        t0 = time.perf_counter()
        log.info("拉取会话列表…")
        # 回调里不能用 self.client：等待期间可能已退出登录置 None
        client = self.client
        # 先渲染上次的会话快照让首屏立即可见；网络结果回来后整体覆盖，
        # 头像走 ImageLoader 缓存不会重复请求
        from core.conv_cache import load_conversations_snapshot
        cached = load_conversations_snapshot(client.server_url,
                                             client.me.username or "")
        if cached:
            for c in cached:
                self._apply_known_latest(c)
            # 快照渲染出的行与 _conversations 保持一致：启动窗口内到达的
            # 实时消息/历史回调才能找到对应会话（否则被当成未知房间丢弃）
            self._conversations = {c.id: c for c in cached}
            self.conversation_list.set_conversations(cached)
            log.info("先用快照渲染 %d 个会话（%.1f ms）",
                     len(cached), (time.perf_counter() - t0) * 1000)
            # 提前按快照房间订阅实时：幂等且未连接时只登记、连接成功后
            # 自动重发（core/realtime），REST 拉取期间消息流就已经在跑
            if self.realtime:
                self.realtime.subscribe_rooms([c.id for c in cached])
            # 快照渲染的同时就排队状态拉取：provider 在 set_session 时已
            # 从磁盘水合，缓存条目在 get() 里同步回调、行立即有点——
            # 不等网络会话列表回来（否则状态点比列表晚整个网络往返，
            # 观感上就是"缓存没生效"）。同一用户与网络轮的请求会经
            # provider 的 pending 表合并，不会重复打接口
            self._queue_conversation_statuses(cached)
            # 顺手预取前几个房间的首屏历史：点开时若已回来直接秒开，
            # 还没回来也不亏（网络返回照样渲染，见 _load_history._done）
            self._prefetch_histories(cached)
            self._refresh_tray_state()

        def _done(convs):
            self._loading_conversations = False
            # 服务端摘要可能滞后于客户端已收到的消息（历史/实时/乐观发送）：
            # 整表落地前先把已知最新尾部盖回去，避免左栏预览被顶回旧值
            for c in convs:
                self._apply_known_latest(c)
            self._conversations = {c.id: c for c in convs}
            self.conversation_list.set_conversations(convs)
            self._refresh_draft_rooms()
            from core.conv_cache import save_conversations_snapshot
            save_conversations_snapshot(client.server_url,
                                        client.me.username or "", convs)
            log.info("会话列表完成：%d 个会话，耗时 %.1f ms",
                     len(convs), (time.perf_counter() - t0) * 1000)
            # 订阅所有会话的实时消息：一次批量，逐房间发会把实时连接拖死
            if self.realtime:
                self.realtime.subscribe_rooms([c.id for c in convs])
            # 异步解析服务器配置的中文显示名（频道 + 私聊）
            self._resolve_display_names(convs)
            # 私聊对方在线状态（会话列表头像角点）；并发由 provider 限制。
            # 多人私聊的 username 是逗号拼接列表，users.info 必然 400，
            # 跳过（这些行本来也不显示单人状态点）
            self._queue_conversation_statuses(convs)
            # 快照为空（换机器/清缓存）时这一轮才是首次预取窗口；已预取
            # /在途的房间经 _prefetch_histories 内部护栏跳过
            self._prefetch_histories(convs)
            # 网络列表是权威的：已不存在的房间不再占托盘提醒
            if self._tray is not None:
                known = {c.id for c in convs}
                self._tray_alerts = deque(
                    (a for a in self._tray_alerts
                     if a.get("rid") in known),
                    maxlen=self._tray_alerts.maxlen or 50)
            self._refresh_tray_state()

        def _fail(err):
            self._loading_conversations = False
            log.error("会话列表失败：%s", err)
            self.statusBar().showMessage(f"加载会话失败：{err}", 5000)

        run_async(self.client.get_conversations, on_done=_done,
                  on_fail=_fail, parent=self)

    def _prefetch_histories(self, convs, limit: int = 5):
        """并行预取列表前几个房间的首屏历史。

        首屏历史原先只在点开房间时才拉（经代理一次往返 1.3s+，冷房间
        表现为点开白等约 3s）；预取后点开走 _open_conversation 的缓存
        重建路径秒开。_load_history 的去重护栏（_history_loading /
        _history_offset）保证重复调用幂等。加密房间跳过：init_keys 未
        就绪时解密失败，占位密文会被写进消息快照永久驻留。
        """
        prefetched = 0
        for conv in convs:
            if prefetched >= limit:
                break
            rid = conv.id
            if (conv.encrypted or rid == self._active_room_id
                    or rid in self._room_messages
                    or rid in self._history_loading
                    or self._history_offset.get(rid, 0) > 0):
                continue
            self._load_history(conv)
            prefetched += 1
        if prefetched:
            log.info("预取房间首屏历史：%d 个", prefetched)

    def _queue_conversation_statuses(self, convs):
        """为一批会话排队私聊对方状态拉取（列表头像角点）。

        provider 有缓存（含磁盘水合条目）时 get() 同步回调、可见行立即
        有点；无缓存的排队等网络回填。同一用户重复排队会经 pending 表
        合并，不重复打接口。多人私聊的 username 是逗号拼接列表，
        users.info 必然 400，跳过（这些行本来也不显示单人状态点）。
        """
        if not self._status_provider:
            return
        queued = hit = 0
        for c in convs:
            if (c.room_type == "d" and c.username
                    and "," not in c.username):
                queued += 1
                # 返回值非 None = 新鲜缓存同步命中（含磁盘水合），
                # 界面此刻就该有点；0 命中则只能等网络回填
                if self._status_provider.get(
                        c.username,
                        lambda info, u=c.username: self._on_status_info(
                            u, info)) is not None:
                    hit += 1
        log.info("批量拉取私聊状态：%d 个会话，排队 %d，缓存命中 %d",
                 len(convs), queued, hit)

    def _on_status_info(self, username: str, info):
        """用户状态拉取回调：刷新会话列表行；当前打开的正是该私聊时
        同步标题栏。拉取失败（info=None）时安排一次延时重试——失败只进
        provider 的 60s 负缓存，过期后没有任何机制会再拉，不重试的话
        该行的状态点会一直缺到用户重新点开会话。"""
        if not username:
            return
        if not info:
            self._retry_status_later(username)
            return
        self._status_attempts.pop(username, None)
        self.conversation_list.apply_status(username, info)
        conv = self._conversations.get(self._active_room_id)
        if (conv is not None and conv.room_type == "d"
                and conv.username == username):
            self.chat_area.set_dm_status(info.get("status_key", "offline"),
                                         info.get("status_text", ""))

    _STATUS_RETRY_DELAY_MS = 65_000   # 略大于 provider 负缓存 60s
    _STATUS_RETRY_MAX = 3

    def _retry_status_later(self, username: str):
        n = self._status_attempts.get(username, 0) + 1
        self._status_attempts[username] = n
        if n > self._STATUS_RETRY_MAX:
            log.warning("用户状态重试 %d 次仍失败，放弃：%s",
                        self._STATUS_RETRY_MAX, username)
            return
        # PyQt5 的 singleShot 无 (ms, QObject, fn) 上下文重载：用挂在
        # 主窗口下的单发定时器，窗口销毁时定时器一并销毁
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(
            lambda u=username: self._status_provider.get(
                u, lambda info, un=u: self._on_status_info(un, info)))
        timer.start(self._STATUS_RETRY_DELAY_MS)

    # ------------------------------------------------------------------ #
    # 系统消息姓名补齐：au/ru 的被操作人等本地拿不到真实姓名，经
    # UserStatusProvider（users.info）异步解析后按模板重排文本
    # ------------------------------------------------------------------ #
    def _resolve_system_names(self, msgs):
        if not self._status_provider:
            return
        for m in msgs:
            if not getattr(m, "system_names", None) or not m.system_template:
                continue
            for ph, uname in list(m.system_names.items()):
                self._status_provider.get(
                    uname,
                    lambda info, msg=m, ph_=ph, un=uname:
                        self._on_system_name_resolved(msg, ph_, un, info))

    def _on_system_name_resolved(self, msg: Message, placeholder: str,
                                 username: str, info: Optional[dict]):
        try:
            if not info or not msg.system_template:
                return
            name = (info.get("name") or "").strip()
            if not name or name == username:
                return
            # 消息对象可能已被重建（切房重拉），占位符对不上就放弃
            if msg.system_names.get(placeholder) != username:
                return
            # system_names 保持不动，解析结果累积进 system_resolved：
            # 后续其它占位符解析时重排不会丢掉已解析出的名字
            msg.system_resolved = dict(msg.system_resolved or {})
            msg.system_resolved[placeholder] = name
            msg.text = format_system_text(msg, {placeholder: name})
            # 同一对象也挂在 _room_messages 缓存里，改完 update_message
            # 触发重绘即可；模型里找不到（已切房）也无妨，缓存已是新值
            self.chat_area.chat_view.model.update_message(msg)
        except Exception:  # noqa: BLE001
            log.exception("系统消息姓名解析回调异常")

    # ------------------------------------------------------------------ #
    # 实时推送（stream-notify-user）：他人状态 / 订阅记录变更
    # ------------------------------------------------------------------ #
    def _on_pushed_user_status(self, username: str, status: str):
        """他人状态实时推送：落 provider 缓存并刷新所有可见面。

        provider 会保留旧资料里的 department 等字段再归一化，离职判定
        不因推送只有 status 而丢失；面板按 username 匹配活行，不在可见
        范围的行等下次渲染时从 provider 缓存取（已是新值）。
        """
        if not self._status_provider:
            return
        info = self._status_provider.apply_push(username, status)
        if not info:
            return
        self.conversation_list.apply_status(username, info)
        conv = self._conversations.get(self._active_room_id)
        if (conv is not None and conv.room_type == "d"
                and conv.username == username):
            self.chat_area.set_dm_status(info.get("status_key", "offline"),
                                         info.get("status_text", ""))
        self._room_panels.members.refresh_status(username, info)
        self._room_panels.directory.refresh_status(username, info)

    def _on_pushed_subscription(self, action: str, sub: dict):
        """订阅记录变更推送：同步未读数/收藏/改名/房间增删。

        服务端在每条消息后也会推 updated（权威未读数），本地 +1 的计数
        会被它纠正；removed = 被踢出/房间被删，清理方式与本地删除一致。
        """
        rid = str((sub or {}).get("rid", "") or "")
        if not rid:
            return
        if action == "removed":
            if rid in self._conversations:
                self._remove_conversation_locally(rid)
            return
        if action == "inserted":
            # 被拉进新房间/新私聊：防抖重拉整表（房间创建方连发几条
            # 消息可能触发多次 inserted，不能逐次整表拉取）
            if rid not in self._conversations:
                self._schedule_conversations_reload()
            return
        if action != "updated":
            return
        conv = self._conversations.get(rid)
        if conv is None:
            return
        conv.unread = int(sub.get("unread", 0) or 0)
        conv.mention_count = int(sub.get("userMentions", 0) or 0)
        # 当前会话不显未读角标：服务器在我们上报已读前推来的计数只该
        # 影响别的行，保持 0 避免当前行闪角标
        if rid == self._active_room_id:
            conv.unread = 0
            conv.mention_count = 0
        conv.is_favorite = bool(sub.get("f"))
        fname = str(sub.get("fname", "") or "")
        if fname:
            conv.name = fname
        self.conversation_list.upsert_conversation(conv)
        # 服务器权威未读归零说明这些提醒已被别处读过；当前会话被强制
        # 置 0，不能据此清掉（用户可能还停在设置页没看消息）
        if rid != self._active_room_id and conv.unread == 0:
            self._drop_tray_alerts(rid)
        self._refresh_tray_state()

    def _schedule_conversations_reload(self):
        if self._conv_reload_timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._load_conversations)
            self._conv_reload_timer = timer
        self._conv_reload_timer.start(1000)

    def _remove_conversation_locally(self, room_id: str):
        """被动移除会话（被踢/房间被删）：与本地删除的清理路径一致。"""
        self._conversations.pop(room_id, None)
        self._latest_by_room.pop(room_id, None)
        self.conversation_list.remove_conversation(room_id)
        self._room_messages.pop(room_id, None)
        self._drop_tray_alerts(room_id)
        self._refresh_tray_state()
        if room_id == self._active_room_id:
            self._active_room_id = None
            self.chat_area.set_title("")
            self.chat_area.chat_view.clear_messages()

    def _resolve_display_names(self, convs: list):
        """异步补齐服务器配置的中文显示名（频道 fname / 私聊 name）。"""
        if not self.client:
            return
        client = self.client

        def _fetch():
            names = dict(client.fetch_room_display_names())
            dm_usernames = [c.username for c in convs
                            if c.room_type == "d" and c.username]
            if dm_usernames:
                by_username = client.fetch_user_names(dm_usernames)
                for c in convs:
                    if c.room_type == "d" and c.id not in names:
                        resolved = by_username.get(c.username, "")
                        if resolved:
                            names[c.id] = resolved
            return names

        def _done(names):
            if not names:
                return
            changed = False
            for c in convs:
                disp = names.get(c.id, "")
                if disp and disp != c.name:
                    c.name = disp
                    changed = True
            if changed:
                self.conversation_list.set_conversations(
                    list(self._conversations.values()))

        def _fail(err):
            log.debug("解析会话显示名失败（非致命）：%s", err)

        run_async(_fetch, on_done=_done, on_fail=_fail, parent=self)

    # ------------------------------------------------------------------ #
    # 房间收藏
    # ------------------------------------------------------------------ #
    def _toggle_favorite_room(self, room_id: str):
        """切换房间收藏状态，并把结果同步到服务器。

        先乐观更新本地显示（收藏优先排序 + 星标），随后经 run_async
        在后台调用 rooms.favorite；失败则回滚并记日志。
        """
        conv = self._conversations.get(room_id)
        if not conv:
            return
        new_val = not conv.is_favorite
        conv.is_favorite = new_val
        # 立即刷新列表（排序 + 收藏标识），不等服务器往返
        self.conversation_list.upsert_conversation(conv)

        def _task():
            return self.client.set_room_favorite(room_id, new_val)

        def _ok(_result):
            pass  # 服务器已确认，乐观状态就是最终状态

        def _fail(e):
            conv.is_favorite = not new_val  # 回滚
            self.conversation_list.upsert_conversation(conv)
            log.exception("设置收藏失败：%s", e)

        run_async(_task, on_done=_ok, on_fail=_fail, parent=self)

    def _on_toggle_mute(self, room_id: str):
        """切换房间免打扰：本地先翻，服务器确认失败则回滚。

        静音只影响提醒（托盘/通知），未读数照常累加；状态以订阅推送
        的 disableNotifications 为准，这里乐观更新只为菜单即时反馈。
        """
        conv = self._conversations.get(room_id)
        if not conv:
            return
        new_val = not conv.muted
        conv.muted = new_val
        self.conversation_list.upsert_conversation(conv)
        if new_val:
            # 已有的托盘提醒也一并撤掉，否则静音后旧预览还挂在图标上
            self._drop_tray_alerts(room_id)
            self._refresh_tray_state()

        def _task():
            return self.client.set_room_muted(room_id, new_val)

        def _ok(_result):
            from core.conv_cache import save_conversations_snapshot
            save_conversations_snapshot(
                self.client.server_url, self.client.me.username or "",
                list(self._conversations.values()))
            self._refresh_tray_state()

        def _fail(e):
            conv.muted = not new_val
            self.conversation_list.upsert_conversation(conv)
            self._refresh_tray_state()
            log.exception("设置房间免打扰失败：%s", e)
            self.statusBar().showMessage(
                "服务器不支持房间免打扰或设置失败", 5000)

        run_async(_task, on_done=_ok, on_fail=_fail, parent=self)

    def _on_mark_unread(self, room_id: str):
        """标为未读：本地红点至少置 1，服务器 subscriptions.unread 同步。
        非 stock 服务端不支持该端点时降级为本地未读（红点保留），不打断。
        """
        conv = self._conversations.get(room_id)
        if not conv:
            return
        conv.unread = max(conv.unread or 0, 1)
        self.conversation_list.upsert_conversation(conv)
        self._refresh_tray_state()

        def _task():
            return self.client.mark_room_unread(room_id)

        def _ok(_result):
            from core.conv_cache import save_conversations_snapshot
            save_conversations_snapshot(
                self.client.server_url, self.client.me.username or "",
                list(self._conversations.values()))

        def _fail(e):
            # 服务端不支持时保留本地未读（红点仍在），仅记日志不打断
            log.exception("标记未读失败（保留本地红点）：%s", e)

        run_async(_task, on_done=_ok, on_fail=_fail, parent=self)

    # ------------------------------------------------------------------ #
    # 删除/退出房间
    # ------------------------------------------------------------------ #
    def _on_delete_room(self, room_id: str):
        """删除/退出房间：确认对话框 + 异步 API 调用。

        本人创建的房间与私聊走删除（服务端清空聊天记录，无法恢复），
        别人创建的房间只能退出。私聊删除需要服务端 delete-room 权限，
        无权限时服务端报错经 _fail 透出。
        """
        conv = self._conversations.get(room_id)
        if not conv:
            return
        deleting = ((conv.room_type == "d")
                    or (conv.i_created and conv.room_type in ("c", "p")))
        if conv.room_type == "d":
            msg = (f"删除与 {conv.name} 的会话将删除该私聊房间，"
                   f"双方的聊天记录都会被清空，且无法恢复。确定删除吗？")
        elif deleting:
            msg = (f"删除房间「{conv.name}」将清空该房间的所有聊天记录，"
                   f"且无法恢复。确定删除吗？")
        else:
            msg = f"确定退出 {conv.name}？"
        if not confirm(self, self.theme_manager, "确认", msg, danger=True):
            return
        client = self.client
        room_type = conv.room_type
        is_team = conv.is_team

        def _done(ok):
            self._remove_conversation_locally(room_id)

        def _fail(err):
            self.statusBar().showMessage(
                f"{'删除' if deleting else '退出'}失败：{err}", 4000)

        action = ((lambda: client.delete_room(room_id, room_type,
                                              is_team=is_team)) if deleting
                  else (lambda: client.leave_room(room_id, room_type,
                                                  is_team=is_team)))
        run_async(action, on_done=_done, on_fail=_fail, parent=self)

    def _on_conversation_selected(self, conv: Conversation):
        self._open_conversation(conv)

    def _on_panel_requested(self, kind: str):
        """点标题栏面板按钮：无房间则忽略；点已开面板的按钮=关闭。"""
        if not self._active_room_id or not self.client:
            return
        panel = self._room_panels.panel(kind)
        if panel.was_recently_closed():
            # Qt 会先把开着的手指面板关掉再回放按钮 click：这次点击就是
            # 「关闭面板」那一下，不能又立即弹回
            return
        if kind == "ai":
            panel.set_encrypted(self.chat_area.chat_view._room_encrypted)
        conv = self._conversations.get(self._active_room_id)
        self._room_panels.toggle(
            kind, self.chat_area.panel_buttons[kind],
            self._active_room_id, self._room_type, (conv.name if conv else "") or "")
        self.chat_area.set_panel_active(kind if self._room_panels.is_open(kind) else None)

    def _on_panel_closed(self, kind: str):
        self.chat_area.set_panel_active(None)
        if kind == "directory":
            self.directory_btn.setChecked(False)
        elif kind == "messages":
            self._ai_search_token += 1
            task = self._ai_search_task
            self._ai_search_task = None
            if task is not None:
                task.cancel()

    def _on_directory_requested(self, *_):
        """目录按钮：与房间面板共用互斥控制器，但不依赖当前房间。"""
        if not self.client:
            return
        panel = self._room_panels.panel("directory")
        if panel.was_recently_closed():
            # 面板失活自关后按钮 click 才回放：这次点击就是「关闭」那一下
            return
        opened = self._room_panels.toggle("directory", self.directory_btn,
                                          "", "", "")
        self.directory_btn.setChecked(opened)

    def _on_directory_entry_open(self, entry):
        """目录行点击：用户开私聊；已加入直接开会话；
        未加入的公开频道/团队进预览；私有团队拦下。"""
        if entry.kind == "user":
            self._on_dm_requested(entry.username)
            return
        conv = self._conversations.get(entry.rid)
        if conv is not None:
            self._open_conversation(conv)
            return
        if entry.kind == "team" and entry.is_private:
            self.statusBar().showMessage("私有团队需受邀加入，无法预览", 4000)
            return
        if not entry.rid:
            self.statusBar().showMessage("该条目缺少房间信息，无法打开", 4000)
            return
        self._open_preview(entry)

    def _open_preview(self, entry):
        """未加入的公开频道/团队：预览模式打开（可看历史、不加入）。

        预览会话不在左侧会话列表、不收实时消息；rooms.info 失败时用目录
        条目字段兜底（fname 可能回落 slug）。"""
        if not self.client:
            return
        rid = entry.rid
        client = self.client

        def _done(room):
            if not isinstance(room, dict):
                room = {}
            conv = Conversation(
                id=rid,
                name=room.get("fname") or room.get("name") or entry.name,
                room_type=room.get("t") or entry.room_type or "c",
                is_team=bool(room.get("teamMain")) or entry.kind == "team",
            )
            self._preview_entries[rid] = entry
            self._open_conversation(conv, preview=True)

        def _fail(err):
            log.debug("预览取房间信息失败，用目录条目兜底：%s", err)
            _done({})

        run_async(lambda: client.fetch_room_info(rid),
                  on_done=_done, on_fail=_fail, parent=self)

    def _join_preview_room(self):
        """预览横幅「加入」：加入成功后重拉列表正式进入会话。"""
        rid = self._active_room_id
        entry = self._preview_entries.get(rid)
        if not self.client or not entry:
            return
        client = self.client

        def _done(ok):
            if not ok:
                self.statusBar().showMessage("加入失败：服务器未确认", 4000)
                return
            self._preview_entries.pop(rid, None)
            self._reload_and_open(rid)

        def _fail(err):
            self.statusBar().showMessage(f"加入失败：{err}", 4000)

        if entry.kind == "team":
            run_async(lambda: client.join_team(rid, entry.team_id,
                                               is_public=not entry.is_private),
                      on_done=_done, on_fail=_fail, parent=self)
        else:
            run_async(lambda: client.join_channel(rid),
                      on_done=_done, on_fail=_fail, parent=self)

    def _on_panel_member_clicked(self, member: dict, anchor):
        """点成员面板里的成员行：弹资料卡（嵌套 popup，面板保持打开）。"""
        if not self.client:
            return
        from core.models import User
        from .profile_popup import ProfileCard
        user = User(
            username=member.get("username") or "",
            name=member.get("name") or "",
            avatar_url="",
        )
        card = ProfileCard(user, self._room_panels.panel("members"))
        card.dm_requested.connect(self._on_dm_requested)
        avatar_url = ""
        try:
            avatar_url = self.client.get_avatar_url(
                username=member.get("username"))
        except Exception:  # noqa: BLE001
            pass
        if avatar_url and self.avatar_loader:
            self.avatar_loader.load(card.avatar, avatar_url)
        if self.client:
            card.enrich(self.client)
        screen = QDesktopWidget().availableGeometry(card)
        card.adjustSize()
        pos = QPoint(screen.center().x() - card.width() // 2,
                     screen.center().y() - card.height() // 2)
        card.move(pos)
        card.show()

    def _on_add_member_requested(self):
        """成员面板「+」：先关面板（模态对话框不能在 popup 抓取下打开），
        再弹添加成员对话框。"""
        if not self._active_room_id or not self.client:
            return
        if self._room_type not in ("c", "p"):
            return
        rid, room_type = self._active_room_id, self._room_type
        self._room_panels.close_all()
        self.chat_area.set_panel_active(None)
        from .create_room_dialogs import AddMemberDialog
        dlg = AddMemberDialog(self.theme_manager, self,
                              search_fn=self.client.search_users)
        if dlg.exec_() != QDialog.Accepted or not dlg.username:
            return
        username = dlg.username

        def _done(_ok):
            # 成员表已变化：失效缓存让面板/未读数/@候选重新拉
            self._members_cache.pop(rid, None)
            self.statusBar().showMessage(f"已添加 {username}", 4000)
            btn = self.chat_area.panel_buttons["members"]
            self._room_panels.toggle("members", btn, self._active_room_id,
                                     self._room_type,
                                     (self._conversations.get(self._active_room_id).name
                                      if self._conversations.get(self._active_room_id) else "") or "")
            self.chat_area.set_panel_active(
                "members" if self._room_panels.is_open("members") else None)

        def _fail(err):
            self.statusBar().showMessage(f"添加成员失败：{err}", 5000)

        run_async(lambda: self.client.add_room_member(rid, username, room_type),
                  on_done=_done, on_fail=_fail, parent=self)

    def _apply_anchored_page(self, rid: str, msgs: list, flash_id: str = ""):
        """用 around 拉出的一页重建窗口（日期跳转/消息定位共用）。

        msgs 为服务端原始顺序（新->旧）。这页是锚定窗口而不是完整历史：
        标记 _history_anchored，向上滚动按窗口最早一条继续向 earlier
        拉页，向下滚到底则用 syncMessages 补到最新（_on_view_bottom）；
        重进房间时 _load_history 会丢弃窗口重拉最新首屏。
        """
        self._room_messages[rid] = list(reversed(msgs))
        self._history_offset[rid] = len(msgs)
        self._history_more[rid] = len(msgs) >= self._page_size
        self._history_anchored.add(rid)
        self._history_forward_done.discard(rid)
        self._history_anchor_gen[rid] = \
            self._history_anchor_gen.get(rid, 0) + 1
        self._history_anchor_ts[rid] = msgs[0].timestamp if msgs else 0.0
        self._resolve_system_names(msgs)
        view = self.chat_area.chat_view
        # 重建时滚动条会瞬时归零、落点滚动也可能贴底，不能当成用户
        # 主动滚到底去补更新消息；等这一轮事件跑完再恢复触发
        self._suppress_bottom_load = True
        try:
            view.rebuild_messages(self._room_messages[rid])
            if flash_id:
                view.flash_row(flash_id)
        finally:
            QTimer.singleShot(0, self._resume_bottom_load)

    def _resume_bottom_load(self):
        try:
            self._suppress_bottom_load = False
        except RuntimeError:
            # 关闭窗口时 0ms 回调可能晚于 C++ 对象销毁
            pass

    def _locate_message(self, msg: Message):
        """搜索结果点击：跳到对应消息并高亮（含未加载进模型的旧消息）。"""
        self._room_panels.close_all()
        self.chat_area.set_panel_active(None)
        rid = self._active_room_id
        if not (self.client and rid and msg is not None):
            return
        if msg.room_id and msg.room_id != rid:
            return
        view = self.chat_area.chat_view
        if view.model.find_message_row(msg.id) >= 0:
            view.flash_row(msg.id)
            return

        client = self.client
        room_type = self._room_type

        def _locate_around():
            return client.fetch_history_around(rid, msg.timestamp,
                                               count=self._page_size,
                                               room_type=room_type)

        def _done(msgs):
            if self._active_room_id != rid:
                return
            if any(m.id == msg.id for m in (msgs or [])):
                # 以命中消息为中心重建窗口，并标记为锚定页（见
                # _apply_anchored_page）
                self._apply_anchored_page(rid, msgs, msg.id)
            else:
                self._fallback_locate(msg, rid)

        def _fail(err):
            if self._active_room_id == rid:
                self.statusBar().showMessage(f"定位消息失败：{err}", 4000)

        run_async(_locate_around, on_done=_done, on_fail=_fail, parent=self)

    def _on_date_jump(self, ts: float):
        """记录面板选日期：跳到该日聊天记录页（latest=该日末尾锚定一页）。"""
        self._room_panels.close_all()
        self.chat_area.set_panel_active(None)
        rid = self._active_room_id
        if not (self.client and rid):
            return
        client = self.client
        room_type = self._room_type

        def _fetch():
            # fetch_history_around 内部 latest=ts+1：传"当日 23:59:59"使
            # 返回页最新一条是该日最后一条消息
            return client.fetch_history_around(rid, ts + 86399,
                                               count=self._page_size,
                                               room_type=room_type)

        def _done(msgs):
            if self._active_room_id != rid:
                return
            msgs = msgs or []
            # 该日无消息则该页全是更早的上下文，按「没有记录」处理且不动
            # 当前视图；有消息时整页保留（含更早上下文），用户向上滚动
            # 仍能续看，不能只留该日几条（真机：选「今天」后历史全消失）
            day_msgs = [m for m in msgs if ts <= m.timestamp < ts + 86400]
            if not day_msgs:
                self.statusBar().showMessage("该日期没有聊天记录", 4000)
                return
            # 闪该日最早一条作落点提示（msgs 新->旧，故取最后一条）
            self._apply_anchored_page(rid, msgs, day_msgs[-1].id)

        def _fail(err):
            if self._active_room_id == rid:
                self.statusBar().showMessage(f"跳转失败：{err}", 4000)

        run_async(_fetch, on_done=_done, on_fail=_fail, parent=self)

    def _fallback_locate(self, msg: Message, rid: str):
        """around 定位未命中（服务端 latest 支持异常等）：从头逐页翻找。"""
        client = self.client
        room_type = self._room_type
        cap = 10

        def _step(offset):
            def _proc():
                return client.get_history(rid, count=100, offset=offset,
                                          room_type=room_type)

            def _done(msgs):
                if self._active_room_id != rid:
                    return
                msgs = msgs or []
                idx = next((i for i, m in enumerate(msgs) if m.id == msg.id), -1)
                if idx >= 0:
                    self._apply_anchored_page(rid, msgs, msg.id)
                elif msgs and offset + len(msgs) < cap * 100:
                    _step(offset + len(msgs))
                else:
                    self.statusBar().showMessage("未能定位到该消息", 4000)

            def _fail(err):
                if self._active_room_id == rid:
                    self.statusBar().showMessage(f"定位消息失败：{err}", 4000)

            run_async(_proc, on_done=_done, on_fail=_fail, parent=self)

        _step(0)

    def _mark_room_read(self, rid: str):
        """向服务端上报已读。失败只记日志：角标已清，别打断打开流程。"""
        if not self.client:
            return
        run_async(lambda: self.client.mark_room_read(rid),
                  on_fail=lambda err: log.warning(
                      "上报已读失败 rid=%s：%s", rid, err),
                  parent=self)

    def _open_conversation(self, conv: Conversation, preview: bool = False):
        log.info("打开会话 rid=%s type=%s preview=%s", conv.id, conv.room_type,
                 preview)
        if not self.client:
            # 登录前快照渲染会自动选中第一条会话：此刻拉不了历史，
            # 先记下，登录成功后在 _on_login_success 里补开
            self._pending_open_conv = conv
            return
        # 切走前先存上一会话的草稿（编辑中的先取消、恢复原文本）；
        # 同一会话重开（加入预览/刷新）不碰输入，否则草稿恢复会把
        # 输入框里的内联截图覆盖掉（草稿只存纯文本）
        switching = conv.id != self._active_room_id
        if switching:
            # 跨房定位挂起只在目标房间兑现：改开别的房间即作废
            pend = getattr(self, "_pending_locate", None)
            if pend and pend[0] != conv.id:
                self._pending_locate = None
            self._save_current_draft()
            # 离开的会话视为已读到当前最新一条（未读分隔线下次从这之后算）
            self._mark_room_last_read(self._active_room_id)
            self._pending_unread.pop(self._active_room_id, None)
            self.chat_area.clear_new_messages()
        # 正常打开退出预览态（预览路径后面会重新登记 entry + 横幅）
        if not preview:
            self._preview_entries.pop(conv.id, None)
            self.chat_area.set_preview_mode(False)
            # 快照渲染早于网络会话列表时 _conversations 里还没有这个房间，
            # 先登记进来：否则历史/实时回调找不到 conv，左侧预览同步被跳过
            self._conversations[conv.id] = conv
        self._active_room_id = conv.id
        self._room_type = conv.room_type
        self._cancel_ai_task_if_room_changed(conv.id)
        # 打开前先取本地已读位置与未读数：随后就把角标清零/上报已读，
        # 不先记下来就再也算不出「从哪开始是新消息」
        if not preview:
            self._pending_unread.pop(conv.id, None)
            if conv.unread > 0:
                ident = self._draft_identity()
                last_read = 0.0
                if ident:
                    from core.read_state import get_last_read
                    last_read = get_last_read(ident[0], ident[1], conv.id)
                self._pending_unread[conv.id] = (last_read, conv.unread)
        # 打开即视为已读，刷新该行角标（@ 徽标一并清）
        if not preview and (conv.unread or conv.mention_count):
            had_unread = conv.unread > 0
            conv.unread = 0
            conv.mention_count = 0
            self.conversation_list.upsert_conversation(conv)
            # 只清本地角标不够：服务端未读计数不减，重启后角标会原样回来
            if had_unread:
                self._mark_room_read(conv.id)
        if not preview:
            # 打开即已读：托盘里该房间的提醒一并清掉
            self._drop_tray_alerts(conv.id)
            self._refresh_tray_state()
        if conv.room_type == "d":
            # 私聊只显示对方人名，不贴类型标签（微信观感）
            title = conv.name
        else:
            # 官方术语标签，团队主房间 t 仍是 c/p，用 room_type_label 区分
            title = f"{conv.name}　({room_type_label(conv)})"
            if conv.is_discussion:
                title += " · 讨论串"
        if preview:
            title += " · 预览"
        self.chat_area.set_title(title)
        # 私聊标题栏的对方状态：先清（非 d 房不显示），再按需拉取回填
        self.chat_area.clear_dm_status()
        if (conv.room_type == "d" and not preview and conv.username
                and "," not in conv.username):
            self._status_provider.get(
                conv.username,
                lambda info, u=conv.username: self._on_status_info(u, info))
        # 切换会话时关闭面板（规避跨房数据竞态；各异步回调另有 rid 校验兜底）
        self._room_panels.close_all()
        self.chat_area.set_panel_active(None)
        view = self.chat_area.chat_view
        view.set_room(conv.id)
        # E2E 房间不提供「编辑」入口：解密明文不回传服务端
        view.set_room_encrypted(bool(conv.encrypted))
        # 切房间先清掉上一房间的历史加载状态条（新房间要不要显示由
        # _load_history 判定）；待重试会话也不能跨房间残留
        self.chat_area.set_history_state("hide")
        self._history_retry_conv = None
        # 群聊要显示发送者名字，私聊不显示
        view.set_show_name(conv.room_type != "d")
        if preview:
            # 预览房间未加入：已读回执/成员数/实时订阅都没有意义且可能被
            # 服务端拒绝，全部跳过；横幅上的「加入」走 _join_preview_room
            self.chat_area.set_preview_mode(True, is_team=conv.is_team)
        else:
            # 已读状态上下文（模型刚被 set_room 清空）：房间类型 + 回执回灌
            self._apply_receipts_state_to_view(conv.id)
            # 1 对 1 私聊的成员恒为对方一人，成员数未知(0)与实拉结果在
            # status_spec 里渲染一致；开房不再拉 im.members——启动时连开
            # 几个私聊曾把它拖到 20s 读超时（真机日志）。@ 候选
            # 直接用已知的对方信息注入；成员面板仍按需经 _members_provider 拉。
            one_to_one_dm = (conv.room_type == "d" and conv.username
                             and "," not in conv.username)
            if one_to_one_dm:
                peer = {"username": conv.username}
                if conv.name and conv.name != conv.username:
                    peer["name"] = conv.name
                self.chat_area.input.set_mention_candidates(
                    conv.room_type, [peer])
            else:
                # 多人私聊要靠成员数 >1 切换成未读数样式，照常拉取
                self._ensure_room_members(conv.id, conv.room_type)
                self._push_mention_candidates(conv.id, conv.room_type)
            self._schedule_receipts_fetch()
        if switching:
            self._restore_draft_for(conv.id)
        # 递增加载代数，使该房间旧的异步回调自动失效
        gen = self._load_gen.get(conv.id, 0) + 1
        self._load_gen[conv.id] = gen
        # 订阅当前会话
        if not preview and self.realtime:
            self.realtime.subscribe(conv.id)
        self._load_history(conv)
        # 延迟渲染：先让 UI 切换完成，再在下一轮事件循环中重建消息行
        cached = self._room_messages.get(conv.id)
        if cached:
            def _deferred_rebuild():
                if self._active_room_id != conv.id:
                    return  # 已切走，放弃渲染
                view.rebuild_messages(cached)
                # rebuild 会 clear 模型，回执状态要重新回灌
                self._apply_receipts_state_to_view(conv.id)
                self._apply_unread_divider(conv.id)
                if not view.jump_to_first_unread():
                    view.scroll_to_bottom(deferred=True)
                # 全局搜索跨房定位：缓存重建即最后一笔渲染（历史不会再
                # 网络加载），在此续接；快照房间留给 _load_history 回调
                if conv.id not in self._snapshot_shown:
                    self._maybe_locate_pending(conv.id)
            QTimer.singleShot(0, _deferred_rebuild)

    def _load_history(self, conv: Conversation):
        """异步加载首屏历史；加载期间再次进入同一房间不重复请求。"""
        rid = conv.id
        if rid in self._history_anchored:
            # 上次停在锚定页（日期跳转/搜索定位）：窗口只是 around 一页，
            # 不是完整历史，必须丢弃后按首屏重拉；否则下面的 offset>0
            # 会让房间永远停在那一页（真机：选「今天」后重进看不到消息）
            self._history_anchored.discard(rid)
            self._history_forward_done.discard(rid)
            self._history_anchor_gen[rid] = \
                self._history_anchor_gen.get(rid, 0) + 1
            self._history_anchor_ts.pop(rid, None)
            self._room_messages.pop(rid, None)
            self._history_offset.pop(rid, None)
            self._history_more.pop(rid, None)
        offset = self._history_offset.get(rid, 0)
        if offset > 0:
            # 已加载过历史，不重复加载（视图已由缓存重建）
            return
        if rid in self._history_loading:
            return
        self._history_loading.add(rid)
        client = self.client
        from core.msg_cache import load_messages_snapshot
        # 首次打开先用磁盘快照渲染首屏（微信式本地优先）：塞进
        # _room_messages 后由 _open_conversation 的 cached 重建路径渲染；
        # 网络首屏回来后整体替换（快照可能过时，且 prepend 去重保不住
        # 编辑/撤回后的新内容）。快照里是解密后的明文，无需再解密
        if rid not in self._room_messages:
            snap = load_messages_snapshot(client.server_url,
                                          client.me.username or "", rid)
            if snap:
                self._room_messages[rid] = snap
                self._snapshot_shown.add(rid)
                self._enrich_quotes(snap)
                self._resolve_system_names(snap)
                self._sync_conversation_from_history(rid, snap)
        t0 = time.perf_counter()
        log.info("加载历史 rid=%s offset=%d page=%d",
                 rid, offset, self._page_size)
        # 只有「当前房间 + 屏幕上确实没内容」才占横幅：预取别的房间不能
        # 把当前房间的状态刷乱，有缓存/快照时也不该多一条噪音
        if rid == self._active_room_id and not self._room_messages.get(rid):
            self.chat_area.set_history_state("loading", "正在加载历史消息…")

        def _done(msgs):
            self._history_loading.discard(rid)
            # 只收本房间的状态条：切走了就该留着新房间自己的 loading/error，
            # 无条件 hide 会把 B 的「正在加载」抹掉（A 的迟到回调）
            if rid == self._active_room_id:
                self.chat_area.set_history_state("hide")
            if self._history_retry_conv is not None \
                    and self._history_retry_conv.id == rid:
                self._history_retry_conv = None
            self._history_offset[rid] = len(msgs)
            self._history_more[rid] = len(msgs) >= self._page_size
            # 缓存统一时间正序（旧->新）：get_history 返回新->旧，反转入库。
            # 快照渲染到网络返回之间经实时推送/乐观发送进来的尾部消息
            # （不在网络首屏内且时间不早于首屏最新一条）要保留，否则
            # 整体替换会把它们闪掉
            net_ids = {m.id for m in msgs}
            last_ts = msgs[0].timestamp if msgs else 0.0
            extra = [m for m in self._room_messages.get(rid, [])
                     if m.id not in net_ids and m.timestamp >= last_ts]
            self._room_messages[rid] = list(reversed(msgs)) + extra
            self._enrich_quotes(self._room_messages[rid])
            self._resolve_system_names(msgs)
            self._sync_conversation_from_history(
                rid, self._room_messages[rid])
            from core.msg_cache import save_messages_snapshot
            save_messages_snapshot(client.server_url,
                                   client.me.username or "", rid,
                                   self._room_messages[rid][-self._page_size:])
            log.info("历史加载完成 rid=%s %d 条，耗时 %.1f ms",
                     rid, len(msgs), (time.perf_counter() - t0) * 1000)
            # 只在房间仍处于打开态时渲染。不看加载代数（_load_gen）：
            # 预取在途时用户点开同一房间会递增代数，但用户要的正是这份
            # 数据；切走则 active 不等，同样拦得住
            if rid != self._active_room_id:
                return
            view = self.chat_area.chat_view
            if rid in self._snapshot_shown:
                # 快照已在屏上：rebuild 整体替换（去重 prepend 会把
                # 快照里的编辑/撤回旧内容留在屏上）
                self._snapshot_shown.discard(rid)
                view.rebuild_messages(self._room_messages[rid])
                self._apply_receipts_state_to_view(rid)
                self._schedule_receipts_fetch()
                self._apply_unread_divider(rid)
                if not view.jump_to_first_unread():
                    view.scroll_to_bottom(deferred=True)
                self._maybe_locate_pending(rid)
            else:
                # 分隔线必须在插入前设置：_finish_prepend 要据此决定停在
                # 分隔线还是底部（模型此刻还没有行，set 只记 id 与高度规则）
                self._apply_unread_divider(rid)
                view.prepend_messages(msgs, first_page=True)
                self._apply_receipts_state_to_view(rid)
                self._schedule_receipts_fetch()
                self._maybe_locate_pending(rid)

        def _fail(err):
            self._history_loading.discard(rid)
            log.error("历史加载失败 rid=%s：%s（耗时 %.1f ms）",
                      rid, err, (time.perf_counter() - t0) * 1000)
            self.statusBar().showMessage(f"加载历史消息失败：{err}", 4000)
            # 屏幕仍然空白才把失败摆到界面上：超时多是冷连接抖动，
            # 给一个重试入口比让用户反复点会话有效
            if rid == self._active_room_id and not self._room_messages.get(rid):
                self.chat_area.set_history_state("error",
                                                 f"历史消息加载失败：{err}")
                self._history_retry_conv = conv

        run_async(
            lambda: client.get_history(rid, count=self._page_size,
                                       offset=0, room_type=conv.room_type),
            on_done=_done, on_fail=_fail, parent=self)

    def _on_history_retry_clicked(self):
        """状态条「重试」：只重开当前房间、且没有在途请求时才重新发起。

        失败不会写 _history_offset（仍为 0），所以 _load_history 的护栏
        不会把重试挡掉。
        """
        conv = self._history_retry_conv
        if not self.client or conv is None:
            return
        rid = conv.id
        if rid != self._active_room_id or rid in self._history_loading:
            return
        self._history_retry_conv = None
        self.chat_area.set_history_state("hide")
        self._load_history(conv)

    def _load_older_history(self):
        """向上滚动到顶部时加载更早的消息（异步，不阻塞 UI）。"""
        if not self.client or not self._active_room_id or self._loading_older:
            return
        rid = self._active_room_id
        if not self._history_more.get(rid, False):
            return
        if rid in self._history_anchored:
            self._load_older_from_anchor(rid)
            return
        offset = self._history_offset.get(rid, 0)
        if offset <= 0:
            return
        self._loading_older = True
        client = self.client
        room_type = self._room_type
        t0 = time.perf_counter()
        log.info("加载更早历史 rid=%s offset=%d", rid, offset)

        def _done(msgs):
            self._loading_older = False
            if not msgs:
                log.info("更早历史已到头 rid=%s offset=%d", rid, offset)
                return
            self._history_offset[rid] = offset + len(msgs)
            self._history_more[rid] = len(msgs) >= self._page_size
            view = self.chat_area.chat_view
            view.prepend_messages(msgs)
            # 合并缓存（均保持时间正序）：更早消息在列表前部
            self._room_messages[rid] = list(reversed(msgs)) + \
                self._room_messages.get(rid, [])
            self._enrich_quotes(self._room_messages[rid])
            self._resolve_system_names(msgs)
            log.info("更早历史完成 rid=%s %d 条，累计 %d，耗时 %.1f ms",
                     rid, len(msgs), len(self._room_messages[rid]),
                     (time.perf_counter() - t0) * 1000)

        def _fail(err):
            self._loading_older = False
            log.error("更早历史失败 rid=%s：%s", rid, err)
            self.statusBar().showMessage(f"加载更早消息失败：{err}", 4000)

        run_async(
            lambda: client.get_history(rid, count=self._page_size,
                                       offset=offset, room_type=room_type),
            on_done=_done, on_fail=_fail, parent=self)

    def _load_older_from_anchor(self, rid: str):
        """锚定窗口向上翻页：按窗口最早一条向更早拉一页。

        offset 分页是按「最新页」往后数的，锚定窗口下 offset 与窗口内容
        不对应，必须改用 latest 锚点（传 earliest-1，fetch_history_around
        内部再加 1 秒，正好排掉窗口内已有的最早一条）。
        """
        cached = self._room_messages.get(rid) or []
        if not cached:
            return
        oldest = cached[0].timestamp
        self._loading_older = True
        client = self.client
        room_type = self._room_type
        t0 = time.perf_counter()

        def _done(msgs):
            self._loading_older = False
            msgs = msgs or []
            if not msgs:
                self._history_more[rid] = False
                log.info("锚定窗口已到头 rid=%s", rid)
                return
            have = {m.id for m in self._room_messages.get(rid, [])}
            older = [m for m in msgs if m.id not in have]
            if not older:
                self._history_more[rid] = False
                return
            self._history_more[rid] = len(msgs) >= self._page_size
            self.chat_area.chat_view.prepend_messages(older)
            self._room_messages[rid] = list(reversed(older)) + \
                self._room_messages.get(rid, [])
            self._enrich_quotes(self._room_messages[rid])
            self._resolve_system_names(older)
            log.info("锚定窗口更早历史 rid=%s %d 条，累计 %d，耗时 %.1f ms",
                     rid, len(older), len(self._room_messages[rid]),
                     (time.perf_counter() - t0) * 1000)

        def _fail(err):
            self._loading_older = False
            log.error("锚定窗口更早历史失败 rid=%s：%s", rid, err)
            self.statusBar().showMessage(f"加载更早消息失败：{err}", 4000)

        run_async(
            lambda: client.fetch_history_around(
                rid, oldest - 1.0, count=self._page_size,
                room_type=room_type),
            on_done=_done, on_fail=_fail, parent=self)

    def _on_view_bottom(self):
        """视图滚到底：锚定窗口向下补齐更新消息（日期跳转后的常态）。"""
        if not self.client or not self._active_room_id or self._loading_newer:
            return
        rid = self._active_room_id
        if rid not in self._history_anchored:
            return
        if rid in self._history_forward_done or self._suppress_bottom_load:
            return
        if self.chat_area.chat_view.is_batching_history:
            return
        self._load_newer_from_anchor(rid)

    def _load_newer_from_anchor(self, rid: str):
        """锚定窗口向下续载：补齐窗口最新一条之后的新消息。

        history 端点只能按时间倒序翻页（带 oldest 也返回区间最新一页），
        补不了正向；chat.syncMessages 按游标升序返回之后的全部更新，
        一次补到最新（见 ApiClient.fetch_messages_since）。只追加不滚动，
        用户继续往下看到的仍是紧接锚定页的内容。
        """
        cached = self._room_messages.get(rid) or []
        if not cached:
            return
        newest = self._history_anchor_ts.get(rid, cached[-1].timestamp)
        self._loading_newer = True
        gen = self._history_anchor_gen.get(rid, 0)
        client = self.client
        t0 = time.perf_counter()

        def _done(msgs):
            self._loading_newer = False
            if self._history_anchor_gen.get(rid, 0) != gen:
                # 在途期间换过锚定页/重进过房间：旧窗口的续载结果作废
                return
            self._history_forward_done.add(rid)
            msgs = msgs or []
            have = {m.id for m in self._room_messages.get(rid, [])}
            newer = [m for m in msgs if m.id not in have]
            if not newer:
                log.info("锚定窗口已到最新 rid=%s", rid)
                return
            self._room_messages[rid] = \
                self._room_messages.get(rid, []) + newer
            self._history_anchor_ts[rid] = max(newest,
                                               newer[-1].timestamp)
            self._enrich_quotes(self._room_messages[rid])
            self._resolve_system_names(newer)
            if rid == self._active_room_id:
                self.chat_area.chat_view.append_messages(newer)
            log.info("锚定窗口更新消息 rid=%s %d 条，累计 %d，耗时 %.1f ms",
                     rid, len(newer), len(self._room_messages[rid]),
                     (time.perf_counter() - t0) * 1000)

        def _fail(err):
            self._loading_newer = False
            log.error("锚定窗口更新消息失败 rid=%s：%s", rid, err)
            self.statusBar().showMessage(f"加载更新消息失败：{err}", 4000)

        run_async(lambda: client.fetch_messages_since(rid, newest),
                  on_done=_done, on_fail=_fail, parent=self)

    def _on_scroll(self, value: int):
        if value > 0:
            return
        # 批量插行期间滚动值会瞬时归零，此时不要重复翻页
        if self.chat_area.chat_view.is_batching_history:
            return
        self._load_older_history()

    # ------------------------------------------------------------------ #
    # 每会话草稿
    # ------------------------------------------------------------------ #
    def _draft_identity(self):
        """草稿隔离身份 (server, username)；未登录返回 None。"""
        if not self.client or not self.client.me:
            return None
        return (self.client.server_url, self.client.me.username or "")

    def _on_draft_timeout(self):
        """输入停顿落盘；编辑中的输入框是待编辑消息，不是本会话草稿。"""
        if self.chat_area.input.is_editing():
            return
        self._save_current_draft()

    def _save_current_draft(self):
        """保存当前会话草稿（输入停顿/切房/关闭/登出时调用）。"""
        ident = self._draft_identity()
        rid = self._active_room_id
        if not ident or not rid:
            return
        # 预览房间不保存（用户还没加入，发不出消息）
        if rid in self._preview_entries:
            return
        # 编辑中的输入不属于草稿：先取消恢复原内容，否则草稿会被待编辑
        # 文本覆盖，下次打开房间莫名其妙多出别人消息的改写稿
        if self.chat_area.input.is_editing():
            self.chat_area.input.cancel_edit()
        snap = self.chat_area.input.draft_snapshot()
        set_draft(ident[0], ident[1], rid, snap["text"], snap["quote"])
        self._sync_draft_room_flag(rid, snap["text"], snap["quote"])

    def _sync_draft_room_flag(self, rid: str, text: str, quote):
        """按草稿内容更新该会话的 [草稿] 标识并刷新会话列表。"""
        if not rid:
            return
        has = bool((text or "").strip() or quote)
        before = rid in self._draft_rooms
        if has:
            self._draft_rooms.add(rid)
        else:
            self._draft_rooms.discard(rid)
        if has != before:
            self.conversation_list.set_draft_rooms(self._draft_rooms)

    def _refresh_draft_rooms(self):
        """登录后遍历会话，初始化有草稿的房间集合并刷新列表。"""
        ident = self._draft_identity()
        if not ident:
            return
        rooms = {rid for rid in self._conversations
                 if get_draft(ident[0], ident[1], rid)}
        self._draft_rooms = rooms
        self.conversation_list.set_draft_rooms(rooms)

    def _restore_draft_for(self, rid: str):
        """切换会话后恢复该房间草稿；无草稿则清空输入。"""
        ident = self._draft_identity()
        if not ident:
            self.chat_area.input.clear_draft_input()
            return
        draft = get_draft(ident[0], ident[1], rid)
        if draft:
            self.chat_area.input.restore_draft(draft)
            self._sync_draft_room_flag(rid, draft.get("text", ""), draft.get("quote"))
        else:
            self.chat_area.input.clear_draft_input()
            self._sync_draft_room_flag(rid, "", None)

    def _on_edit_cancelled(self):
        self._editing_msg_id = ""

    # ------------------------------------------------------------------ #
    # 未读分隔线 / 新消息提示
    # ------------------------------------------------------------------ #
    def _mark_room_last_read(self, rid: str):
        """把会话的本地已读位置推进到当前已知最新一条消息。"""
        ident = self._draft_identity()
        if not ident or not rid:
            return
        msgs = self._room_messages.get(rid) or []
        if not msgs:
            return
        from core.read_state import set_last_read
        set_last_read(ident[0], ident[1], rid, msgs[-1].timestamp)

    def _apply_unread_divider(self, rid: str) -> bool:
        """按打开会话时记录的已读位置/未读数标出首条未读。

        优先用本地已读时间戳（服务端消息时间，避免客户端时钟偏差）；
        没有记录（升级后首次打开）时退回服务端未读数从尾部反推。
        返回是否显示了分隔线。
        """
        view = self.chat_area.chat_view
        state = self._pending_unread.get(rid)
        msgs = self._room_messages.get(rid) or []
        target = None
        if state is not None and msgs:
            last_read, unread = state
            if last_read > 0:
                for m in msgs:
                    if m.timestamp > last_read and m.msg_type != "system":
                        target = m.id
                        break
            elif unread > 0:
                idx = min(max(0, len(msgs) - int(unread)), len(msgs) - 1)
                target = msgs[idx].id
        if target:
            view.set_unread_divider(target)
        else:
            view.clear_unread_divider()
        # 本次打开的房间已读位置推进到最新，下次打开不会再标出旧分隔线
        self._mark_room_last_read(rid)
        return bool(target)

    def _on_new_messages_clicked(self):
        """点「N 条新消息」：跳到底部并清掉提示条。"""
        self.chat_area.clear_new_messages()
        self.chat_area.chat_view.scroll_to_bottom(deferred=True)

    # ------------------------------------------------------------------ #
    def _on_send_text(self, text: str):
        if not self.client or not self._active_room_id:
            return
        rid = self._active_room_id
        # 编辑态回车走 chat.update，不能当新消息发（否则会多发一条）
        if self._editing_msg_id:
            self._submit_edit(rid, text)
            return
        # 引用上下文
        quote = self.chat_area.input.quote_context
        quote_sender = quote.get("sender", "") if quote else ""
        quote_text = quote.get("text", "") if quote else ""
        quote_msg_id = quote.get("msg_id", "") if quote else ""
        # 组装相对 permalink，收方（网页版/本应用）点击引用块可跳转原消息；
        # 用引用时记录的房间（rid/room_type）而不是发送瞬间的 active 房间，
        # 避免中途中会切换造成的自产自收不匹配
        quote_link = ""
        if quote_msg_id:
            q_rid = (quote.get("room_id") or rid) if quote else rid
            q_slug = (quote.get("room_slug") or "") if quote else ""
            q_rtype = (
                quote.get("room_type") or self._room_type) if quote else self._room_type
            prefix = {"d": "direct", "p": "group", "c": "channel"}.get(q_rtype)
            if prefix:
                # 网页版 permalink 用房间名/用户名；缺失时回落房间 id
                # （服务端展开引用只取 ?msg= 里的消息 id，路径段不影响展开）
                quote_link = f"/{prefix}/{q_slug or q_rid}?msg={quote_msg_id}"
        payload = {
            "room_id": rid,
            "text": text,
            "quote_sender": quote_sender,
            "quote_text": quote_text,
            "quote_msg_id": quote_msg_id,
            "quote_link": quote_link,
        }
        self._send_text_payload(payload)

    def _send_text_payload(self, payload: dict, local_id: str = ""):
        """发送文本/引用消息（首次发送与失败重发共用同一条乐观回显）。

        payload 里带 room_id/文本/引用字段；local_id 为空表示首次发送，
        生成 local- 假 id 并登记重发载荷；非空表示重发，复用原对象与
        原时间戳，避免重发后出现两条。
        """
        if not self.client:
            return ""
        rid = payload["room_id"]
        view = self.chat_area.chat_view
        if not local_id:
            import uuid
            local_id = f"local-{uuid.uuid4().hex[:12]}"
            local = Message(
                id=local_id,
                room_id=rid, text=payload["text"],
                sender=self.client.me, is_mine=True,
                msg_type="quote" if payload["quote_sender"] else "text",
                description=payload["quote_text"],
                quote_msg_id=payload["quote_msg_id"],
                quote_author=payload["quote_sender"],
                quote_link=payload["quote_link"],
                status="sending",
                timestamp=time.time(),
            )
            view.append_message(local)
            self._room_messages.setdefault(rid, []).append(local)
        else:
            local = next(
                (m for m in self._room_messages.get(rid, [])
                 if m.id == local_id), None)
            if local is None:
                self._retry_payloads.pop(local_id, None)
                return ""
            local.status = "sending"
            view.update_message_status(local_id, "sending")
        # 载荷一直保留到发送成功：失败/断网时右键或点红叉可原样重发
        self._retry_payloads[local_id] = payload
        client = self.client

        def _done(sent):
            if sent:
                # 更新状态为已发送
                view.update_message_status(local_id, "sent")
                local.status = "sent"
                self._reconcile_sent_local_echo(local, sent)
                self._bump_conversation(rid, sent)
                self._schedule_receipts_fetch()
                self._retry_payloads.pop(local_id, None)
            else:
                view.update_message_status(local_id, "failed")
                local.status = "failed"

        def _fail(err):
            view.update_message_status(local_id, "failed")
            local.status = "failed"
            self.statusBar().showMessage(f"发送失败：{err}", 4000)

        run_async(lambda: client.send_text(
                      rid, payload["text"],
                      quote_sender=payload["quote_sender"],
                      quote_text=payload["quote_text"],
                      quote_msg_id=payload["quote_msg_id"],
                      quote_link=payload["quote_link"]),
                  on_done=_done, on_fail=_fail, parent=self)
        return local_id

    def _on_retry_message(self, message: Message):
        """右键/点红叉重发失败消息；只对仍在失败态的乐观回显生效。"""
        if not message or message.status != "failed":
            return
        payload = self._retry_payloads.get(message.id)
        if not payload:
            return
        self._send_text_payload(payload, local_id=message.id)

    # ------------------------------------------------------------------ #
    # 表情回应 / 编辑 / 置顶星标
    # ------------------------------------------------------------------ #
    def _find_room_message(self, rid: str, msg_id: str) -> Optional[Message]:
        for m in self._room_messages.get(rid, []):
            if m.id == msg_id:
                return m
        view = self.chat_area.chat_view
        row = view.model.find_message_row(msg_id)
        return view.model.get_message(row) if row >= 0 else None

    def _apply_local_message_update(self, msg: Message):
        """就地改过字段的消息同步到缓存与视图（行高变化由视图重排）。"""
        cache = self._room_messages.get(msg.room_id)
        if cache is not None:
            for i, c in enumerate(cache):
                if c.id == msg.id:
                    cache[i] = msg
                    break
        if msg.room_id == self._active_room_id:
            self.chat_area.chat_view.update_message(msg)

    def _on_reaction_toggle(self, message: Message, key: str):
        """点 chip 切换自己是否点过；乐观改本地，失败回滚。"""
        if not self.client or not message or not key:
            return
        # 乐观回显还没有服务端 id，回应无处可挂
        if not message.id or message.id.startswith("local-"):
            return
        me = (self.client.me.username or "") if self.client.me else ""
        if not me:
            return
        original = message.reactions or {}
        updated = {k: {**(v or {}),
                       "usernames": list((v or {}).get("usernames") or [])}
                   for k, v in original.items()}
        should_react = me not in (updated.get(key, {}).get("usernames") or [])
        if should_react:
            info = updated.setdefault(key, {})
            info["usernames"] = list(info.get("usernames") or []) + [me]
        else:
            info = updated.get(key)
            if info is not None:
                left = [u for u in info.get("usernames") or [] if u != me]
                if left:
                    info["usernames"] = left
                else:
                    updated.pop(key, None)
        message.reactions = updated
        self._apply_local_message_update(message)
        client = self.client
        msg_id = message.id

        def _fail(err):
            # 服务端拒绝/断网：恢复原回应，等下一次推送或手动重试
            message.reactions = original
            self._apply_local_message_update(message)
            log.warning("表情回应失败 msg=%s key=%s：%s", msg_id, key, err)
            self.statusBar().showMessage(f"表情回应失败：{err}", 4000)

        run_async(lambda: client.react_message(msg_id, key, should_react),
                  on_done=lambda _ok: None, on_fail=_fail, parent=self)

    def _on_reaction_add(self, message: Message):
        """右键「添加表情回应…」：弹 emoji 面板，选中后走同一套切换。"""
        if not message or message.msg_type == "system":
            return
        if self._reaction_picker is None:
            from .emoji_picker import EmojiPicker
            self._reaction_picker = EmojiPicker(
                self, theme=self.theme_manager, close_on_select=True)
            self._reaction_picker.emoji_selected.connect(
                self._react_with_emoji)
        self._reaction_target = message
        from PyQt5.QtGui import QCursor
        pos = QCursor.pos()
        self._reaction_picker.open_at(QPoint(pos.x() - 40, pos.y() - 40))

    def _react_with_emoji(self, char: str):
        message = self._reaction_target
        self._reaction_target = None
        if message is None:
            return
        # 服务端按 emojione 短码校验；肤色必须是 :ok_hand_tone5: 这种方言，
        # emoji.demojize 给的 CLDR 名服务端不认
        self._on_reaction_toggle(message, rc_shortcode(char))

    def _on_edit_message(self, message: Message):
        """进入编辑态：当前输入先落草稿，再换成待编辑正文。"""
        if not message or not self.client or message.id.startswith("local-"):
            return
        if message.room_id != self._active_room_id:
            return
        if self.chat_area.chat_view._room_encrypted:
            return
        self._save_current_draft()
        self._editing_msg_id = message.id
        self.chat_area.input.begin_edit(message.id, message.text or "")

    def _submit_edit(self, rid: str, text: str):
        """编辑发送分流：调 chat.update，成功后刷新行并退出编辑态。"""
        msg = self._find_room_message(rid, self._editing_msg_id)
        text = (text or "").strip()
        if msg is None:
            self._editing_msg_id = ""
            self.chat_area.input.cancel_edit()
            return
        if not text:
            self.statusBar().showMessage("消息内容不能为空", 3000)
            return
        client = self.client
        msg_id = msg.id

        def _done(updated):
            if isinstance(updated, Message) and updated.id == msg_id:
                self._apply_local_message_update(updated)
            else:
                # 服务端响应没带 message：本地乐观更新，等实时推送覆盖
                msg.text = text
                msg.edited_at = time.time()
                self._apply_local_message_update(msg)
            self._editing_msg_id = ""
            self.chat_area.input.finish_edit()
            # 输入已被清空，让草稿定时器把该房间草稿删掉
            self._draft_timer.start()
            self.statusBar().showMessage("消息已编辑", 2000)

        def _fail(err):
            # 保留编辑态与输入内容，回车可再次提交
            log.warning("编辑消息失败 msg=%s：%s", msg_id, err)
            self.statusBar().showMessage(f"编辑失败：{err}", 4000)

        run_async(lambda: client.update_message(rid, msg_id, text),
                  on_done=_done, on_fail=_fail, parent=self)

    def _on_pin_message(self, message: Message, pinned: bool):
        self._toggle_message_flag(message, "pinned", pinned,
                                  lambda mid, val: self.client.pin_message(
                                      mid, val), "置顶")

    def _on_star_message(self, message: Message, starred: bool):
        self._toggle_message_flag(message, "starred", starred,
                                  lambda mid, val: self.client.star_message(
                                      mid, val), "星标")

    def _on_remind_requested(self, message: Message, due: float):
        """右键「提醒」：落盘后到点由 _check_reminders 经托盘 toast 通知。
        同一条消息重复设提醒按覆盖处理（旧的移除），避免列表里堆重复项。"""
        rid = message.room_id or self._active_room_id
        if not rid:
            return
        from config import load_config, save_reminders
        try:
            cfg = load_config()
            reminders = [r for r in cfg.get("reminders", [])
                         if not (isinstance(r, dict)
                                 and r.get("rid") == rid
                                 and r.get("msg_id") == (message.id or ""))]
            reminders.append({
                "rid": rid,
                "msg_id": message.id or "",
                "text": (message.text or "").strip()[:80],
                "due": float(due),
            })
            save_reminders(reminders)
        except Exception:  # noqa: BLE001
            log.exception("保存消息提醒失败")
            return
        self._sync_remind_map()
        self.statusBar().showMessage("已设置提醒，到点将在托盘通知", 3000)

    def _on_remind_cancel_requested(self, message: Message):
        """右键「取消提醒」：移除该消息的全部提醒并刷新标记。"""
        rid = message.room_id or self._active_room_id
        if not rid:
            return
        if not self._remove_reminders(rid, message.id or ""):
            return
        self._sync_remind_map()
        self.statusBar().showMessage("已取消提醒", 3000)

    def _remove_reminders(self, rid: str, msg_id: str) -> bool:
        """删配置里 rid+msg_id 的提醒；没有命中返回 False。"""
        from config import load_config, save_reminders
        try:
            reminders = load_config().get("reminders", [])
            keep = [r for r in reminders
                    if not (isinstance(r, dict) and r.get("rid") == rid
                            and r.get("msg_id") == msg_id)]
            if len(keep) == len(reminders):
                return False
            save_reminders(keep)
            return True
        except Exception:  # noqa: BLE001
            log.exception("移除消息提醒失败")
            return False

    def _sync_remind_map(self):
        """把未到期提醒刷进消息模型的时间行标记（msg_id -> 到期 epoch）。"""
        from config import load_config
        try:
            reminders = load_config().get("reminders", [])
        except Exception:  # noqa: BLE001
            return
        now = time.time()
        mapping = {}
        for r in reminders:
            if not isinstance(r, dict):
                continue
            due = float(r.get("due", 0) or 0)
            msg_id = str(r.get("msg_id", "") or "")
            if due > now and msg_id:
                mapping[msg_id] = due
        self.chat_area.chat_view.model.set_remind_map(mapping)

    def _reminder_rows(self) -> list:
        """提醒列表弹窗的数据：附带当前房间名（找不到用兜底文案）。"""
        from config import load_config
        try:
            reminders = load_config().get("reminders", [])
        except Exception:  # noqa: BLE001
            log.exception("读取消息提醒失败")
            return []
        rows = []
        for r in reminders:
            if not isinstance(r, dict):
                continue
            rid = str(r.get("rid", "") or "")
            conv = self._conversations.get(rid)
            rows.append({
                "rid": rid,
                "msg_id": str(r.get("msg_id", "") or ""),
                "text": str(r.get("text", "") or ""),
                "due": float(r.get("due", 0) or 0),
                "room_name": (conv.name if conv else "") or "未知房间",
            })
        rows.sort(key=lambda r: r["due"])
        return rows

    def _show_reminders_dialog(self):
        """托盘「消息提醒」：查看/取消未到期提醒，点击跳原消息。"""
        dlg = getattr(self, "_reminders_dialog", None)
        if dlg is None:
            from .reminders_dialog import RemindersDialog
            dlg = RemindersDialog(self.theme_manager, self)
            dlg.open_requested.connect(self._on_reminder_open)
            dlg.cancel_requested.connect(self._on_reminder_cancel)
            self._reminders_dialog = dlg
        dlg.set_reminders(self._reminder_rows())
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_reminder_open(self, rid: str, msg_id: str):
        """列表点击：关掉弹窗，走托盘 toast 同一条恢复/开会话/高亮链路。"""
        dlg = getattr(self, "_reminders_dialog", None)
        if dlg is not None:
            dlg.close()
        self._on_tray_message_activated(rid, msg_id)

    def _on_reminder_cancel(self, rid: str, msg_id: str):
        if self._remove_reminders(rid, msg_id):
            self._sync_remind_map()
        dlg = getattr(self, "_reminders_dialog", None)
        if dlg is not None:
            dlg.set_reminders(self._reminder_rows())
        self.statusBar().showMessage("已取消提醒", 3000)

    def _check_reminders(self, *_):
        """轮询到期提醒：toast（点击打开对应房间）+ 从配置中移除。"""
        from config import load_config, save_reminders
        try:
            reminders = load_config().get("reminders", [])
        except Exception:  # noqa: BLE001
            log.exception("读取消息提醒失败")
            return
        now = time.time()
        due = [r for r in reminders
               if isinstance(r, dict) and float(r.get("due", 0) or 0) <= now]
        if not due:
            return
        keep = [r for r in reminders if r not in due]
        try:
            save_reminders(keep)
        except Exception:  # noqa: BLE001  toast 照发，仅本轮删不掉
            log.exception("清理到期提醒失败")
        # 到期项消失，时间行的表盘标记要同步撤掉
        self._sync_remind_map()
        for r in due:
            rid = str(r.get("rid", "") or "")
            conv = self._conversations.get(rid)
            title = (conv.name if conv else "") or "消息提醒"
            text = str(r.get("text", "") or "") or "到你设置的提醒时间了"
            if self._tray is not None:
                self._tray.show_message_toast(title, text, rid,
                                              str(r.get("msg_id", "") or ""))
            else:
                self.statusBar().showMessage(f"提醒：{text}", 5000)

    def _toggle_message_flag(self, message: Message, attr: str, value: bool,
                             call, label: str):
        """置顶/星标的乐观切换；失败回滚并提示。"""
        if not self.client or not message or not message.id:
            return
        if message.id.startswith("local-"):
            return
        old = bool(getattr(message, attr, False))
        setattr(message, attr, bool(value))
        self._apply_local_message_update(message)
        msg_id = message.id

        def _fail(err):
            setattr(message, attr, old)
            self._apply_local_message_update(message)
            log.warning("%s消息失败 msg=%s：%s", label, msg_id, err)
            self.statusBar().showMessage(f"{label}失败：{err}", 4000)

        run_async(lambda: call(msg_id, bool(value)),
                  on_done=lambda _ok: None, on_fail=_fail, parent=self)

    def _show_message_list(self, kind: str):
        """标题栏「更多」→ 置顶/星标消息列表；点击条目跳转并高亮。"""
        conv = self._conversations.get(self._active_room_id or "")
        if not conv or not self.client:
            return
        dlg = MessageListDialog(self.client, conv.id, kind,
                                theme=self.theme_manager, parent=self)
        dlg.message_selected.connect(self._locate_message)
        self._message_list_dialog = dlg
        dlg.finished.connect(lambda _=0: setattr(
            self, "_message_list_dialog", None))
        dlg.exec_()

    def _on_send_mixed(self, parts):
        """图文混排发送：parts 为有序 [("text", str) | ("image"/"file", 本地路径)]。

        乐观显示一条本地占位消息，随即调 client.send_mixed：媒体逐个上传 +
        mediaConfirm，服务端为每个媒体生成原生文件/图片消息（上传记录永久
        保留；旧实现拼 markdown 正文，其 rooms.media URL 未确认、约 24 小时
        后被服务端清理，历史消息里的媒体全部 404，此前定位）。引用
        降级为正文前缀 `> 作者：原文`——媒体消息由服务端生成，带不了引用
        附件。占位行在 REST 响应/服务端回推回来时对账接管（见
        _reconcile_sent_local_echo / _adopt_server_message）。
        """
        if not self.client or not self._active_room_id:
            return
        rid = self._active_room_id
        quote = self.chat_area.input.quote_context
        quote_sender = quote.get("sender", "") if quote else ""
        quote_text = quote.get("text", "") if quote else ""
        parts = self._with_quote_prefix(parts, quote_sender, quote_text)
        text = self._mixed_markdown(parts)
        import uuid
        local_id = f"local-{uuid.uuid4().hex[:12]}"
        local = Message(
            id=local_id,
            room_id=rid, text=text,
            sender=self.client.me, is_mine=True,
            msg_type="text",
            status="sending",
            timestamp=time.time(),
        )
        self.chat_area.chat_view.append_message(local)
        self._room_messages.setdefault(rid, []).append(local)
        client = self.client

        def _done(sent):
            if sent:
                self.chat_area.chat_view.update_message_status(local_id, "sent")
                local.status = "sent"
                self._reconcile_sent_local_echo(local, sent)
                if local.id == sent.id:
                    # 占位行被改名接管：占位是 markdown 拼的，服务端回的是
                    # 原生媒体消息——只改 id 会缺附件字段、按错误类型绘制，
                    # 需整体采用服务端字段后重绘
                    self._adopt_server_message(local, sent)
                self._bump_conversation(rid, sent)
                self._schedule_receipts_fetch()
            else:
                self.chat_area.chat_view.update_message_status(local_id, "failed")
                local.status = "failed"

        def _fail(err):
            self.chat_area.chat_view.update_message_status(local_id, "failed")
            local.status = "failed"
            self.statusBar().showMessage(f"发送失败：{err}", 4000)

        run_async(lambda: client.send_mixed(rid, parts),
                  on_done=_done, on_fail=_fail, parent=self)

    def _with_quote_prefix(self, parts, sender: str, text: str) -> list:
        """把引用降级成混排正文的 markdown 前缀 `> 作者：原文`。

        混排的媒体消息由服务端 mediaConfirm 生成，而服务端引用附件只认
        chat.postMessage 正文首行的 permalink——媒体消息带不了引用卡片，
        文本前缀是两端（本应用/官方客户端）都认的引用块，保住语义。
        前缀并入第一段文本；没有文本段则新插一段在最前。
        """
        if not sender and not text:
            return parts
        line = ("> " + "：".join(x for x in (sender, text) if x)
                ).replace("\n", " ")
        out = list(parts)
        for i, (kind, payload) in enumerate(out):
            if kind == "text":
                out[i] = ("text", line + "\n" + payload)
                return out
        out.insert(0, ("text", line))
        return out

    def _mixed_markdown(self, parts) -> str:
        """把 parts 拼成一条 markdown 文本，仅用于发送前的本地乐观占位。

        图片/文件用本地路径临时拼接（只回显给自己看）；真正的发送由
        core.send_mixed 走上传 + mediaConfirm，服务端消息落地后占位行
        会被服务端字段整体接管（_adopt_server_message）。配置了上传服务
        时图片用内联 ![..]（与实际发送形态一致），否则用文件链接。
        """
        inline = bool(getattr(self.client, "upload_service_url", ""))
        body = []
        for kind, payload in parts:
            if kind == "text":
                if payload:
                    body.append(payload)
            elif kind == "file":
                # 文件不因图床配置而改成内联图，永远以文件链接呈现
                name = os.path.basename(payload)
                body.append(f"\n[{name}]({payload})\n")
            else:
                name = os.path.basename(payload)
                if inline:
                    # 内联图片的描述统一用固定 "picture"，不用文件名
                    body.append(f"\n![picture]({payload})\n")
                else:
                    body.append(f"\n[{name}]({payload})\n")
        return "\n".join(p for p in body if p)

    def _on_send_file(self, path: str):
        if not self.client or not self._active_room_id:
            return
        rid = self._active_room_id
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        # 非模态进度弹窗（无父独立顶层窗口，可切后台/取消）；持强引用防 GC，
        # destroyed 自动出集合。完成/失败经信号回状态栏（取消「已取消」不提示）。
        dlg = FileUploadDialog(
            self.client, rid, path, name,
            theme=self.theme_manager, center_on=self)
        self._upload_dialogs.add(dlg)
        dlg.destroyed.connect(
            lambda *_, s=self._upload_dialogs, d=dlg: s.discard(d))
        dlg.succeeded.connect(
            lambda p: self.statusBar().showMessage(f"已发送：{name}", 4000))
        dlg.failed.connect(
            lambda e: self.statusBar().showMessage(f"发送文件失败：{e}", 4000)
            if e != "已取消" else None)
        dlg.show()

    def _on_chat_view_drop(self, paths):
        """拖入本地文件到消息列表：与文件选择器同一条路径，逐个走 _on_send_file
        （非模态上传进度弹窗，可切后台/取消）。

        不再按图片/文件分流合并成混排消息，也不消费输入框内容——拖拽即发送，
        每个文件一个进度弹窗，取消按钮就是反悔的机会（替代旧的预确认框）。
        """
        if not self.client or not self._active_room_id or not paths:
            return
        for p in paths:
            self._on_send_file(p)

    # ------------------------------------------------------------------ #
    def _on_screenshot(self):
        """截图：最小化本窗口 → 抓主屏 → 全屏框选遮罩 → 选中区域插入输入框草稿。

        先最小化避免本窗口挡到要截的内容；抓屏后再显示遮罩，遮罩自身不会被
        抓进画面。用户拖拽框选（或 Esc/右键取消），结束后还原窗口。截图直接
        内联插入输入框光标处，随消息一起发送。
        """
        self.showMinimized()

        def _restore():
            self.showNormal()
            self.activateWindow()

        def _open():
            screen = QGuiApplication.primaryScreen()
            if screen is None:
                _restore()
                return
            pixmap = screen.grabWindow(0)
            if pixmap is None or pixmap.isNull():
                _restore()
                return
            overlay = ScreenshotOverlay(pixmap)
            self._capture_overlay = overlay   # 防局部引用被 GC

            def _done(img):
                self.chat_area.input.insert_screenshot_image(img)
                _restore()
                overlay.close()

            def _cancel():
                _restore()
                overlay.close()

            def _closed():
                self._capture_overlay = None

            def _ocr(path):
                # 标注态「识别文字」：成图已落临时 png，直接走离线识别；
                # 主窗口先还原，遮罩收尾（closed 已在 _ocr 内发出）
                _restore()
                overlay.close()
                self._ocr_token += 1
                self._run_ocr(path, self._ocr_token)

            overlay.selected.connect(_done)
            overlay.cancelled.connect(_cancel)
            overlay.closed.connect(_closed)
            overlay.ocr_requested.connect(_ocr)
            overlay.show_fullscreen()

        # 留出一帧让最小化动画/桌面稳定
        QTimer.singleShot(250, _open)

    # ------------------------------------------------------------------ #
    def _on_recall_message(self, message: Message):
        """撤回消息：异步调用 API，成功后替换 UI。"""
        if not self.client or not message.id:
            return
        rid = message.room_id or self._active_room_id
        msg_id = message.id
        client = self.client

        def _done(ok):
            if ok:
                self.chat_area.chat_view.replace_with_recalled(msg_id)
                # 从缓存中移除
                cached = self._room_messages.get(rid, [])
                self._room_messages[rid] = [m for m in cached if m.id != msg_id]

        def _fail(err):
            self.statusBar().showMessage(f"撤回失败：{err}", 4000)

        run_async(lambda: client.delete_message(rid, msg_id),
                  on_done=_done, on_fail=_fail, parent=self)

    def _on_quote_message(self, message: Message):
        """引用回复：在输入区显示引用预览条。

        文件/图片这类无 `text` 正文的消息用 display_text 占位（如「[文件] xxx」），
        保证引用块一定能进入发送流程而不是静默退化成普通文本。
        """
        sender_name = (message.sender.name or message.sender.username
                       if message.sender else "")
        text = message.text or message.display_text or ""
        if message.msg_type == "image" and text == "图片":
            # 「图片」是解析层的无说明占位；引用要带真实说明（官方图文
            # 混排在附件 description），走 display_text 拿到「[图片] 说明」
            text = message.display_text
        conv = self._conversations.get(message.room_id or "")
        room_type = conv.room_type if conv else self._room_type
        # permalink 用的房间名：频道 ASCII slug / 私聊对方 username；缺失则回落房间 id
        room_slug = (conv.slug or conv.username) if conv else ""
        self.chat_area.input.set_quote(
            sender_name, text, message.id,
            room_id=message.room_id or "", room_type=room_type,
            room_slug=room_slug)

    # ------------------------------------------------------------------ #
    # 引用消息：补抓被引用原消息 + 点击跳转
    # ------------------------------------------------------------------ #
    def _enrich_quotes(self, messages):
        """为引用消息补全原消息（作者/内容/附件字段，供跳转与保存原文件）。

        不能只补 description 为空的引用：附件自带内容的引用（网页版和
        本应用发的都属此类）看起来完整，但拿不到原消息的 file_url，
        右键保存和跳转就废了。每条引用每会话只补一次。
        """
        if not self.client:
            return
        for m in messages:
            if not (m.msg_type == "quote" and m.quote_msg_id):
                continue
            if m.id in self._quote_enriched:
                continue
            self._quote_enriched.add(m.id)
            mid = m.quote_msg_id
            orig = self._find_cached_message(m.room_id, mid)
            if orig is not None:
                self._apply_quote_source(m, orig)
            elif mid not in self._quote_fetching:
                self._quote_fetching.add(mid)
                client = self.client

                def _done(orig, mid=mid):
                    self._quote_fetching.discard(mid)
                    if not orig:
                        return  # 取不到则维持「引用消息」占位
                    self._on_quote_source_fetched(mid, orig)

                def _fail(err, mid=mid):
                    self._quote_fetching.discard(mid)
                    log.warning("补抓引用原消息失败 mid=%s：%s", mid, err)

                run_async(lambda mid=mid: client.get_message(mid),
                          on_done=_done, on_fail=_fail, parent=self)

    def _find_cached_message(self, rid: str, msg_id: str) -> Optional[Message]:
        for m in self._room_messages.get(rid, []):
            if m.id == msg_id:
                return m
        return None

    def _on_quote_source_fetched(self, mid: str, orig: Message):
        """原消息取回后，刷新缓存与视图中所有引用它的消息。

        不筛 description：附件自带内容的引用也需要回填附件字段。
        """
        for rid, msgs in self._room_messages.items():
            changed = []
            for m in msgs:
                if m.msg_type == "quote" and m.quote_msg_id == mid:
                    self._apply_quote_source(m, orig)
                    changed.append(m)
            if changed and rid == self._active_room_id:
                view = self.chat_area.chat_view
                for m in changed:
                    view.model.update_message(m)

    def _apply_quote_source(self, quote_msg: Message, orig: Message):
        quote_msg.quote_author = (orig.sender.name or orig.sender.username
                                  if orig.sender else "")
        quote_msg.description = orig.display_text
        # 原消息是文件/图片/音视频时带上附件字段，右键即可保存原文件
        if orig.msg_type in ("image", "file", "video", "audio") and orig.file_url:
            quote_msg.file_url = orig.file_url
            quote_msg.cover_url = orig.cover_url
            quote_msg.file_name = orig.file_name
            quote_msg.file_size = orig.file_size
        # 引用块媒体展示：图片/视频画缩略图，文件画图标 + 文件名（微信式）
        if orig.msg_type in ("image", "video"):
            quote_msg.quote_image_url = orig.cover_url or orig.file_url
        elif orig.msg_type == "file":
            quote_msg.quote_file_name = orig.file_name

    def _on_quote_jump(self, msg_id: str):
        """点击引用块：本会话内跳转高亮；跨房间的文件原消息直接弹保存
        （微信式：点引用拿到文件），其余仅提示。"""
        view = self.chat_area.chat_view
        if view.model.find_message_row(msg_id) >= 0:
            view.flash_row(msg_id)
            return
        if not self.client:
            return
        client = self.client

        def _done(orig):
            if not orig:
                self.statusBar().showMessage("原消息不存在或已删除", 4000)
            elif orig.room_id and orig.room_id == self._active_room_id:
                # 在本会话但尚未加载进视图（更早的历史）
                self.statusBar().showMessage(
                    "原消息在更早的历史记录中，向上滚动加载后即可查看", 4000)
            elif orig.file_url:
                view._save_attachment(orig)
            else:
                self.statusBar().showMessage(
                    "原消息在其他会话，暂不支持跨房间跳转", 4000)

        def _fail(err):
            log.warning("跳转引用取原消息失败 mid=%s：%s", msg_id, err)
            # 真机踩过：引用指向他人私聊时服务端返回 error-not-allowed，
            # 必须给用户可见提示，不能只留日志
            if "error-not-allowed" in str(err) or "unauthorized" in str(err):
                self.statusBar().showMessage("原消息所在会话无权访问", 4000)
            else:
                self.statusBar().showMessage(f"无法获取原消息：{err}", 4000)

        run_async(lambda: client.get_message(msg_id),
                  on_done=_done, on_fail=_fail, parent=self)

    def _on_forward_message(self, messages: list):
        """转发消息：先拉会话列表再弹选择对话框。"""
        if not self.client:
            return
        msgs = list(messages or [])
        if not msgs:
            return
        run_async(self.client.get_conversations,
                  on_done=lambda convs: self._open_forward_dialog(msgs, convs, "each"),
                  on_fail=lambda err: self.statusBar().showMessage(
                      f"加载会话失败：{err}", 4000),
                  parent=self)

    def _on_forward_many(self, payload: tuple):
        """多选转发：msgs + 模式（merge 合并 / each 逐条）。"""
        if not self.client:
            return
        msgs, mode = payload
        if not msgs:
            return
        run_async(self.client.get_conversations,
                  on_done=lambda convs: self._open_forward_dialog(list(msgs), convs, mode),
                  on_fail=lambda err: self.statusBar().showMessage(
                      f"加载会话失败：{err}", 4000),
                  parent=self)

    def _open_forward_dialog(self, messages: list, conversations, mode: str):
        """弹转发对话框。messages 可为多条，conversations 可多选目标。"""
        if not conversations:
            self.statusBar().showMessage("没有可转发的会话", 3000)
            return
        from .forward_dialog import ForwardDialog
        dlg = ForwardDialog(conversations, self, mode=mode,
                            theme=self.theme_manager)
        if dlg.exec_() != QDialog.Accepted or not dlg.selected_conversations:
            return
        targets = dlg.selected_conversations
        from .forward_dialog import build_forward_payload, build_forward_payload_many
        client = self.client
        resolver = self.image_loader.resolve

        # 构造每条消息对每个目标的发送任务
        def _do_send(room_id, text, attachments):
            return client.forward_message(room_id, text, attachments)

        if mode == "merge" and len(messages) > 1:
            # 合并转发可能拆成多条（汇总文本 + 逐条附件），逐条按目标入队
            sends = build_forward_payload_many(messages, resolver, merge=True)
            jobs = [(t.id, text, atts) for t in targets for text, atts in sends]
        elif mode == "merge":
            # 只有一条时合并与逐条等价
            text, attachments = build_forward_payload(messages[0], resolver)
            jobs = [(t.id, text, attachments) for t in targets]
        else:
            jobs = []
            for m in messages:
                text, attachments = build_forward_payload(m, resolver)
                for t in targets:
                    jobs.append((t.id, text, attachments))

        total = len(jobs)

        # 逐项发送；全部完成后统一提示一次
        state = {"done": 0}

        def _done(_ok):
            state["done"] += 1
            if state["done"] >= total:
                self.statusBar().showMessage(
                    f"已转发到 {len(targets)} 个会话，共 {total} 条", 3000)

        def _fail(err):
            self.statusBar().showMessage(f"转发失败：{err}", 4000)

        for room_id, text, attachments in jobs:
            run_async(lambda rid=room_id, t=text, a=attachments: _do_send(rid, t, a),
                      on_done=_done, on_fail=_fail, parent=self)

    def _on_profile_requested(self, message: Message, anchor):
        """点消息头像：在头像下方弹出资料卡，点其它位置自动关闭。"""
        from .profile_popup import ProfileCard
        user = message.sender
        card = ProfileCard(user, self)
        card.dm_requested.connect(self._on_dm_requested)
        if user.avatar_url and self.avatar_loader:
            self.avatar_loader.load(card.avatar, user.avatar_url)
        if self.client:
            card.enrich(self.client)
        screen = QDesktopWidget().availableGeometry(card)
        pos = anchor + QPoint(0, 6)
        pos.setX(max(screen.left(),
                     min(pos.x(), screen.right() - card.width() + 1)))
        pos.setY(max(screen.top(),
                     min(pos.y(), screen.bottom() - card.height() + 1)))
        card.move(pos)
        card.show()

    def _on_mention_clicked(self, username: str, anchor=None):
        """点击 @提及：弹出对应用户的资料卡。"""
        if not username or not self.client:
            return
        from core.models import User
        from .profile_popup import ProfileCard

        def _done(info):
            if not info:
                return
            # status 不在 User 字段里，卡片随后由 _apply_info(info) 补上
            user = User(
                username=info.get("username", username),
                name=info.get("name", ""),
                avatar_url="",
            )
            card = ProfileCard(user, self)
            card.dm_requested.connect(self._on_dm_requested)
            card._apply_info(info)
            screen = QDesktopWidget().availableGeometry(card)
            # 卡片放到 @ 所在消息行的正下方，左边与 @ 字符左缘对齐；
            # 行下方放不下时转到行上方；无锚点时回退到屏幕居中
            if anchor is not None:
                x = anchor.x()
                y = anchor.bottom() + 6
                x = min(max(screen.left(), x), screen.right() - card.width())
                if y + card.height() > screen.bottom():
                    y = anchor.top() - card.height() - 6
                y = max(screen.top(), y)
                card.move(x, y)
            else:
                cx = screen.width() // 2 - card.width() // 2
                cy = screen.height() // 2 - card.height() // 2
                card.move(max(screen.left(), cx), max(screen.top(), cy))
            card.show()

        def _fail(err):
            log.debug("获取提及用户资料失败 %s：%s", username, err)

        from .api_workers import run_async
        run_async(lambda: self.client.fetch_user_info(username),
                  on_done=_done, on_fail=_fail, parent=self)

    # ------------------------------------------------------------------ #
    # 标题栏「更多」菜单：开关房间 E2E / 查看房间信息
    # ------------------------------------------------------------------ #
    def _on_room_menu(self, *_):
        """「更多」按钮：弹出房间菜单。菜单每次现建，文案读会话对象的
        当前 encrypted，开/关后无需主动刷新。"""
        conv = self._conversations.get(self._active_room_id)
        if not conv or not self.client:
            return
        from .icons import icon
        menu = QMenu(self)
        act_e2e = menu.addAction(
            icon("e2e_on" if conv.encrypted else "e2e_off", 16),
            "关闭端到端加密" if conv.encrypted else "开启端到端加密")
        # 真机实测：服务端仅私有群(p)/私聊(d)支持加密，
        # 公开频道(t=c)一律拒绝 error-action-not-allowed → 置灰；
        # 私有群仍要求本人创建（房主权限）
        can_e2e = conv.room_type == "d" or (
            conv.room_type == "p" and conv.i_created)
        act_e2e.setEnabled(can_e2e)
        act_e2e.triggered.connect(
            lambda _=False, c=conv: self._toggle_room_e2e(c))
        act_starred = menu.addAction(icon("favorite", 16), "星标消息")
        act_starred.triggered.connect(
            lambda _=False: self._show_message_list("starred"))
        act_pinned = menu.addAction(icon("pin", 16), "置顶消息")
        act_pinned.triggered.connect(
            lambda _=False: self._show_message_list("pinned"))
        act_info = menu.addAction(icon("about", 16), "查看房间信息")
        act_info.triggered.connect(
            lambda _=False, c=conv: self._show_room_info(c))
        menu.exec_(self.chat_area.menu_btn.mapToGlobal(
            QPoint(0, self.chat_area.menu_btn.height())))

    def _toggle_room_e2e(self, conv, *_):
        """开关房间端到端加密：乐观翻转 + 失败回滚（不做确认弹窗）。

        成功后必须失效 E2E 房间密钥缓存：开关加密服务端会重置房间密钥，
        旧缓存会把新密文按旧密钥解，必然失败。
        """
        is_dm = conv.room_type == "d"
        if not is_dm and (conv.room_type != "p" or not conv.i_created):
            return
        room_id = conv.id
        new_val = not conv.encrypted
        conv.encrypted = new_val

        def _task():
            return self.client.set_room_e2e(room_id, new_val)

        def _ok(_result):
            if self.client and self.client.e2e:
                self.client.e2e.invalidate_room(room_id)
            if room_id == self._active_room_id:
                self.chat_area.chat_view.set_room_encrypted(bool(conv.encrypted))
            # 订阅就绪前的开关系统消息服务端不补发（真机实测
            # 开启消息比订阅生效早 3 秒导致只显示一条），重拉一页按 id 合并补显
            self._refresh_room_history_tail(room_id, conv.room_type)

        def _fail(e):
            conv.encrypted = not new_val  # 回滚
            log.exception("设置房间加密失败：%s", e)

        run_async(_task, on_done=_ok, on_fail=_fail, parent=self)

    def _refresh_room_history_tail(self, rid: str, room_type: str):
        """轻量重拉最近一页历史，补回订阅前漏推的消息（如开关加密系统消息）。"""
        client = self.client

        def _task():
            return client.get_history(rid, self._page_size, 0, room_type)

        def _done(msgs):
            try:
                self._merge_history_tail(rid, msgs)
            except Exception:  # noqa: BLE001
                log.exception("开关加密后合并历史失败 rid=%s", rid)

        def _fail(e):
            log.warning("开关加密后重拉历史失败 rid=%s：%s", rid, e)

        run_async(_task, on_done=_done, on_fail=_fail, parent=self)

    def _merge_history_tail(self, rid: str, msgs):
        """把重拉的一页（新->旧）按 id 合并进缓存（旧->新）与当前视图。

        只有比视图末尾更新的缺失消息才追加，更早的缺口留给翻页/重建，
        避免把旧消息错插到尾部。"""
        cache = self._room_messages.setdefault(rid, [])
        have = {c.id for c in cache}
        missing = [m for m in reversed(msgs) if m.id and m.id not in have]
        if not missing:
            return
        # 缓存统一时间正序（旧->新）；稳定排序保住同时间戳的既有相对次序
        cache[:] = sorted(cache + missing, key=lambda m: m.timestamp)
        self._resolve_system_names(missing)
        if rid != self._active_room_id:
            return
        model = self.chat_area.chat_view.model
        for m in missing:
            rows = model.rowCount()
            last = model.get_message(rows - 1) if rows else None
            if last and m.timestamp < last.timestamp:
                continue
            model.append_message(m)

    def _show_room_info(self, conv, *_):
        """查看房间信息：私聊弹用户资料卡，频道/团队弹房间信息卡。"""
        btn = self.chat_area.menu_btn
        anchor = btn.mapToGlobal(QPoint(0, btn.height()))
        screen = QDesktopWidget().availableGeometry(self)
        if conv.room_type == "d":
            if not conv.username:
                return
            from core.models import User
            from .profile_popup import ProfileCard
            user = User(username=conv.username, name=conv.name,
                        avatar_url=conv.avatar_url)
            card = ProfileCard(user, self)
            card.dm_requested.connect(self._on_dm_requested)
            if user.avatar_url and self.avatar_loader:
                self.avatar_loader.load(card.avatar, user.avatar_url)
            if self.client:
                card.enrich(self.client)
        else:
            from .room_info_popup import RoomInfoCard
            card = RoomInfoCard(conv, self.theme_manager, self)
            if self.avatar_loader and conv.avatar_url:
                self.avatar_loader.load(card.avatar, conv.avatar_url)
            if self.client:
                card.enrich(self.client, conv.id)
        pos = anchor + QPoint(0, 6)
        pos.setX(max(screen.left(),
                     min(pos.x(), screen.right() - card.width() + 1)))
        pos.setY(max(screen.top(),
                     min(pos.y(), screen.bottom() - card.height() + 1)))
        card.move(pos)
        card.show()

    def _on_dm_requested(self, username: str):
        """从资料卡点击「发送消息」：创建/查找 DM 房间并切换。"""
        if not username or not self.client:
            return
        client = self.client

        def _done(room_id):
            if not room_id:
                return
            for conv in self._conversations.values():
                if conv.id == room_id:
                    self._open_conversation(conv)
                    return

            def _reload(convs):
                self._conversations = {c.id: c for c in convs}
                self.conversation_list.set_conversations(convs)
                for c in convs:
                    if c.id == room_id:
                        self._open_conversation(c)
                        return

            def _reload_fail(err):
                log.debug("重新加载会话列表失败：%s", err)

            run_async(client.get_conversations, on_done=_reload,
                      on_fail=_reload_fail, parent=self)

        def _fail(err):
            self.statusBar().showMessage(f"创建私聊失败：{err}", 4000)

        run_async(lambda: client.create_dm(username),
                  on_done=_done, on_fail=_fail, parent=self)

    def _on_search_changed(self, text: str):
        """顶部搜索框实时改动：按会话名/用户名/聊天记录内容过滤会话列表。

        聊天记录内容取自 `self._room_messages`（已打开/实时收到的消息缓存），
        拼成每会话一份小写全文索引传给列表做过滤。
        """
        text = (text or "").strip()
        if not text:
            self.conversation_list.set_search_filter("")
            return
        index = {}
        for rid, conv in self._conversations.items():
            parts = []
            for m in self._room_messages.get(rid, []):
                t = m.text or ""
                if t:
                    parts.append(t)
            if parts:
                index[rid] = " ".join(parts).lower()
        self.conversation_list.set_search_filter(text, index)

    def _search_messages(self):
        """搜索框回车：在当前打开的会话内按关键字搜消息并跳转高亮。

        会话列表已由 textChanged 实时过滤；回车做**消息级**搜索，命中即
        跳转到对应消息（复用 chat_view.flash_row 高亮）。
        """
        text = (self.search_edit.text() or "").strip()
        rid = self._active_room_id
        if not text or not rid or not self.client:
            return
        client = self.client

        def _done(results):
            if not results:
                self.statusBar().showMessage("未找到匹配的消息", 3000)
                return
            msg = results[0]
            view = self.chat_area.chat_view
            if msg.id and view.model.find_message_row(msg.id) >= 0:
                view.flash_row(msg.id)
            else:
                # 命中在更早的历史里，向上滚动加载后再尝试定位
                self.load_around(msg.id, msg.room_id or rid)

        def _fail(err):
            self.statusBar().showMessage(f"搜索消息失败：{err}", 4000)

        run_async(lambda: client.search_messages(rid, text),
                  on_done=_done, on_fail=_fail, parent=self)

    def load_around(self, msg_id: str, rid: str):
        """跳转到某条消息：若不在当前视图，重拉该房间历史后定位高亮。"""
        if not self.client or rid != self._active_room_id:
            return
        client = self.client

        def _done(msgs):
            if not msgs:
                self.statusBar().showMessage("未找到该消息", 3000)
                return
            self._room_messages[rid] = list(reversed(msgs))
            self._history_offset[rid] = len(msgs)
            view = self.chat_area.chat_view
            view.rebuild_messages(self._room_messages[rid])
            view.flash_row(msg_id)

        def _fail(err):
            self.statusBar().showMessage(f"加载失败：{err}", 4000)

        run_async(lambda: client.get_history(rid, count=self._page_size,
                                             offset=0, room_type=self._room_type),
                  on_done=_done, on_fail=_fail, parent=self)

    # ------------------------------------------------------------------ #
    # 会话列表顶部「新建」：私聊 / 频道 / 团队 / 讨论串
    # ------------------------------------------------------------------ #
    def _show_create_menu(self, *_):
        """搜索框旁的「+」按钮：弹出新建房间菜单。"""
        if not self.client:
            return
        from .icons import icon
        menu = QMenu(self)
        menu.addAction(icon("send", 16), "发起私聊",
                       self._create_room_dm)
        menu.addSeparator()
        menu.addAction(icon("general", 16), "新建频道",
                       lambda: self._create_room_channel())
        menu.addAction(icon("leave", 16), "新建团队",
                       lambda: self._create_room_team())
        menu.addAction(icon("quote", 16), "新建讨论串",
                       self._create_room_discussion)
        menu.exec_(self.new_room_btn.mapToGlobal(
            QPoint(0, self.new_room_btn.height())))

    def _reload_and_open(self, room_id: str, flash_msg_id: str = ""):
        """建房间成功后重拉列表并把新房间切到前台。

        flash_msg_id 用于托盘预览点击的新会话：打开后等模型就绪高亮消息。
        """
        if not room_id:
            return
        client = self.client

        def _reload(convs):
            if convs is None:
                return
            self._conversations = {c.id: c for c in convs}
            self.conversation_list.set_conversations(convs)
            for c in convs:
                if c.id == room_id:
                    self._open_conversation(c)
                    if flash_msg_id:
                        self._flash_when_ready(c.id, flash_msg_id)
                    return
            if flash_msg_id:
                log.debug("托盘跳转：重拉列表后仍无房间 %s", room_id)

        def _fail(err):
            log.debug("建房间后重拉列表失败：%s", err)

        run_async(client.get_conversations, on_done=_reload,
                  on_fail=_fail, parent=self)

    def _create_room_channel(self):
        """创建频道：公开走 channels.create，私有走 groups.create。"""
        if not self.client:
            return
        from .create_room_dialogs import CreateRoomDialog
        theme = self.theme_manager
        client = self.client
        dlg = CreateRoomDialog("channel", theme, self,
                               search_fn=client.search_users)
        if dlg.exec_() != QDialog.Accepted or not dlg.result_name:
            return
        name, topic = dlg.result_name, dlg.result_topic
        members = list(dlg.result_members)
        private = dlg.result_private

        def _done(room_id):
            self._reload_and_open(room_id)

        def _fail(err):
            self.statusBar().showMessage(f"新建频道失败：{err}", 4000)

        run_async(lambda: (client.create_group(name, topic, members)
                           if private else
                           client.create_channel(name, topic, members)),
                  on_done=_done, on_fail=_fail, parent=self)

    def _create_room_team(self):
        """创建团队：统一走 teams.create（公开/私有由对话框配置）。"""
        if not self.client:
            return
        from .create_room_dialogs import CreateRoomDialog
        theme = self.theme_manager
        client = self.client
        dlg = CreateRoomDialog("team", theme, self,
                               search_fn=client.search_users)
        if dlg.exec_() != QDialog.Accepted or not dlg.result_name:
            return
        name, topic = dlg.result_name, dlg.result_topic
        members = list(dlg.result_members)
        private = dlg.result_private

        def _done(room_id):
            self._reload_and_open(room_id)

        def _fail(err):
            self.statusBar().showMessage(f"新建团队失败：{err}", 4000)

        run_async(lambda: client.create_team(name, topic, members,
                                             private=private),
                  on_done=_done, on_fail=_fail, parent=self)

    def _create_room_dm(self):
        """发起私聊。"""
        if not self.client:
            return
        from .create_room_dialogs import CreateDmDialog
        theme = self.theme_manager
        client = self.client
        dlg = CreateDmDialog(theme, self, search_fn=client.search_users)
        if dlg.exec_() != QDialog.Accepted or not dlg.username:
            return
        username = dlg.username

        def _done(room_id):
            self._reload_and_open(room_id)

        def _fail(err):
            self.statusBar().showMessage(f"发起私聊失败：{err}", 4000)

        run_async(lambda: client.create_dm(username),
                  on_done=_done, on_fail=_fail, parent=self)

    def _create_room_discussion(self):
        """创建讨论串（挂在某个频道/团队下）。"""
        if not self.client:
            return
        from .create_room_dialogs import CreateDiscussionDialog
        theme = self.theme_manager
        convs = list(self._conversations.values())
        dlg = CreateDiscussionDialog(convs, theme, self)
        if dlg.exec_() != QDialog.Accepted or not dlg.result_name:
            return
        parent_id, name = dlg.result_parent_id, dlg.result_name
        client = self.client

        def _done(room_id):
            self._reload_and_open(room_id)

        def _fail(err):
            self.statusBar().showMessage(f"新建讨论串失败：{err}", 4000)

        run_async(lambda: client.create_discussion(parent_id, name),
                  on_done=_done, on_fail=_fail, parent=self)

    def _on_image_view_requested(self, message: Message):
        """点击图片消息：弹出原图查看器，可左右键翻看本会话其它图片。"""
        from .image_viewer import ImageViewer
        url = message.file_url or message.cover_url or ""
        if not url:
            return
        # 用当前会话已加载的消息拼浏览列表：不额外请求，翻页顺序与聊天一致
        urls = []
        index = -1
        for msg in self._room_messages.get(self._active_room_id) or []:
            if msg.msg_type != "image":
                continue
            m_url = msg.file_url or msg.cover_url or ""
            if not m_url:
                continue
            if msg.id == message.id:
                index = len(urls)
            urls.append(m_url)
        if index < 0:
            # 缓存尚未落地（实时追加与点击竞态）：按 URL 找，找不到就补进去
            try:
                index = urls.index(url)
            except ValueError:
                urls.append(url)
                index = len(urls) - 1
        if not urls:
            urls = [url]
        viewer = ImageViewer(url, self.image_loader, self,
                             urls=urls, index=index)
        viewer.ocr_requested.connect(self._on_ocr_requested)
        viewer.show()

    def _on_inline_image_view_requested(self, src: str):
        """点击 markdown 内联图片：弹出原图查看器。"""
        from .image_viewer import ImageViewer
        if not src:
            return
        viewer = ImageViewer(src, self.image_loader, self)
        viewer.ocr_requested.connect(self._on_ocr_requested)
        viewer.show()

    # ------------------------------------------------------------------ #
    def _on_realtime_message(self, message: Message):
        """收到实时消息：刷新会话列表 + 若在当前会话则缓冲后批量渲染。"""
        if not self.client:
            return
        # 同 id 再次推送 = 编辑/表情回应/置顶星标等更新，不是新消息。必须
        # 在未读与提醒之前判定：否则每编辑一次都会未读 +1、弹 toast，列表
        # 还会被拽到底部（编辑旧消息时尤其明显）。
        existing = self._find_realtime_existing(message)
        if existing is not None:
            self._apply_realtime_update(message, existing)
            return
        # 先记下实际收到的最新消息：即便会话列表尚未落地（启动窗口），
        # 整表到达时也能把滞后的服务端摘要盖掉
        self._note_latest_message(message)
        # 更新会话最后消息
        conv = self._conversations.get(message.room_id)
        if conv is None:
            # 新会话（可能是新私聊）：先记托盘提醒再重拉列表（此时还没有
            # 房间名，卡片显示「新会话」）
            if self._add_tray_alert(message, ""):
                self._refresh_tray_state(flash=True)
            self._load_conversations()
            return
        conv.last_message_text = self._conv_preview(message, conv)
        conv.last_message_time = message.timestamp
        # 非当前会话则未读数+1；@我 的再叠一个徽标计数（订阅推送会覆盖成
        # 服务端权威值，这里先本地累加以免徽标等到下一次整表刷新才出现）
        if message.room_id != self._active_room_id and not message.is_mine:
            conv.unread += 1
            if message.mentions_me or message.mention_all:
                conv.mention_count += 1
        self.conversation_list.upsert_conversation(conv)
        # 自己消息回推 / 系统消息 / 正在看的会话不会产生提醒，返回值决定
        # 要不要闪（避免一条无关推送把历史未读重新闪起来）
        added = self._add_tray_alert(message, conv.name)
        self._refresh_tray_state(flash=added)
        # 自己刚发的消息被服务端回推：它是乐观回显那条，不是新消息——
        # 若仍以 local- 假 id 挂在视图里，就原位接管 id 而不重复追加
        # （若 _done 已把 local 改成真实 id，则落到 append_message 的按 id 去重）。
        if message.is_mine and self._absorb_own_echo(message):
            return
        # 当前会话则缓冲到队列，由定时器批量渲染（False = 新增，需要滚动）
        if message.room_id == self._active_room_id:
            self._pending_realtime_messages.append((message, False))
            self._realtime_flush_timer.start()
        # 写入缓存（后续切换回该会话可直接重建视图）。按 id 去重：吸收失败
        # 但视图层已按同 id 原位接管时（如 _done 先改名的竞态），这里再无条件
        # append 会让缓存出现两份同 id，切回房间重建成双条
        cache = self._room_messages.setdefault(message.room_id, [])
        if all(c.id != message.id for c in cache):
            cache.append(message)

    def _find_realtime_existing(self, message: Message) -> Optional[Message]:
        """在缓存与当前视图里按 id 找同一条消息，区分新增与更新。

        服务端的编辑/表情回应/置顶星标都表现为「同 id、新 _updatedAt」
        的整条重推（_is_replay 只吃完全相同的重复帧），所以 id 命中即
        说明这是原位更新。
        """
        for m in self._room_messages.get(message.room_id, []):
            if m.id == message.id:
                return m
        view = getattr(self.chat_area, "chat_view", None)
        model = view.model if view else None
        if model is not None:
            row = model.find_message_row(message.id)
            if row >= 0:
                return model.get_message(row)
        return None

    def _apply_realtime_update(self, message: Message, existing: Message):
        """同 id 重推：原位替换内容，不产生未读/提醒/滚动。

        缓存必须换成新对象，否则切房重建会退回编辑前的旧内容；左侧预览
        只在这条确实是房间最后一条时才跟着改（编辑旧消息不该把列表摘要
        改成旧内容）。
        """
        cache = self._room_messages.get(message.room_id)
        if cache is not None:
            for i, c in enumerate(cache):
                if c is existing or c.id == message.id:
                    cache[i] = message
                    break
            else:
                # 视图里有、缓存里没有（如 _done 已改名而缓存被清理过）：
                # 只在时间顺序允许时补回去，别把旧消息塞到缓存尾部
                if not cache or message.timestamp >= cache[-1].timestamp:
                    cache.append(message)
        conv = self._conversations.get(message.room_id)
        latest = self._latest_by_room.get(message.room_id)
        latest_ts = latest[0] if latest else (
            conv.last_message_time if conv else 0.0)
        if message.timestamp >= latest_ts > 0:
            self._latest_by_room[message.room_id] = (
                message.timestamp, message)
            if conv is not None:
                conv.last_message_text = self._conv_preview(message, conv)
                conv.last_message_time = message.timestamp
                self.conversation_list.upsert_conversation(conv)
        if message.room_id == self._active_room_id:
            self._pending_realtime_messages.append((message, True))
            self._realtime_flush_timer.start()

    def _absorb_own_echo(self, message: Message) -> bool:
        """把刚发送的消息的服务端回推并入视图里的本地乐观回显。

        发送时插入的是 local- 假 id（见 _on_send_text），服务端回推的是
        真实 id，两路各自插入会导致"发一条显示两条"。这里按
        （room + is_mine + msg_type + 文本相同）命中仍挂着 local- id 的那条，
        原位把 id/状态更新为服务端版本，并保持一致，从而不重复追加。
        没命中（本地那条已被 _done 改名）返回 False，走 append_message 去重。
        """
        view = getattr(self.chat_area, "chat_view", None)
        model = view.model if view else None
        if model is None:
            return False
        for m in model.all_messages():
            if (m.id.startswith("local-")
                    and m.room_id == message.room_id
                    and m.msg_type == message.msg_type
                    and m.is_mine
                    and m.text == message.text):
                # 用服务端真实字段接管本地回显，保持同一条消息。
                # sender 也要换成服务端解析版：本地回显的 sender 是 client.me，
                # 旧登录路径下它没有 avatar_url，吸收后仍会画成首字占位
                m.id = message.id
                m.timestamp = message.timestamp
                m.sender = message.sender
                m.status = "sent"
                # 服务端可能带回本地没有的字段（@提及、编辑时间、星标等），
                # 不一并接管的话切房重建会丢掉这些状态
                m.mentions = message.mentions
                m.mentions_me = message.mentions_me
                m.mention_all = message.mention_all
                m.edited_at = message.edited_at
                m.starred = message.starred
                m.pinned = message.pinned
                m.reactions = message.reactions
                model.update_message(m)
                # 缓存与视图按引用持有同一个对象，原位改完两边自然一致；
                # 若缓存里恰好是服务端那条同 id 对象，也统一成 m，防止
                # 切回房间时重建出两份
                cache = self._room_messages.get(message.room_id)
                if cache is not None:
                    for i, c in enumerate(cache):
                        if c is m or c.id == message.id:
                            cache[i] = m
                            break
                return True
        return False

    def _reconcile_sent_local_echo(self, local, sent):
        """REST 响应归来时对账，兜住"发一条显示两条"的最后一种竞态。

        服务端回推可能先于 REST 响应被渲染成真实 id 的行——含文件/图片的
        混排消息本地与服务端 text 必不同（本地占位是本地路径，服务端是
        上传 URL），_absorb_own_echo 按 text 全等匹配吸收不到。此时视图里
        已有服务端行，本地回显行成了重复：移除本地行，让服务端行作唯一
        显示；服务端行尚未渲染则照旧改名，后续同 id 回推由 append_message
        原位去重。
        """
        if not sent.id:
            return
        model = self.chat_area.chat_view.model
        if any(m is not local and m.id == sent.id
               for m in model.all_messages()):
            model.remove_message(local.id)
            cache = self._room_messages.get(local.room_id)
            if cache:
                self._room_messages[local.room_id] = [
                    c for c in cache if c is not local]
            return
        local.id = sent.id

    def _adopt_server_message(self, local, sent):
        """把服务端消息的渲染字段整体贴到本地占位行上并重绘。

        混排发送的占位行是 markdown 拼的（text 含本地路径、无附件字段），
        服务端回的是原生文件/图片消息（caption 作正文、attachments 齐备）。
        只改 id 不换字段，气泡会继续按 markdown 文本绘制且附件缺失（媒体
        不显示）。本地对象被视图与缓存按引用持有，只能原位改字段。
        """
        local.text = sent.text
        local.msg_type = sent.msg_type
        local.attachments = sent.attachments
        local.file_url = sent.file_url
        local.cover_url = sent.cover_url
        local.file_name = sent.file_name
        local.file_size = sent.file_size
        local.description = sent.description
        local.quote_msg_id = sent.quote_msg_id
        local.quote_author = sent.quote_author
        local.quote_link = sent.quote_link
        local.quote_image_url = sent.quote_image_url
        local.quote_file_name = sent.quote_file_name
        local.mentions = sent.mentions
        if sent.sender:
            local.sender = sent.sender
        if sent.timestamp:
            local.timestamp = sent.timestamp
        self.chat_area.chat_view.model.update_message(local)

    def _flush_realtime_messages(self):
        """批量渲染缓冲区内的实时消息，减少 UI 刷新次数。"""
        if not self._pending_realtime_messages:
            return
        pending = self._pending_realtime_messages
        self._pending_realtime_messages = []
        fresh = [m for m, is_update in pending if not is_update]
        if fresh:
            self._enrich_quotes(fresh)
            self._resolve_system_names(fresh)
        view = self.chat_area.chat_view
        # 用户向上翻历史时不能把视图拽回底部，改为底部提示条；自己发的
        # 消息与本来就在底部时仍照旧自动滚动
        at_bottom = view.is_near_bottom()
        appended = False
        own_appended = False
        new_other = 0
        view.setUpdatesEnabled(False)
        try:
            for message, is_update in pending:
                if is_update:
                    # 行可能已被切房/重建移除：缓存已是新值，不再补插
                    if view.model.find_message_row(message.id) >= 0:
                        view.update_message(message)
                    continue
                # 同 id 已在屏上（吸收竞态）时模型会原位替换，不重复追加；
                # 但只要出现过一次真正的追加就滚到底部
                before = view.model.rowCount()
                view.append_message(message)
                if view.model.rowCount() > before:
                    appended = True
                    if message.is_mine:
                        own_appended = True
                    else:
                        new_other += 1
        finally:
            view.setUpdatesEnabled(True)
        if appended:
            if own_appended or at_bottom:
                self.chat_area.clear_new_messages()
                view.scroll_to_bottom(deferred=True)
            elif new_other:
                self.chat_area.show_new_messages(new_other)
            # 当前会话正在看：已读位置随新消息推进
            self._mark_room_last_read(self._active_room_id)
            # 有人刚在当前房间发言，多半也读到了自己的消息——立刻拉回执，
            # 不用等 30s 定时（服务端不推回执事件，这是唯一能缩短延迟的时机）
            self._schedule_receipts_fetch()

    def _preview_from_message(self, message: Message) -> str:
        # 托盘提醒用（toast 自己拼发送人），不带前缀；规则收口在 core
        return preview_text(message)

    def _conv_preview(self, message: Message,
                      conv: Optional[Conversation] = None) -> str:
        """会话列表预览：频道/群和多人私聊带发送人，一对一私聊不带。"""
        if conv is None:
            conv = self._conversations.get(message.room_id)
        one_to_one = bool(conv is not None and conv.room_type == "d"
                          and not conv.is_multi_dm)
        return preview_with_sender(message, one_to_one)

    def _bump_conversation(self, room_id: str, message: Message):
        self._note_latest_message(message)
        conv = self._conversations.get(room_id)
        if conv:
            conv.last_message_text = self._conv_preview(message, conv)
            conv.last_message_time = message.timestamp
            self.conversation_list.upsert_conversation(conv)

    def _note_latest_message(self, message: Message) -> None:
        """记录房间实际见过的最后一条消息（只前进不后退）。

        左侧预览来自服务端 rooms.get 的 lm/lastMessage，真机实测会滞后
        （lm 停在 16:42:51，history 已有 16:45 的消息）。
        会话列表整表重拉时用这张表兜底，不能被过期摘要顶回去。
        """
        prev = self._latest_by_room.get(message.room_id)
        if prev and message.timestamp <= prev[0]:
            return
        self._latest_by_room[message.room_id] = (
            message.timestamp, message)

    def _apply_known_latest(self, conv: Conversation) -> bool:
        """把已知最新尾部盖到 conv 上（仅当它比服务端摘要新）。"""
        ent = self._latest_by_room.get(conv.id)
        if not ent or ent[0] <= conv.last_message_time:
            return False
        conv.last_message_time = ent[0]
        conv.last_message_text = self._conv_preview(ent[1], conv)
        return True

    def _sync_conversation_from_history(self, room_id: str, msgs):
        """用实际加载到的最后一条消息修正左侧预览与排序。

        左侧预览来自 rooms.get 的 lm/lastMessage，服务端摘要可能滞后
        （真机实测 lm 停在 16:42:51，而 channels.history
        已有 16:43 与 16:45 的消息），只信 lm 会让左侧与聊天区长期
        不一致。历史是真实消息来源；启动时历史常先于会话列表返回，
        此时 _conversations 里还没有该房间，也要先记入 _latest_by_room，
        待整表落地时再盖回去。
        """
        if not msgs:
            return
        self._note_latest_message(msgs[-1])
        conv = self._conversations.get(room_id)
        if conv is not None and self._apply_known_latest(conv):
            self.conversation_list.upsert_conversation(conv)

    # ------------------------------------------------------------------ #
    # 已读回执：按需 REST 拉取（打开会话/新消息/30s 定时），服务端不支持
    # 时自动降级为仅发送状态图标。实时流订阅不做（魔改服务器风险高）。
    # ------------------------------------------------------------------ #
    def _schedule_receipts_fetch(self):
        if not self.client or self._receipts_degraded:
            return
        # 稍等一拍：让服务端先把刚发送/回推的消息落库，否则查不到回执
        QTimer.singleShot(600, self._fetch_receipts_for_active_room)

    def _fetch_receipts_for_active_room(self):
        """拉当前会话最后一条消息的回执（更早消息靠单调外推）。

        目标链：最后一条 + 最多 2 条更早候选。最后一条的回执若为空
        （剔除自己后没人读过它，李肖房间实测场景），空名单对更早的
        消息不含单调信息，必须补拉更早一条才有数据外推；链上后续项
        只在前一项名单为空时才消费，常规场景仍是一次请求。"""
        if not self.client or self._receipts_degraded:
            return
        rid = self._active_room_id
        if not rid or rid in self._receipts_fetching:
            return
        view = self.chat_area.chat_view
        if view._current_room_id != rid:
            return
        # 超大群不显示未读数（UNREAD_COUNT_LIMIT），回执也不拉
        if view.model._member_count > UNREAD_COUNT_LIMIT:
            return
        chain = []
        for m in reversed(view.model.all_messages()):
            # 不限于自己消息：最后一条若是别人发的，其回执同样提供最新
            # 已读状态（房间级单调），且别人消息行自己也要显示未读数
            if (m.msg_type != "system"
                    and not m.id.startswith("local-")):
                chain.append(m)
                if len(chain) >= 3:
                    break
        if not chain:
            return
        self._receipts_fetching.add(rid)
        self._fetch_receipts_chain(rid, chain)

    def _fetch_receipts_chain(self, rid: str, chain):
        """顺序拉取 chain[0] 的回执；名单为空且链上还有候选时继续往前
        补拉（补拉期间保持 _receipts_fetching 在途去重）。"""
        client = self.client
        msg = chain[0]

        def _proc():
            try:
                return client.get_read_receipts(msg.id)
            except ReceiptsUnsupportedError as e:
                # run_async 的 fail 回调只带字符串，类型分类必须在 _proc 内完成
                return {"unsupported": str(e)}

        def _done(result):
            self._receipts_fetching.discard(rid)
            if self._active_room_id != rid:
                return  # 已切走
            if isinstance(result, dict) and result.get("unsupported"):
                self._receipts_degraded = True
                self.chat_area.chat_view.model.set_receipts_supported(False)
                log.info("服务器不支持已读回执，降级为仅发送状态：%s",
                         result["unsupported"])
                return
            receipts = self._drop_own_receipt(result or [])
            self._receipts.setdefault(rid, {})[msg.id] = receipts
            view = self.chat_area.chat_view
            if view._current_room_id == rid:
                view.model.set_read_receipts(msg.id, receipts)
            rest = chain[1:]
            if not receipts and rest:
                self._receipts_fetching.add(rid)
                self._fetch_receipts_chain(rid, rest)

        def _fail(err):
            self._receipts_fetching.discard(rid)
            # 瞬时失败（网络抖动）不降级，下个周期重试
            log.debug("拉取已读回执失败 rid=%s：%s", rid, err)

        run_async(_proc, on_done=_done, on_fail=_fail, parent=self)

    def _drop_own_receipt(self, receipts):
        """剔除自己的回执：服务端（多人私聊实测）会把发消息人自己也计入
        已读名单，而未读数分母（成员数）不含自己，分子不剔除会少算一人。"""
        uid = self.client.user_id if self.client else ""
        return [r for r in receipts or []
                if isinstance(r, dict) and r.get("userId") != uid]

    def _ensure_room_members(self, rid: str, room_type: str, on_ready=None):
        """房间成员列表（「N人未读」的分母），5 分钟缓存；失败只影响数字。

        "d" 也要拉：多人和某人的私聊成员多于两人，得靠成员数区分
        1 对 1（画勾）和多人（未读数）。on_ready(members|None) 在成员
        就绪（缓存新鲜时同步）或拉取失败（None）时回调，供成员面板建表。
        """
        if room_type not in ("c", "p", "d"):
            if on_ready:
                on_ready(None)
            return
        cached = self._members_cache.get(rid)
        if cached and time.time() - cached[0] < 300:
            self._push_member_count(rid)
            if on_ready:
                on_ready(cached[1])
            return
        if on_ready:
            self._members_ready_cbs.setdefault(rid, []).append(on_ready)
        if rid in self._members_fetching:
            return
        self._members_fetching.add(rid)
        client = self.client

        def _ready(members):
            for cb in self._members_ready_cbs.pop(rid, []):
                try:
                    cb(members)
                except Exception:  # noqa: BLE001
                    log.exception("成员就绪回调失败 rid=%s", rid)

        def _done(members):
            self._members_fetching.discard(rid)
            self._members_cache[rid] = (time.time(), members or [])
            self._push_member_count(rid)
            self._push_mention_candidates(rid, room_type)
            _ready(members or [])

        def _fail(err):
            self._members_fetching.discard(rid)
            _ready(None)
            log.warning("拉取群成员失败 rid=%s：%s（不影响收发，仅缺未读人数）",
                        rid, err)

        run_async(lambda: client.fetch_room_members(rid, room_type),
                  on_done=_done, on_fail=_fail, parent=self)

    def _members_provider(self, rid: str, room_type: str, on_ready):
        """成员面板的数据源：缓存新鲜同步返回列表；否则补拉并经 on_ready 回调。"""
        cached = self._members_cache.get(rid)
        if cached and time.time() - cached[0] < 300:
            return cached[1]
        self._ensure_room_members(rid, room_type, on_ready=on_ready)
        return None

    def _push_member_count(self, rid: str):
        """把成员数（不含自己）推给当前视图，供 status_spec 计算未读数。"""
        if self._active_room_id != rid:
            return
        cached = self._members_cache.get(rid)
        if not cached:
            return
        uid = self.client.user_id if self.client else ""
        count = sum(1 for m in cached[1] if m.get("_id") != uid)
        self.chat_area.chat_view.set_room_member_count(count)

    def _push_mention_candidates(self, rid: str, room_type: str):
        """把缓存里的当前房间成员注入输入框 @ 候选。"""
        if self._active_room_id != rid:
            return
        cached = self._members_cache.get(rid)
        members = cached[1] if cached else []
        self.chat_area.input.set_mention_candidates(room_type, members)

    def _on_mention_wanted(self):
        """输入框键入 @ 但候选未就绪：补拉当前房间成员（带 5 分钟缓存）。"""
        rid = self._active_room_id
        if not rid or not self.client:
            return
        self._ensure_room_members(rid, self._room_type)

    def _apply_receipts_state_to_view(self, rid: str):
        """回执状态回灌到视图（模型被 set_room/rebuild 清空后调用）。"""
        view = self.chat_area.chat_view
        if view._current_room_id != rid:
            return
        model = view.model
        model.set_receipts_supported(not self._receipts_degraded)
        view.set_room_kind(self._room_type)
        cached = self._members_cache.get(rid)
        if cached:
            uid = self.client.user_id if self.client else ""
            count = sum(1 for m in cached[1] if m.get("_id") != uid)
            view.set_room_member_count(count)
        for mid, rs in self._receipts.get(rid, {}).items():
            model.set_read_receipts(mid, rs)

    def _on_read_receipts_requested(self, message: Message, anchor):
        """点群聊「N人未读」：弹已读/未读名单浮窗，精确回执反哺行内数字。"""
        if not self.client or self._receipts_degraded:
            return
        from .read_receipts_popup import ReadReceiptsPopup
        rid = message.room_id or self._active_room_id
        room_type = self._room_type
        popup = ReadReceiptsPopup(message, self.client.server_url, self,
                                  my_user_id=self.client.user_id or "")
        popup.show_loading()
        screen = QDesktopWidget().availableGeometry(popup)
        pos = anchor + QPoint(0, 6)
        pos.setX(max(screen.left(),
                     min(pos.x(), screen.right() - popup.width() + 1)))
        pos.setY(max(screen.top(),
                     min(pos.y(), screen.bottom() - popup.height() + 1)))
        popup.move(pos)
        popup.show()
        client = self.client
        members_cached = self._members_cache.get(rid)

        def _proc():
            try:
                receipts = client.get_read_receipts(message.id)
            except ReceiptsUnsupportedError as e:
                return {"unsupported": str(e)}
            # 同一 _proc 内串行（rocketchat_API 共享 Session 非线程安全）
            members = (members_cached[1]
                       if members_cached
                       and time.time() - members_cached[0] < 300
                       else client.fetch_room_members(rid, room_type))
            return {"receipts": receipts, "members": members}

        def _done(result):
            try:
                if not popup.isVisible():
                    return  # 用户已点外关闭
            except RuntimeError:
                return  # WA_DeleteOnClose 已析构
            if isinstance(result, dict) and result.get("unsupported"):
                self._receipts_degraded = True
                self.chat_area.chat_view.model.set_receipts_supported(False)
                popup.close()
                return
            receipts = self._drop_own_receipt(list(result["receipts"] or []))
            self._receipts.setdefault(rid, {})[message.id] = receipts
            self.chat_area.chat_view.model.set_read_receipts(
                message.id, receipts)
            if not (members_cached
                    and time.time() - members_cached[0] < 300):
                self._members_cache[rid] = (time.time(), result["members"])
                self._push_member_count(rid)
            popup.populate(receipts, result["members"],
                           avatar_loader=self.avatar_loader)

        def _fail(err):
            try:
                if popup.isVisible():
                    popup.close()
            except RuntimeError:
                pass
            self.statusBar().showMessage(f"获取已读名单失败：{err}", 4000)

        run_async(_proc, on_done=_done, on_fail=_fail, parent=self)

    # ------------------------------------------------------------------ #
    def _on_logout_requested(self):
        """退出登录：停止实时、清空本地会话与聊天、回到登录框。"""
        # client 还在，先把当前输入存成草稿（下次登录同一账号恢复）
        self._save_current_draft()
        self._cancel_ai_panel_task()
        if self.realtime:
            self.realtime.stop()
            self.realtime = None
        self.chat_area.reset_connection_state()
        if self.client:
            self.client.logout()
        self.client = None
        # 清空会话与聊天界面
        self._receipts_timer.stop()
        self._reminders_timer.stop()
        self._receipts.clear()
        self._receipts_fetching.clear()
        self._receipts_degraded = False
        self._members_cache.clear()
        self._members_fetching.clear()
        self._members_ready_cbs.clear()
        self._status_provider.clear()
        self._status_attempts.clear()
        self._active_room_id = None
        self.chat_area.clear_dm_status()
        self._room_panels.close_all()
        self.chat_area.set_panel_active(None)
        self.conversation_list.clear_conversations()
        self._conversations.clear()
        self._latest_by_room.clear()
        self._room_messages.clear()
        self._retry_payloads.clear()
        self._history_offset.clear()
        self._history_more.clear()
        self._history_anchored.clear()
        self._history_forward_done.clear()
        self._history_anchor_gen.clear()
        self._history_anchor_ts.clear()
        self._loading_newer = False
        self.chat_area.chat_view.clear_messages()
        self.chat_area.chat_view.set_room(None)
        self.chat_area.chat_view.set_my_username("")
        self._editing_msg_id = ""
        self.chat_area.input.finish_edit()
        self.chat_area.title.setText("")
        self.setWindowTitle("Fchat")
        # 托盘提醒属于上一个账号，全部清掉并复位图标
        self._clear_tray_alerts()
        self._refresh_tray_state()
        # 账号卡复位并弹登录框；头像 URL 一并清掉，避免下次上传误清旧 URL 的缓存
        self._avatar_url = ""
        self.settings_page.show_account("", "", "")
        # 微信式退出：主界面隐藏只留登录窗（登录框不再是主窗之上的模态子
        # 框）。登录成功（client 已重建）恢复主界面；直接关掉登录窗视为
        # 放弃登录，结束应用。认证 gate 是异步的，exec_() 返回时 client
        # 可能还没挂上——_login_pending 为真时等 _complete_login 恢复窗口。
        self.hide()
        self._show_login(standalone=True)
        if self.client is not None:
            self.show()
        elif self._login_pending:
            pass
        else:
            self._quit_app()

    def _on_status_changed(self, status: str):
        """用户切换在线状态：同步到服务器并保存到配置。"""
        from config import save_status
        save_status(status)
        if self.client:
            self.client.set_status(status)
        cn = {"online": "在线", "away": "离开", "busy": "忙碌",
              "dnd": "勿扰", "offline": "离线"}.get(status, status)
        self.statusBar().showMessage(f"状态已切换为{cn}", 3000)

    def _on_shortcuts_changed(self, shortcuts: dict):
        from config import save_shortcuts
        save_shortcuts(shortcuts)
        self._apply_shortcuts(shortcuts)

    def _apply_shortcuts(self, shortcuts: dict):
        """按配置创建/更新全局快捷键。同一动作先移除旧的再重建，避免叠加。"""
        from PyQt5.QtWidgets import QShortcut
        from PyQt5.QtGui import QKeySequence
        existing = getattr(self, "_shortcuts", {})
        seqs = {
            "open_settings": shortcuts.get("open_settings", "Ctrl+,"),
            "close_settings": shortcuts.get("close_settings", "Esc"),
            "quick_switch": shortcuts.get("quick_switch", "Ctrl+K"),
        }
        handlers = {
            "open_settings": self._show_settings_tab,
            "close_settings": self._toggle_settings_tab,
            "quick_switch": self._show_quick_switcher,
        }
        for action, seq in seqs.items():
            old = existing.pop(action, None)
            if old:
                old.setEnabled(False)
                old.deleteLater()
            sc = QShortcut(QKeySequence(seq), self)
            sc.activated.connect(handlers[action])
            existing[action] = sc
        # 清理已不存在的旧动作（如已移除的 send_message）：旧 QShortcut
        # 挂在主窗口上，不显式销毁会残留并继续拦截按键
        for old in existing.values():
            old.setEnabled(False)
            old.deleteLater()
        self._shortcuts = existing

    def _show_quick_switcher(self):
        """Ctrl+K：模糊搜索本地会话，停顿后补目录（频道/用户）。"""
        from .quick_switcher import QuickSwitcher
        dlg = QuickSwitcher(list(self._conversations.values()), self.client,
                            self.theme_manager, self)
        dlg.open_requested.connect(self._on_quick_switch_open)
        dlg.global_search_requested.connect(self._show_global_search)
        dlg.exec_()
        dlg.deleteLater()

    def _show_global_search(self, keyword: str):
        """Ctrl+K 固定项：跨房间扫描聊天记录（结果面板非模态、复用实例，
        面板内改词重扫不打断旧扫描——token 递增使旧回调失效）。"""
        panel = getattr(self, "_global_search_panel", None)
        if panel is None:
            from .global_search_panel import GlobalSearchPanel
            panel = GlobalSearchPanel(self.theme_manager, self)
            panel.locate_requested.connect(self._on_global_search_locate)
            panel.restart_requested.connect(self._show_global_search)
            self._global_search_panel = panel
        panel.start_scan(keyword, list(self._conversations.values()),
                         self.client)
        panel.show()
        panel.raise_()
        panel.activateWindow()

    def _on_global_search_locate(self, rid: str, msg):
        """全局搜索结果点击：跨房先打开房间，渲染完成后由
        _maybe_locate_pending 续接定位；本房间直接定位。"""
        conv = self._conversations.get(rid)
        if not (self.client and conv is not None):
            self.statusBar().showMessage("该房间已不在会话列表", 3000)
            return
        if rid == self._active_room_id:
            self._locate_message(msg)
            return
        self._pending_locate = (rid, msg)
        self._open_conversation(conv)

    def _maybe_locate_pending(self, rid: str):
        """房间渲染完成后续接全局搜索定位（无挂起或不匹配即忽略）。"""
        pend = getattr(self, "_pending_locate", None)
        if not pend or pend[0] != rid:
            return
        self._pending_locate = None
        self._locate_message(pend[1])

    def _on_quick_switch_open(self, obj):
        """快速切换选中项：本地会话直接打开，目录条目复用目录点击链路。"""
        if isinstance(obj, Conversation):
            self._open_conversation(obj)
        else:
            self._on_directory_entry_open(obj)

    def _toggle_settings_tab(self):
        """Esc 在设置页时收起回到聊天；在聊天页时打开设置。"""
        if self.content_stack.currentIndex() == 1:
            self._show_chat_tab()
        else:
            self._show_settings_tab()

    # ------------------------------------------------------------------ #
    def _show_chat_tab(self):
        self.content_stack.setCurrentIndex(0)
        self.conversation_panel.show()
        self.sidebar.set_active("chats")
        self.chat_area.input.focus_input()
        # 在设置页收到的当前会话消息，回到聊天页即视为已看到
        if self._active_room_id:
            self._drop_tray_alerts(self._active_room_id)
            self._refresh_tray_state()

    def _show_settings_tab(self):
        self.content_stack.setCurrentIndex(1)
        # 设置页只显示内容区：整块会话面板（搜索框+新建+列表）一并隐藏，
        # 否则左侧会留下搜索框和「新建」按钮，与设置页无关却仍可见。
        self.conversation_panel.hide()
        self.sidebar.set_active("settings")
        # 设置页隐藏期间收不到 resizeEvent，浮层三按钮位置要补一次
        self.settings_page.position_window_controls()

    # ------------------------------------------------------------------ #
    # 主题管理
    # ------------------------------------------------------------------ #
    def _load_and_apply_theme(self):
        """从配置加载主题设置并应用。"""
        from config import load_config
        cfg = load_config()
        self.theme_manager.theme = cfg.get("theme", "light")
        self.theme_manager.accent = cfg.get("accent_color", "#07C160")
        self.theme_manager.font_family = cfg.get("font_family", "Microsoft YaHei")
        self.theme_manager.font_size = int(cfg.get("font_size", 13))
        # 应用到 QApplication
        from PyQt5.QtWidgets import QApplication
        self.theme_manager.apply(QApplication.instance())
        # 委托从 ThemeManager 取色，需注入并强制重绘（修复切换主题后聊天区不生效）
        self.chat_area.chat_view.set_theme(self.theme_manager)
        # 气泡 delegate 不走 QSS，字体需单独推到模型并重算行高
        self.chat_area.chat_view.set_fonts(
            self.theme_manager.font_family, self.theme_manager.font_size)
        # 同步到设置页面
        self.settings_page.set_theme(self.theme_manager)
        self.conversation_list.set_theme(self.theme_manager)
        self.settings_page.set_accent(self.theme_manager.accent)
        self.sidebar.set_theme(self.theme_manager)
        for wc in self._window_controls:
            wc.set_theme(self.theme_manager)
        # 浮层面板在构造期（703 行附近）按默认亮色 manager 注入过主题，
        # 这里配置里若是暗色，必须再注入一次：否则面板白底挂在暗色应用里，
        # 且正文 _render 从 manager 实时取色会烤出暗色文字配白底（真机踩过）
        self._room_panels.set_theme(self.theme_manager)
        self.chat_area.set_preview_theme(self.theme_manager)
        # 浮层面板在构造期（703 行附近）按默认亮色 manager 注入过主题，
        # 这里配置里若是暗色，必须再注入一次：否则面板白底挂在暗色应用里，
        # 且正文 _render 从 manager 实时取色会烤出暗色文字配白底（真机踩过）
        self.settings_page.set_font_family(self.theme_manager.font_family)
        self.settings_page.set_font_size(self.theme_manager.font_size)
        # 聊天背景图（委托层绘制，独立于 QSS 色板）
        self._apply_chat_wallpaper(cfg.get("chat_wallpaper", ""))
        self.settings_page.set_wallpaper(cfg.get("chat_wallpaper", ""))
        if self._tray is not None:
            self._tray.set_theme(self.theme_manager)

    def _refresh_chat_theme(self):
        """主题变化后刷新聊天区（委托从 ThemeManager 取色，需注入并重绘）。

        set_theme 内部会按新主题重渲染视口背景并请求重绘，这里不再重复
        viewport().update()；重复调用会让「主题切换只重绘一次」的断言失效。
        """
        self.chat_area.chat_view.set_theme(self.theme_manager)
        # 状态点 pixmap 缓存已随主题清空，重取即新色
        self.chat_area.refresh_status_icon()

    def _apply_theme_safely(self):
        """app.setStyleSheet 延迟到事件循环下一轮执行。

        外观 handler 都由下拉框 currentIndexChanged 直接触发，此刻弹出
        列表还在关闭销毁中；同步 setStyleSheet 会 re-polish 全部控件、
        波及正在关闭的弹窗，Windows 上 access violation 崩溃
        （crash.log）。错开当前事件轮次即安全。
        """
        from PyQt5.QtCore import QTimer
        from PyQt5.QtWidgets import QApplication
        QTimer.singleShot(
            0, lambda: self.theme_manager.apply(QApplication.instance()))

    def _on_theme_changed(self, theme: str):
        self.theme_manager.theme = theme
        self._apply_theme_safely()
        self.chat_area.chat_view.set_theme_palette(self.theme_manager.palette)
        from config import save_appearance
        save_appearance(theme=theme)
        self._refresh_chat_theme()
        self._room_panels.set_theme(self.theme_manager)
        self.conversation_list.set_theme(self.theme_manager)
        # 设置页的取色控件也要跟着切（导航栏/下拉框/标签），否则停在亮色
        self.settings_page.set_theme(self.theme_manager)
        # 图标重挂必须等 _apply_theme_safely 里延迟一轮的 apply 先换完全局
        # 图标色：立即重挂会拿旧色渲染，亮→暗→亮一圈后图标停在暗色
        # #e0e0e0，白底上几乎看不见（真机已复现）。singleShot FIFO 保证
        # 本回调排在 apply 之后
        QTimer.singleShot(0, self._refresh_chrome_icons)
        self.sidebar.set_theme(self.theme_manager)
        self.chat_area.set_preview_theme(self.theme_manager)
        if self._tray is not None:
            self._tray.set_theme(self.theme_manager)

    def _refresh_chrome_icons(self):
        """按当前全局图标色重挂随主题换色的图标（主题应用后调用）。

        图标 pixmap 按渲染时全局色缓存，凡构造期挂上、之后没有重挂点的
        都会在换主题后停在旧色（暗色下深灰画深灰）。新增图标按钮时要么
        挂进这里，要么自己提供 set_theme 重挂点。"""
        self.chat_area.refresh_icons()
        self.chat_area.input.refresh_icons()
        self.directory_btn.setIcon(icon("directory", 16))
        self.new_room_btn.setIcon(icon("plus", 16))
        # 跳到底部悬浮钮是懒建的，存在才重挂（创建时取的就是当前色）
        jump = self.chat_area.chat_view._jump_btn
        if jump is not None:
            jump.setIcon(icon("arrow_down"))
        for wc in self._window_controls:
            wc.set_theme(self.theme_manager)

    def _on_accent_changed(self, color: str):
        self.theme_manager.accent = color
        self._apply_theme_safely()
        from config import save_appearance
        save_appearance(accent_color=color)
        self._refresh_chat_theme()
        if self._tray is not None:
            self._tray.set_theme(self.theme_manager)

    def _on_font_family_changed(self, family: str):
        self.theme_manager.font_family = family
        self._apply_theme_safely()
        from config import save_appearance
        save_appearance(font_family=family)
        self.chat_area.chat_view.set_fonts(family, self.theme_manager.font_size)

    def _on_font_size_changed(self, size: int):
        self.theme_manager.font_size = size
        self._apply_theme_safely()
        from config import save_appearance
        save_appearance(font_size=size)
        self.chat_area.chat_view.set_fonts(self.theme_manager.font_family, size)
        # 设置页 Wx 组件 / 会话列表行的字号按基础字号推算，改完要重刷样式
        self.settings_page.set_theme(self.theme_manager)
        self.conversation_list.set_theme(self.theme_manager)

    def _on_wallpaper_changed(self, path: str):
        from config import save_appearance
        # 空串=恢复默认
        save_appearance(chat_wallpaper=path or "")
        self._apply_chat_wallpaper(path)

    def _apply_chat_wallpaper(self, value: str):
        """把配置值交给聊天视图：""=默认、preset:<id>=预设、其它=图片路径。"""
        self.chat_area.chat_view.set_background(value or "")

    # ------------------------------------------------------------------ #
    # 无边框窗口：自绘圆角底 + 拖动带 + 边缘缩放（模式同 dialog_base）
    # ------------------------------------------------------------------ #
    def _in_drag_band(self, pos) -> bool:
        for w in self._drag_bands:
            # isVisibleTo：窗口未 show 时其内部可见标志仍有效（测试环境
            # 不 show 全窗），设置页下被隐藏的会话面板头部则正确判 False
            if not w.isVisibleTo(self):
                continue
            top_left = w.mapTo(self, QPoint(0, 0))
            if QRect(top_left, w.size()).contains(pos):
                return True
        # 设置页右内容区没有标题行，顶部一条横带补作拖动带；空白处
        # 按下会沿子控件链传上来。nav 可见即设置页激活，聊天页不误判
        nav = self.settings_page.nav
        if nav.isVisibleTo(self):
            nav_br = nav.mapTo(self, QPoint(0, 0)) + QPoint(nav.width(), 0)
            if pos.y() <= _SETTINGS_BAND_H and pos.x() >= nav_br.x():
                return True
        return False

    def _edge_at(self, pos) -> int:
        """指针落在哪条边/角（0 表示不在缩放热区）；最大化时禁用。
        用 _WIN_RESIZE_MARGIN 而非共享的 _RESIZE_MARGIN：主窗口四角被
        setMask 圆角裁掉，6px 热区够不到角落的多边组合，见常量注释。"""
        if self.isMaximized():
            return 0
        m = _WIN_RESIZE_MARGIN
        edges = 0
        if pos.x() <= m:
            edges |= int(Qt.LeftEdge)
        elif pos.x() >= self.width() - m - 1:
            edges |= int(Qt.RightEdge)
        if pos.y() <= m:
            edges |= int(Qt.TopEdge)
        elif pos.y() >= self.height() - m - 1:
            edges |= int(Qt.BottomEdge)
        return edges

    def _sync_resize_cursor(self, pos) -> None:
        shape = _RESIZE_CURSORS.get(self._edge_at(pos))
        if shape == self._resize_cursor:
            return
        self._resize_cursor = shape
        if shape is None:
            self.unsetCursor()
        else:
            self.setCursor(shape)

    def _begin_resize(self, edges: int, global_pos) -> None:
        self._resize_edges = edges
        self._resize_start = (self.geometry(), global_pos)

    def _resize_to(self, global_pos) -> None:
        geo, origin = self._resize_start
        dx = global_pos.x() - origin.x()
        dy = global_pos.y() - origin.y()
        left, top, width, height = (geo.x(), geo.y(),
                                    geo.width(), geo.height())
        if self._resize_edges & Qt.LeftEdge:
            width = max(self.minimumWidth(), width - dx)
            left = geo.right() - width + 1
        elif self._resize_edges & Qt.RightEdge:
            width = max(self.minimumWidth(), width + dx)
        if self._resize_edges & Qt.TopEdge:
            height = max(self.minimumHeight(), height - dy)
            top = geo.bottom() - height + 1
        elif self._resize_edges & Qt.BottomEdge:
            height = max(self.minimumHeight(), height + dy)
        self.setGeometry(left, top, width, height)

    def _toggle_maximized(self, *_):
        # 还原必须走 showNormal：最大化前的 geometry 由 Qt 自己记，
        # test_copy_and_screenshot 的 showNormal 断言也依赖它
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _update_window_mask(self) -> None:
        """把不透明子部件（侧栏/聊天视口）钳进圆角形。mask 边缘无抗锯齿，
        直角变圆角处有轻微锯齿，观感待真机确认。"""
        if self.isMaximized():
            self.clearMask()
            return
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
                            _WIN_RADIUS, _WIN_RADIUS)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            pos = event.pos()
            edges = self._edge_at(pos)
            if edges:
                self._begin_resize(edges, event.globalPos())
                event.accept()
                return
            if self._in_drag_band(pos) and not self.isMaximized():
                # 手动拖动（dialog_base 同款）。PyQt5 未包装 QWidget
                # .startSystemMove（真机 AttributeError，别再试系统调用）
                self._drag_offset = (event.globalPos()
                                     - self.frameGeometry().topLeft())
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resize_edges and event.buttons() & Qt.LeftButton:
            self._resize_to(event.globalPos())
            event.accept()
            return
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPos() - self._drag_offset)
            event.accept()
            return
        self._sync_resize_cursor(event.pos())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._resize_edges = 0
        self._resize_start = None
        self._drag_offset = None
        self._sync_resize_cursor(self.mapFromGlobal(event.globalPos()))
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton and self._in_drag_band(event.pos()):
            self._toggle_maximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def eventFilter(self, obj, event):
        etype = event.type()
        if etype == QEvent.MouseButtonPress \
                and event.button() == Qt.LeftButton:
            # 子部件（聊天视口/会话列表）会吞掉边沿上的按下，过滤器先于
            # 它们的 mousePressEvent 执行，这里拦下热区内的按下才能缩放
            pos = self.mapFromGlobal(event.globalPos())
            edges = self._edge_at(pos)
            if edges:
                self._begin_resize(edges, event.globalPos())
                return True
        elif etype == QEvent.MouseMove:
            # 缩放拖动：按下发生在子部件（或滚动条）上，后续 MouseMove 也
            # 发给子部件，MainWindow.mouseMoveEvent 不会被调用，这里代为
            # 执行 _resize_to（同 mouseMoveEvent 的缩放分支），否则右边缘
            # 能显示光标却拖不动窗口。
            if self._resize_edges and event.buttons() & Qt.LeftButton:
                self._resize_to(event.globalPos())
                return True
            # 用事件携带的位置（而非 QCursor.pos()）刷新光标：QCursor.pos()
            # 是当前全局鼠标位置，快速移动时可能滞后于事件报上来的位置，
            # 导致移到热区时光标没刷成缩放箭头（聊天记录右边缘偶发不显示）。
            self._sync_resize_cursor(self.mapFromGlobal(event.globalPos()))
        elif etype == QEvent.Enter:
            # Enter 事件不携带位置，只能退回全局鼠标位置复位光标
            self._sync_resize_cursor(self.mapFromGlobal(QCursor.pos()))
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_window_mask()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        radius = 0.0 if self.isMaximized() else _WIN_RADIUS
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
                            radius, radius)
        p.fillPath(path, QColor(_c(self.theme_manager, "conv_bg", "#ffffff")))
        p.setPen(QColor(_c(self.theme_manager, "border", "#e7e7e7")))
        p.drawPath(path)

    # ------------------------------------------------------------------ #
    def changeEvent(self, event):
        super().changeEvent(event)
        # 最大化/还原：同步两处三按钮图标；frameless 下 showMaximized
        # 可能盖住任务栏，钳回可用区
        if event.type() == QEvent.WindowStateChange:
            maximized = self.isMaximized()
            for wc in getattr(self, "_window_controls", ()):
                wc.set_maximized(maximized)
            if maximized:
                avail = self.screen().availableGeometry()
                if not avail.contains(self.frameGeometry()):
                    self.setGeometry(avail)
            self._update_window_mask()
            self.update()
        # 主窗口回到前台：停止闪烁；若正停在当前聊天页，该会话未读算已看
        if event.type() == QEvent.ActivationChange and self.isActiveWindow():
            tray = self._tray
            if tray is None:
                return
            tray.stop_alert()
            if self._is_chat_foreground() and self._active_room_id:
                self._drop_tray_alerts(self._active_room_id)
                self._refresh_tray_state()

    def closeEvent(self, event: QCloseEvent):
        if self._closing:
            event.accept()
            if self._tray is not None:
                self._request_quit()
            return
        if self._tray is not None and self.client is not None \
                and not self._force_quit:
            # 已登录时点 X 只收进托盘：实时连接、订阅和后台任务全部保持，
            # 这样托盘里还能收新消息。输入到一半直接点 X 也不能丢草稿
            event.ignore()
            self._save_current_draft()
            self._room_panels.close_all()
            self._tray.hide_card()
            self.hide()
            return
        # 真退出前输入框还有未发送的文字：确认一次，防手滑丢内容
        # （点 X 收托盘的路径不弹——草稿已保存，回来还在）
        draft = self.chat_area.input.draft_snapshot()
        if draft.get("text", "").strip() and not confirm(
                self, self.theme_manager, "退出确认",
                "输入框还有未发送的文字，确定退出吗？"):
            # 用户取消退出：_force_quit 是托盘「关闭程序」置位的，若在此
            # 保留 True，之后从右上角点 X 会跳过「缩小到托盘」直接退出
            # （真机踩过：托盘关闭→取消→清草稿→点 X 程序直接退）。取消
            # 即恢复「点 X 收托盘」行为
            self._force_quit = False
            event.ignore()
            return
        self._closing = True
        self._save_current_draft()
        self._room_panels.close_all()
        if self._tray is not None:
            self._tray.shutdown()
        if self.realtime:
            self.realtime.stop()
        # 登录刚完成就关窗口时，run_async 的后台任务（E2E / 部门 / 会话
        # 拉取）可能还在跑；主窗口销毁会连带销毁还活着的 QThread，报
        # "QThread: Destroyed while thread is still running"。统一等在途
        # 线程收尾；网络卡死时最多等 CLOSE_DRAIN_TIMEOUT 秒，不能把退出
        # 无限挂住
        from .api_workers import _AsyncTask, detach_unfinished_tasks
        deadline = time.monotonic() + CLOSE_DRAIN_TIMEOUT
        for task in self.findChildren(_AsyncTask):
            left = int(max(0.0, deadline - time.monotonic()) * 1000)
            if left <= 0 or not task.wait_finished(left):
                # 放弃等待：未结束的任务解绑出主窗口（模块级引用保活到
                # 线程自然结束，通常就差几十毫秒），否则主窗口销毁会把
                # 还在跑的线程连带销毁，报同款 ERROR
                n = detach_unfinished_tasks(self)
                if n:
                    log.warning("退出时有 %d 个后台任务未结束，"
                                "已解绑让其自行收尾", n)
                break
        # AI 任务同款：先等在途的收尾，超时则取消并解绑（流式请求通常
        # 几毫秒内就能在下一个 chunk 边界退出）
        from .ai_worker import AITask
        from .ai_worker import detach_unfinished_tasks as detach_ai_tasks
        for task in self.findChildren(AITask):
            left = int(max(0.0, deadline - time.monotonic()) * 1000)
            if left <= 0 or not task.wait_finished(left):
                n = detach_ai_tasks(self)
                if n:
                    log.warning("退出时有 %d 个 AI 任务未结束，"
                                "已解绑让其自行收尾", n)
                break
        if self._login_runner and self._login_runner.running:
            if not self._login_runner.wait_finished(1500):
                self._login_runner.detach()
                log.warning("退出时登录线程仍未结束，已解绑让其自行收尾")
        # 下载弹窗是无父独立顶层窗口，会被计入 quitOnLastWindowClosed 的
        # 「主窗口」；不 hide 掉，主窗口关闭后 app 不会退出。残留线程交给
        # main.py 的 os._exit 收尾，退出路径上绝不 wait()
        cv = getattr(self.chat_area, "chat_view", None)
        if cv is not None:
            cv.close_download_dialogs()
        # 上传进度弹窗同款（无父独立顶层窗口）：force_close 取消并 hide，
        # 不等线程，否则主窗口关闭后 app 不退出
        for dlg in list(self._upload_dialogs):
            dlg.force_close()
        self._upload_dialogs.clear()
        event.accept()
        # quitOnLastWindowClosed 已关掉（托盘存在），必须显式结束事件循环
        if self._tray is not None:
            self._request_quit()
