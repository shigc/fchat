"""聊天输入区：微信风格。多行输入 + 发送按钮 + 附件按钮 + 引用预览条。"""
from __future__ import annotations

import os
import tempfile

from PyQt5.QtCore import (
    Qt, QEvent, QUrl, QSize, QVariant, QPoint, pyqtSignal,
)
from PyQt5.QtGui import (
    QImage, QPixmap, QKeySequence, QTextCursor, QTextDocument, QTextImageFormat,
    QTextFormat,
)
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QTextBrowser, QPushButton,
    QFileDialog, QLabel, QFrame, QShortcut, QListWidget, QListWidgetItem,
    QDesktopWidget, QMenu,
)

from .icons import icon
from .widgets import install_text_menu

# 输入框内文件占位片段的自定义属性键：存原始本地路径，发送时据此区分
# image 与 file（图片是 QImage 片段，文件只有路径）。
_FILE_PROP = QTextFormat.UserProperty + 1


class ChatInputEdit(QTextEdit):
    """输入框：拦截粘贴/拖入的图片，转发给上层走文件发送。

    直接在 insertFromMimeData 里判定——这是 Qt 的标准扩展点，无论
    Ctrl+V、右键粘贴还是拖入的文件都会经过它。QTextEdit 默认就消费
    拖放事件，外层 ChatInput 的 dropEvent 只对非文本区生效，所以文件
    拖放也统一在这处理，才能覆盖输入框本体。
    """
    image_pasted = pyqtSignal(object)   # 携带 QImage
    files_dropped = pyqtSignal(list)    # 携带本地文件路径列表
    send_requested = pyqtSignal()       # 按发送方式判定为「发送」时发射

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        install_text_menu(self)
        # 发送方式（enter/ctrlenter）：决定回车是发送还是换行。
        # 不能用 QShortcut 抢 Return——QTextEdit 对 Return 键有内建
        # 换行处理且优先级高于快捷键（真机实测 QShortcut 拦不到
        # Return，回车永远变换行），必须在 keyPressEvent 里拦截。
        self._send_mode = "enter"

    def set_send_mode(self, mode: str):
        """设置发送方式（enter/ctrlenter），回车行为随之切换。"""
        self._send_mode = mode or "enter"

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if self._send_mode == "ctrlenter":
                # Ctrl+回车发送 / 回车换行
                if event.modifiers() & Qt.ControlModifier:
                    self.send_requested.emit()
                    event.accept()
                    return
            else:
                # 回车发送 / Shift+回车换行
                if not (event.modifiers() & Qt.ShiftModifier):
                    self.send_requested.emit()
                    event.accept()
                    return
        super().keyPressEvent(event)

    def insertFromMimeData(self, source):
        if source is None:
            return
        # 本地文件（含图片文件）：交给上层在输入框内嵌为待发送部件（图片
        # 内联、文件占位），随消息一起发送；不走「立即发送」。只有剪贴板里
        # 的纯图（无文件路径）才需要重编码成临时 PNG。
        paths = [u.toLocalFile() for u in source.urls()
                 if u.isLocalFile() and u.toLocalFile()]
        if paths:
            self.files_dropped.emit(paths)
            return
        img = self._pick_image(source)
        if img is not None and not img.isNull():
            self.image_pasted.emit(img)
            return
        super().insertFromMimeData(source)

    @staticmethod
    def _pick_image(source):
        # 剪贴板来源五花八门（截图工具、浏览器、画图、应用内复制为图片等），
        # imageData() 可能直接是 QImage，也可能是 QPixmap（clipboard().setPixmap
        # 的读回结果），还可能是包一层 QVariant。不能只认 QImage，否则
        # 「复制为图片→粘贴」这类用 setPixmap 写入的图会粘贴失败。
        data = source.imageData()
        # PyQt5 里 QImage/QPixmap 互不 isinstance，且 QPixmap 判空用 isNull()。
        # 常见情况：imageData() 直接给 QImage 或 QPixmap，逐个转成 QImage 返回。
        if isinstance(data, (QImage, QPixmap)):
            img = data.toImage() if isinstance(data, QPixmap) else data
            return img if not img.isNull() else None
        # 某些来源 imageData 是 QVariant 包装（罕见），hasImage 为真时再解一层
        if data is not None and source.hasImage():
            try:
                inner = data.value() if hasattr(data, "value") else data
                if isinstance(inner, (QImage, QPixmap)):
                    img = inner.toImage() if isinstance(inner, QPixmap) else inner
                    if not img.isNull():
                        return img
            except AttributeError:
                pass
        return None


