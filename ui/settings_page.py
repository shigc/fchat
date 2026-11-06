"""设置页面：微信风格的左侧图标导航 + 右侧内容区。

布局仿微信 PC 客户端设置页：左侧竖排导航按钮（带图标），右侧 QStackedWidget
切换内容页。设置项之间用分割线分隔，设置块之间留白。开关改用圆角切换
SwitchButton（替代 QCheckBox）。

设置变更通过信号通知主窗口。对外保留旧接口（set_theme / set_accent / ...），
并新增账号卡、状态下拉、退出登录、快捷键页相关接口。
"""
from __future__ import annotations

import os

from PyQt5.QtCore import Qt, pyqtSignal, QSize, QRectF
from PyQt5.QtGui import (
    QFont, QFontDatabase, QFontMetrics, QPainter, QPainterPath, QColor,
    QPen, QBrush, QKeySequence, QPixmap,
)
from PyQt5.QtWidgets import (
    QAbstractButton, QWidget, QVBoxLayout, QHBoxLayout, QStackedWidget,
    QFormLayout, QPushButton, QGroupBox, QGridLayout,
    QFrame, QButtonGroup, QScrollArea, QFileDialog, QLabel,
)

from .icons import icon
from .widgets import (
    AvatarWidget, WxLabel, WxLineEdit, WxComboBox, WxPushButton,
)
from .window_chrome import WindowControls
from .chat_background import (
    PRESETS, preset_value, render_background, theme_mode,
)

from core.version import APP_VERSION
import config

# 状态 → 中文显示名（与 profile_popup 保持一致的文案）
_STATUS_CN = {
    "online": "在线",
    "away": "离开",
    "busy": "忙碌",
    "dnd": "勿扰",
    "offline": "离线",
}


class SwitchButton(QWidget):
    """圆角切换开关：灰色轨道 + 白色圆形滑块，开关时轨道变主题绿。

    轨道与主题色从取色器取，随亮/暗主题切换（修复暗色下轨道不切换）。
    """

    toggled = pyqtSignal(bool)

    _W = 40
    _H = 22
    _R = _H / 2
    _KNOB = _H - 4

    def __init__(self, theme=None, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._checked = False
        self.setFixedSize(self._W, self._H)
        self.setCursor(Qt.PointingHandCursor)

    def set_theme(self, theme):
        self._theme = theme
        self.update()

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, checked: bool):
        checked = bool(checked)
        if checked != self._checked:
            self._checked = checked
            self.toggled.emit(checked)
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self.setChecked(not self._checked)
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        from .widgets import _color
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        on = _color(self._theme, "accent", "#07C160")
        off = _color(self._theme, "scrollbar_idle", "#d7d7d7")
        track = QColor(on) if self._checked else QColor(off)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(track))
        p.drawRoundedRect(0, 0, self._W, self._H, self._R, self._R)
        # 滑块：左/右定位
        x = self._W - self._KNOB - 2 if self._checked else 2
        p.setBrush(QBrush(QColor("white")))
        p.drawEllipse(x, 2, self._KNOB, self._KNOB)
        p.end()


class BackgroundSwatch(QAbstractButton):
    """聊天背景预设小卡：圆角缩略图 + 选中强调色描边。

    缩略图按 2 倍逻辑尺寸渲染再缩画：纹理预设的间距按逻辑像素定义，
    直接按 56×34 渲染时一款纹理只能看到两三条线，看不出图案。
    """

    _W = 56
    _H = 34

    def __init__(self, value: str, name: str, theme=None, parent=None):
        super().__init__(parent)
        self._value = value or ""
        self._name = name
        self._theme = theme
        self._pixmap = None
        self._pixmap_key = None
        self.setCheckable(True)
        self.setFixedSize(self._W, self._H)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(name)
        self.setAttribute(Qt.WA_Hover, True)

    @property
    def value(self) -> str:
        return self._value

    def set_theme(self, theme):
        self._theme = theme
        self._pixmap_key = None
        self._pixmap = None
        self.update()

    def _preview_pixmap(self) -> QPixmap:
        dpr = self.devicePixelRatioF() or 1.0
        mode = theme_mode(self._theme)
        key = (self._value, mode, round(dpr, 3))
        if key != self._pixmap_key:
            self._pixmap = render_background(
                self._value, self._W * 2, self._H * 2, mode, dpr)
            self._pixmap_key = key
        return self._pixmap

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _):
        from .widgets import _color
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
        radius = 6.0
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        pm = self._preview_pixmap()
        if pm is not None and not pm.isNull():
            p.setClipPath(path)
            p.drawPixmap(rect.toRect(), pm)
            p.setClipping(False)
        else:
            # 默认背景没有固定色（随主题 chat_bg 变），画当前底色作示意
            p.fillPath(path, QColor(_color(self._theme, "chat_bg", "#f5f5f5")))
            p.setPen(QColor(_color(self._theme, "text_secondary", "#888888")))
            font = p.font()
            font.setPixelSize(11)
            p.setFont(font)
            p.drawText(rect, Qt.AlignCenter, "默认")
        if self.isChecked():
            border = QPen(QColor(_color(self._theme, "accent", "#07C160")), 2.0)
        elif self.underMouse():
            border = QPen(QColor(_color(self._theme, "accent", "#07C160")), 1.0)
        else:
            border = QPen(
                QColor(_color(self._theme, "input_border", "#dcdcdc")), 1.0)
        p.setPen(border)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(rect, radius, radius)
        p.end()


class ShortcutEdit(WxLineEdit):
    """可点击捕捉按键组合的快捷键输入框（复用 WxLineEdit 微信风格主题样式）。

    聚焦后按任意组合键即捕获并写入文本框；单独按修饰键只等待主键，单独按 Esc
    恢复为该动作的出厂默认值。捕获后立即发 shortcutCaptured 信号，供设置页保存。
    """

    shortcutCaptured = pyqtSignal(str)

    def __init__(self, default: str = "", theme=None, parent=None):
        super().__init__(theme, parent)
        self._default = default
        self.setText(default)
        self.setMinimumWidth(140)

    def keyPressEvent(self, event):
        key = int(event.key())
        mods = int(event.modifiers())
        # 单独按下修饰键时只等待主键，不写入也不触发
        if key in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta):
            event.accept()
            return
        # 单独 Esc：恢复为默认组合键
        if key == Qt.Key_Escape and mods == 0:
            self.setText(self._default)
            self.shortcutCaptured.emit(self._default)
            event.accept()
            return
        # 其余按键：拼成组合键序列写入并通知
        seq = QKeySequence(mods | key)
        text = seq.toString(QKeySequence.PortableText)
        if text:
            self.setText(text)
            self.shortcutCaptured.emit(text)
        event.accept()


def _h_line(theme=None) -> QFrame:
    """一条浅色水平分割线（颜色随主题取 border 语义值）。"""
    from .widgets import _color
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Sunken)
    line.setStyleSheet(f"color:{_color(theme, 'border', '#e5e5e5')};")
    line.setFixedHeight(1)
    return line


