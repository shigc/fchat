"""聊天视图：基于 QListView 的虚拟化消息流。

使用 MessageListModel + MessageDelegate 实现虚拟化渲染，
仅绘制可见区域的消息，大幅提升大量消息时的性能。

支持分批延迟加载历史消息，保持 UI 响应性。
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
import urllib.parse

from typing import List

from PyQt5.QtCore import Qt, QUrl, QRect, QSize, QPoint, QTimer, pyqtSignal
from PyQt5.QtGui import (
    QBrush, QColor, QDesktopServices, QKeySequence, QFont, QPalette,
    QTransform,
)
from PyQt5.QtWidgets import (
    QListView, QMenu, QApplication, QFileDialog, QAbstractItemView,
    QFrame, QPushButton, QHBoxLayout, QLabel, QWidget, QToolButton,
)

from core.models import Message
from core.url_guard import (
    MAX_REDIRECT_HOPS, can_carry_credentials, is_openable_url, is_redirect,
    redirect_target)
from .message_model import MessageListModel, MessageDelegate, RoleMessage
from .chat_background import render_background, theme_mode
from .icons import icon
from .api_workers import run_async
from .download_dialog import FileDownloadDialog

log = logging.getLogger(__name__)

# 音频内联播放：QtMultimedia 缺失（个别精简 wheel）时整体降级为
# 系统播放器打开，不阻塞其他功能
try:
    from PyQt5.QtMultimedia import QMediaPlayer, QMediaContent  # noqa: E402
    _AUDIO_PLAYBACK_OK = True
except Exception:  # noqa: BLE001
    _AUDIO_PLAYBACK_OK = False


# ---------------------------------------------------------------------- #
# 文件下载工具
# ---------------------------------------------------------------------- #
def _safe_filename(name: str) -> str:
    """去掉路径分隔与 Windows 非法字符，避免缓存文件名穿越/非法。"""
    for ch in '\\/:*?"<>|':
        name = name.replace(ch, "_")
    return name or "attachment"


def file_cache_dest(url: str, name: str) -> str:
    """文件附件的本地缓存路径：cache/files/<md5(url)>-<安全文件名>。

    md5 前缀区分不同 URL 的同名文件；原文件名保留便于直接双击识别。
    文件名没有扩展名时从 URL 路径补（视频附件 title 常无后缀，没有
    扩展名系统程序打不开）；键取 canon_url 归一（FullyEncoded），
    与 ImageLoader/AvatarLoader 的缓存键同源，同一附件的不同百分号
    编码写法才不会缓存成两份。
    core.paths 懒读配置，调用方可能只是算路径，import 留在函数内。
    """
    from core.paths import files_dir
    from .avatar_loader import canon_url
    safe = _safe_filename(name)
    if not os.path.splitext(safe)[1]:
        ext = os.path.splitext(urllib.parse.urlparse(canon_url(url)).path)[1]
        if ext:
            safe += ext
    return os.path.join(
        files_dir(),
        f"{hashlib.md5(canon_url(url).encode()).hexdigest()}-{safe}")


class ChatView(QListView):
    """虚拟化消息列表视图。

    使用 QListView + MessageListModel + MessageDelegate 实现：
    - 仅渲染可见区域的消息
    - 分批延迟加载历史消息
    - 滚动锚点保持（加载历史时保持当前阅读位置）
    """

    # 冒泡信号（由 main_window 连接处理）
    recall_requested = pyqtSignal(object)   # Message
    quote_requested = pyqtSignal(object)    # Message
    forward_requested = pyqtSignal(object)  # list[Message] — 单条转发也包成列表
    forward_many_requested = pyqtSignal(object)  # (list[Message], "merge"|"each")
    profile_requested = pyqtSignal(object, object)  # Message, 头像全局坐标
    image_view_requested = pyqtSignal(object)  # Message — 图片查看器
    inline_image_view_requested = pyqtSignal(str)  # src — markdown 内联图查看器
    mention_clicked = pyqtSignal(str, object)  # (username, @锚定QRect全局：x=@左缘,上下=整行)
    quote_jump_requested = pyqtSignal(str)  # quote_msg_id — 跳转被引用消息
    read_receipts_requested = pyqtSignal(object, object)  # (Message, 全局锚点) — 群聊「N人未读」点击
    status_message = pyqtSignal(str, int)  # (文本, 毫秒) — 状态栏提示，由主窗口转发
    drop_files_requested = pyqtSignal(object)  # list[本地路径] — 拖入本地文件到消息列表
    retry_requested = pyqtSignal(object)  # Message — 发送失败的消息重新发送
    reaction_toggle_requested = pyqtSignal(object, str)  # (Message, emoji key)
    reaction_add_requested = pyqtSignal(object)  # Message — 打开表情选择器
    edit_requested = pyqtSignal(object)  # Message — 编辑自己发出的消息
    pin_requested = pyqtSignal(object, bool)  # (Message, 置顶/取消)
    star_requested = pyqtSignal(object, bool)  # (Message, 星标/取消)
    ai_action_requested = pyqtSignal(object, str)  # (Message, 动作 key)
    remind_requested = pyqtSignal(object, float)  # (Message, 到期 epoch 秒)
    remind_cancel_requested = pyqtSignal(object)  # Message — 取消该消息提醒
    ocr_requested = pyqtSignal(str)  # 图片绝对 URL — 离线识别文字
    scrolled_to_bottom = pyqtSignal()  # 用户滚到底部：清「新消息」提示条

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ChatScrollArea")

        # 禁用项目选择
        self.setSelectionMode(QListView.NoSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        # 创建模型和代理
        self._model = MessageListModel(self)
        self._delegate = MessageDelegate(self._model, self)
        self.setItemDelegate(self._delegate)
        self.setModel(self._model)

        # 配置列表视图
        self.setSpacing(0)
        self.setUniformItemSizes(False)
        self.setWrapping(False)
        self.setLayoutMode(QListView.SinglePass)
        self.setViewportMargins(0, 4, 0, 4)
        # 默认按整行滚动会让加载历史时的像素级锚点算不准
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)

        # 内部状态
        self._current_room_id = None
        # E2E 房间禁止编辑（解密明文不回传服务端）
        self._room_encrypted = False
        # AI 入口默认隐藏，启用并配置后由主窗口打开
        self._ai_available = False
        self.avatar_loader = None
        self.image_loader = None
        self._theme = None          # 主题取色器（供下载进度弹窗等子对话框取色）

        # 聊天背景走视口 palette 的 Base 刷子：背景整块铺满且滚动时固定。
        # 不能在 delegate 里按行画图——每行拿整张图缩放到行高、还不裁剪，
        # 会盖住其它行（旧实现的实际表现）。
        self._bg_value = ""         # ""=默认色 / preset:<id> / 图片路径
        self._bg_key = None
        self._bg_pixmap = None
        self._bg_timer = QTimer(self)
        self._bg_timer.setSingleShot(True)
        self._bg_timer.setInterval(80)
        self._bg_timer.timeout.connect(self._render_viewport_background)
        self.viewport().setBackgroundRole(QPalette.Base)
        self.viewport().setAutoFillBackground(True)
        # 文本选区拖拽：在文本气泡内按下时记录（消息 id, 按下矩形），
        # 拖拽时用 hitTest 延伸选区。选区本身存在模型上（sel_msg_id 等）。
        self._sel_press_msg_id = None
        self._sel_press_bubble = None
        # 选区是否落在合并卡片某条目内（区分整条消息 doc 与单条目 doc）
        self._sel_press_merge = False

        # 多选转发模式：进入后点消息勾选，底部操作条提供合并/逐条转发
        self._multi_select = False
        self._ms_selected: List[str] = []
        self._ms_bar = None

        # 跳到底部悬浮钮（viewport 子控件，滚离底部才显示）
        self._jump_btn = None
        self.verticalScrollBar().valueChanged.connect(
            lambda *_: self._update_jump_btn())

        # 分批预加载相关
        self._pending_prepend: List[Message] = []
        self._prepend_timer = QTimer()
        self._prepend_timer.setInterval(16)  # 16ms per batch
        self._prepend_timer.setSingleShot(True)
        self._prepend_timer.timeout.connect(self._flush_prepend_batch)
        self._prepend_anchor_value = -1      # 插入前的滚动值，-1 表示本轮不需要复原
        self._prepend_inserted = 0           # 本轮已插入的行数
        self._anchor_target = -1             # 待布局完成后生效的滚动值
        self._first_page_pending = False     # 首屏：渲染完应停在底部
        # 打开会话时若有未读分隔线，首屏渲染完要停在分隔线而不是底部
        self._pending_jump_to_divider = False

        # 连接信号
        self._model.batch_finished.connect(self._on_model_batch_finished)
        self.verticalScrollBar().valueChanged.connect(self._on_scroll_value)

        self._downloading = set()   # 在途附件下载的 url（点击去重）
        # 另存为下载弹窗无 widget parent（独立顶层窗口，才能被主窗口压到后面），
        # 靠本集合持强引用防止 show() 后被 GC；关窗经 destroyed 移除。
        self._save_dialogs = set()

        # 音频内联播放：主线程持播放器，200ms 轮询进度刷行
        self._audio_player = None
        self._audio_msg_id = ""
        self._audio_dur = {}    # msg_id → 时长 ms（播放过一次后记住）
        self._audio_timer = QTimer(self)
        self._audio_timer.setInterval(200)
        self._audio_timer.timeout.connect(self._on_audio_tick)

        # 接收拖入本地文件到消息列表（QListView 默认不吞 drop，需显式开启）
        self.setAcceptDrops(True)

    def closeEvent(self, event):
        """窗口关闭时断开持久信号连接，打破「子对象持有 self」的引用环。

        QScrollBar / model 通过 connect(bound method 或捕获 self 的 lambda)
        持有本视图的强引用，与「本视图持有子对象」互为循环引用，deleteLater
        因此永远不生效（C++ 对象残留）。残留视图在后续 setStyleSheet 重算
        样式时会被 Qt 访问，其已释放的 Python 包装触发 access violation
        （测试套件组合崩溃）。关闭即断开，让对象可被回收。

        注意：只断开 scrollbar 与 model，不断 QTimer——timer 的 timeout 信号
        可能被 moveToThread 到工作线程，disconnect 会报 "Timers cannot be
        stopped from another thread" 并卡死；且 timer 不是 setStyleSheet
        崩溃的直接来源。
        """
        try:
            sb = self.verticalScrollBar()
            for sig in (sb.valueChanged, self._model.batch_finished):
                try:
                    sig.disconnect()
                except TypeError:
                    pass  # 无连接或已断开
        except RuntimeError:
            pass  # 关闭过程中 C++ 对象已部分销毁
        super().closeEvent(event)

    def dragEnterEvent(self, event):
        """拖入本地文件时接受，其余 mime 交给默认处理。"""
        if self._has_local_urls(event.mimeData()):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if self._has_local_urls(event.mimeData()):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        mime = event.mimeData()
        if not self._has_local_urls(mime):
            super().dropEvent(event)
            return
        paths = [u.toLocalFile() for u in mime.urls()
                 if u.isLocalFile() and u.toLocalFile()]
        if paths:
            self.drop_files_requested.emit(paths)
        event.acceptProposedAction()

    @staticmethod
    def _has_local_urls(mime) -> bool:
        urls = mime.urls() if mime is not None else []
        return any(u.isLocalFile() for u in urls)

    # ------------------------------------------------------------------ #
    # 公共属性
    # ------------------------------------------------------------------ #
    @property
    def model(self) -> MessageListModel:
        return self._model

    def set_avatar_loader(self, loader):
        self.avatar_loader = loader
        self._delegate.set_avatar_loader(loader)

    def set_image_loader(self, loader):
        self.image_loader = loader
        self._delegate.set_image_loader(loader)
        self._model.set_image_loader(loader)

    def set_theme_palette(self, palette: dict):
        """切换主题色板并让 delegate 重新取色重绘。"""
        self._delegate.set_palette(palette)

    def set_show_name(self, show: bool):
        self._model.set_show_name(show)

    def set_my_username(self, username: str):
        """当前登录用户名：用于区分「我点过的」回应 chip。"""
        self._model.set_my_username(username)

    def set_room_encrypted(self, encrypted: bool):
        self._room_encrypted = bool(encrypted)

    def set_ai_available(self, available: bool):
        """AI 功能启用后才显示右键「AI 助手」子菜单。"""
        self._ai_available = bool(available)

    def set_room_kind(self, room_type: str):
        """当前房间类型（"d" 私聊 / "c"/"p" 群聊），影响状态图标判定。"""
        self._model.set_room_context(room_type, self._model._member_count)

    def set_room_member_count(self, count: int):
        """群成员数（不含自己），用于「N人未读」计算。"""
        self._model.set_room_context(self._model._room_type, count)

    def set_theme(self, theme):
        """通知委托切换主题取色器（主题切换后用于重绘气泡颜色）。"""
        self._theme = theme
        self._delegate.set_theme(theme)
        self._apply_jump_btn_style()
        self._render_viewport_background()
        # QSS 是在本函数之后才异步应用的，re-polish 可能重置视口调色板，
        # 下一轮事件循环再把缓存的背景刷子套回去
        QTimer.singleShot(0, self._reapply_background_palette)

    def set_fonts(self, family: str, size: int):
        """更新聊天气泡字体并让整表按新字体重算行高重绘。

        气泡由 MessageDelegate 用 QPainter 直接绘制，不走 QSS，
        必须更新模型里的 QFont 并触发 layoutChanged 才能实时生效。
        """
        font = QFont()
        if family:
            font.setFamily(family)
        font.setPointSize(int(size) if size > 0 else 14)
        self._model.set_fonts(font)
        self._model.layoutChanged.emit()

    @property
    def background_value(self) -> str:
        return self._bg_value

    def set_background(self, value: str):
        """设置聊天背景：""=主题默认色、preset:<id>=内置预设、其它=图片路径。"""
        self._bg_value = str(value or "")
        self._render_viewport_background()

    def _render_viewport_background(self):
        """按当前视口尺寸渲染背景并写入 palette Base 刷子。"""
        viewport = self.viewport()
        w, h = viewport.width(), viewport.height()
        if w <= 0 or h <= 0:
            return
        mode = theme_mode(self._theme)
        chat_bg = self._theme.color("chat_bg", "#f5f5f5") \
            if self._theme is not None else "#f5f5f5"
        dpr = viewport.devicePixelRatioF() or 1.0
        key = (self._bg_value, mode, w, h, round(dpr, 3), chat_bg)
        if key != self._bg_key:
            self._bg_pixmap = render_background(
                self._bg_value, w, h, mode, dpr)
            self._bg_key = key
        self._apply_background_palette()

    def _apply_background_palette(self):
        viewport = self.viewport()
        palette = viewport.palette()
        if self._bg_pixmap is not None and not self._bg_pixmap.isNull():
            brush = QBrush(self._bg_pixmap)
            dpr = self._bg_pixmap.devicePixelRatio() or 1.0
            pw = self._bg_pixmap.width() / dpr
            ph = self._bg_pixmap.height() / dpr
            # 窗口拖动中尺寸还没重渲染，先把旧图拉伸铺满，避免平铺接缝
            if pw > 0 and ph > 0 and (
                    abs(pw - viewport.width()) > 0.5
                    or abs(ph - viewport.height()) > 0.5):
                brush.setTransform(QTransform().scale(
                    viewport.width() / pw, viewport.height() / ph))
            palette.setBrush(QPalette.Base, brush)
        else:
            chat_bg = self._theme.color("chat_bg", "#f5f5f5") \
                if self._theme is not None else "#f5f5f5"
            palette.setBrush(QPalette.Base, QBrush(QColor(chat_bg)))
        viewport.setPalette(palette)
        viewport.setAutoFillBackground(True)
        viewport.update()

    def _reapply_background_palette(self):
        try:
            self._apply_background_palette()
        except RuntimeError:
            # 关闭窗口时排队的回调可能晚于 C++ 对象销毁
            pass

    @property
    def is_batching_history(self) -> bool:
        """正在批量插行；此时滚动位置不稳定，不要据此触发新的加载。"""
        return self._first_page_pending or self._prepend_timer.isActive()

    # ------------------------------------------------------------------ #
    # 公共方法
    # ------------------------------------------------------------------ #
    def clear_messages(self):
        self._model.clear()
        self._pending_prepend.clear()
        self._prepend_timer.stop()
        self._delegate.clear_pending_resources()
        self._prepend_anchor_value = -1
        self._prepend_inserted = 0
        self._anchor_target = -1
        self._first_page_pending = False

    def set_room(self, room_id: str):
        self._stop_audio()
        self._current_room_id = room_id
        self._clear_text_selection()
        self._cancel_multi_select()
        self.clear_messages()
        self._pending_jump_to_divider = False
        # 空列表，接下来的首屏历史要停在底部显示最新消息
        self._first_page_pending = True

    # ---- 未读分隔线 / 新消息提示 ---- #
    def set_unread_divider(self, msg_id: str):
        """在指定消息行首插入「以下为新消息」；空串清除。"""
        self._pending_jump_to_divider = bool(msg_id)
        self._model.set_unread_divider(msg_id)

    def clear_unread_divider(self):
        self._pending_jump_to_divider = False
        self._model.set_unread_divider("")

    def jump_to_first_unread(self) -> bool:
        """滚到未读分隔线；没有分隔线或行未加载返回 False。"""
        self._pending_jump_to_divider = False
        mid = self._model._unread_divider_id
        if not mid or self._model.find_message_row(mid) < 0:
            return False
        QTimer.singleShot(0, lambda: self._scroll_to_divider_now(mid))
        return True

    def _scroll_to_divider_now(self, msg_id: str):
        row = self._model.find_message_row(msg_id)
        if row < 0:
            return
        self.scrollTo(self._model.index(row, 0),
                      QAbstractItemView.PositionAtTop)

    def is_near_bottom(self, slack: int = 100) -> bool:
        """当前是否停在底部附近（首屏分批插入期间视为在底部）。"""
        if self._first_page_pending or self._prepend_timer.isActive():
            return True
        bar = self.verticalScrollBar()
        return bar.value() >= bar.maximum() - slack

    def _on_scroll_value(self, value: int):
        bar = self.verticalScrollBar()
        if value >= bar.maximum() - 4:
            self.scrolled_to_bottom.emit()

    def wheelEvent(self, event):
        super().wheelEvent(event)
        # 内容一屏放得下（maximum==0）时滚动条不会产生 valueChanged，
        # 用户继续向下滚滚轮就是"想往更新处看"，同样通知到底
        if event.angleDelta().y() < 0 or event.pixelDelta().y() < 0:
            bar = self.verticalScrollBar()
            if bar.value() >= bar.maximum() - 4:
                self.scrolled_to_bottom.emit()

    def append_message(self, message: Message):
        """在底部追加一条消息，并自动滚到底部（如果已在底部附近）。"""
        if message.room_id and self._current_room_id and message.room_id != self._current_room_id:
            return
        self._model.append_message(message)
        # 如果当前在底部附近，自动滚到底部
        bar = self.verticalScrollBar()
        if bar.value() >= bar.maximum() - 100:
            self.scroll_to_bottom()

    def append_messages(self, messages: List[Message]):
        """批量追加到尾部且不滚动：锚定窗口向下续载要保持阅读位置。"""
        for m in messages:
            self._model.append_message(m)

    def prepend_messages(self, messages: List[Message], first_page: bool = False):
        """在顶部插入更早的历史消息（新→旧顺序输入）。

        分批延迟插入以保持 UI 响应；完成后首屏停在底部，
        其余情况复原到插入前的阅读位置。
        """
        if not messages:
            return
        # 过滤重复消息
        filtered = [m for m in messages if not self._is_duplicate(m.id)]
        if not filtered:
            return
        if first_page:
            self._first_page_pending = True
        elif self._prepend_anchor_value < 0 and not self._first_page_pending:
            # 一轮里只有第一批需要记锚点
            self._prepend_anchor_value = self.verticalScrollBar().value()
        # 插入行数在 _flush_prepend_batch 里按模型实际行数统计：模型会
        # 折叠重复系统消息，用 len(filtered) 会把滚动锚点多算几行
        self._pending_prepend.extend(filtered)
        # 启动分批处理
        if not self._prepend_timer.isActive():
            self._flush_prepend_batch()

    def rebuild_messages(self, messages: List[Message]):
        """清空后按时间正序（旧→新）重建整个消息流。

        使用分批插入以保持 UI 响应（小数据集使用立即插入）。
        """
        self.clear_messages()
        if not messages:
            return
        # 小数据集使用立即插入，大数据集使用分批插入
        if len(messages) <= 100:
            self._model.prepend_messages_immediate(messages)
        else:
            self._model.prepend_messages_batched(list(reversed(messages)))

    def update_message(self, message: Message) -> bool:
        """同 id 原位刷新一条消息（编辑/表情回应/置顶星标等实时更新）。"""
        if (message.room_id and self._current_room_id
                and message.room_id != self._current_room_id):
            return False
        row = self._model.find_message_row(message.id)
        if row < 0:
            return False
        old_h = self._model._get_height(row)
        self._model.update_message(message)
        # 回应条/编辑换行会改变行高：dataChanged 不足以让 QListView 重问
        # sizeHint，主动排一次布局，避免下一行被压在旧高度上
        if self._model._get_height(row) != old_h:
            self.scheduleDelayedItemsLayout()
        return True

    def update_message_status(self, message_id: str, status: str) -> bool:
        """更新指定消息的状态指示。"""
        row = self._model.find_message_row(message_id)
        if row < 0:
            return False
        msg = self._model.get_message(row)
        if msg:
            msg.status = status
            self._model.update_message(msg)
            return True
        return False

    def replace_with_recalled(self, message_id: str) -> bool:
        """将指定消息替换为"消息已撤回"系统消息。"""
        row = self._model.find_message_row(message_id)
        if row < 0:
            return False
        msg = self._model.get_message(row)
        if not msg:
            return False
        # 创建撤回系统消息
        from core.models import User
        recalled = Message(
            id=msg.id,
            room_id=msg.room_id,
            text="消息已撤回",
            msg_type="system",
            timestamp=msg.timestamp,
            sender=msg.sender or User(),
        )
        self._model.update_message(recalled)
        return True

    def scroll_to_bottom(self, deferred: bool = False):
        """滚到底部显示最新消息。

        deferred=True 用于刚插完行的场合：此时 SinglePass 布局还没跑完，
        滚动条 maximum() 仍是旧值，要等一轮事件循环再定位。
        """
        if deferred:
            QTimer.singleShot(0, self._scroll_to_bottom_now)
            return
        self._scroll_to_bottom_now()

    def _scroll_to_bottom_now(self):
        self.scrollToBottom()
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ------------------------------------------------------------------ #
    # 分批预加载
    # ------------------------------------------------------------------ #
    def _flush_prepend_batch(self):
        """分批插入待处理的消息。"""
        if not self._pending_prepend:
            return
        # 每次处理一批（50条）
        batch_size = 50
        batch = self._pending_prepend[:batch_size]
        self._pending_prepend = self._pending_prepend[batch_size:]
        # 队列是新→旧顺序，反转为旧→新后插入模型
        batch.reverse()
        before = self._model.rowCount()
        self._model.prepend_messages_immediate(batch)
        self._prepend_inserted += self._model.rowCount() - before
        if self._pending_prepend:
            self._prepend_timer.start()
        else:
            self._finish_prepend()

    def _finish_prepend(self):
        """一批历史全部插完，决定停在哪里。"""
        if self._first_page_pending:
            self._first_page_pending = False
            self._prepend_inserted = 0
            # 未读分隔线优先：打开会话先看新消息从哪开始，而不是直接跳最新
            if self._pending_jump_to_divider and self.jump_to_first_unread():
                return
            self.scroll_to_bottom(deferred=True)
            return
        self._restore_prepend_anchor()

    def _restore_prepend_anchor(self):
        """复原到加载更早消息前的阅读位置。"""
        anchor_value = self._prepend_anchor_value
        inserted = self._prepend_inserted
        self._prepend_anchor_value = -1
        self._prepend_inserted = 0
        if anchor_value < 0:
            return
        # 新插入的行全部落在 0..inserted-1，它们的高度就是需要补偿的量
        added = 0
        for i in range(min(inserted, self._model.rowCount())):
            added += self._model._get_height(i)
        self._scroll_then(anchor_value + added)

    def _scroll_then(self, value: int):
        """等本轮布局跑完再设滚动值（此刻 maximum() 还是旧的）。"""
        self._anchor_target = value
        QTimer.singleShot(0, self._apply_anchor_target)

    def _apply_anchor_target(self):
        value, self._anchor_target = self._anchor_target, -1
        if value >= 0:
            self.verticalScrollBar().setValue(value)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_ms_bar()
        self._update_jump_btn()
        if self._bg_value:
            # 旧图先拉伸铺满顶过拖动过程，停手后再按新尺寸重渲染
            self._apply_background_palette()
            self._bg_timer.start()

    def scrollContentsBy(self, dx, dy):
        """滚动手动把多选操作条钉回原位。

        操作条是 viewport 子控件，QAbstractScrollArea 滚动时会随内容一起
        位移；不处理的话用户在滚动时操作条会被推出可视区（表现为"多选
        顶部菜单消失"）。这里在每次滚动后重新定位，同一事件循环内完成，
        绘制前已归位，不会闪动。
        """
        super().scrollContentsBy(dx, dy)
        self._position_ms_bar()
        self._position_jump_btn()
        if (dx or dy) and self._bg_value:
            # Qt 滚动是 blit 优化：已画像素整体平移（背景跟着内容走），
            # 只有新露出的条带按刷子原点重画——图片背景会出现上下错位的
            # 割裂。整视口重绘让背景刷子始终锚定视口原点（纯色背景无
            # 此问题，不必付出整屏重绘的代价）。
            self.viewport().update()

    # ---- 跳到底部悬浮钮（微信式，滚离底部 200px 以上才出现） ---- #
    def _ensure_jump_btn(self) -> QToolButton:
        if self._jump_btn is not None:
            return self._jump_btn
        btn = QToolButton(self.viewport())
        btn.setObjectName("JumpBottomBtn")
        btn.setIcon(icon("arrow_down"))
        btn.setIconSize(QSize(16, 16))
        btn.setFixedSize(36, 36)
        btn.setToolTip("跳到底部")
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(self._on_jump_bottom)
        self._jump_btn = btn
        self._apply_jump_btn_style()
        return btn

    def _apply_jump_btn_style(self):
        if self._jump_btn is None:
            return
        bg = "#ffffff"
        if self._theme is not None:
            bg = self._theme.color("bubble_other", bg)
        self._jump_btn.setStyleSheet(
            f"QToolButton{{background:{bg};"
            f"border:1px solid rgba(128,128,128,90);border-radius:18px;}}"
            f"QToolButton:hover{{border:1px solid rgba(128,128,128,180);}}")

    def _on_jump_bottom(self, *_):
        self.scroll_to_bottom()

    def _update_jump_btn(self):
        bar = self.verticalScrollBar()
        away = bar.maximum() - bar.value() > 200 and self._model.rowCount() > 0
        if not away:
            if self._jump_btn is not None and self._jump_btn.isVisible():
                self._jump_btn.hide()
            return
        self._ensure_jump_btn()
        self._position_jump_btn()
        self._jump_btn.show()
        self._jump_btn.raise_()

    def _position_jump_btn(self):
        if self._jump_btn is None or not self._jump_btn.isVisible():
            return
        vp = self.viewport()
        self._jump_btn.move(vp.width() - 36 - 16, vp.height() - 36 - 16)
        self._jump_btn.raise_()

    def _on_model_batch_finished(self):
        """模型分批加载完成。"""
        if self.is_batching_history or self._first_page_pending:
            return
        bar = self.verticalScrollBar()
        if bar.value() >= bar.maximum() - 100:
            self.scroll_to_bottom()

    # ------------------------------------------------------------------ #
    # 左键命中：头像 → 资料卡；图片 → 查看器；文件 → 保存对话框；链接 → 浏览器
    # ------------------------------------------------------------------ #
    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        # 多选转发模式下，点任意一行仅切换勾选，不做文本选区/锚点等
        if self._multi_select:
            pos = event.pos()
            index = self.indexAt(pos)
            if index.isValid():
                msg = index.data(RoleMessage)
                if msg:
                    self._toggle_ms_selected(msg)
            return
        pos = event.pos()
        index = self.indexAt(pos)
        if index.isValid():
            msg = index.data(RoleMessage)
            bubble, avatar, image = self._rects_at(index)
            if not avatar.isNull() and avatar.contains(pos):
                if msg and msg.sender:
                    self._delegate.flash(msg.id)
                    anchor = self.viewport().mapToGlobal(avatar.bottomLeft())
                    self.profile_requested.emit(msg, anchor)
                    return
            # 群聊「N人未读」状态块点击 → 已读/未读名单浮窗
            # （别人消息的未读块同样可点：未读是房间级状态，名单一样有意义）
            if msg:
                srect = self._delegate.status_rect(index, self.visualRect(index))
                if not srect.isNull() and srect.contains(pos):
                    self._delegate.flash(msg.id)
                    anchor = self.viewport().mapToGlobal(srect.bottomLeft())
                    self.read_receipts_requested.emit(msg, anchor)
                    return
                # 红叉状态图标可点：发送失败消息重新发送
                frect = self._delegate.failed_status_rect(
                    index, self.visualRect(index))
                if not frect.isNull() and frect.contains(pos):
                    self._delegate.flash(msg.id)
                    self.retry_requested.emit(msg)
                    return
                # 表情回应 chip：点击切换自己是否点过（优先于图片/选区）
                for key, rrect in self._delegate.reaction_rects(
                        index, self.visualRect(index)):
                    if rrect.contains(pos):
                        self.reaction_toggle_requested.emit(msg, key)
                        return
            if not image.isNull() and image.contains(pos):
                if msg and msg.msg_type == "image":
                    self._delegate.flash(msg.id)
                    self.image_view_requested.emit(msg)
                    return
                if msg and msg.msg_type == "video":
                    # 视频封面点开不能进图片查看器（视频 URL 无法按图片
                    # 解码，表现为毫无反应）：下载后交给系统播放器
                    self._delegate.flash(msg.id)
                    self._open_file_cached(msg)
                    return
                if msg and msg.msg_type == "file":
                    self._delegate.flash(msg.id)
                    self._open_file_cached(msg)
                    return
                if self._open_attachment(msg):
                    self._delegate.flash(msg.id)
                    return
            # 文件/音频/无封面视频没有专用命中矩形（image_rect 为空），
            # 点气泡即打开；音频先试播放钮
            if not bubble.isNull() and bubble.contains(pos) and msg:
                if msg.msg_type == "audio":
                    arect = self._delegate.audio_btn_rect(
                        index, self.visualRect(index))
                    if not arect.isNull() and arect.contains(pos):
                        self._delegate.flash(msg.id)
                        self._toggle_audio_play(msg)
                        return
                if msg.msg_type in ("file", "video", "audio"):
                    self._delegate.flash(msg.id)
                    self._open_file_cached(msg)
                    return
            # 合并卡片：先命中文件链接/图片打开，否则在条目内容内起文本选区
            if msg and msg.msg_type == "merge":
                if not bubble.isNull() and bubble.contains(pos):
                    href = self._merge_anchor_at(msg, index, pos)
                    if href:
                        self._delegate.flash(msg.id)
                        QDesktopServices.openUrl(QUrl(href))
                        return
                    img_src = self._merge_image_at(msg, index, pos)
                    if img_src:
                        self._delegate.flash(msg.id)
                        self.inline_image_view_requested.emit(img_src)
                        return
                    if self._begin_merge_selection(msg, index, pos):
                        return
            # 引用块点击 → 跳转原消息（优先于文本选区起手）
            if msg and msg.msg_type == "quote" and msg.quote_msg_id:
                qrect = self._delegate.quote_block_rect(index, self.visualRect(index))
                if not qrect.isNull() and qrect.contains(pos):
                    self._delegate.flash(msg.id)
                    self.quote_jump_requested.emit(msg.quote_msg_id)
                    return
            # 文本区域内的链接 / @提及 点击检测（图片消息只有说明文字可选）
            if msg and msg.msg_type in ("text", "quote", "image"):
                if bubble.contains(pos):
                    if msg.msg_type in ("text", "quote"):
                        href = self._text_anchor_at(msg, bubble, pos)
                        if href:
                            self._delegate.flash(msg.id)
                            if href.startswith("mention:"):
                                self.mention_clicked.emit(
                                    href[len("mention:"):],
                                    self._mention_anchor_rect(msg, index,
                                                              bubble, pos))
                            else:
                                QDesktopServices.openUrl(QUrl(href))
                            return
                        # 内联 markdown 图片（无外层链接）→ 弹原图查看器
                        img_src = self._text_img_at(msg, bubble, pos)
                        if img_src:
                            self._delegate.flash(msg.id)
                            self.inline_image_view_requested.emit(img_src)
                            return
                    # 在文本气泡/图片说明内按下：设置选区锚点，拖拽延伸
                    if self._begin_text_selection(msg, bubble, pos):
                        return
                elif msg.msg_type in ("text", "quote") \
                        and self.visualRect(index).contains(pos):
                    # 气泡紧贴文本，短消息两侧大片是行内空白；用户习惯
                    # 从空白处起手拖拽，这里也设锚点（FuzzyHit 吸附最近字符）
                    if self._begin_text_selection(msg, bubble, pos):
                        return
        # 按在其他位置：清除选区
        self._clear_text_selection()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """头像/图片/链接上显示手型光标，提示可点；拖拽延伸文本选区。"""
        pos = event.pos()
        # 文本选区拖拽
        if (self._sel_press_msg_id is not None
                and event.buttons() & Qt.LeftButton):
            self._extend_text_selection(pos)
            return
        clickable = False
        index = self.indexAt(pos)
        if index.isValid():
            msg = index.data(RoleMessage)
            _bubble, avatar, image = self._rects_at(index)
            clickable = ((not avatar.isNull() and avatar.contains(pos))
                         or (not image.isNull() and image.contains(pos)))
            if not clickable and msg and msg.msg_type in ("file", "audio"):
                clickable = _bubble.contains(pos)
            if (not clickable and msg and msg.msg_type == "quote"
                    and msg.quote_msg_id):
                qrect = self._delegate.quote_block_rect(index, self.visualRect(index))
                clickable = not qrect.isNull() and qrect.contains(pos)
            if not clickable and msg:
                srect = self._delegate.status_rect(index, self.visualRect(index))
                clickable = not srect.isNull() and srect.contains(pos)
            if not clickable and msg:
                frect = self._delegate.failed_status_rect(
                    index, self.visualRect(index))
                clickable = not frect.isNull() and frect.contains(pos)
            if not clickable and msg:
                clickable = any(
                    rrect.contains(pos) for _key, rrect
                    in self._delegate.reaction_rects(
                        index, self.visualRect(index)))
            if not clickable and msg and _bubble.contains(pos) and msg.msg_type in ("text", "quote"):
                clickable = (self._text_anchor_at(msg, _bubble, pos) is not None
                             or self._text_img_at(msg, _bubble, pos) is not None)
            if not clickable and msg and msg.msg_type == "merge" and _bubble.contains(pos):
                clickable = (self._merge_anchor_at(msg, index, pos) is not None
                             or self._merge_image_at(msg, index, pos) is not None)
        # 只在可点击处设手型；否则 unsetCursor 恢复继承父窗口光标。
        # 不能设成 ArrowCursor：主窗口在缩放热区会 setCursor(缩放箭头)，
        # 这里每帧把它覆盖回 ArrowCursor 会让聊天视口上缩放箭头不显示
        # （viewport 继承 ChatView 光标，比 MainWindow 的祖先光标优先）。
        if clickable:
            self.setCursor(Qt.PointingHandCursor)
        else:
            self.unsetCursor()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            # 结束拖拽，保留选区供 Ctrl+C / 右键复制
            self._sel_press_msg_id = None
            self._sel_press_bubble = None
            self._sel_press_merge = False
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        """双击文本气泡：自己消息进编辑，他人消息发起引用回复（微信同款）。
        仅命中文本/引用气泡本体（非头像/图片/状态块）时触发。"""
        if event.button() != Qt.LeftButton or self._multi_select:
            super().mouseDoubleClickEvent(event)
            return
        pos = event.pos()
        index = self.indexAt(pos)
        if not index.isValid():
            super().mouseDoubleClickEvent(event)
            return
        msg = index.data(RoleMessage)
        if not msg:
            super().mouseDoubleClickEvent(event)
            return
        bubble, avatar, image = self._rects_at(index)
        # 头像/图片/气泡外的双击交给默认处理（不做编辑/引用）
        if bubble.isNull() or not bubble.contains(pos):
            super().mouseDoubleClickEvent(event)
            return
        if (not avatar.isNull() and avatar.contains(pos)) \
                or (not image.isNull() and image.contains(pos)):
            super().mouseDoubleClickEvent(event)
            return
        # 仅文本/引用消息支持双击编辑或引用；图片/文件/合并/系统消息不触发
        if msg.msg_type not in ("text", "quote"):
            super().mouseDoubleClickEvent(event)
            return
        # 自己的文本消息 → 编辑（与右键菜单条件一致：E2E 房间/转发不回传）
        if (msg.is_mine and msg.msg_type == "text" and not msg.is_forwarded
                and not self._room_encrypted):
            self.edit_requested.emit(msg)
        else:
            # 他人消息或不可编辑文本 → 引用回复
            self.quote_requested.emit(msg)

    def keyPressEvent(self, event):
        if (event.matches(QKeySequence.Copy) and self._selected_text()):
            QApplication.clipboard().setText(self._selected_text())
            return
        super().keyPressEvent(event)

    def _rects_at(self, index):
        return self._delegate.layout_rects(index, self.visualRect(index))

    def flash_row(self, msg_id: str):
        """滚动到指定消息并短暂高亮（点击引用块跳转原消息）。"""
        row = self._model.find_message_row(msg_id)
        if row < 0:
            return
        index = self._model.index(row, 0)
        self.scrollTo(index, QAbstractItemView.EnsureVisible)
        self._model.flash_msg_id = msg_id
        self._model.dataChanged.emit(index, index)
        QTimer.singleShot(900, lambda: self._clear_flash(msg_id))

    def _clear_flash(self, msg_id: str):
        if self._model.flash_msg_id != msg_id:
            return
        self._model.flash_msg_id = None
        row = self._model.find_message_row(msg_id)
        if row >= 0:
            self._model.dataChanged.emit(self._model.index(row, 0),
                                         self._model.index(row, 0))

    def _attachment_headers(self, url: str) -> dict:
        """附件下载认证头（/file-upload/ 需要，与 _save_attachment 同源）。

        只在同源时给出：服务器消息里的附件 URL 可以是任意主机，
        无脑挂令牌等于把会话凭据送给那个主机。
        """
        headers = {}
        if not self.image_loader:
            return headers
        server = getattr(self.image_loader, "server_url", "")
        if not can_carry_credentials(url or "", server):
            return headers
        if getattr(self.image_loader, "auth_token", ""):
            headers["X-Auth-Token"] = self.image_loader.auth_token
        if getattr(self.image_loader, "auth_uid", ""):
            headers["X-User-Id"] = self.image_loader.auth_uid
        return headers

    def _absolute_url(self, url: str) -> str:
        """相对路径（/file-upload/…）按 image_loader 补成绝对地址。"""
        if url and url.startswith("/") and self.image_loader:
            return self.image_loader.resolve(url)
        return url or ""

    def open_room_file(self, url: str, name: str) -> None:
        """下载文件到缓存后用系统程序打开（点击消息附件与文件面板共用入口）。"""
        if not url:
            return
        url = self._absolute_url(url)
        dest = file_cache_dest(url, name or "attachment")
        self._ensure_cached_file(
            url, dest,
            on_ready=lambda p: QDesktopServices.openUrl(QUrl.fromLocalFile(p)))

    def _ensure_cached_file(self, url: str, dest: str, on_ready,
                            on_fail=None) -> None:
        """确保 url 落到 dest，就绪后在**主线程**回调 on_ready(path)。

        点击打开附件与离线 OCR 都要「已缓存立刻用、没缓存带认证头下载」，
        以前只有 open_room_file 一份实现，OCR 再接一遍会容易走样。
        下载仍走 _downloading 去重 + .part 原子改名（半截文件不能被
        当成已缓存）。
        """
        try:
            if os.path.getsize(dest) > 0:
                on_ready(dest)
                return
        except (OSError, ValueError):
            pass
        if url in self._downloading:
            self.status_message.emit("文件正在下载，请稍候…", 3000)
            return

        def _proc():
            import requests
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".part"
            target = url
            # 手动跟跳转，不用 allow_redirects：requests 跨主机只剥
            # Authorization，我们的 X-Auth-Token/X-User-Id 会被原样带到
            # 任意重定向目标。每一跳都重新按同源判定决定带不带凭据。
            for _ in range(MAX_REDIRECT_HOPS + 1):
                with requests.get(target, stream=True, timeout=60,
                                  headers=self._attachment_headers(target),
                                  verify=getattr(self.image_loader,
                                                 "ssl_verify", True),
                                  allow_redirects=False) as resp:
                    nxt = redirect_target(resp, target)
                    if nxt:
                        target = nxt
                        continue
                    if is_redirect(resp.status_code):
                        raise OSError("重定向没有给出可下载的目标地址")
                    resp.raise_for_status()
                    with open(tmp, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                    break
            else:
                raise OSError(f"重定向超过 {MAX_REDIRECT_HOPS} 跳")
            os.replace(tmp, dest)
            return dest

        def _done(path):
            self._downloading.discard(url)
            on_ready(path)

        def _fail(err):
            self._downloading.discard(url)
            # 半截 .part 清掉：下次失败不会叠加残留，也便于排查目录
            try:
                os.remove(dest + ".part")
            except OSError:
                pass
            self.status_message.emit(f"下载失败：{err}", 4000)
            if on_fail:
                on_fail(str(err))

        self._downloading.add(url)
        run_async(_proc, on_done=_done, on_fail=_fail, parent=self)

    def local_image_path(self, url: str, on_ready, on_fail=None) -> None:
        """把图片 URL 变成本机文件路径，供离线 OCR 使用。

        顺序：磁盘原图缓存（气泡显示过就一定有，且免网络）→ 已下载的文件
        缓存 → 带认证头下载。服务器会清理历史上传（实测带认证头仍 404），
        所以缓存没命中时要如实报错，不能卡在「识别中」。
        """
        url = self._absolute_url(url)
        if not url:
            if on_fail:
                on_fail("没有可识别的图片")
            return
        cached = (self.image_loader.cached_original_path(url)
                  if self.image_loader else None)
        if cached:
            on_ready(cached)
            return
        name = os.path.basename(QUrl(url).path()) or "image.png"
        dest = file_cache_dest(url, name)
        self._ensure_cached_file(url, dest, on_ready, on_fail)

    def _open_file_cached(self, msg) -> None:
        """点击文件附件：下载到缓存路径后用系统程序打开；已缓存直接打开。"""
        if not msg.file_url:
            return
        self.open_room_file(msg.file_url, msg.file_name or "attachment")

    # ------------------------------------------------------------------ #
    # 音频消息内联播放
    # ------------------------------------------------------------------ #
    def _toggle_audio_play(self, msg: Message):
        """点音频卡播放钮：未播→下载并播放；播放中→暂停；暂停中→续播。

        换另一条时自动停前一条；QtMultimedia 不可用直接退回系统播放器。
        """
        if not _AUDIO_PLAYBACK_OK:
            self._open_file_cached(msg)
            return
        if self._audio_msg_id == msg.id:
            player = self._audio_player
            if player is not None:
                if player.state() == QMediaPlayer.PlayingState:
                    player.pause()
                else:
                    player.play()
                self._update_audio_state()
            return
        self._stop_audio()
        url = self._absolute_url(msg.file_url or "")
        if not url:
            return
        dest = file_cache_dest(url, msg.file_name or "audio")
        self._ensure_cached_file(
            url, dest,
            on_ready=lambda p, mid=msg.id: self._start_audio(mid, p),
            on_fail=lambda e, mid=msg.id: self._audio_start_failed(mid, e))

    def _start_audio(self, msg_id: str, path: str):
        """缓存就绪：建播放器起播。迟到回调（已切走/换曲）直接丢弃。"""
        if self._audio_msg_id and self._audio_msg_id != msg_id:
            return
        try:
            player = QMediaPlayer(self)
        except Exception:  # noqa: BLE001 —— 无音频设备等环境问题降级
            log.exception("QMediaPlayer 创建失败")
            return
        self._audio_player = player
        self._audio_msg_id = msg_id
        player.setMedia(QMediaContent(QUrl.fromLocalFile(path)))
        player.durationChanged.connect(self._on_audio_duration)
        player.mediaStatusChanged.connect(self._on_audio_status)
        player.stateChanged.connect(self._on_audio_state_changed)
        player.play()
        self._audio_timer.start()
        self._update_audio_state()

    def _audio_start_failed(self, msg_id: str, err: str):
        if self._audio_msg_id == msg_id:
            self._stop_audio()

    def _on_audio_duration(self, dur: int):
        if dur > 0 and self._audio_msg_id:
            self._audio_dur[self._audio_msg_id] = dur
        self._update_audio_state()

    def _on_audio_status(self, status):
        if not _AUDIO_PLAYBACK_OK:
            return
        if status == QMediaPlayer.InvalidMedia:
            # 服务器编码不在 WMF 解码范围（如部分 ogg）：如实告知并回退
            self.status_message.emit(
                "系统不支持该音频格式，已用系统播放器打开", 4000)
            msg_id = self._audio_msg_id
            self._stop_audio()
            row = self._model.find_message_row(msg_id)
            if row >= 0:
                msg = self._model.index(row, 0).data(RoleMessage)
                if msg:
                    self._open_file_cached(msg)

    def _on_audio_state_changed(self, *_):
        player = self._audio_player
        if player is not None and player.state() == QMediaPlayer.StoppedState:
            # 播完自然停止：进度复位（手动暂停走 PausedState，不进这里）
            if 0 < player.duration() <= player.position() + 30:
                player.setPosition(0)
        self._update_audio_state()

    def _on_audio_tick(self):
        self._update_audio_state()

    def _update_audio_state(self):
        """把播放器当前状态喂给 delegate 并只重绘那一行。"""
        player = self._audio_player
        if player is None or not self._audio_msg_id:
            return
        playing = player.state() == QMediaPlayer.PlayingState
        pos = player.position()
        dur = player.duration() or self._audio_dur.get(
            self._audio_msg_id, 0)
        self._delegate.set_audio_state(self._audio_msg_id, playing, pos, dur)
        row = self._model.find_message_row(self._audio_msg_id)
        if row >= 0:
            idx = self._model.index(row, 0)
            self._model.dataChanged.emit(idx, idx)

    def _stop_audio(self):
        self._audio_timer.stop()
        mid = self._audio_msg_id
        if self._audio_player is not None:
            try:
                self._audio_player.stop()
                self._audio_player.setMedia(QMediaContent())
            except RuntimeError:  # noqa: BLE001 —— 已析构
                pass
            self._audio_player.deleteLater()
            self._audio_player = None
        self._audio_msg_id = ""
        if mid:
            # 进度复位、保留已知时长（第二行不再显示「语音消息」占位）
            self._delegate.clear_audio_state(
                mid, self._audio_dur.get(mid, 0))
            row = self._model.find_message_row(mid)
            if row >= 0:
                idx = self._model.index(row, 0)
                self._model.dataChanged.emit(idx, idx)

    def _open_attachment(self, msg) -> bool:
        """用系统默认程序打开附件（图片走原始 URL）。"""
        if not msg or not msg.file_url:
            return False
        url = msg.file_url
        if url.startswith("/") and self.image_loader:
            url = self.image_loader.resolve(url)
        # 附件 URL 来自服务器/他人消息：openUrl 在 Windows 上是
        # ShellExecute，file:// 之类会被解释成本地执行或 UNC 认证外泄
        if not is_openable_url(url):
            self.status_message.emit("该附件链接不可直接打开", 4000)
            return False
        return QDesktopServices.openUrl(QUrl(url))

    # ------------------------------------------------------------------ #
    # 文本命中 / 选区
    # ------------------------------------------------------------------ #
    def _selectable_doc(self, msg: Message, bubble_rect):
        """消息可选中文本的 (doc, 绘制原点)；无文本返回 (None, QPoint())。

        选区命中、拖拽延伸、取文本与委托绘制必须同源：普通文本在气泡
        内边距内起笔（逐条转发上面还有标记行），引用正文在引用块下方，
        图片说明画在图片下方的通栏区域（无内边距）。
        """
        from .message_model import _BUBBLE_PAD, _FORWARD_TAG_H
        if msg.msg_type == "image":
            cap = self._model.image_caption(msg)
            if not cap:
                return None, QPoint()
            width = bubble_rect.width()
            doc = self._model._caption_doc(cap, width)
            # 绘制端：img_h = 气泡高 - 回应条 - 说明高（含间距），
            # 说明紧随图片，顶边即气泡底 - 回应条 - 文本高
            reactions_h = self._model.reaction_band(msg, width)[1]
            top = (bubble_rect.height() - reactions_h
                   - int(doc.size().height()))
            return doc, QPoint(0, top)
        text = msg.text or msg.display_text or ""
        if not text:
            return None, QPoint()
        content_w = bubble_rect.width() - _BUBBLE_PAD * 2
        doc = self._delegate._build_text_doc(msg, content_w)
        top = _BUBBLE_PAD
        if msg.msg_type == "quote":
            # 回复正文画在引用块下方（_paint_quote_content 里起笔 =
            # content.top + 引用块高 + 4）
            top += self._model.quote_block_height(msg, content_w) + 4
        elif msg.is_forwarded:
            top += _FORWARD_TAG_H
        return doc, QPoint(_BUBBLE_PAD, top)

    def _text_hit_pos(self, msg: Message, bubble_rect, pos, fuzzy: bool):
        """把气泡内坐标转成 (doc 内字符位置, doc)；命不中返回 (None, None)。

        文档与原点都取自 _selectable_doc（与委托绘制同源），否则换行/
        偏移对不上，选区锚点、链接与 @提及都会错位。
        """
        doc, origin = self._selectable_doc(msg, bubble_rect)
        if doc is None:
            return None, None
        local = pos - bubble_rect.topLeft() - origin
        if msg.msg_type == "quote" and local.y() < 0:
            return None, doc
        if msg.msg_type == "image":
            # 图片说明上下分别是图片与回应条，从图上传来的拖拽不算命中；
            # 文本消息保留「行内空白起手」的吸附行为，不做纵向裁剪
            if local.y() < 0 or local.y() > doc.size().height():
                return None, doc
        mode = Qt.FuzzyHit if fuzzy else Qt.ExactHit
        pos_in_doc = doc.documentLayout().hitTest(local, mode)
        if pos_in_doc < 0:
            return None, doc
        return pos_in_doc, doc

    def _text_anchor_at(self, msg: Message, bubble_rect, click_pos) -> str | None:
        """检测气泡内点击是否命中 <a> 标签，返回 href 或 None。"""
        from PyQt5.QtGui import QTextCursor
        pos_in_doc, doc = self._text_hit_pos(msg, bubble_rect, click_pos,
                                             fuzzy=False)
        if pos_in_doc is None:
            return None
        cursor = QTextCursor(doc)
        cursor.setPosition(min(pos_in_doc, max(0, doc.characterCount() - 1)))
        fmt = cursor.charFormat()
        if fmt.isAnchor():
            return fmt.anchorHref()
        return None

    def _mention_anchor_rect(self, msg, index, bubble_rect, pos):
        """@提及的锚定矩形（全局坐标）：x 取 @ 字符左缘，上下取 @ 所在
        文本行。

        资料卡据此贴着 @ 行下方弹出、左边与 @ 对齐。不用整行底部：
        多行长消息会让卡片离 @ 很远。命中失败返回 None，由调用方回退
        屏幕居中。文档几何与 _text_hit_pos 同源，charFormat 判定与
        _text_anchor_at 一致（同一 ExactHit 命中点）。
        """
        from PyQt5.QtGui import QTextCursor
        pos_in_doc, doc = self._text_hit_pos(msg, bubble_rect, pos,
                                             fuzzy=False)
        if pos_in_doc is None or doc is None:
            return None
        cur = QTextCursor(doc)
        cur.setPosition(min(pos_in_doc, max(0, doc.characterCount() - 1)))
        if not cur.charFormat().isAnchor():
            return None
        href = cur.charFormat().anchorHref()
        # 命中的可能是提及中任意字符，回退到提及起点（@ 字符）
        start = pos_in_doc
        while start > 0:
            cur.setPosition(start - 1)
            fmt = cur.charFormat()
            if not fmt.isAnchor() or fmt.anchorHref() != href:
                break
            start -= 1
        block = doc.findBlock(start)
        rel = start - block.position()
        line = block.layout().lineForTextPosition(rel)
        if not line.isValid():
            return None
        x_rel, _cp = line.cursorToX(rel)
        from .message_model import _BUBBLE_PAD
        blk = doc.documentLayout().blockBoundingRect(block)
        char_x = bubble_rect.left() + _BUBBLE_PAD + blk.left() + line.x() + x_rel
        line_top = (bubble_rect.top() + _BUBBLE_PAD + blk.top()
                    + line.y())
        if msg.msg_type == "quote":
            # 回复正文画在引用块下方，屏幕坐标要加回同一偏移
            # （与 _text_hit_pos、_paint_quote_content 同源）
            line_top += (self._model.quote_block_height(
                msg, bubble_rect.width() - _BUBBLE_PAD * 2) + 4)
        # doc 布局坐标是视口局部坐标，popup 的 move 吃全局屏幕坐标，
        # 漏掉视口偏移会把卡片甩到屏幕最左
        top_left = self.viewport().mapToGlobal(
            QPoint(int(round(char_x)), int(round(line_top))))
        return QRect(top_left.x(), top_left.y(), 1,
                     int(round(line.height())))

    def _text_img_at(self, msg: Message, bubble_rect, click_pos) -> str | None:
        """检测气泡内点击是否命中内联 markdown <img>，返回其 src 或 None。

        与 _text_anchor_at 同源（同一 _text_hit_pos 命中），命中处字符若
        是图片格式则取 imageName（=html 里 <img src> 原样）。链接式图片
        [![a](i)](url) 已被 <a> 包裹，由 _text_anchor_at 先命中返回 href，
        不会走到这里。
        """
        from PyQt5.QtGui import QTextCursor
        pos_in_doc, doc = self._text_hit_pos(msg, bubble_rect, click_pos,
                                             fuzzy=True)
        if pos_in_doc is None:
            return None
        for p in (pos_in_doc, pos_in_doc - 1, pos_in_doc + 1):
            if 0 <= p < doc.characterCount():
                cursor = QTextCursor(doc)
                cursor.setPosition(p)
                fmt = cursor.charFormat()
                if fmt.isImageFormat():
                    return fmt.toImageFormat().name()
        return None

    # ------------------------------------------------------------------ #
    # 合并卡片命中 / 选区
    # ------------------------------------------------------------------ #
    def _merge_layout(self, msg: Message, index) -> list:
        """重建合并卡片的内容命中布局（视口坐标）。

        merge_content_layout 已由委托/模型用于高度与绘制，这里用同一套
        布局做锚点命中与文本选区定位，保证点击位置与绘制位置一致。
        """
        from .message_model import _BUBBLE_PAD
        bubble, _a, _i = self._delegate.layout_rects(
            index, self.visualRect(index))
        if bubble.isNull():
            return []
        content = bubble.adjusted(_BUBBLE_PAD, _BUBBLE_PAD,
                                  -_BUBBLE_PAD, -_BUBBLE_PAD)
        return self._model.merge_content_layout(msg, content)

    def _merge_anchor_at(self, msg: Message, index, hit_pos) -> str | None:
        """合并卡片内点击是否命中文件 markdown 链接，返回 href 或 None。"""
        from PyQt5.QtGui import QTextCursor
        for item in self._merge_layout(msg, index):
            doc = item["doc"]
            rect = item["rect"]
            if doc is None or not rect.contains(hit_pos):
                continue
            local = hit_pos - rect.topLeft()
            p = doc.documentLayout().hitTest(local, Qt.ExactHit)
            if p < 0:
                continue
            cursor = QTextCursor(doc)
            cursor.setPosition(min(p, max(0, doc.characterCount() - 1)))
            fmt = cursor.charFormat()
            if fmt.isAnchor():
                return fmt.anchorHref()
        return None

    def _merge_image_at(self, msg: Message, index, hit_pos) -> str | None:
        """合并卡片内点击是否命中方案二图片条目，返回其 image_url 或 None。

        图片条目在 merge_content_layout 里 doc 为 None、entry 带 att.image_url
        （文本条目才有 doc），据此与文件链接/文本条目区分。
        """
        for item in self._merge_layout(msg, index):
            if item["doc"] is not None:
                continue
            if not item["rect"].contains(hit_pos):
                continue
            entry = item.get("entry") or {}
            att = entry.get("att") or {}
            url = att.get("image_url") or ""
            if url:
                return url
        return None

    def _begin_merge_selection(self, msg: Message, index, hit_pos) -> bool:
        """在合并卡片某条目内容内按下：命中锚定该条 doc 的选区起点。"""
        model = self._model
        for i, item in enumerate(self._merge_layout(msg, index)):
            doc = item["doc"]
            rect = item["rect"]
            if doc is None or not rect.contains(hit_pos):
                continue
            local = hit_pos - rect.topLeft()
            p = doc.documentLayout().hitTest(local, Qt.FuzzyHit)
            if p < 0:
                continue
            model.sel_msg_id = msg.id
            model.sel_merge_idx = i
            model.sel_start = p
            model.sel_end = p
            self._sel_press_msg_id = msg.id
            self._sel_press_merge = True
            self.viewport().update()
            return True
        return False

    def _begin_text_selection(self, msg, bubble_rect, pos) -> bool:
        """在文本气泡内按下：设置选区锚点。返回是否接管了该按下事件。"""
        model = self._model
        anchor, _doc = self._text_hit_pos(msg, bubble_rect, pos, fuzzy=True)
        if anchor is None:
            return False
        model.sel_msg_id = msg.id
        model.sel_start = anchor
        model.sel_end = anchor
        # 清掉可能残留的合并卡片选区标记，否则 _selected_text 会按合并
        # 分支取文本（对普通消息返回空串，复制失效）
        model.sel_merge_idx = -1
        self._sel_press_msg_id = msg.id
        self._sel_press_bubble = QRect(bubble_rect)
        self.viewport().update()
        return True

    def _extend_text_selection(self, pos):
        """拖拽过程中延伸选区。"""
        msg_id = self._sel_press_msg_id
        if msg_id is None:
            return
        model = self._model
        row = model.find_message_row(msg_id)
        if row < 0:
            self._clear_text_selection()
            return
        msg = model.get_message(row)
        # 合并卡片选区按条目 doc 延伸，其余走常规文本 doc
        if self._sel_press_merge:
            index = model.index(row, 0)
            layout = self._merge_layout(msg, index)
            if model.sel_merge_idx < 0 or model.sel_merge_idx >= len(layout):
                self._clear_text_selection()
                return
            item = layout[model.sel_merge_idx]
            doc = item["doc"]
            rect = item["rect"]
            if doc is None or not rect.contains(pos):
                return
            local = pos - rect.topLeft()
            p = doc.documentLayout().hitTest(local, Qt.FuzzyHit)
            if p < 0:
                return
            if p != model.sel_end:
                model.sel_end = p
                self.viewport().update()
            return
        bubble = self._sel_press_bubble
        if bubble is None:
            self._clear_text_selection()
            return
        pos_in_doc, _doc = self._text_hit_pos(msg, bubble, pos, fuzzy=True)
        if pos_in_doc is None:
            return
        if pos_in_doc != model.sel_end:
            model.sel_end = pos_in_doc
            self.viewport().update()

    def _clear_text_selection(self):
        model = self._model
        if model.sel_msg_id is not None:
            model.sel_msg_id = None
            model.sel_start = 0
            model.sel_end = 0
            model.sel_merge_idx = -1
            self.viewport().update()
        self._sel_press_msg_id = None
        self._sel_press_bubble = None
        self._sel_press_merge = False

    def _selected_text(self) -> str:
        """当前选中的纯文本，无选区返回空串。"""
        from PyQt5.QtGui import QTextCursor
        model = self._model
        if not model.sel_msg_id or model.sel_start == model.sel_end:
            return ""
        row = model.find_message_row(model.sel_msg_id)
        if row < 0:
            return ""
        msg = model.get_message(row)
        if msg is None:
            return ""
        # 合并卡片：从选中的那条内容 doc 里取文本
        if model.sel_merge_idx >= 0:
            index = model.index(row, 0)
            layout = self._merge_layout(msg, index)
            if model.sel_merge_idx >= len(layout):
                return ""
            doc = layout[model.sel_merge_idx]["doc"]
            if doc is None:
                return ""
            max_pos = max(0, doc.characterCount() - 1)
            a = min(model.sel_start, model.sel_end, max_pos)
            b = min(max(model.sel_start, model.sel_end), max_pos)
            if a >= b:
                return ""
            cursor = QTextCursor(doc)
            cursor.setPosition(a)
            cursor.setPosition(b, QTextCursor.KeepAnchor)
            return cursor.selectedText().replace("\u2029", "\n")
        if msg.msg_type == "image":
            # 图片消息可选中的只有下方说明（正文里是「[图片]」占位）
            cap = model.image_caption(msg)
            doc = model._caption_doc(cap, 10 ** 6) if cap else None
        else:
            doc = self._delegate._build_text_doc(msg, 10 ** 6)
        if doc is None:
            return ""
        cursor = QTextCursor(doc)
        max_pos = max(0, doc.characterCount() - 1)
        cursor.setPosition(min(model.sel_start, model.sel_end, max_pos))
        cursor.setPosition(min(max(model.sel_start, model.sel_end), max_pos),
                           QTextCursor.KeepAnchor)
        # QTextDocument 用 U+2029 表示换行，剪贴板需要 \n
        return cursor.selectedText().replace("\u2029", "\n")

    # ------------------------------------------------------------------ #
    # 右键菜单
    # ------------------------------------------------------------------ #
    def contextMenuEvent(self, event):
        """视口右键菜单。"""
        pos = event.pos()
        index = self.indexAt(pos)
        if not index.isValid():
            return
        msg = index.data(RoleMessage)
        if not msg:
            return
        self._show_context_menu(msg, event.globalPos())

    def _show_context_menu(self, msg: Message, global_pos):
        """按消息类型弹出右键菜单：复制 / 引用回复 / 撤回 / 保存附件。"""
        menu = QMenu(self)

        # 发送失败：重新发送（仅文本/引用的乐观回显；文件/混排没有可回滚的
        # 单条本地消息，不提供该入口）
        if (msg.is_mine and msg.status == "failed"
                and msg.id.startswith("local-")):
            retry_action = menu.addAction(icon("send"), "重新发送")
            retry_action.triggered.connect(
                lambda _=False: self.retry_requested.emit(msg))
            menu.addSeparator()

        # 复制（纯文本/选区）
        copy_action = menu.addAction(icon("copy"), "复制")
        copy_action.triggered.connect(lambda: self._copy_to_clipboard(msg))

        # 复制为图片（按消息渲染样式输出 PNG；仅纯文字消息渲染路径一致）
        if msg.msg_type == "text":
            copy_img_action = menu.addAction(icon("copy"), "复制为图片")
            copy_img_action.triggered.connect(
                lambda: self._copy_msg_as_image(msg))

        # 复制链接（图片/文件原址；图文混排取内联图片链接）
        links = self._copyable_links(msg)
        if links:
            link_action = menu.addAction(icon("copy"), "复制链接")
            link_action.triggered.connect(
                lambda: QApplication.clipboard().setText("\n".join(links)))

        # 表情回应（系统消息无意义）
        if msg.msg_type != "system":
            react_action = menu.addAction(icon("emoji"), "添加表情回应…")
            react_action.triggered.connect(
                lambda _=False: self.reaction_add_requested.emit(msg))

        # 引用回复
        quote_action = menu.addAction(icon("quote"), "引用回复")
        quote_action.triggered.connect(lambda: self.quote_requested.emit(msg))

        # AI 助手（仅带正文的文本类消息；E2E 明文外发的二次确认在主窗口）
        if self._ai_available and msg.msg_type in ("text", "quote") \
                and (msg.text or "").strip():
            ai_menu = menu.addMenu(icon("ai"), "AI 助手")
            for key, label in (("translate", "翻译"),
                               ("explain", "解释"),
                               ("todos", "提取待办"),
                               ("proofread", "校对纠错"),
                               ("reply", "起草回复")):
                act = ai_menu.addAction(label)
                act.triggered.connect(
                    lambda _=False, k=key: self.ai_action_requested.emit(msg, k))

        # 转发（系统消息无转发意义）
        if msg.msg_type != "system":
            fwd_action = menu.addAction(icon("forward"), "转发")
            fwd_action.triggered.connect(
                lambda: self.forward_requested.emit([msg]))
            menu.addSeparator()
            multi_action = menu.addAction(icon("forward"), "多选转发")
            multi_action.triggered.connect(
                lambda: self._enter_multi_select())

        # 置顶 / 星标（系统消息无意义；乐观翻转由主窗口负责）
        if msg.msg_type != "system":
            pin_label = "取消置顶" if msg.pinned else "置顶消息"
            pin_action = menu.addAction(icon("pin"), pin_label)
            pin_action.triggered.connect(
                lambda _=False: self.pin_requested.emit(msg, not msg.pinned))
            star_label = "取消星标" if msg.starred else "星标消息"
            star_action = menu.addAction(icon("favorite"), star_label)
            star_action.triggered.connect(
                lambda _=False: self.star_requested.emit(msg, not msg.starred))

        # 编辑（仅自己发出的纯文本；E2E 房间明文不回传服务端，不提供入口）
        if (msg.is_mine and msg.msg_type == "text" and not msg.is_forwarded
                and not self._room_encrypted):
            edit_action = menu.addAction(icon("edit"), "编辑")
            edit_action.triggered.connect(
                lambda _=False: self.edit_requested.emit(msg))

        # 撤回（仅自己的消息）
        if msg.is_mine and msg.msg_type != "system":
            recall_action = menu.addAction(icon("recall"), "撤回")
            recall_action.triggered.connect(lambda: self.recall_requested.emit(msg))

        # 定时提醒（到点经系统托盘通知，重启后仍生效）；已设提醒的消息
        # 时间行有表盘标记，右键可直接取消
        if msg.msg_type != "system":
            if self._model.remind_for(msg.id):
                cancel_action = menu.addAction(icon("remind"), "取消提醒")
                cancel_action.triggered.connect(
                    lambda _=False: self.remind_cancel_requested.emit(msg))
            remind_menu = menu.addMenu(icon("remind"), "提醒")
            for label, due in (("1 小时后", self._remind_due("1h")),
                               ("3 小时后", self._remind_due("3h")),
                               ("明天 9 点", self._remind_due("tomorrow9"))):
                act = remind_menu.addAction(label)
                act.triggered.connect(
                    lambda _=False, d=due: self.remind_requested.emit(msg, d))

        # 收藏为表情（图片消息原图进 stickers/，从表情包页签发送）
        if msg.msg_type == "image" and msg.file_url:
            sticker_action = menu.addAction("收藏为表情")
            sticker_action.triggered.connect(
                lambda: self._save_sticker(msg.file_url))

        # 保存附件（图片 / 文件 / 视频 / 音频；引用消息可保存原消息附件）
        if msg.file_url and msg.msg_type in ("image", "file", "video", "audio", "quote"):
            menu.addSeparator()
            save_label, save_icon = {
                "image": ("保存图片…", "save_image"),
                "file": ("保存文件…", "save_file"),
                "video": ("保存视频…", "save_video"),
                "audio": ("保存文件…", "save_file"),
            }.get(msg.msg_type, ("保存原文件…", "save_file"))
            save_action = menu.addAction(icon(save_icon), save_label)
            save_action.triggered.connect(lambda: self._save_attachment(msg))

        # 识别文字（本机离线 OCR，不上传任何图片字节）
        images = self._ocr_targets(msg)
        if images:
            menu.addSeparator()
            if len(images) == 1:
                ocr_action = menu.addAction(icon("ocr"), "识别文字")
                ocr_action.triggered.connect(
                    lambda _=False: self.ocr_requested.emit(images[0]))
            else:
                ocr_menu = menu.addMenu(icon("ocr"), "识别文字")
                for idx, one in enumerate(images, 1):
                    act = ocr_menu.addAction(f"图片 {idx}")
                    act.triggered.connect(
                        lambda _=False, u=one: self.ocr_requested.emit(u))

        menu.exec_(global_pos)

    @staticmethod
    def _remind_due(kind: str) -> float:
        """提醒选项 → 到期 epoch 秒。明天 9 点固定在次日（含今天未到 9 点时）。"""
        now = time.time()
        if kind == "tomorrow9":
            lt = time.localtime(now)
            nine = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                9, 0, 0, 0, 0, -1))
            return nine + 86400
        return now + (3600 if kind == "1h" else 3 * 3600)

    def _save_sticker(self, url: str):
        """收藏图片为表情：取本机原图路径（必要时下载）后复制进 stickers/。"""
        from core.stickers import save_sticker
        self.local_image_path(url, on_ready=lambda p: self._sticker_saved(p),
                              on_fail=lambda e: self._sticker_failed(e))

    def _sticker_saved(self, path: str):
        try:
            from core.stickers import save_sticker
            save_sticker(path)
            self.status_message.emit("已收藏为表情", 3000)
        except OSError as e:
            self._sticker_failed(str(e))

    def _sticker_failed(self, err: str):
        self.status_message.emit(f"收藏表情失败：{err}", 4000)

    def _copyable_links(self, msg: Message) -> list:
        """消息里可「复制链接」的地址（绝对 URL，按出现顺序）。

        附件消息（图片/文件/视频/音频/引用）取 file_url 原址；图文混排
        取内联图片链接（优先模型已解析的 canonical 形式）。相对路径按
        image_loader 补全成绝对地址。"""
        links = []
        if msg.file_url and msg.msg_type in ("image", "file", "video",
                                             "audio", "quote"):
            links.append(msg.file_url)
        else:
            for ent in self._model._inline_entries(msg):
                resolved = ent.get("resolved")
                if not resolved and self.image_loader:
                    resolved = self.image_loader.resolve(ent["url"])
                if resolved:
                    links.append(resolved)
        return [self.image_loader.resolve(u)
                if u.startswith("/") and self.image_loader else u
                for u in links]

    def _ocr_targets(self, msg: Message) -> list:
        """消息里可离线识别的图片绝对 URL（只认图片，PDF/视频/音频不做）。"""
        if msg.msg_type == "image" and msg.file_url:
            return [self._absolute_url(msg.file_url)]
        if msg.msg_type in ("file", "video", "audio", "system"):
            return []
        urls = []
        for ent in self._model._inline_entries(msg):
            u = self._absolute_url(ent.get("resolved") or ent.get("url") or "")
            if u and u not in urls:
                urls.append(u)
        return urls

    # ------------------------------------------------------------------ #
    # 多选转发模式
    # ------------------------------------------------------------------ #
    @property
    def multi_select_mode(self) -> bool:
        return self._multi_select

    def _enter_multi_select(self):
        """进入多选转发模式：点消息勾选，底部出操作条。"""
        if self._multi_select:
            return
        self._multi_select = True
        self._ms_selected = []
        self.viewport().update()
        self._ensure_ms_bar().show()
        self._position_ms_bar()
        self._refresh_ms_bar()

    def _ensure_ms_bar(self) -> QWidget:
        if self._ms_bar is not None:
            return self._ms_bar
        bar = QFrame(self.viewport())
        bar.setObjectName("MultiSelectBar")
        # 深色半透明浮层（微信多选条同款），内部按钮图标+文字居中、统一定高
        bar.setStyleSheet(
            "QFrame#MultiSelectBar{background:rgba(38,46,58,235);"
            "border-radius:10px;}"
            "QPushButton{background:transparent;color:#fff;border:none;"
            "border-radius:6px;padding:0 14px;font-size:13px;height:36px;}"
            "QPushButton:hover{background:rgba(255,255,255,32);}"
            "QPushButton#MsMergeBtn{color:#8CF0B1;}"
            "QPushButton#MsEachBtn{color:#8AD0FF;}"
            "QLabel{color:rgba(255,255,255,210);padding:0 6px;font-size:13px;}")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 6, 12, 6)
        lay.setSpacing(6)
        lay.setAlignment(Qt.AlignVCenter)
        self._ms_count = QLabel("已选 0 条")
        lay.addWidget(self._ms_count)
        lay.addStretch(1)
        merge_btn = QPushButton(icon("forward"), "合并转发")
        merge_btn.setObjectName("MsMergeBtn")
        merge_btn.setIconSize(QSize(15, 15))
        merge_btn.clicked.connect(
            lambda: self._finish_multi_select("merge"))
        each_btn = QPushButton(icon("forward"), "逐条转发")
        each_btn.setObjectName("MsEachBtn")
        each_btn.setIconSize(QSize(15, 15))
        each_btn.clicked.connect(
            lambda: self._finish_multi_select("each"))
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self._cancel_multi_select)
        lay.addWidget(merge_btn)
        lay.addWidget(each_btn)
        lay.addWidget(cancel_btn)
        bar.adjustSize()
        self._ms_bar = bar
        return bar

    def _refresh_ms_bar(self):
        if self._ms_count is not None:
            self._ms_count.setText(f"已选 {len(self._ms_selected)} 条")

    def _position_ms_bar(self):
        if self._ms_bar is not None and self._ms_bar.isVisible():
            bar = self._ms_bar
            w = self.viewport().width()
            h = bar.height()
            bar.move(max(0, (w - bar.width()) // 2),
                     max(0, self.viewport().height() - h - 16))
            bar.raise_()

    def _toggle_ms_selected(self, msg: Message):
        """多选模式下点一行 → 勾选/取消勾选并重绘。"""
        if msg.msg_type == "system":
            return
        if msg.id in self._ms_selected:
            self._ms_selected = [m for m in self._ms_selected if m != msg.id]
        else:
            self._ms_selected.append(msg.id)
        row = self._model.find_message_row(msg.id)
        if row >= 0:
            idx = self._model.index(row, 0)
            self._model.dataChanged.emit(idx, idx)
        self._refresh_ms_bar()

    def _ms_messages(self) -> "list":
        order = []
        for mid in self._ms_selected:
            row = self._model.find_message_row(mid)
            if row >= 0:
                order.append(self._model.get_message(row))
        return order

    def _finish_multi_select(self, mode: str):
        msgs = self._ms_messages()
        self._cancel_multi_select()
        if not msgs:
            return
        self.forward_many_requested.emit((msgs, mode))

    def _cancel_multi_select(self, *_):
        self._multi_select = False
        self._ms_selected = []
        if self._ms_bar is not None:
            self._ms_bar.hide()
        self.viewport().update()

    def ms_active(self) -> bool:
        """委托查询：当前是否处于多选转发模式。"""
        return self._multi_select

    def ms_is_selected(self, msg_id: str) -> bool:
        return self._multi_select and msg_id in self._ms_selected

    def _copy_to_clipboard(self, msg: Message):
        """复制消息内容到剪贴板；有选区时优先复制选中文本。"""
        cb = QApplication.clipboard()
        selected = self._selected_text()
        if selected:
            cb.setText(selected)
            return
        text = msg.text
        if msg.msg_type == "image":
            # 图文混排复制说明正文，不复制「[图片]」占位前缀
            text = self._model.image_caption(msg) or text
        if msg.msg_type == "quote" and not text:
            # 纯 permalink 引用没有回复正文，复制被引用内容而不是「[引用]」
            text = msg.description or ""
        text = text or msg.file_name or msg.display_text or ""
        cb.setText(text)

    def _copy_msg_as_image(self, msg: Message):
        """把消息按当前渲染样式画成图片放入剪贴板（复制为图片）。

        宽度/高度与模型 text_bubble_metrics 同源，保证图片观感与屏幕气泡一致。
        markdown / @提及 / 链接都走同一 build_text_doc 路径。
        """
        from PyQt5.QtGui import QPixmap, QPainter, QColor
        from .message_model import _BUBBLE_PAD, _BUBBLE_RADIUS, _BUBBLE_MAX_W
        model = self._model
        # 用与屏幕一致的最大内容宽（控件最大气泡宽扣掉 padding），
        # 长文会自动换行，图片与屏幕气泡观感一致。
        max_content_w = _BUBBLE_MAX_W - _BUBBLE_PAD * 2
        content_w, content_h = model.text_bubble_metrics(msg, max_content_w)
        w = content_w + _BUBBLE_PAD * 2
        h = content_h + _BUBBLE_PAD * 2
        if w <= 0 or h <= 0:
            return
        pm = QPixmap(int(w), int(h))
        pm.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pm)
        try:
            # 气泡底色与屏幕一致（从主题色板取，复制图随主题）
            key = "bubble_mine" if msg.is_mine else "bubble_other"
            bg = self._delegate._col(key, "#ffffff").name()
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(bg))
            painter.drawRoundedRect(painter.viewport(), _BUBBLE_RADIUS, _BUBBLE_RADIUS)
            doc = self._delegate._build_text_doc(msg, content_w)
            painter.translate(_BUBBLE_PAD, _BUBBLE_PAD)
            # drawContents 用全局调色板（黑字），必须带主题文字色绘制，
            # 否则暗色主题复制的气泡图文字不可读
            self._delegate._draw_doc_plain(painter, doc)
        finally:
            painter.end()
        QApplication.clipboard().setPixmap(pm)

    def save_file_as(self, url: str, name: str, total: int = 0):
        """把指定 url 的文件另存到用户选择的路径（带认证头）。

        消息附件"另存为"与文件面板右键共用的公开入口。选路径后弹**非模态**
        微信式进度弹窗（FileDownloadDialog），下载在后台线程流式进行并实时
        刷新进度条，可取消，期间主窗口仍可操作；url 相对路径按 image_loader
        补成绝对地址。total 为已知文件大小，服务端不给 Content-Length 时用它
        兜底以显示完整大小与百分比。
        """
        if not url:
            return
        if url.startswith("/") and self.image_loader:
            url = self.image_loader.resolve(url)
        headers = self._attachment_headers(url)

        # 记忆上次保存目录（QSettings），下次作为起始路径
        from PyQt5.QtCore import QSettings
        settings = QSettings()
        last_dir = settings.value("save/last_dir", "", str)
        start = str(os.path.join(last_dir, name)) if last_dir else name
        path, _ = QFileDialog.getSaveFileName(
            self, "保存文件", start, "所有文件 (*.*)")
        if not path:
            return
        settings.setValue("save/last_dir", os.path.dirname(path))
        verify = getattr(self.image_loader, "ssl_verify", True)

        dlg = FileDownloadDialog(
            url, path, name or os.path.basename(path), total=total,
            headers=headers, verify=verify, theme=self._theme, center_on=self)
        self._save_dialogs.add(dlg)
        dlg.destroyed.connect(
            lambda *_, s=self._save_dialogs, d=dlg: s.discard(d))
        dlg.succeeded.connect(
            lambda p: self.status_message.emit(f"已保存到 {p}", 4000))
        dlg.failed.connect(
            lambda e: self.status_message.emit(f"下载失败：{e}", 4000)
            if e != "已取消" else None)
        dlg.show()

    def close_download_dialogs(self):
        """主窗口关闭时调用：强制关掉所有在途下载弹窗。

        下载弹窗是无父独立顶层窗口，会被计入 quitOnLastWindowClosed 的
        「主窗口」；不 hide 掉它们，主窗口关闭后 app 不会退出。
        """
        for dlg in list(self._save_dialogs):
            dlg.force_close()
        self._save_dialogs.clear()

    def _save_attachment(self, msg: Message):
        """保存附件到本地（带认证头，/file-upload/ 需要）。

        先弹保存对话框选路径，再由 FileDownloadDialog 非模态显示实时进度；
        下载在其自带工作线程流式进行，不占用 UI 主线程（大文件/慢网络
        不冻结界面），期间可继续操作主窗口，也可随时取消。
        """
        if not msg.file_url:
            return
        self.save_file_as(msg.file_url, msg.file_name or "attachment",
                          total=msg.file_size or 0)

    # ------------------------------------------------------------------ #
    # 辅助方法
    # ------------------------------------------------------------------ #
    def _is_duplicate(self, message_id: str) -> bool:
        """检查消息是否已存在。"""
        return self._model.find_message_row(message_id) >= 0