class MentionPicker(QWidget):
    """@ 提及候选下拉：当前房间成员列表 + 固定「所有成员(@all)」项。

    Qt.Popup 窗口，由 ChatInput 定位到输入框光标下方显示。候选通过
    set_candidates 注入；列表用 QListWidget 自带滚动。键盘（方向键选择、
    Enter 确认、Esc 关闭）与鼠标点击都支持。
    """

    mention_selected = pyqtSignal(str)   # 要插入的 @xxx（不含尾随空格）

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Popup)
        self.setObjectName("MentionPicker")
        self.setMinimumWidth(240)
        self.setMaximumWidth(320)
        self.setStyleSheet(
            "background:#ffffff;border:1px solid #d0d0d0;border-radius:6px;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.list = QListWidget()
        self.list.setStyleSheet(
            "QListWidget{background:transparent;border:none;font-size:13px;}"
            "QListWidget::item{height:30px;padding:0 8px;border-radius:4px;}"
            "QListWidget::item:selected{background:#07c160;color:#fff;}")
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.itemClicked.connect(self._on_activated)
        self.list.itemActivated.connect(self._on_activated)
        lay.addWidget(self.list)
        self._esc = QShortcut(QKeySequence("Escape"), self)
        self._esc.activated.connect(self.close)
        self._cached_room_type = "c"
        self._cached_members = []

    def set_candidates(self, room_type: str, members: list):
        """存下候选并重建列表；@all 仅在频道/群（c/p）显示，私聊（d）不提供。"""
        self._cached_room_type = room_type
        self._cached_members = list(members or [])
        self._rebuild("")

    def _rebuild(self, keyword: str):
        """按小写前缀过滤关键词重建列表；@all 项在关键词为空或 "all" 前缀时保留。"""
        self.list.clear()
        kw = keyword.lower()
        if self._cached_room_type in ("c", "p") and (not kw or "all".startswith(kw)):
            all_item = QListWidgetItem("所有成员  @all")
            all_item.setData(Qt.UserRole, "@all")
            all_item.setToolTip("通知房间内的所有成员")
            self.list.addItem(all_item)
        for m in self._cached_members:
            if not isinstance(m, dict):
                continue
            username = m.get("username")
            if not username:
                continue
            name = m.get("name") or ""
            if kw and not (username.lower().startswith(kw)
                           or (name and name.lower().startswith(kw))):
                continue
            label = f"{name}  @{username}" if name else f"@{username}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, f"@{username}")
            self.list.addItem(item)
        self.list.setCurrentRow(0)

    def _on_activated(self, item, *_):
        text = item.data(Qt.UserRole)
        if text:
            self.mention_selected.emit(text)
            self.close()

    def filter_to(self, keyword: str):
        """按当前键入的过滤关键词刷新可见项。"""
        self._rebuild(keyword)

    def show_popup_at(self, global_pos):
        """把弹窗放在指定全局坐标（光标下方），越界约束到屏幕内。"""
        self.adjustSize()
        screen = QDesktopWidget().availableGeometry(self)
        x = min(max(screen.left(), global_pos.x()),
                screen.right() - self.width())
        y = global_pos.y() + 6
        if y + self.height() > screen.bottom():
            y = max(screen.top(), global_pos.y() - self.height() - 6)
        self.move(x, max(screen.top(), y))
        self.show()
        self.raise_()
        self.list.setCurrentRow(0)


class ChatInput(QWidget):
    send_text = pyqtSignal(str)
    send_file = pyqtSignal(str)   # 文件路径
    sticker_selected = pyqtSignal(str)   # 表情包图片路径（按文件发送）
    send_mixed = pyqtSignal(object)   # 图文混排 parts：[("text", str) | ("image", 本地路径)]
    screenshot_requested = pyqtSignal()
    mention_wanted = pyqtSignal()   # 键入 @ 但暂无成员候选时请求主窗口补拉成员
    edit_cancelled = pyqtSignal()   # 取消编辑（主窗口据此清 _editing_msg_id）
    ai_assist_requested = pyqtSignal(str)  # 写作助手动作 key（只回填不发送）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ChatInputWidget")
        self.setMinimumHeight(120)
        self.setAcceptDrops(True)

        # 引用上下文（由 set_quote 设置，发送后自动清除）
        self.quote_context = None   # {"msg_id": str, "sender": str, "text": str}
        # 编辑态：正在改写的那条消息 id（空串 = 普通发送）与进入编辑前的
        # 输入快照，取消编辑时恢复，别把用户原来打了一半的内容吞掉
        self._edit_msg_id = ""
        self._pre_edit = None
        # AI 入口默认隐藏，主窗口按配置启用后显示
        self._ai_available = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 10)
        layout.setSpacing(6)

        # 引用预览条（默认隐藏）
        self.quote_bar = QFrame()
        self.quote_bar.setObjectName("QuoteBar")
        self.quote_bar.setStyleSheet(
            "background:#f0f0f0;border-left:3px solid #07c160;"
            "border-radius:2px;padding:4px 8px;")
        self.quote_bar.hide()
        qb_layout = QHBoxLayout(self.quote_bar)
        qb_layout.setContentsMargins(8, 4, 4, 4)
        self.quote_label = QLabel("")
        self.quote_label.setStyleSheet(
            "background:transparent;color:#666;font-size:12px;")
        self.quote_label.setWordWrap(True)
        qb_layout.addWidget(self.quote_label, 1)
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(20, 20)
        close_btn.setStyleSheet(
            "background:transparent;color:#999;border:none;font-size:14px;")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.clear_quote)
        qb_layout.addWidget(close_btn, 0, Qt.AlignTop)
        layout.addWidget(self.quote_bar)

        # 编辑预览条（默认隐藏）：与引用条同款细条，取消时恢复编辑前的输入
        self.edit_bar = QFrame()
        self.edit_bar.setObjectName("EditBar")
        self.edit_bar.setStyleSheet(
            "background:#eef6ff;border-left:3px solid #3f7fd6;"
            "border-radius:2px;padding:4px 8px;")
        self.edit_bar.hide()
        eb_layout = QHBoxLayout(self.edit_bar)
        eb_layout.setContentsMargins(8, 4, 4, 4)
        self.edit_label = QLabel("正在编辑消息")
        self.edit_label.setStyleSheet(
            "background:transparent;color:#4a6fa5;font-size:12px;")
        eb_layout.addWidget(self.edit_label, 1)
        cancel_edit_btn = QPushButton("取消")
        cancel_edit_btn.setFixedHeight(20)
        cancel_edit_btn.setCursor(Qt.PointingHandCursor)
        cancel_edit_btn.setStyleSheet(
            "background:transparent;color:#999;border:none;font-size:12px;")
        cancel_edit_btn.clicked.connect(self.cancel_edit)
        eb_layout.addWidget(cancel_edit_btn, 0, Qt.AlignTop)
        layout.addWidget(self.edit_bar)

        # 工具栏：纯图标按钮 + tooltip，右对齐发送
        self._icon_buttons = []
        self.tool_bar = QHBoxLayout()
        self.tool_bar.setSpacing(4)
        self.attach_btn = self._icon_button("attach", "发送文件")
        self.attach_btn.clicked.connect(self._choose_file)
        self.tool_bar.addWidget(self.attach_btn)

        self.emoji_btn = self._icon_button("emoji", "插入表情")
        self.emoji_btn.clicked.connect(self._toggle_emoji_picker)
        self.tool_bar.addWidget(self.emoji_btn)

        self.screenshot_btn = self._icon_button("screenshot", "截图发送")
        self.screenshot_btn.clicked.connect(self.screenshot_requested.emit)
        self.tool_bar.addWidget(self.screenshot_btn)

        self.ai_btn = self._icon_button("ai", "AI 写作助手")
        self.ai_btn.clicked.connect(self._show_ai_menu)
        self.ai_btn.hide()
        self.tool_bar.addWidget(self.ai_btn)

        self.preview_btn = self._icon_button(
            "preview", "Markdown 实时预览", checkable=True)
        self.preview_btn.toggled.connect(self._set_preview_visible)
        self.tool_bar.addWidget(self.preview_btn)

        self.tool_bar.addStretch()
        self.send_btn = self._icon_button("send", "发送(S)")
        self.send_btn.clicked.connect(self._send)
        self.tool_bar.addWidget(self.send_btn)
        layout.addLayout(self.tool_bar)

        self.input = ChatInputEdit()
        self.input.setObjectName("MessageInput")
        self.input.setAcceptRichText(False)
        self.input.setPlaceholderText("输入消息，按回车发送，Shift+回车换行")
        self.input.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        # 粘贴的纯剪贴板图片内联插入光标处（与截图一致），发送时随 send 一起走；
        # 拖入/粘贴的本地图片/文件也在输入框内嵌（_embed_dropped_paths）。
        self.input.image_pasted.connect(self.insert_screenshot_image)
        self.input.files_dropped.connect(self._embed_dropped_paths)
        layout.addWidget(self.input)

        # Markdown 实时预览（默认隐藏，勾选后随输入刷新）
        self.preview_bar = QFrame()
        self.preview_bar.setObjectName("MarkdownPreview")
        self.preview_bar.setStyleSheet(
            "background:#fafafa;border:1px solid #e0e0e0;border-radius:4px;")
        self.preview_bar.hide()
        pb_layout = QVBoxLayout(self.preview_bar)
        pb_layout.setContentsMargins(8, 6, 8, 6)
        # 预览图加载器（登录后由 MainWindow 注入，未注入则预览不显示内联图）
        self._image_loader = None
        # 预览用只读 QTextBrowser 而非 QLabel：QLabel 不暴露内部 QTextDocument，
        # 无法像 message_model 那样 addResource 注入预览图片的 pixmap，
        # 直接 setHtml 的 <img> 只会显示空白。QTextBrowser 是只读的
        # QTextEdit，既有 document() 可注入资源，又自带外链打开。
        self.preview_label = QTextBrowser()
        self.preview_label.setFrameStyle(0)
        install_text_menu(self.preview_label)
        self.preview_label.setStyleSheet(
            "background:transparent;border:none;color:#333;")
        self.preview_label.document().setDocumentMargin(0)
        self.preview_label.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.preview_label.setOpenExternalLinks(True)
        self.preview_label.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        pb_layout.addWidget(self.preview_label)
        layout.addWidget(self.preview_bar)

        self._connect_shortcut()
        self.input.textChanged.connect(self._on_input_text_changed)
        # 移动光标也可能退出 @ 输入态（如点其他位置），单独监听
        self.input.cursorPositionChanged.connect(self._update_mention_trigger)
        # 拦截 Backspace/Delete：已插入的 @提及 是整体，不能逐个删字符，只能整段删
        self.input.installEventFilter(self)

        # @ 提及状态：候选由主窗口 set_mention_candidates 注入
        self._mention_candidates = []        # [member dict]
        self._mention_room_type = "c"
        self._mention_picker = None
        self._mention_active = False         # 当前输入态 @ 片段
        self._mention_anchor = 0             # 当前 @ 片段的起始偏移（-1 表示无）
        self._mention_commit_span = None     # 最近一次插入的完整 @提及 [start,end)（含尾随空格），用于删除时一键撤销

    def _icon_button(self, icon_name: str, tip: str = "",
                     checkable: bool = False) -> QPushButton:
        """建一个纯图标工具栏按钮：等尺寸、圆角、hover/checked 由 QSS 控制。

        菜单/气泡图标在 16x16 网格里画，这里放大到 24 渲染；按钮固定 30x30
        方形，间距由 tool_bar 的 spacing 控制。
        """
        btn = QPushButton()
        btn.setObjectName("ToolIconButton")
        btn._icon_name = icon_name
        btn.setIcon(icon(icon_name, 24))
        btn.setIconSize(QSize(24, 24))
        btn.setFixedSize(30, 30)
        btn.setCursor(Qt.PointingHandCursor)
        if tip:
            btn.setToolTip(tip)
        if checkable:
            btn.setCheckable(True)
        self._icon_buttons.append(btn)
        return btn

    def refresh_icons(self):
        """主题切换后按新全局图标色重挂工具栏图标。

        图标 pixmap 按渲染时的全局色缓存，构造期渲染的图标不会自己跟
        着换主题变色（暗色下深灰图标画在深底上，真机复现过 + 号同款）。"""
        for btn in self._icon_buttons:
            btn.setIcon(icon(btn._icon_name, 24))

    # ------------------------------------------------------------------ #
    # AI 写作助手
    # ------------------------------------------------------------------ #
    def set_ai_available(self, available: bool):
        self._ai_available = bool(available)
        self.ai_btn.setVisible(self._ai_available)

    def _show_ai_menu(self, *_):
        if not self._ai_available:
            return
        menu = QMenu(self)
        for key, label in (("polish", "润色"),
                           ("rewrite", "改写"),
                           ("shorten", "精简"),
                           ("reply_draft", "根据最近聊天起草回复")):
            act = menu.addAction(label)
            act.triggered.connect(
                lambda _=False, k=key: self.ai_assist_requested.emit(k))
        pos = self.ai_btn.mapToGlobal(
            QPoint(0, -menu.sizeHint().height()))
        menu.exec_(pos)

    def has_media_content(self) -> bool:
        """输入框里是否有内联图片/文件（写作助手只处理纯文本）。"""
        return any(k in ("image", "file")
                   for k, _ in self._collect_inline_parts())

    def replace_text(self, text: str):
        """整体替换输入框文本（写作助手润色/改写/精简回填用）。"""
        self.input.setPlainText(text or "")
        cursor = self.input.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.input.setTextCursor(cursor)
        self.input.setFocus()

    def append_text(self, text: str):
        """在光标处追加结果；与已有内容之间补一个换行避免粘连。

        消息 AI 的「插入输入框」走这里，只回填不发送。
        """
        text = (text or "").strip()
        if not text:
            return
        cursor = self.input.textCursor()
        if cursor.hasSelection():
            cursor.removeSelectedText()
            self.input.setTextCursor(cursor)
        prefix = ""
        probe = QTextCursor(cursor)
        if probe.position() > 0:
            probe.movePosition(QTextCursor.PreviousCharacter,
                               QTextCursor.KeepAnchor)
            prev = probe.selectedText()
            if prev and not prev[-1].isspace():
                prefix = "\n"
        self.input.insertPlainText(prefix + text)
        self.input.moveCursor(QTextCursor.End)
        self.input.setFocus()

    def _connect_shortcut(self):
        # 发送/换行由 ChatInputEdit.keyPressEvent 按发送方式拦截 Return 后
        # 经 send_requested 信号转发到这里（QShortcut 拦不到 QTextEdit 的
        # Return 键，见 ChatInputEdit 注释）。
        self.input.send_requested.connect(self._send)
        # 粘贴图片：由 ChatInputEdit.insertFromMimeData 拦截后经
        # image_pasted 信号内联插入输入框，这里不再用 QShortcut 抢占
        # Ctrl+V（QTextEdit 内部会自己处理，抢不灵）。

    def set_send_mode(self, mode: str):
        """动态切换发送方式（enter/ctrlenter），转发给输入框。
        回车发送/换行的判定在 ChatInputEdit.keyPressEvent 里按 _send_mode 做。
        """
        self.input.set_send_mode(mode or "enter")

    def _save_qimage(self, image: QImage) -> str:
        """把 QImage 存成临时 PNG，返回路径；保存失败返回空串。

        图文混排时每张内联图都要先落盘成临时文件，才能在 core 层
        经 rooms.media 上传拿到 URL 后拼进 markdown 正文。
        """
        if image is None or image.isNull():
            return ""
        fd, path = tempfile.mkstemp(suffix=".png", prefix="fchat_paste_")
        os.close(fd)
        if image.save(path, "PNG"):
            return path
        try:
            os.remove(path)
        except OSError:
            pass
        return ""

    def _embed_dropped_paths(self, paths):
        """把拖入/粘贴的本地文件内嵌进输入框：图片内联，其他文件放占位片段。

        逐个文件按 QImage 能否加载分类——能加载即视为图片走 insert_screenshot
        (image)，否则用 insert_file 放文件占位。发送时 _collect_inline_parts
        据此还原成 ("image", QImage) / ("file", 路径)。
        """
        for p in paths:
            if not p:
                continue
            img = QImage(p)
            if not img.isNull():
                self.insert_screenshot_image(img)
            else:
                self.insert_file(p)

    def insert_file(self, path: str):
        """在输入框光标处插入一个文件占位片段（QTextImageFormat + 资源 pixmap）。

        QTextEdit 没有原生「文件」片段类型，借 QTextImageFormat 当通用多态
        片段：用自定义属性 _FILE_PROP 存原始路径，显示层用一个 QPainter 现画
        的占位图呈现文件名，发送层按 _FILE_PROP 有无区分「文件」与「图片」。
        """
        if not path:
            return
        self._inline_file_seq = getattr(self, "_inline_file_seq", 0) + 1
        name = f"fchat_file_{self._inline_file_seq}_{id(self)}"
        doc = self.input.document()
        pm = self._file_placeholder_pixmap(os.path.basename(path))
        doc.addResource(QTextDocument.ImageResource, QUrl(name), pm)
        cur = self.input.textCursor()
        fmt = QTextImageFormat()
        fmt.setName(name)
        fmt.setWidth(pm.width())
        fmt.setHeight(pm.height())
        fmt.setProperty(_FILE_PROP, path)
        cur.insertImage(fmt)
        self.input.setTextCursor(cur)
        self.input.setFocus()

    @staticmethod
    def _file_placeholder_pixmap(basename: str, width: int = 220,
                                 height: int = 54) -> QPixmap:
        """现画文件占位图：灰底圆角 + 文件名，避免引入图片资源。"""
        pm = QPixmap(width, height)
        pm.fill(Qt.transparent)
        from PyQt5.QtGui import QPainter, QColor, QPen, QFont, QFontMetrics
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0xED, 0xED, 0xED))
        painter.drawRoundedRect(0, 0, width, height, 6, 6)
        # 左侧小文件图标
        painter.setBrush(QColor(0xA5, 0xA5, 0xA5))
        painter.drawRoundedRect(10, height // 2 - 10, 20, 20, 3, 3)
        painter.setPen(QColor(0x33, 0x33, 0x33))
        font = QFont()
        font.setPointSize(9)
        painter.setFont(font)
        fm = QFontMetrics(font)
        elided = fm.elidedText(basename, Qt.ElideMiddle, width - 44)
        painter.drawText(38, height // 2 + fm.ascent() // 2 - 1, elided)
        painter.end()
        return pm

    def _send(self, *_):
        # 按文档顺序取回文本与截图；toPlainText() 会丢掉内联图，
        # 必须逐块逐片段遍历。没有任何内容则不发送。
        self._close_mention()
        parts = self._collect_inline_parts()
        if not parts:
            return
        has_media = any(k in ("image", "file") for k, _ in parts)
        if has_media:
            # 编辑态下发媒体：媒体不走 chat.update 提交，必须先退出编辑，
            # 否则 _edit_msg_id 残留导致末尾不清空输入框——文件占位卡片
            # 常驻输入框，之后每次回车都把同一批文件重发一遍（真机踩过：
            # 服务器屏蔽编辑 → 编辑失败后编辑条常驻，必现）。
            # 文本回车仍走编辑提交（主窗口 _on_send_text 分流），不受影响。
            self._abandon_edit_silently()
            media = [(k, p) for k, p in parts if k in ("image", "file")]
            has_text = any(p.strip() for k, p in parts if k == "text")
            if not has_text and all(k == "file" for k, _ in media):
                # 纯文件（无文字、无截图）：逐个走正规附件通道 send_file
                # （上传 + mediaConfirm，服务端生成原生文件消息且上传永久
                # 保留，附进度弹窗/可取消）。不走 send_mixed：旧实现把文件
                # 拼进 markdown 正文，上传记录未确认，隔天被服务端清理后
                # 消息里的文件全部 404（此前定位）
                for _kind, path in media:
                    self.send_file.emit(path)
            else:
                # 图文混排：图片 QImage 落盘成临时 PNG、文件路径透传，
                # 连同文本按序组装成 parts 走 send_mixed（媒体逐个上传 +
                # mediaConfirm，文本作 caption，见 core send_mixed）
                out = []
                for kind, payload in parts:
                    if kind == "text":
                        if payload.strip():
                            out.append(("text", payload.strip()))
                    elif kind == "file":
                        out.append(("file", payload))
                    else:
                        path = self._save_qimage(payload)
                        if path:
                            out.append(("image", path))
                if out:
                    self.send_mixed.emit(out)
        else:
            # 纯文本：has_media 为假时输入里没有内联图/文件，直接取
            # toPlainText() 原样保留段落换行/空行，一条发出避免拆条。
            # 不能再用 _collect_inline_parts 拼接——它会丢掉空行段落。
            text = self.input.toPlainText().strip()
            if text:
                self.send_text.emit(text)
        # 编辑态不在这里清空：更新请求可能失败，失败要保留用户改到一半的
        # 内容与编辑条；成功后由主窗口调 finish_edit() 收尾
        if not self._edit_msg_id:
            self.input.clear()
            self.clear_quote()

    def _collect_inline_parts(self):
        """按文档顺序收集输入框内容：文本片段 + 内联截图 QImage + 文件占位路径。

        内联截图以 QTextImageFormat 片段 + document resource 的形式存在，
        toPlainText() 会丢掉图片，发送前需按顺序逐块遍历取回，保证
        文字与插入其中的截图相对顺序不乱。文件占位同样用 QTextImageFormat，
        但带 _FILE_PROP 属性存原始路径，命中则产出 ("file", 路径)。
        """
        doc = self.input.document()
        parts = []
        block = doc.begin()
        while block.isValid():
            it = block.begin()
            buf = []
            while not it.atEnd():
                frag = it.fragment()
                fmt = frag.charFormat()
                if fmt.isImageFormat():
                    if buf:
                        parts.append(("text", "".join(buf)))
                        buf = []
                    file_path = fmt.property(_FILE_PROP)
                    if file_path:
                        parts.append(("file", str(file_path)))
                    else:
                        res = doc.resource(
                            QTextDocument.ImageResource,
                            QUrl(fmt.toImageFormat().name()))
                        if isinstance(res, QImage) and not res.isNull():
                            parts.append(("image", res))
                else:
                    buf.append(frag.text())
                it += 1
            if buf:
                parts.append(("text", "".join(buf)))
            block = block.next()
        return parts

    # ------------------------------------------------------------------ #
    # 截图/粘贴草稿（待发送图片）
    # ------------------------------------------------------------------ #
    def insert_screenshot_image(self, image):
        """把截图/粘贴的图片内联插入输入框光标处，随消息一起发送。

        不走输入框上方的待发草稿栏，而是作为富文本里的内联图片段插到
        光标位置；原图分辨率以 document resource 保存，发送时再由
        _collect_inline_parts 取回。过宽的原图只在**显示尺寸**上缩到 480
        内免得把输入框挤满（用 QTextImageFormat 的 width/height 控制），
        存进 resource 的仍是原图，避免发送/查看原图时分辨率被永久下采样。
        截图（main_window 触发）与剪贴板粘贴（image_pasted 信号）共用此入口。
        """
        if image is None or image.isNull():
            return
        self._inline_img_seq = getattr(self, "_inline_img_seq", 0) + 1
        name = f"fchat_screenshot_{self._inline_img_seq}_{id(self)}"
        disp_w, disp_h = image.width(), image.height()
        if image.width() > 480:
            disp_w = 480
            disp_h = int(image.height() * 480 / image.width())
        doc = self.input.document()
        # 存原图，发送时取回全分辨率；显示大小交给 format 的 width/height
        doc.addResource(QTextDocument.ImageResource, QUrl(name), image)
        cur = self.input.textCursor()
        fmt = QTextImageFormat()
        fmt.setName(name)
        fmt.setWidth(disp_w)
        fmt.setHeight(disp_h)
        cur.insertImage(fmt)
        self.input.setTextCursor(cur)
        self.input.setFocus()

    # ------------------------------------------------------------------ #
    # 引用回复
    # ------------------------------------------------------------------ #
    def set_quote(self, sender_name: str, text: str, msg_id: str,
                  room_id: str = "", room_type: str = "", room_slug: str = ""):
        """显示引用预览条。quote_context 里存下 msg_id 与所在房间，发送时直接用它
        拼 permalink，避免发送瞬间房间已切换导致自产自收不匹配。room_slug 是
        网页版 permalink 用的房间名（频道 ASCII slug / 私聊对方 username）。"""
        preview = text[:60] + "…" if len(text) > 60 else text
        self.quote_label.setText(f"回复 {sender_name}: {preview}")
        self.quote_context = {
            "msg_id": msg_id, "sender": sender_name, "text": text,
            "room_id": room_id, "room_type": room_type, "room_slug": room_slug,
        }
        self.quote_bar.show()
        self.input.setFocus()

    def clear_quote(self, *_):
        """清除引用预览条。"""
        self.quote_context = None
        self.quote_bar.hide()

    # ------------------------------------------------------------------ #
    # 草稿（每会话文本 + 引用）
    # ------------------------------------------------------------------ #
    def draft_snapshot(self) -> dict:
        """当前输入的可持久化快照。

        内联截图是 QTextDocument resource，无法序列化，只保留纯文本与
        引用条；恢复后截图不会回来。
        """
        return {
            "text": self.input.toPlainText(),
            "quote": dict(self.quote_context) if self.quote_context else None,
        }

    def restore_draft(self, draft):
        """恢复草稿；blockSignals 避免触发 @ 候选与草稿定时器。"""
        draft = draft or {}
        self.input.blockSignals(True)
        try:
            self.input.setPlainText(draft.get("text") or "")
            cur = self.input.textCursor()
            cur.movePosition(QTextCursor.End)
            self.input.setTextCursor(cur)
        finally:
            self.input.blockSignals(False)
        self.clear_quote()
        quote = draft.get("quote")
        if isinstance(quote, dict):
            self.set_quote(
                quote.get("sender", ""), quote.get("text", ""),
                quote.get("msg_id", ""), quote.get("room_id", ""),
                quote.get("room_type", ""), quote.get("room_slug", ""))
        # 恢复内容不弹 @ 候选；清掉可能残留的触发态
        self._mention_active = False
        self._mention_anchor = -1
        self._close_mention()
        if self.preview_btn.isChecked():
            self._refresh_preview()

    def clear_draft_input(self):
        """清空输入与引用条。

        不屏蔽 textChanged：主窗口的草稿定时器据此把该会话的旧草稿删掉，
        否则发送/编辑成功后残留的草稿会在下次切回时复活。
        """
        self.input.clear()
        self.clear_quote()
        self._mention_active = False
        self._mention_anchor = -1
        self._close_mention()

    # ------------------------------------------------------------------ #
    # 编辑已发送消息
    # ------------------------------------------------------------------ #
    def begin_edit(self, msg_id: str, text: str):
        """进入编辑态：记下当前输入，输入框换成待编辑文本。"""
        if not msg_id:
            return
        self._pre_edit = self.draft_snapshot()
        self._edit_msg_id = msg_id
        self.input.blockSignals(True)
        try:
            self.input.setPlainText(text or "")
            cur = self.input.textCursor()
            cur.movePosition(QTextCursor.End)
            self.input.setTextCursor(cur)
        finally:
            self.input.blockSignals(False)
        self.edit_label.setText("正在编辑消息")
        self.edit_bar.show()
        self.input.setFocus()

    def cancel_edit(self, *_):
        """取消编辑：恢复进入编辑前的输入与引用。"""
        if not self._edit_msg_id:
            return
        pre = self._pre_edit or {"text": "", "quote": None}
        self._edit_msg_id = ""
        self._pre_edit = None
        self.edit_bar.hide()
        self.restore_draft(pre)
        self.edit_cancelled.emit()

    def finish_edit(self):
        """编辑成功：收掉编辑条并清空输入（不触发 edit_cancelled）。"""
        self._edit_msg_id = ""
        self._pre_edit = None
        self.edit_bar.hide()
        self.clear_draft_input()

    def is_editing(self) -> bool:
        return bool(self._edit_msg_id)

    def _abandon_edit_silently(self):
        """退出编辑态但不恢复进入前的草稿（与 cancel_edit 的区别）。

        供媒体发送等绕过 chat.update 的路径使用：输入内容即将被发送/清空，
        恢复草稿会把刚拖进来的媒体占位冲掉。edit_cancelled 照常发射，让
        主窗口同步清掉 _editing_msg_id。
        """
        if not self._edit_msg_id:
            return
        self._edit_msg_id = ""
        self._pre_edit = None
        self.edit_bar.hide()
        self.edit_cancelled.emit()

    def _choose_file(self, *_):
        # 多选：微信式一次挑多张图/多个文件逐个发送
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择要发送的文件", "",
            "图片 (*.png *.jpg *.jpeg *.gif *.bmp *.webp);;所有文件 (*.*)")
        for path in paths:
            self.send_file.emit(path)

    # ------------------------------------------------------------------ #
    # 拖拽文件 / 图片
    # ------------------------------------------------------------------ #
    def dragEnterEvent(self, event):
        """拖入本地文件时接受，文本/其他 mime 交给默认处理。"""
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
        paths = [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
        self._embed_dropped_paths(paths)
        event.acceptProposedAction()

    @staticmethod
    def _has_local_urls(mime) -> bool:
        urls = mime.urls() if mime is not None else []
        return any(u.isLocalFile() for u in urls)

    # ------------------------------------------------------------------ #
    # Markdown 实时预览
    # ------------------------------------------------------------------ #
    def _set_preview_visible(self, visible: bool):
        if visible:
            self.preview_bar.show()
            self._refresh_preview()
        else:
            self.preview_bar.hide()

    def _on_input_text_changed(self):
        if self.preview_btn.isChecked():
            self._refresh_preview()
        self._update_mention_trigger()

    # ------------------------------------------------------------------ #
    # @ 提及（微信式）
    # ------------------------------------------------------------------ #
    def set_mention_candidates(self, room_type: str, members: list):
        """注入当前房间成员候选（主窗口在打开会话/成员到货时调用）。

        若正处在 @ 输入态（候选晚到、经历 mention_wanted 补拉），按当前
        过滤关键词重刷下拉，避免会员到货后清掉用户已输入的过滤词。
        """
        self._mention_room_type = room_type
        self._mention_candidates = list(members or [])
        if self._mention_picker is None:
            return
        if self._mention_active:
            _, keyword = self._word_before_cursor()
            self._mention_picker.set_candidates(room_type, self._mention_candidates)
            self._mention_picker.filter_to(keyword or "")
            self._show_mention_popup()

    def _mention_picker_widget(self) -> MentionPicker:
        """惰性创建 @ 候选下拉并接线。"""
        if self._mention_picker is None:
            self._mention_picker = MentionPicker(self)
            self._mention_picker.mention_selected.connect(self._insert_mention)
        return self._mention_picker

    def _word_before_cursor(self) -> tuple:
        """取光标前的"词"：若以 @ 起始则返回 (@start_offset, 词文本)，否则 (None, "")。

        微信式触发：@ 前面必须是行首或空白字符（词边界），否则不当作
        @提及开头而当作普通内容（如 email、代码里的 @）。
        """
        cur = self.input.textCursor()
        text = self.input.toPlainText()
        pos = cur.position()
        if pos > len(text):
            pos = len(text)
        before = text[:pos]
        # 找光标前最后一个 @
        at = before.rfind("@")
        if at < 0:
            return (None, "")
        # @ 前必须是行首或空白
        if at > 0 and not before[at - 1].isspace():
            return (None, "")
        # 词内不能再有空白（@ 片段是一整个不含空白的 token）
        seg = before[at:]
        if " " in seg or "\n" in seg:
            return (None, "")
        return (at, seg[1:])   # 去掉 @ 之后的过滤关键词

    def _update_mention_trigger(self):
        """文本/光标变化时维护 @ 输入态：触发弹出、实时过滤或关闭。"""
        start, keyword = self._word_before_cursor()
        if start is None:
            self._close_mention()
            return
        self._mention_active = True
        self._mention_anchor = start
        # 过滤关键词里出现会让 @ 片段失效的字符（如再敲 @）则关闭
        if "@" in keyword:
            self._close_mention()
            return
        if not self._mention_candidates:
            # 候选未就绪：只显示 @all（若适用），并请求主窗口补拉成员
            room_type = self._mention_room_type
            has_all = room_type in ("c", "p")
            if not has_all:
                self._close_mention()
                return
            picker = self._mention_picker_widget()
            picker.set_candidates(room_type, [])
            picker.filter_to(keyword)
            self._show_mention_popup()
            self.mention_wanted.emit()
            return
        picker = self._mention_picker_widget()
        picker.set_candidates(self._mention_room_type, self._mention_candidates)
        picker.filter_to(keyword)
        self._show_mention_popup()

    def _show_mention_popup(self):
        """把 @ 下拉定位到光标下方并显示。"""
        picker = self._mention_picker_widget()
        if picker.list.count() == 0:
            picker.close()
            return
        # 取输入框光标的全局坐标
        cur = self.input.cursorRect()
        global_pos = self.input.mapToGlobal(cur.bottomLeft())
        picker.show_popup_at(global_pos)

    def _close_mention(self):
        """退出 @ 输入态并关闭下拉。"""
        self._mention_active = False
        self._mention_anchor = -1
        if self._mention_picker is not None:
            self._mention_picker.close()

    def _insert_mention(self, mention: str):
        """把当前 @ 片段替换为选中的 @xxx，光标置于其后并恢复焦点。

        用锚点偏移替换：当前文本里 [anchor, cursor) 这一段就是正在输入的
        @片段，替换成完整 @xxx 并补一个空格，避免选中项与已输入部分拼接错误。
        """
        cur = self.input.textCursor()
        pos = cur.position()
        text = self.input.toPlainText()
        anchor = self._mention_anchor if self._mention_anchor >= 0 else pos
        if anchor < 0:
            anchor = pos
        new_text = text[:anchor] + mention + " " + text[pos:]
        self.input.setPlainText(new_text)
        new_pos = anchor + len(mention) + 1
        # 记下完整提及（含尾随空格）的 [start,end)，删除时整体撤销，不能逐字删
        self._mention_commit_span = (anchor, new_pos)
        c = self.input.textCursor()
        c.setPosition(new_pos)
        self.input.setTextCursor(c)
        self._close_mention()
        self.input.setFocus()

    def eventFilter(self, obj, event):
        """拦下输入框的 Backspace/Delete：已插入的 @提及 是整体，命中即整段删除，
        避免退格时逐个删字符（每删一个就重新弹 @ 列表）。
        """
        if obj is self.input and event.type() == QEvent.KeyPress:
            key = event.key()
            if key in (Qt.Key_Backspace, Qt.Key_Delete):
                if self._undo_mention_on_delete(key):
                    return True   # 吞掉该次删除，交给 @提及 整体删除逻辑
        return super().eventFilter(obj, event)

    def _undo_mention_on_delete(self, key: int) -> bool:
        """Backspace/Delete 光标命中已插入的 @提及 时，整体删除并返回 True。

        命中规则：
        - 有选区且选区与提及区间相交 → 整段删（不允许单独删其中某个字符）
        - Backspace：光标在 (start, end] 内（end 处即紧贴尾随空格，删它就是开始撤销提及）
        - Delete：光标在 [start, end) 内
        """
        span = self._mention_commit_span
        if not span:
            return False
        start, end = span
        cur = self.input.textCursor()
        pos = cur.position()
        if pos < start:      # 光标在提及前，正常删除
            return False
        if cur.hasSelection():
            sel_start, sel_end = cur.selectionStart(), cur.selectionEnd()
            if max(sel_start, start) < min(sel_end, end):
                return self._delete_mention_span(start, end)
            return False
        if key == Qt.Key_Backspace and start < pos <= end:
            return self._delete_mention_span(start, end)
        if key == Qt.Key_Delete and start <= pos < end:
            return self._delete_mention_span(start, end)
        return False

    def _delete_mention_span(self, start: int, end: int) -> bool:
        """删掉整个已插入的 @提及（含尾随空格），光标回到提及起点之后。"""
        text = self.input.toPlainText()
        if len(text) < end:
            end = len(text)
        new_text = text[:start] + text[end:]
        self._mention_commit_span = None
        self.input.setPlainText(new_text)
        c = self.input.textCursor()
        c.setPosition(start)
        self.input.setTextCursor(c)
        self._close_mention()
        return True

    def set_image_loader(self, loader):
        """注入图片加载器（登录后由 MainWindow 调用）。

        image_ready 统一接到 _refresh_preview：预览打开期间补上刚加载的图，
        无关的图片下载到货时因预览未开而 no-op。
        """
        if self._image_loader is loader:
            return
        if self._image_loader is not None:
            try:
                self._image_loader.image_ready.disconnect(self._on_preview_image_ready)
            except (RuntimeError, TypeError):
                pass
        self._image_loader = loader
        if loader is not None:
            loader.image_ready.connect(self._on_preview_image_ready)
            if self.preview_btn.isChecked():
                self._refresh_preview()

    def _on_preview_image_ready(self, url):
        if self.preview_btn.isChecked():
            self._refresh_preview()

    def _refresh_preview(self):
        from core import markdown
        text = self.input.toPlainText()
        html_text = markdown.md_to_html(text) if text else ""
        doc = self.preview_label.document()
        doc.setHtml(html_text)
        self._inject_preview_images(doc, text)

    def _inject_preview_images(self, doc: QTextDocument, md_text: str):
        """把已加载的内联图 pixmap 写进预览 doc（键用 html src 原样）。

        与 message_model._inject_inline_resources 同款：resolve 补全相对路径
        发请求、命中缓存则给 <img> 注入图片资源，否则登记等待，图片到货后
        由 image_ready 重刷补齐。未注入（离线/未登录）时 <img> 保持空白。
        """
        loader = self._image_loader
        if loader is None or not md_text:
            return
        from core import markdown
        entries = markdown.inline_image_urls(md_text)
        if not entries:
            return
        entries_by_src = {e["src"]: e for e in entries}
        cur = QTextCursor(doc)
        cur.movePosition(cur.Start)
        while True:
            fmt = cur.charFormat()
            if fmt.isImageFormat():
                ent = entries_by_src.get(fmt.toImageFormat().name())
                if ent:
                    resolved = loader.resolve(ent["url"])
                    pm = loader.pixmap_for(resolved, ent["w"])
                    if pm is not None and not pm.isNull():
                        doc.addResource(QTextDocument.ImageResource,
                                        QUrl(ent["src"]), pm)
            if cur.atEnd():
                break
            cur.movePosition(cur.NextCharacter)

    def focus_input(self):
        self.input.setFocus()

    def set_sending(self, sending: bool):
        self.send_btn.setEnabled(not sending)
        self.input.setEnabled(not sending)

    # ------------------------------------------------------------------ #
    # Emoji 选择器
    # ------------------------------------------------------------------ #
    def _toggle_emoji_picker(self, *_):
        """显示/隐藏 emoji 选择器。"""
        if not hasattr(self, "_emoji_picker"):
            from .emoji_picker import EmojiPicker
            self._emoji_picker = EmojiPicker(self)
            self._emoji_picker.emoji_selected.connect(self._insert_emoji)
            self._emoji_picker.sticker_selected.connect(
                self.sticker_selected.emit)
        picker = self._emoji_picker
        # 点按钮时 picker 先因失活自关、click 才回放，这次点击就是「关掉」
        if picker.isVisible() or picker.was_recently_closed():
            picker.close()
            return
        anchor = self.emoji_btn.mapToGlobal(QPoint(0, 0))
        picker.open_at(QPoint(anchor.x(), anchor.y() - picker.height() - 4))

    def _insert_emoji(self, emoji_char: str):
        """将 emoji 插入到输入框光标位置。"""
        self.input.insertPlainText(emoji_char)
        # 面板开着时不抢焦点：Tool 窗失活会立即自关，连选几个表情就断了
        picker = getattr(self, "_emoji_picker", None)
        if picker is None or not picker.isVisible():
            self.input.setFocus()