class SettingsPage(QWidget):
    """完整设置页面（左侧图标导航 + 右侧内容）。

    theme: 可选的 ThemeManager / 取色器，未传时用默认亮色调色板。登录后由
    主窗口注入真实 theme，保证设置页配色随亮/暗主题切换。
    """

    # 信号
    theme_changed = pyqtSignal(str)         # "light" / "dark"
    accent_changed = pyqtSignal(str)        # hex color
    font_family_changed = pyqtSignal(str)
    font_size_changed = pyqtSignal(int)
    auto_login_toggled = pyqtSignal(bool)
    logout_requested = pyqtSignal()         # 用户点击退出登录
    status_changed = pyqtSignal(str)        # 用户切换状态
    shortcuts_changed = pyqtSignal(dict)    # 快捷键变化 {动作: 组合键}
    wallpaper_changed = pyqtSignal(str)     # 聊天背景图路径（空串=恢复默认）
    avatar_clicked = pyqtSignal()           # 用户点击头像（换头像）
    storage_message = pyqtSignal(str, int)  # (文本, 毫秒) 状态栏提示，Task 7 用
    upload_service_changed = pyqtSignal(str)  # 图片上传服务地址变化
    upload_service_toggled = pyqtSignal(bool)  # 「使用自定义服务器」开关
    update_auto_check_toggled = pyqtSignal(bool)  # 「启动时自动检查更新」开关
    check_update_requested = pyqtSignal()         # 关于页手动检查更新
    server_saved = pyqtSignal(str)          # 用户在通用页保存了新的服务器地址
    llm_enabled_toggled = pyqtSignal(bool)  # AI 功能总开关
    llm_config_changed = pyqtSignal(str, str, str)  # (地址, 模型, Token)
    llm_test_requested = pyqtSignal(str, str, str)  # 用当前输入测试连接
    llm_refresh_requested = pyqtSignal()    # 进入 AI 页时从配置回读
    notify_toggled = pyqtSignal(bool)          # 通知总开关
    notify_mentions_toggled = pyqtSignal(bool)  # 仅 @我/私聊时提醒
    send_mode_changed = pyqtSignal(str)        # 发送方式（enter/ctrlenter）

    def __init__(self, theme=None, parent=None):
        super().__init__(parent)
        self._theme = theme
        # 更改路径/清空缓存都是 run_async 后台操作，按钮不会因此置灰；
        # 重入会并发迁移（配置写入竞态、迁移中删源文件），必须用标志挡住
        self._storage_busy = False
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 左侧导航
        from .widgets import _color
        self._nav = QWidget()
        self._nav.setFixedWidth(120)
        self.nav = self._nav   # 主窗口拖动带/光标过滤器引用的公开别名
        self._nav.setStyleSheet(
            f"background:{_color(self._theme, 'conv_bg', '#f4f4f4')};"
            f"border-right:1px solid {_color(self._theme, 'border', '#e5e5e5')};")
        nav_lay = QVBoxLayout(self._nav)
        nav_lay.setContentsMargins(0, 8, 0, 8)
        nav_lay.setSpacing(2)

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self._nav_buttons = []
        for key, text in [
            ("status", "账号"),
            ("general", "通用"),
            ("appearance", "外观"),
            ("font", "字体"),
            ("shortcuts", "快捷键"),
            ("storage", "存储"),
            ("ai", "AI"),
            ("about", "关于"),
        ]:
            btn = QPushButton(f"  {text}")
            btn.setCheckable(True)
            btn._icon_key = key
            btn.setIconSize(QSize(16, 16))
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(self._nav_button_qss())
            self._nav_group.addButton(btn)
            self._nav_buttons.append(btn)
            nav_lay.addWidget(btn)
        nav_lay.addStretch()
        root.addWidget(self._nav)

        # 右侧内容
        self._stack = QStackedWidget()
        root.addWidget(self._stack, 1)

        self._theme_widgets = []   # 取色控件登记表，主题切换时统一 re-style
        self._theme_blocks = []    # QGroupBox 块标题也是构建时上色，切换时重刷
        self._theme_lines = []     # 分割线同上
        # 无边框主窗口的三按钮：设置页自身没有标题行，浮在右上角
        self.window_controls = WindowControls(self)
        self.window_controls.set_theme(self._theme)
        self._theme_widgets.append(self.window_controls)
        self._build_account_page()
        self._build_general_page()
        self._build_appearance_page()
        self._build_font_page()
        self._build_shortcuts_page()
        self._build_storage_page()
        self._build_ai_page()
        self._build_about_page()

        # 默认选中「账号」页
        self._nav_buttons[0].setChecked(True)
        self._update_nav_icons()
        self._stack.setCurrentIndex(0)
        for i, btn in enumerate(self._nav_buttons):
            btn.clicked.connect(lambda checked, idx=i: self._on_nav_clicked(idx))

    def _on_nav_clicked(self, idx: int):
        self._stack.setCurrentIndex(idx)
        self._update_nav_icons()
        # 进存储页时刷新占用统计（数据会随使用持续变化）
        text = self._nav_buttons[idx].text().strip()
        if text == "存储":
            self.refresh_storage_usage()
        elif text == "AI":
            # 配置可能被主窗口/登录流程更新过，进页时回读一次
            self.llm_refresh_requested.emit()

    def _add_page(self, page: QWidget):
        """把页面包进滚动容器再入栈。

        各页内容的最小高度普遍超过矮窗口的可用高度，直接入栈时布局会把
        条目压到 sizeHint 以下，换行提示类文本被裁剪、与相邻行重叠（通用
        页上传服务提示真机踩过）。缩小窗口时改为出滚动条而不是挤压内容。
        """
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        # 视口不自动填充，让主窗口背景透过来，与直接铺在栈里的观感一致
        scroll.viewport().setAutoFillBackground(False)
        scroll.setStyleSheet("background:transparent;")
        scroll.setWidget(page)
        self._stack.addWidget(scroll)

    def position_window_controls(self):
        """三按钮浮层跟随右上角。设置页隐藏时收不到 resizeEvent，
        切页时由主窗口补调一次，避免按钮停在旧位置。"""
        wc = self.window_controls
        wc.move(self.width() - wc.width() - 12, 8)
        wc.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.position_window_controls()

    # ------------------------------------------------------------------ #
    # 主题注入
    # ------------------------------------------------------------------ #
    def set_theme(self, palette_or_manager):
        """注入取色源并刷新本页所有取色控件。

        传入 ThemeManager 或任何带 .color(key, default) 的对象，或 "light"/"dark"
        字符串（兼容旧调用）。主题切换时由主窗口调用，让设置页配色与聊天气泡
        随亮/暗一致性变化；同时把主题下拉框同步到当前模式。
        """
        if isinstance(palette_or_manager, str):
            # 旧接口：直接给主题模式，仅同步下拉框
            idx = 1 if palette_or_manager == "dark" else 0
            if hasattr(self, "theme_combo"):
                self.theme_combo.blockSignals(True)
                self.theme_combo.setCurrentIndex(idx)
                self.theme_combo.blockSignals(False)
            return
        self._theme = palette_or_manager
        self._refresh_theme()
        # 让主题下拉框反映当前模式（从配置恢复时主窗口只传 manager）
        mode = getattr(palette_or_manager, "theme", None)
        if mode in ("light", "dark") and hasattr(self, "theme_combo"):
            self.theme_combo.blockSignals(True)
            self.theme_combo.setCurrentIndex(1 if mode == "dark" else 0)
            self.theme_combo.blockSignals(False)

    def _refresh_theme(self):
        from .widgets import _color
        # 导航栏背景 / 分割线
        self._nav.setStyleSheet(
            f"background:{_color(self._theme, 'conv_bg', '#f4f4f4')};"
            f"border-right:1px solid {_color(self._theme, 'border', '#e5e5e5')};")
        for btn in self._nav_buttons:
            btn.setStyleSheet(self._nav_button_qss())
        # 图标缓存按（颜色, 描边）分键，换主题后要重新生成再挂（否则留在旧色）
        self._update_nav_icons()
        # 通用 Wx 组件统一 re-style；带 _icon_key 的按钮图标一并换色
        widgets = getattr(self, "_theme_widgets", [])
        for w in widgets:
            w.set_theme(self._theme)
            key = getattr(w, "_icon_key", None)
            if key:
                w.setIcon(icon(key, 16))
        # 块标题与分割线是构建时上色，这里重刷
        for g in self._theme_blocks:
            self._apply_block_style(g)
        line_color = _color(self._theme, "border", "#e5e5e5")
        for line in self._theme_lines:
            line.setStyleSheet(f"color:{line_color};")
        if hasattr(self, "preview_label"):
            # 预览 QSS 里带主题文字色，主题变了要重算
            self._update_preview()
        # 背景色板带亮/暗两套配色，缩略图与预览都要按新主题重渲染
        for swatch in getattr(self, "_wallpaper_swatches", []):
            swatch.set_theme(self._theme)
        self._update_wallpaper_preview()
        self.update()

    def _nav_button_qss(self) -> str:
        from .widgets import _color, _base_size
        t = self._theme
        normal = _color(t, "text_primary", "#333333")
        hover_bg = _color(t, "conv_hover", "#e8e8e8")
        checked_bg = _color(t, "conv_hover", "#d6ecdf")
        checked_fg = _color(t, "accent", "#07C160")
        return (
            "QPushButton{text-align:left;padding:10px 12px;border:none;"
            f"background:transparent;font-size:{_base_size(t)}px;color:{normal};}} "
            f"QPushButton:hover{{background:{hover_bg};border-radius:4px;}} "
            f"QPushButton:checked{{background:{checked_bg};color:{checked_fg};"
            "border-radius:4px;font-weight:bold;}")

    # 导航图标描边加粗：默认 1.4 的细线在 16px 下抗锯齿后大量半透明像素，
    # 与并排的深色正文相比明显发灰发浅（真机反馈）
    _NAV_STROKE = 1.9

    def _update_nav_icons(self):
        """按选中态重挂导航图标：选中项文字变强调色，图标也跟着换色。"""
        from .widgets import _color
        accent = _color(self._theme, "accent", "#07C160")
        for btn in self._nav_buttons:
            color = accent if btn.isChecked() else None
            btn.setIcon(icon(btn._icon_key, 16,
                             color=color, stroke=self._NAV_STROKE))

    # ------------------------------------------------------------------ #
    # 通用布局辅助：设置块容器
    # ------------------------------------------------------------------ #
    def _block(self, title: str) -> QGroupBox:
        g = QGroupBox(title)
        self._theme_blocks.append(g)
        self._apply_block_style(g)
        return g

    def _apply_block_style(self, g: QGroupBox):
        from .widgets import _color, _base_size
        c = _color(self._theme, "text_primary", "#333333")
        g.setStyleSheet(
            f"QGroupBox{{font-size:{_base_size(self._theme)}px;"
            f"font-weight:bold;color:{c};"
            "border:none;margin-top:4px;padding-top:8px;} "
            "QGroupBox::title{subcontrol-origin:margin;left:0px;top:0px;}")

    def _add_h_line(self, lay):
        """创建分割线并登记，主题切换时由 _refresh_theme 重刷颜色。"""
        line = _h_line(self._theme)
        self._theme_lines.append(line)
        lay.addWidget(line)

    # ------------------------------------------------------------------ #
    # 账号页
    # ------------------------------------------------------------------ #
    def _build_account_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        # 顶边距 36：右上角浮着无边框窗口的三按钮（y 8..38），第一行控件
        # 原 y=41 只距 3px，贴脸（真机反馈）；36 让首行从 y=57 起
        lay.setContentsMargins(20, 36, 20, 20)
        lay.setSpacing(16)

        # 头像 + 姓名 + 用户名 + 部门（微信风格：
        # 头像左，文字右）
        # 头像与文字同排顶部对齐（AlignTop）：姓名是首行，加粗大字，用户名退居次要
        info_row = QHBoxLayout()
        info_row.setSpacing(16)
        self._avatar = AvatarWidget(64)
        self._avatar.setFixedSize(64, 64)
        self._avatar.set_clickable(True)
        self._avatar.setToolTip("点击更换头像")
        self._avatar.clicked.connect(self.avatar_clicked.emit)
        info_row.addWidget(self._avatar, 0, Qt.AlignTop)

        text_col = QVBoxLayout()
        text_col.setSpacing(4)
        self._name_label = WxLabel("未登录", self._theme)
        self._name_label.set_variant("bold")   # 姓名加粗；无姓名时显示用户名
        self._username_label = WxLabel("", self._theme)
        self._username_label.set_variant("secondary")
        self._department_label = WxLabel("", self._theme)
        self._department_label.set_variant("hint")
        self._department_label.setVisible(False)   # 无部门信息时不占行
        # 不能在 text_col 里加 stretch：内含 stretch 的布局在垂直方向
        # 变成 expanding，会和页面底部的 addStretch 平分剩余空间，头像行
        # 被撑到 200px+，头像下方出现大块空白；页面级 addStretch 已兜底
        for w in (self._name_label, self._username_label, self._department_label):
            text_col.addWidget(w)
            self._theme_widgets.append(w)
        info_row.addLayout(text_col, 1)
        lay.addLayout(info_row)

        self._add_h_line(lay)

        # 状态切换
        status_block = self._block("在线状态")
        status_form = QFormLayout(status_block)
        status_form.setSpacing(12)
        self.status_combo = WxComboBox(self._theme)
        for value, cn in _STATUS_CN.items():
            self.status_combo.addItem(cn, value)
        self.status_combo.currentIndexChanged.connect(self._on_status_changed)
        self._theme_widgets.append(self.status_combo)
        status_form.addRow("当前状态：", self.status_combo)
        lay.addWidget(status_block)

        self._add_h_line(lay)

        # 退出登录
        logout_block = self._block("账户操作")
        logout_lay = QVBoxLayout(logout_block)
        logout_btn = WxPushButton(" 退出登录", self._theme, danger=True)
        logout_btn._icon_key = "logout"
        logout_btn.setIcon(icon("logout", 16))
        logout_btn.setCursor(Qt.PointingHandCursor)
        logout_btn.clicked.connect(self.logout_requested.emit)
        self._theme_widgets.append(logout_btn)
        logout_lay.addWidget(logout_btn)
        logout_hint = WxLabel("退出后不会清除「记住密码/自动登录」配置。", self._theme)
        logout_hint.set_variant("hint")
        self._theme_widgets.append(logout_hint)
        logout_lay.addWidget(logout_hint)
        lay.addWidget(logout_block)

        lay.addStretch()
        self._add_page(page)

    def _on_status_changed(self, index: int):
        value = self.status_combo.itemData(index)
        if value:
            self.status_changed.emit(value)

    def show_account(self, username: str, name: str, avatar_url: str = "",
                     department: str = ""):
        """显示当前登录用户信息。未登录时调用 show_account("") 显示占位。

        姓名是主显示行：有姓名时显示姓名加粗、用户名以 @xxx 作次要行；
        没有姓名时姓名行回退显示用户名，用户名行留空避免重复。
        """
        if username:
            self._name_label.setText(name or username)
            has_name = bool(name)
            self._username_label.setText(f"@{username}" if has_name else "")
            # 空文本的 QLabel 仍占一行高，姓名回退成用户名时必须整行隐藏，
            # 否则姓名和部门之间隔出一行空白
            self._username_label.setVisible(has_name)
            self._avatar.set_name(name or username)
            if department:
                self._department_label.setText(department)
                self._department_label.setVisible(True)
            else:
                self._department_label.setVisible(False)
        else:
            self._name_label.setText("未登录")
            self._username_label.setText("")
            self._username_label.setVisible(False)
            self._department_label.setText("")
            self._department_label.setVisible(False)
            self._avatar.set_name("")
        self._pending_avatar_url = avatar_url

    def set_department(self, department: str):
        """异步补齐部门信息后回写（登录时不再串行查 users_info）。"""
        if department:
            self._department_label.setText(department)
            self._department_label.setVisible(True)
        else:
            self._department_label.setVisible(False)

    def set_avatar_loader(self, loader):
        """注入头像加载器，异步加载头像图片。"""
        self._avatar_loader = loader
        if loader is not None and loader is not getattr(
                self, "_avatar_loader_prev", None):
            # 头像 URL 不随内容变化，登录后的一致性检查发现远端变了时，
            # 靠这个信号把新头像刷到控件上（磁盘缓存已被更新）。
            # set_avatar_loader 会被多次调用（登录成功/换头像后），防重复连接
            loader.avatar_ready.connect(self._on_avatar_ready)
        self._avatar_loader_prev = loader
        url = getattr(self, "_pending_avatar_url", "")
        if url and loader:
            loader.load(self._avatar, url)

    def _on_avatar_ready(self, url: str):
        if url == getattr(self, "_pending_avatar_url", "") \
                and self._avatar_loader is not None:
            self._avatar_loader.load(self._avatar, url)

    def set_status(self, status: str):
        """回写当前状态到下拉框（用于从配置/登录结果恢复）。"""
        idx = self.status_combo.findData(status)
        if idx >= 0:
            self.status_combo.blockSignals(True)
            self.status_combo.setCurrentIndex(idx)
            self.status_combo.blockSignals(False)

    # ------------------------------------------------------------------ #
    # 通用页
    # ------------------------------------------------------------------ #
    def _build_general_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        # 顶边距 36：右上角浮着无边框窗口的三按钮（y 8..38），第一行控件
        # 原 y=41 只距 3px，贴脸（真机反馈）；36 让首行从 y=57 起
        lay.setContentsMargins(20, 36, 20, 20)
        lay.setSpacing(16)

        # 服务器地址（可配置）；账号信息在「账号」页展示，这里不再重复
        server_block = self._block("服务器")
        server_form = QFormLayout(server_block)
        server_form.setSpacing(12)
        self._server_input = WxLineEdit(self._theme)
        self._server_input.setPlaceholderText("例如 chat.example.com")
        self._theme_widgets.append(self._server_input)
        server_form.addRow("服务器地址：", self._server_input)
        server_row = QHBoxLayout()
        save_btn = WxPushButton("保存", self._theme)
        save_btn.setCursor(Qt.PointingHandCursor)
        save_btn.clicked.connect(self._on_server_saved)
        self._theme_widgets.append(save_btn)
        server_row.addWidget(save_btn)
        save_hint = WxLabel("修改后需重启应用生效。", self._theme)
        save_hint.set_variant("hint")
        self._theme_widgets.append(save_hint)
        server_row.addWidget(save_hint)
        server_row.addStretch()
        server_form.addRow("", server_row)
        lay.addWidget(server_block)

        self._add_h_line(lay)

        # 登录设置
        login_block = self._block("登录")
        self.auto_login_switch = SwitchButton(self._theme)
        self.auto_login_switch.toggled.connect(self.auto_login_toggled.emit)
        self._theme_widgets.append(self.auto_login_switch)
        login_row = QHBoxLayout()
        auto_login_desc = WxLabel("启动时自动登录", self._theme)
        self._theme_widgets.append(auto_login_desc)
        login_row.addWidget(auto_login_desc)
        login_row.addStretch()
        login_row.addWidget(self.auto_login_switch)
        login_block_layout = QVBoxLayout(login_block)
        login_block_layout.addLayout(login_row)
        # keyring 不可用时密码不落盘，自动登录必然失效，提示用户
        if not config.keyring_available():
            kr_hint = WxLabel("当前环境无法安全保存密码，自动登录不可用。",
                              self._theme)
            kr_hint.set_variant("hint")
            self._theme_widgets.append(kr_hint)
            login_block_layout.addWidget(kr_hint)
        lay.addWidget(login_block)

        self._add_h_line(lay)

        # 图片上传服务
        up_block = self._block("图片上传服务")
        up_form = QFormLayout(up_block)
        up_form.setSpacing(12)
        self.upload_custom_switch = SwitchButton(self._theme)
        self.upload_custom_switch.toggled.connect(
            self._on_upload_custom_toggled)
        self._theme_widgets.append(self.upload_custom_switch)
        up_switch_row = QHBoxLayout()
        up_switch_desc = WxLabel("使用自定义服务器", self._theme)
        self._theme_widgets.append(up_switch_desc)
        up_switch_row.addWidget(up_switch_desc)
        up_switch_row.addStretch()
        up_switch_row.addWidget(self.upload_custom_switch)
        up_form.addRow("", up_switch_row)
        self.upload_service_input = WxLineEdit(self._theme)
        self.upload_service_input.setPlaceholderText(
            "例如 https://example.com/api/upload")
        self.upload_service_input.editingFinished.connect(
            lambda: self.upload_service_changed.emit(
                self.upload_service_input.text().strip()))
        self._theme_widgets.append(self.upload_service_input)
        up_form.addRow("上传服务地址：", self.upload_service_input)
        up_hint = WxLabel("默认关闭，图片上传到配置的服务器（聊天服务器），"
                          "以文件链接展示；开启后图文混排的图片改经下方自定义"
                          "上传服务上传，并在消息中内联显示。\n"
                          "上传服务需服务端配置 IMAGE_TOKEN，以 "
                          "Authorization: Bearer <token> 鉴权。",
                          self._theme)
        up_hint.set_variant("hint")
        up_hint.setWordWrap(True)
        self._theme_widgets.append(up_hint)
        up_form.addRow("", up_hint)
        self.upload_service_token_input = WxLineEdit(self._theme)
        self.upload_service_token_input.setEchoMode(WxLineEdit.Password)
        self.upload_service_token_input.setPlaceholderText("图床 IMAGE_TOKEN")
        self.upload_service_token_input.editingFinished.connect(
            lambda: self.upload_service_changed.emit(
                self.upload_service_input.text().strip()))
        self._theme_widgets.append(self.upload_service_token_input)
        up_form.addRow("上传服务 Token：", self.upload_service_token_input)
        self._apply_upload_custom_state(self.upload_custom_switch.isChecked())
        lay.addWidget(up_block)

        self._add_h_line(lay)

        # 软件更新
        upd_block = self._block("软件更新")
        upd_row = QHBoxLayout()
        upd_desc = WxLabel("启动时自动检查新版本", self._theme)
        self._theme_widgets.append(upd_desc)
        upd_row.addWidget(upd_desc)
        upd_row.addStretch()
        self.update_auto_switch = SwitchButton(self._theme)
        self.update_auto_switch.toggled.connect(
            self.update_auto_check_toggled.emit)
        self._theme_widgets.append(self.update_auto_switch)
        upd_row.addWidget(self.update_auto_switch)
        upd_block_layout = QVBoxLayout(upd_block)
        upd_block_layout.addLayout(upd_row)
        lay.addWidget(upd_block)

        self._add_h_line(lay)

        # 通知设置
        notify_block = self._block("通知")
        notify_lay = QVBoxLayout(notify_block)
        # 总开关
        notify_row = QHBoxLayout()
        notify_desc = WxLabel("接收消息提醒", self._theme)
        self._theme_widgets.append(notify_desc)
        notify_row.addWidget(notify_desc)
        notify_row.addStretch()
        self.notify_switch = SwitchButton(self._theme)
        self.notify_switch.toggled.connect(self.notify_toggled.emit)
        self._theme_widgets.append(self.notify_switch)
        notify_row.addWidget(self.notify_switch)
        notify_lay.addLayout(notify_row)
        # 仅 @我/私聊
        mentions_row = QHBoxLayout()
        mentions_desc = WxLabel("仅 @我 或私聊时提醒", self._theme)
        self._theme_widgets.append(mentions_desc)
        mentions_row.addWidget(mentions_desc)
        mentions_row.addStretch()
        self.notify_mentions_switch = SwitchButton(self._theme)
        self.notify_mentions_switch.toggled.connect(
            self.notify_mentions_toggled.emit)
        self._theme_widgets.append(self.notify_mentions_switch)
        mentions_row.addWidget(self.notify_mentions_switch)
        notify_lay.addLayout(mentions_row)
        notify_hint = WxLabel("关闭后仍会累计未读，只是不再弹出提醒/闪烁托盘。",
                             self._theme)
        notify_hint.set_variant("hint")
        notify_hint.setWordWrap(True)
        self._theme_widgets.append(notify_hint)
        notify_lay.addWidget(notify_hint)
        lay.addWidget(notify_block)

        self._add_h_line(lay)

        # 发送方式
        send_block = self._block("发送")
        send_form = QFormLayout(send_block)
        send_form.setSpacing(12)
        self.send_mode_combo = WxComboBox(self._theme)
        self.send_mode_combo.addItem("回车发送（Shift+回车换行）", "enter")
        self.send_mode_combo.addItem("Ctrl+回车发送（回车换行）", "ctrlenter")
        self.send_mode_combo.currentIndexChanged.connect(
            self._on_send_mode_changed)
        self._theme_widgets.append(self.send_mode_combo)
        send_form.addRow("发送方式：", self.send_mode_combo)
        lay.addWidget(send_block)

        self._add_h_line(lay)

        # E2E 密码
        e2e_block = self._block("端到端加密")
        e2e_form = QFormLayout(e2e_block)
        self.e2e_input = WxLineEdit(self._theme)
        self.e2e_input.setEchoMode(WxLineEdit.Password)
        self.e2e_input.setPlaceholderText("输入 E2E 密码（可选）")
        self._theme_widgets.append(self.e2e_input)
        e2e_form.addRow("E2E 密码：", self.e2e_input)
        # 保存/验证结果就地提示（红=失败，灰=成功或说明），由主窗口回写
        self.e2e_hint = WxLabel("", self._theme)
        self.e2e_hint.set_variant("hint")
        self.e2e_hint.setWordWrap(True)
        self._theme_widgets.append(self.e2e_hint)
        e2e_form.addRow(self.e2e_hint)
        lay.addWidget(e2e_block)

        self._add_h_line(lay)

        hint = WxLabel("提示：会话数据来自 Rocket.Chat 服务器，"
                       "实时消息通过 WebSocket(DDP) 推送。", self._theme)
        hint.set_variant("hint")
        hint.setWordWrap(True)
        self._theme_widgets.append(hint)
        lay.addWidget(hint)

        lay.addStretch()
        self._add_page(page)

    def _on_server_saved(self, *_):
        """读取服务器地址输入框，发射保存信号（判空交给主窗口提示）。"""
        self.server_saved.emit(self._server_input.text())

    def set_server_url(self, text: str):
        """回写服务器地址输入框（不触发保存信号）。"""
        self._server_input.setText(text or "")

    def set_auto_login(self, enabled: bool):
        self.auto_login_switch.blockSignals(True)
        self.auto_login_switch.setChecked(bool(enabled))
        self.auto_login_switch.blockSignals(False)

    def set_upload_service(self, url: str):
        """回写已保存的上传服务地址（登录启动时恢复）。"""
        self.upload_service_input.setText(url or "")

    def set_upload_service_token(self, token: str):
        """回写已保存的上传服务 token（登录启动时恢复）。"""
        self.upload_service_token_input.setText(token or "")

    def set_upload_service_enabled(self, enabled: bool):
        """回写「使用自定义服务器」开关（登录启动时恢复），不触发保存。"""
        self.upload_custom_switch.blockSignals(True)
        self.upload_custom_switch.setChecked(bool(enabled))
        self.upload_custom_switch.blockSignals(False)
        self._apply_upload_custom_state(bool(enabled))

    def set_e2e_password(self, text: str):
        """回写已保存的 E2E 密码（启动时恢复，不触发保存）。"""
        self.e2e_input.setText(text or "")

    def set_e2e_hint(self, text: str, error: bool = False):
        """回写 E2E 密码保存/验证结果提示；error=True 显示红色。"""
        self.e2e_hint.setText(text or "")
        self.e2e_hint.set_variant("danger" if error else "hint")

    def set_update_auto_check(self, enabled: bool):
        """回写「启动时自动检查更新」开关（不触发保存）。"""
        self.update_auto_switch.blockSignals(True)
        self.update_auto_switch.setChecked(bool(enabled))
        self.update_auto_switch.blockSignals(False)

    def set_notify(self, enabled: bool):
        """回写通知总开关（启动时恢复，不触发保存）。"""
        self.notify_switch.blockSignals(True)
        self.notify_switch.setChecked(bool(enabled))
        self.notify_switch.blockSignals(False)

    def set_notify_mentions(self, enabled: bool):
        """回写「仅 @我/私聊」开关（启动时恢复，不触发保存）。"""
        self.notify_mentions_switch.blockSignals(True)
        self.notify_mentions_switch.setChecked(bool(enabled))
        self.notify_mentions_switch.blockSignals(False)

    def set_send_mode(self, mode: str):
        """回写发送方式下拉框（启动时恢复，不触发保存）。"""
        idx = self.send_mode_combo.findData(mode or "enter")
        if idx < 0:
            idx = self.send_mode_combo.findData("enter")
        self.send_mode_combo.blockSignals(True)
        self.send_mode_combo.setCurrentIndex(idx)
        self.send_mode_combo.blockSignals(False)

    def _on_send_mode_changed(self, index: int):
        """发送方式下拉变化时发射信号。"""
        mode = self.send_mode_combo.itemData(index) or "enter"
        self.send_mode_changed.emit(mode)

    def _on_upload_custom_toggled(self, checked: bool):
        self._apply_upload_custom_state(checked)
        self.upload_service_toggled.emit(checked)

    def _apply_upload_custom_state(self, checked: bool):
        """关闭时地址/Token 输入框置灰（配置保留但不用）。"""
        self.upload_service_input.setEnabled(checked)
        self.upload_service_token_input.setEnabled(checked)

    # ------------------------------------------------------------------ #
    # 外观页
    # ------------------------------------------------------------------ #
    def _build_appearance_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        # 顶边距 36：右上角浮着无边框窗口的三按钮（y 8..38），第一行控件
        # 原 y=41 只距 3px，贴脸（真机反馈）；36 让首行从 y=57 起
        lay.setContentsMargins(20, 36, 20, 20)
        lay.setSpacing(16)

        theme_block = self._block("主题模式")
        theme_row = QHBoxLayout(theme_block)
        theme_row.setSpacing(12)
        theme_desc = WxLabel("选择主题：", self._theme)
        self._theme_widgets.append(theme_desc)
        theme_row.addWidget(theme_desc)
        self.theme_combo = WxComboBox(self._theme)
        self.theme_combo.addItems(["亮色", "暗色"])
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        self._theme_widgets.append(self.theme_combo)
        theme_row.addWidget(self.theme_combo)
        theme_row.addStretch()
        lay.addWidget(theme_block)

        wall_block = self._block("聊天背景")
        wall_body = QVBoxLayout(wall_block)
        wall_body.setSpacing(12)
        wall_row = QHBoxLayout()
        wall_row.setSpacing(12)
        # 缩略图 QLabel 挂 parent，避免构造期 setVisible 闪独立窗口
        self.wallpaper_preview = QLabel("默认", wall_block)
        self.wallpaper_preview.setFixedSize(120, 68)
        self.wallpaper_preview.setAlignment(Qt.AlignCenter)
        wall_row.addWidget(self.wallpaper_preview)
        wall_btns = QVBoxLayout()
        wall_btns.setSpacing(8)
        wall_choose_btn = WxPushButton("选择图片…", self._theme)
        wall_choose_btn.clicked.connect(self._on_choose_wallpaper)
        wall_reset_btn = WxPushButton("恢复默认", self._theme)
        wall_reset_btn.clicked.connect(self._on_reset_wallpaper)
        wall_btns.addWidget(wall_choose_btn)
        wall_btns.addWidget(wall_reset_btn)
        wall_row.addLayout(wall_btns)
        wall_row.addStretch()
        wall_body.addLayout(wall_row)

        swatch_grid = QGridLayout()
        swatch_grid.setHorizontalSpacing(10)
        swatch_grid.setVerticalSpacing(8)
        self._wallpaper_group = QButtonGroup(self)
        self._wallpaper_group.setExclusive(True)
        self._wallpaper_swatches = []
        cols = 8
        items = [("", "默认")] + [
            (preset_value(p.id), p.name) for p in PRESETS]
        for idx, (value, name) in enumerate(items):
            swatch = BackgroundSwatch(value, name, self._theme, wall_block)
            swatch.clicked.connect(
                lambda checked=False, v=value: self._on_preset_wallpaper(v))
            self._wallpaper_group.addButton(swatch)
            self._wallpaper_swatches.append(swatch)
            swatch_grid.addWidget(swatch, idx // cols, idx % cols)
        # 末尾留弹性列：QGridLayout 会把多余宽度均分给各列，色板会散开
        swatch_grid.setColumnStretch(cols, 1)
        wall_body.addLayout(swatch_grid)
        lay.addWidget(wall_block)

        lay.addStretch()
        self._add_page(page)
        self.set_wallpaper("")

    def set_wallpaper(self, value: str):
        """回显当前背景：空串=默认、preset:<id>=预设、其它=图片路径。"""
        self._wallpaper_value = value or ""
        self._sync_wallpaper_swatch()
        self._update_wallpaper_preview()

    def _sync_wallpaper_swatch(self):
        """选中匹配的色板；本地图片没有对应色板时全部取消选中。"""
        group = getattr(self, "_wallpaper_group", None)
        if group is None:
            return
        target = next(
            (s for s in self._wallpaper_swatches
             if s.value == self._wallpaper_value), None)
        # 独占组的按钮没法直接取消勾选，需临时解除独占
        group.setExclusive(False)
        for swatch in self._wallpaper_swatches:
            swatch.setChecked(swatch is target)
        group.setExclusive(True)

    def _update_wallpaper_preview(self):
        if not hasattr(self, "wallpaper_preview"):
            return
        from .widgets import _color
        value = getattr(self, "_wallpaper_value", "")
        dpr = self.wallpaper_preview.devicePixelRatioF() or 1.0
        # 120×68 外框去掉 1px 边框，内容区 118×66
        pm = render_background(value, 118, 66, theme_mode(self._theme), dpr)
        bg = _color(self._theme, "chat_bg", "#f5f5f5")
        self.wallpaper_preview.setStyleSheet(
            f"background:{bg};"
            f"color:{_color(self._theme, 'text_secondary', '#888888')};"
            f"border:1px solid {_color(self._theme, 'input_border', '#dcdcdc')};")
        if pm is not None and not pm.isNull():
            self.wallpaper_preview.setText("")
            self.wallpaper_preview.setPixmap(pm)
        else:
            self.wallpaper_preview.setPixmap(QPixmap())
            self.wallpaper_preview.setText("默认")

    def _on_choose_wallpaper(self, *_):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择背景图片", "", "图片 (*.png *.jpg *.jpeg *.bmp *.webp)")
        if not path:
            return
        self.set_wallpaper(path)
        self.wallpaper_changed.emit(path)

    def _on_reset_wallpaper(self, *_):
        self.set_wallpaper("")
        self.wallpaper_changed.emit("")

    def _on_preset_wallpaper(self, value: str):
        self.set_wallpaper(value)
        self.wallpaper_changed.emit(value)

    def _on_theme_changed(self, index: int):
        theme = "dark" if index == 1 else "light"
        self.theme_changed.emit(theme)

    # ------------------------------------------------------------------ #
    # 字体页
    # ------------------------------------------------------------------ #
    def _build_font_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        # 顶边距 36：右上角浮着无边框窗口的三按钮（y 8..38），第一行控件
        # 原 y=41 只距 3px，贴脸（真机反馈）；36 让首行从 y=57 起
        lay.setContentsMargins(20, 36, 20, 20)
        lay.setSpacing(16)

        font_block = self._block("字体")
        font_form = QFormLayout(font_block)
        font_form.setSpacing(12)

        self.font_combo = WxComboBox(self._theme)
        # 按「是否含中文字形」筛选，而不是按字体名关键字白名单：硬编码
        # 英文名（SimSun/SimHei 等）会漏掉以中文名或其他命名出现的字体
        # （宋体/黑体/华文系列等），导致可选字体极少。用 QFontMetrics.inFont
        # 判断 U+4E2D「中」是否有字形即可覆盖全平台命名差异。
        chinese_fonts = []
        db = QFontDatabase()
        for f in db.families():
            try:
                if QFontMetrics(QFont(f)).inFont("中"):
                    chinese_fonts.append(f)
            except Exception:  # noqa: BLE001 个别字体构造失败直接跳过
                continue
        seen = set()
        unique = []
        for f in chinese_fonts:
            if f not in seen:
                seen.add(f)
                unique.append(f)
        self.font_combo.addItems(sorted(unique) or ["Microsoft YaHei"])
        self.font_combo.currentTextChanged.connect(self._on_font_family_changed)
        self._theme_widgets.append(self.font_combo)
        font_form.addRow("字体族：", self.font_combo)

        self.font_size_combo = WxComboBox(self._theme)
        # 字号下拉框：item 文本显示 "Npx"，data 存整数，方便按数值恢复当前选中项
        for size in range(10, 25):
            self.font_size_combo.addItem(f"{size}", size)
        self.font_size_combo.setCurrentIndex(self.font_size_combo.findData(13))
        self.font_size_combo.currentIndexChanged.connect(self._on_font_size_combo_changed)
        self._theme_widgets.append(self.font_size_combo)
        font_form.addRow("大小：", self.font_size_combo)
        lay.addWidget(font_block)

        self._add_h_line(lay)

        preview_block = self._block("预览")
        preview_lay = QVBoxLayout(preview_block)
        self.preview_label = WxLabel(
            "这是一段预览文本。The quick brown fox jumps over the lazy dog.\n"
            "你好世界！1234567890", self._theme)
        self.preview_label.setWordWrap(True)
        self._theme_widgets.append(self.preview_label)
        preview_lay.addWidget(self.preview_label)
        lay.addWidget(preview_block)

        lay.addStretch()
        self._add_page(page)

    def _on_font_size_combo_changed(self, index: int):
        self._update_preview()
        self.font_size_changed.emit(self.font_size_combo.itemData(index))

    def _on_font_family_changed(self, family: str):
        self._update_preview()
        self.font_family_changed.emit(family)

    def _preview_qss(self) -> str:
        """预览标签的完整 QSS：跟随字体设置 + 主题文字色（避免 setStyleSheet 覆盖主题色）。"""
        from .widgets import _color
        fg = _color(self._theme, "text_primary", "#333333")
        return (
            f"font-family:'{self.font_combo.currentText()}';"
            f"font-size:{self.font_size_combo.currentData()}px;color:{fg};"
            "background:transparent;")

    def _update_preview(self):
        """让预览文本实时跟随当前字体族和大小。"""
        self.preview_label.setStyleSheet(self._preview_qss())

    def _set_font_size(self, size: int):
        idx = self.font_size_combo.findData(size)
        if idx >= 0:
            self.font_size_combo.blockSignals(True)
            self.font_size_combo.setCurrentIndex(idx)
            self.font_size_combo.blockSignals(False)

    # ------------------------------------------------------------------ #
    # 快捷键页
    # ------------------------------------------------------------------ #
    def _build_shortcuts_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        # 顶边距 36：右上角浮着无边框窗口的三按钮（y 8..38），第一行控件
        # 原 y=41 只距 3px，贴脸（真机反馈）；36 让首行从 y=57 起
        lay.setContentsMargins(20, 36, 20, 20)
        lay.setSpacing(16)

        block = self._block("快捷键")
        form = QFormLayout(block)
        form.setSpacing(12)

        self._shortcut_edits = {}
        # 动作名 → (中文说明, 默认组合键)
        self._shortcut_defaults = {
            "open_settings": ("打开设置", "Ctrl+,"),
            "close_settings": ("收起设置", "Esc"),
            "quick_switch": ("快速切换会话", "Ctrl+K"),
        }
        for action, (desc, default) in self._shortcut_defaults.items():
            edit = ShortcutEdit(default, self._theme)
            edit.setPlaceholderText("点击后输入组合键")
            # 快捷键编辑用按键捕捉，不依赖 editingFinished（键盘可编辑框已屏蔽输入）
            edit.shortcutCaptured.connect(
                lambda a=action: self._on_shortcut_edited(a))
            form.addRow(desc + "：", edit)
            self._shortcut_edits[action] = edit
            self._theme_widgets.append(edit)
        lay.addWidget(block)

        tip = WxLabel("提示：在输入框中点击，直接按下按键组合即可自动设置。", self._theme)
        tip.set_variant("hint")
        self._theme_widgets.append(tip)
        lay.addWidget(tip)

        lay.addStretch()
        self._add_page(page)

    def _on_shortcut_edited(self, action: str):
        """读取该动作输入框的组合键，发射变化信号。"""
        self.shortcuts_changed.emit(self.get_shortcuts())

    def get_shortcuts(self) -> dict:
        """收集当前所有快捷键：{动作: 组合键}。"""
        return {a: e.text().strip() for a, e in self._shortcut_edits.items()}

    def set_shortcuts(self, shortcuts: dict):
        """从配置回写快捷键到输入框（不触发 signal）。"""
        # 不写 (shortcuts or {}).items()：Cython 3.3 在 annotation_typing=False
        # 下对 or 联合表达式推断类型会编译器崩溃
        if not shortcuts:
            return
        for action, value in shortcuts.items():
            edit = self._shortcut_edits.get(action)
            if edit and value:
                edit.setText(str(value))

    # ------------------------------------------------------------------ #
    # 存储页
    # ------------------------------------------------------------------ #
    @staticmethod
    def _fmt_size(n: int) -> str:
        for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
            if n >= div:
                return f"{n / div:.1f} {unit}"
        return f"{n} B"

    def _open_dir_button(self, dir_fn) -> WxPushButton:
        """「打开」按钮：点击时用系统资源管理器打开 dir_fn() 当前值。

        点时才取路径（迁移后目录会变）；目录还没建过时先创建，
        否则资源管理器报路径不存在。
        """
        import os
        from PyQt5.QtCore import QUrl
        from PyQt5.QtGui import QDesktopServices
        btn = WxPushButton("打开", self._theme)
        btn.setCursor(Qt.PointingHandCursor)

        def _open():
            path = dir_fn()
            try:
                os.makedirs(path, exist_ok=True)
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            except OSError as e:
                self.storage_message.emit(f"无法打开目录 {path}：{e}", 6000)

        btn.clicked.connect(_open)
        self._theme_widgets.append(btn)
        return btn

    def _build_storage_page(self):
        from core.paths import cache_root, logs_dir
        page = QWidget()
        lay = QVBoxLayout(page)
        # 顶边距 36：右上角浮着无边框窗口的三按钮（y 8..38），第一行控件
        # 原 y=41 只距 3px，贴脸（真机反馈）；36 让首行从 y=57 起
        lay.setContentsMargins(20, 36, 20, 20)
        lay.setSpacing(16)

        cache_block = self._block("缓存目录")
        form = QFormLayout(cache_block)
        form.setSpacing(12)
        self._cache_path_label = WxLabel(cache_root(), self._theme)
        self._cache_path_label.setWordWrap(True)
        self._theme_widgets.append(self._cache_path_label)
        cache_path_row = QHBoxLayout()
        cache_path_row.addWidget(self._cache_path_label, 1)
        cache_path_row.addWidget(self._open_dir_button(cache_root))
        form.addRow("路径：", cache_path_row)

        size_row = QHBoxLayout()
        self._cache_size_label = WxLabel("计算中…", self._theme)
        self._theme_widgets.append(self._cache_size_label)
        size_row.addWidget(self._cache_size_label)
        size_row.addStretch()
        refresh_btn = WxPushButton("刷新", self._theme)
        refresh_btn.setCursor(Qt.PointingHandCursor)
        refresh_btn.clicked.connect(self.refresh_storage_usage)
        self._theme_widgets.append(refresh_btn)
        size_row.addWidget(refresh_btn)
        form.addRow("占用：", size_row)

        op_row = QHBoxLayout()
        change_btn = WxPushButton("更改路径…", self._theme)
        change_btn.setCursor(Qt.PointingHandCursor)
        change_btn.clicked.connect(self._on_change_storage_path)
        self._theme_widgets.append(change_btn)
        op_row.addWidget(change_btn)
        clear_btn = WxPushButton("清空缓存", self._theme, danger=True)
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.clicked.connect(self._on_clear_cache)
        self._theme_widgets.append(clear_btn)
        op_row.addWidget(clear_btn)
        op_row.addStretch()
        form.addRow("操作：", op_row)
        lay.addWidget(cache_block)

        self._add_h_line(lay)

        log_block = self._block("日志目录")
        log_form = QFormLayout(log_block)
        log_form.setSpacing(12)
        self._logs_path_label = WxLabel(logs_dir(), self._theme)
        self._logs_path_label.setWordWrap(True)
        self._theme_widgets.append(self._logs_path_label)
        logs_path_row = QHBoxLayout()
        logs_path_row.addWidget(self._logs_path_label, 1)
        logs_path_row.addWidget(self._open_dir_button(logs_dir))
        log_form.addRow("路径：", logs_path_row)
        self._logs_size_label = WxLabel("计算中…", self._theme)
        self._theme_widgets.append(self._logs_size_label)
        log_form.addRow("占用：", self._logs_size_label)
        lay.addWidget(log_block)

        hint = WxLabel("提示：聊天图片与文件会自动缓存到上述目录；"
                       "清空缓存不影响聊天记录，需要时会自动重新下载。",
                       self._theme)
        hint.set_variant("hint")
        hint.setWordWrap(True)
        self._theme_widgets.append(hint)
        lay.addWidget(hint)

        lay.addStretch()
        self._add_page(page)

    def refresh_storage_usage(self, *_):
        """后台线程统计缓存/日志目录大小，回填标签。"""
        from core.paths import cache_root, logs_dir, dir_size
        from .api_workers import run_async

        def _fail(_err):
            self._cache_size_label.setText("统计失败")
            self._logs_size_label.setText("统计失败")

        run_async(lambda: dir_size(cache_root()),
                  on_done=lambda n: self._cache_size_label.setText(
                      self._fmt_size(n)),
                  on_fail=_fail, parent=self)
        run_async(lambda: dir_size(logs_dir()),
                  on_done=lambda n: self._logs_size_label.setText(
                      self._fmt_size(n)),
                  on_fail=_fail, parent=self)

    def _on_change_storage_path(self, *_):
        from PyQt5.QtWidgets import QFileDialog
        from core.paths import logs_dir, migrate_storage, storage_root
        from .api_workers import run_async
        from .confirm_dialog import confirm
        # 忙时在打开文件对话框之前就拦下，避免叠一个新对话框出来
        if self._storage_busy:
            self.storage_message.emit("有存储操作正在进行，请稍候…", 4000)
            return
        new_root = QFileDialog.getExistingDirectory(
            self, "选择新的存储路径", storage_root())
        if not new_root:
            return
        if not confirm(self, self._theme, "更改存储路径",
                       f"将把缓存与日志迁移到：\n{new_root}\n"
                       "迁移期间请勿关闭程序。是否继续？"):
            return
        self._storage_busy = True
        self.storage_message.emit("正在迁移存储数据…", 0)

        def _proc():
            return migrate_storage(new_root)

        def _done(moved):
            self._storage_busy = False
            self._cache_path_label.setText(storage_root())
            self._logs_path_label.setText(logs_dir())
            self.refresh_storage_usage()
            self.storage_message.emit(f"存储路径已更改，已迁移：{moved}", 6000)

        def _fail(err):
            self._storage_busy = False
            self.storage_message.emit(f"迁移失败，存储路径保持不变：{err}", 8000)

        run_async(_proc, on_done=_done, on_fail=_fail, parent=self)

    def _on_clear_cache(self, *_):
        from core.paths import clear_cache
        from .api_workers import run_async
        from .confirm_dialog import confirm
        if self._storage_busy:
            self.storage_message.emit("有存储操作正在进行，请稍候…", 4000)
            return
        if not confirm(self, self._theme, "清空缓存",
                       "将删除全部本地缓存（头像缩略图、聊天图片、文件），"
                       "需要时会自动重新下载。是否继续？", danger=True):
            return
        self._storage_busy = True
        self.storage_message.emit("正在清空缓存…", 0)

        def _proc():
            clear_cache()
            return True

        def _done(_ok):
            self._storage_busy = False
            self.refresh_storage_usage()
            self.storage_message.emit("缓存已清空", 4000)

        def _fail(err):
            self._storage_busy = False
            self.storage_message.emit(f"清空缓存失败：{err}", 6000)

        run_async(_proc, on_done=_done, on_fail=_fail, parent=self)

    # ------------------------------------------------------------------ #
    # AI 页
    # ------------------------------------------------------------------ #
    def _build_ai_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        # 顶边距 36：右上角浮着无边框窗口的三按钮（y 8..38），第一行控件
        # 原 y=41 只距 3px，贴脸（真机反馈）；36 让首行从 y=57 起
        lay.setContentsMargins(20, 36, 20, 20)
        lay.setSpacing(16)

        block = self._block("AI 服务")
        form = QFormLayout(block)
        form.setSpacing(12)

        self.llm_enabled_switch = SwitchButton(self._theme)
        self.llm_enabled_switch.toggled.connect(self._on_llm_switch_toggled)
        self._theme_widgets.append(self.llm_enabled_switch)
        switch_row = QHBoxLayout()
        switch_desc = WxLabel("启用 AI 功能", self._theme)
        self._theme_widgets.append(switch_desc)
        switch_row.addWidget(switch_desc)
        switch_row.addStretch()
        switch_row.addWidget(self.llm_enabled_switch)
        form.addRow("", switch_row)

        self.llm_url_input = WxLineEdit(self._theme)
        self.llm_url_input.setPlaceholderText("例如 https://api.example.com/v1")
        self._theme_widgets.append(self.llm_url_input)
        form.addRow("服务地址：", self.llm_url_input)

        self.llm_model_input = WxLineEdit(self._theme)
        self.llm_model_input.setPlaceholderText("例如 qwen3.6")
        self._theme_widgets.append(self.llm_model_input)
        form.addRow("模型名称：", self.llm_model_input)

        self.llm_token_input = WxLineEdit(self._theme)
        self.llm_token_input.setEchoMode(WxLineEdit.Password)
        self.llm_token_input.setPlaceholderText("API Token（可留空）")
        self._theme_widgets.append(self.llm_token_input)
        form.addRow("访问 Token：", self.llm_token_input)

        privacy = WxLabel(
            "AI 功能默认关闭。使用翻译、划重点、写作助手等功能时，会把所选"
            "聊天文字或输入框内容发送到上述服务用于生成结果；不会上传图片、"
            "文件或附件。请在发送前自行确认内容不含敏感信息。",
            self._theme)
        privacy.set_variant("hint")
        privacy.setWordWrap(True)
        self._theme_widgets.append(privacy)
        form.addRow("", privacy)

        btn_row = QHBoxLayout()
        save_btn = WxPushButton("保存配置", self._theme)
        save_btn.clicked.connect(self._on_llm_save)
        self._theme_widgets.append(save_btn)
        btn_row.addWidget(save_btn)
        self.llm_test_btn = WxPushButton("测试连接", self._theme)
        self.llm_test_btn.clicked.connect(self._on_llm_test)
        self._theme_widgets.append(self.llm_test_btn)
        btn_row.addWidget(self.llm_test_btn)
        btn_row.addStretch()
        form.addRow("", btn_row)

        self.llm_hint = WxLabel("", self._theme)
        self.llm_hint.set_variant("hint")
        self.llm_hint.setWordWrap(True)
        self._theme_widgets.append(self.llm_hint)
        form.addRow("", self.llm_hint)

        lay.addWidget(block)
        lay.addStretch()
        self._add_page(page)

    def _on_llm_switch_toggled(self, checked: bool):
        self._apply_llm_enabled_state(checked)
        self.llm_enabled_toggled.emit(checked)

    def _apply_llm_enabled_state(self, enabled: bool):
        """关闭时地址/模型/Token 置灰但保留内容，重新打开无需重填。"""
        self.llm_url_input.setEnabled(enabled)
        self.llm_model_input.setEnabled(enabled)
        self.llm_token_input.setEnabled(enabled)

    def _on_llm_save(self, *_):
        url = self.llm_url_input.text().strip()
        model = self.llm_model_input.text().strip()
        token = self.llm_token_input.text().strip()
        if not url or not model:
            self.set_llm_hint("请先填写服务地址和模型名称", error=True)
            return
        self.llm_config_changed.emit(url, model, token)

    def _on_llm_test(self, *_):
        url = self.llm_url_input.text().strip()
        model = self.llm_model_input.text().strip()
        token = self.llm_token_input.text().strip()
        if not url or not model:
            self.set_llm_hint("请先填写服务地址和模型名称", error=True)
            return
        self.llm_test_requested.emit(url, model, token)

    def set_llm_config(self, url: str, model: str, token: str,
                       enabled: bool = False):
        """回写已保存的配置；不触发开关/保存信号。"""
        self.llm_enabled_switch.blockSignals(True)
        self.llm_enabled_switch.setChecked(bool(enabled))
        self.llm_enabled_switch.blockSignals(False)
        self.llm_url_input.setText(url or "")
        self.llm_model_input.setText(model or "")
        self.llm_token_input.setText(token or "")
        self._apply_llm_enabled_state(bool(enabled))

    def set_llm_hint(self, text: str, error: bool = False):
        self.llm_hint.setText(text or "")
        self.llm_hint.set_variant("danger" if error else "hint")

    def set_llm_test_busy(self, busy: bool):
        self.llm_test_btn.setEnabled(not busy)
        if busy:
            self.set_llm_hint("正在测试连接…")

    def show_ai_page(self):
        """供主窗口在「未启用/未配置」提示后直接跳到 AI 页。"""
        for i, btn in enumerate(self._nav_buttons):
            if btn._icon_key == "ai":
                btn.setChecked(True)
                self._stack.setCurrentIndex(i)
                self.llm_refresh_requested.emit()
                return

    # ------------------------------------------------------------------ #
    # 关于页
    # ------------------------------------------------------------------ #
    def _build_about_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        # 顶边距 36：右上角浮着无边框窗口的三按钮（y 8..38），第一行控件
        # 原 y=41 只距 3px，贴脸（真机反馈）；36 让首行从 y=57 起
        lay.setContentsMargins(20, 36, 20, 20)
        lay.setSpacing(12)

        title = WxLabel("Fchat", self._theme)
        title.set_variant("bold")
        title.set_size("24px")
        lay.addWidget(title)

        version = WxLabel(f"版本 {APP_VERSION}", self._theme)
        version.set_variant("hint")
        version.set_size("14px")
        lay.addWidget(version)

        check_btn = WxPushButton("检查更新", self._theme)
        check_btn.setCursor(Qt.PointingHandCursor)
        check_btn.clicked.connect(self.check_update_requested.emit)
        self._theme_widgets.append(check_btn)
        lay.addWidget(check_btn)

        lay.addSpacing(12)

        desc = WxLabel(
            "Fchat 是一款对标微信的 Rocket.Chat 桌面客户端。\n\n"
            "功能特性：\n"
            "• 微信风格三栏界面\n"
            "• 支持端到端加密（E2E）消息\n"
            "• 消息复制、撤回、引用回复\n"
            "• 收藏房间、删除/退出\n"
            "• 亮色/暗色主题切换\n"
            "• 自定义字体\n"
            "• 实时消息推送（DDP/WebSocket）\n", self._theme)
        desc.setWordWrap(True)
        desc.set_size("13px")
        for w in (title, version, desc):
            self._theme_widgets.append(w)
        lay.addWidget(desc)

        lay.addStretch()
        self._add_page(page)

    # ------------------------------------------------------------------ #
    # 外部设置当前值（从配置恢复）
    # ------------------------------------------------------------------ #
    def set_accent(self, color: str):
        # 主色调已在 UI 层移除，保留空实现以兼容主窗口旧调用
        pass

    def set_font_family(self, family: str):
        idx = self.font_combo.findText(family)
        if idx >= 0:
            self.font_combo.blockSignals(True)
            self.font_combo.setCurrentIndex(idx)
            self.font_combo.blockSignals(False)
        self._update_preview()

    def set_font_size(self, size: int):
        idx = self.font_size_combo.findData(size)
        if idx >= 0:
            self.font_size_combo.blockSignals(True)
            self.font_size_combo.setCurrentIndex(idx)
            self.font_size_combo.blockSignals(False)
        self._update_preview()
