"""主题管理器：亮色/暗色主题 + 自定义主色调 + 字体设置。

ThemeManager 负责根据当前设置动态生成 QSS 并应用到 QApplication。
"""
from __future__ import annotations

import os
from typing import Dict


# 预设主题色
ACCENT_COLORS: Dict[str, str] = {
    "微信绿": "#07C160",
    "QQ蓝": "#1296db",
    "微信蓝": "#576B95",
    "樱花粉": "#FF6B81",
    "琥珀橙": "#FA9D3B",
}

# 亮色主题色板
_LIGHT = {
    "bg": "#f7f7f7",
    "sidebar_bg": "#2e2e2e",
    "sidebar_btn_hover": "#3a3a3a",
    "conv_bg": "#ffffff",
    "conv_hover": "#eef3f4",
    "chat_bg": "#f5f5f5",
    "bubble_mine": "#ffffff",
    "bubble_other": "#ffffff",
    "input_bg": "#ffffff",
    "input_border": "#e0e0e0",
    "text_primary": "#191919",
    "text_secondary": "#999999",
    "text_link": "#576b95",
    "border": "#e7e7e7",
    "scrollbar": "#cccccc",
    "scrollbar_hover": "#aaaaaa",
    "scrollbar_idle": "#e2e2e2",
    "system_bubble_bg": "#f5f5f5",
    "quote_bg": "#f0f0f0",
    "quote_border": "#c0c0c0",
    # 消息委托（MessageDelegate.paint）直接在画布上取色，不走 QSS：
    "system_bubble_fill": "#e8e8e8",
    "system_bubble_text": "#999999",
    "sender_name": "#888888",
    "bubble_other_border": "#e0e0e0",
    "time_label": "#b0b0b0",
    "text_tertiary": "#666666",
    "status_sent": "#b8b8b8",
    "status_read": "#07C160",
    "status_failed": "#fa5151",
    # 用户在线状态点（AvatarWidget / 私聊标题栏）
    "status_online": "#07C160",
    "status_away": "#faad14",
    "status_busy": "#fa5151",
    "status_dnd": "#fa5151",
    "status_offline": "#b8b8b8",
    "status_departed": "#8a8a8a",
    "link_strong": "#1256b8",
    "selection_bg": "#3399ff",
    "selection_text": "#ffffff",
    "image_placeholder": "#d8d8d8",
    "flash_highlight": "#ffe58f",
    # 表情回应 chip 与 @我 行高亮（MessageDelegate 直接绘制）
    "reaction_bg": "#f0f2f5",
    "reaction_border": "#dfe3e8",
    "reaction_mine_bg": "#e3f2fd",
    "reaction_mine_text": "#1256b8",
    "mention_highlight": "#ff9f2e",
    # 危险操作（WxPushButton danger 变体 / 确认弹窗红色确认键）
    "danger_bg": "#fdeaea",
    "danger_fg": "#e64340",
    "danger_hover": "#f8d3d3",
}

# 暗色主题色板
_DARK = {
    "bg": "#1e1e1e",
    "sidebar_bg": "#171717",
    "sidebar_btn_hover": "#2a2a2a",
    "conv_bg": "#252525",
    "conv_hover": "#333333",
    "chat_bg": "#1e1e1e",
    "bubble_mine": "#2d4a2d",
    "bubble_other": "#2d2d2d",
    "input_bg": "#2d2d2d",
    "input_border": "#3a3a3a",
    "text_primary": "#e0e0e0",
    "text_secondary": "#888888",
    "text_link": "#7a9bc0",
    "border": "#333333",
    "scrollbar": "#555555",
    "scrollbar_hover": "#777777",
    "scrollbar_idle": "#3d3d3d",
    "system_bubble_bg": "#2a2a2a",
    "quote_bg": "#333333",
    "quote_border": "#555555",
    # 消息委托（MessageDelegate.paint）直接在画布上取色，不走 QSS：
    "system_bubble_fill": "#2f2f2f",
    "system_bubble_text": "#9a9a9a",
    "sender_name": "#9a9a9a",
    "bubble_other_border": "#3d3d3d",
    "time_label": "#8a8a8a",
    "text_tertiary": "#aaaaaa",
    "status_sent": "#7a7a7a",
    "status_read": "#5fd068",
    "status_failed": "#ff7b7b",
    # 用户在线状态点（AvatarWidget / 私聊标题栏）
    "status_online": "#5fd068",
    "status_away": "#e0a52e",
    "status_busy": "#ff7b7b",
    "status_dnd": "#ff7b7b",
    "status_offline": "#6b6b6b",
    "status_departed": "#555555",
    "link_strong": "#5b9bd5",
    "selection_bg": "#3f7fd6",
    "selection_text": "#ffffff",
    "image_placeholder": "#3f3f3f",
    "flash_highlight": "#6b5d2e",
    # 表情回应 chip 与 @我 行高亮（MessageDelegate 直接绘制）
    "reaction_bg": "#333333",
    "reaction_border": "#454545",
    "reaction_mine_bg": "#2f4a66",
    "reaction_mine_text": "#9ccaff",
    "mention_highlight": "#ffb454",
    # 危险操作（WxPushButton danger 变体 / 确认弹窗红色确认键）
    "danger_bg": "#4a2626",
    "danger_fg": "#ff7875",
    "danger_hover": "#5c2e2e",
}


# QSS 模板（用 {key} 占位）
_QSS_TEMPLATE = """\
* {{
    font-family: "{font_family}", "Segoe UI", sans-serif;
    font-size: {font_size}px;
    outline: none;
}}

/* 无边框主窗口背景由 paintEvent 自绘圆角（WA_TranslucentBackground），
   这里不能再给 #MainWindow 上 QSS 背景：QSS 画的是直角矩形，会把透明
   圆角盖成直角。注意 QSS 只认斜星注释，行首 # 会被当成 ID 选择器、
   吞掉后面的规则（曾把 #Sidebar 深色背景整条吃掉）。 */

QMainWindow::separator {{
    background: {border};
    width: 1px;
    height: 1px;
}}

/* 会话列表与内容区之间只留一条细分割线（默认手柄透出窗口底色，
   视觉上像一条空白缝） */
QSplitter::handle {{
    background: {border};
}}

#Sidebar {{
    background: {sidebar_bg};
}}

/* 状态栏只在有消息时短暂出现（main_window 里 messageChanged 控制显隐），
   底色跟聊天区走、不露默认白底，否则亮色下会在底部割出一条白带 */
QStatusBar {{
    background: {chat_bg};
    color: {text_secondary};
    border-top: 1px solid {border};
}}

#SidebarButton {{
    background: transparent;
    border: none;
    border-radius: 6px;
    color: #ffffff;
    font-size: 12px;
}}

#SidebarButton:hover {{
    background: {sidebar_btn_hover};
}}

#SidebarButton:checked {{
    background: {accent};
    color: #ffffff;
    border-left: 3px solid {accent};
}}

#ConversationList {{
    background: {conv_bg};
    border: none;
}}

#ConversationList::item {{
    border: none;
}}

#ConversationList::item:selected,
#ConversationList::item:hover {{
    background: {conv_hover};
}}

#ConversationTitle {{
    background: {border};
    color: {text_primary};
    font-size: 16px;
    font-weight: bold;
    border-bottom: 1px solid {border};
    padding-left: 12px;
}}

#ChatArea {{
    background: {chat_bg};
}}

#ChatTitle {{
    background: {conv_bg};
    color: {text_primary};
    font-size: 15px;
    font-weight: bold;
    border-bottom: 1px solid {border};
    padding: 0 16px;
}}

#ChatHeaderBtn {{
    background: transparent;
    color: {text_secondary};
    border: none;
    border-radius: 6px;
    padding: 0;
}}
#ChatHeaderBtn:hover {{
    background: {conv_hover};
}}
#ChatHeaderBtn:pressed, #ChatHeaderBtn:checked {{
    background: {border};
}}

#ChatScrollArea {{
    background: {chat_bg};
    border: none;
}}

#ChatInputWidget {{
    background: {chat_bg};
    border-top: 1px solid {border};
}}

#MessageInput {{
    background: {input_bg};
    border: 1px solid {input_border};
    border-radius: 4px;
    padding: 8px 10px;
    color: {text_primary};
    selection-background-color: {accent};
    selection-color: #ffffff;
}}

#MessageInput:focus {{
    border-color: {accent};
}}

#SendButton {{
    background: {accent};
    color: #ffffff;
    border: none;
    border-radius: 4px;
    padding: 6px 20px;
    font-size: 13px;
}}

#SendButton:hover {{
    background: {accent_hover};
}}

#SendButton:pressed {{
    background: {accent_press};
}}

#SendButton:disabled {{
    background: {accent_disabled};
    color: #eafff3;
}}

#ToolButton {{
    background: transparent;
    border: none;
    color: {text_link};
    font-size: 13px;
}}

#ToolButton:hover {{
    color: {accent};
}}

#ToolIconButton {{
    background: transparent;
    border: none;
    border-radius: 6px;
}}

#ToolIconButton:hover {{
    background: {conv_hover};
}}

#ToolIconButton:pressed {{
    background: {border};
}}

#ToolIconButton:checked {{
    background: {conv_hover};
}}

#ConversationHeader {{
    background: {conv_bg};
    border-bottom: 1px solid {border};
}}

#HeaderSearch {{
    background: {conv_hover};
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 6px 8px;
    color: {text_primary};
}}

#HeaderSearch:focus {{
    border-color: {accent};
    background: {input_bg};
}}

#NewRoomButton {{
    background: transparent;
    border: none;
    border-radius: 6px;
    color: {text_secondary};
    font-size: 20px;
    font-weight: bold;
}}

#NewRoomButton:hover {{
    background: {conv_hover};
    color: {text_primary};
}}

#DirectoryButton {{
    background: transparent;
    border: none;
    border-radius: 6px;
}}

#DirectoryButton:hover {{
    background: {conv_hover};
}}

#DirectoryButton:checked {{
    background: {conv_hover};
}}

#BubbleSystem {{
    background: {system_bubble_bg};
    color: {text_secondary};
    font-size: 12px;
    border: none;
    padding: 2px 8px;
    border-radius: 4px;
}}

#MsgSenderName {{
    color: {text_link};
    font-size: 12px;
}}

#TimeLabel {{
    color: {text_secondary};
    font-size: 11px;
}}

#AvatarLabel {{
    background: #ddd;
}}

#LoginDialog {{
    background: {input_bg};
}}

#LoginTitle {{
    font-size: 24px;
    font-weight: bold;
    color: {text_primary};
}}

#LoginSubtitle {{
    color: {text_secondary};
    font-size: 13px;
}}

#LoginInput {{
    background: {chat_bg};
    border: 1px solid {input_border};
    border-radius: 4px;
    padding: 10px 12px;
    font-size: 14px;
    color: {text_primary};
}}

#LoginInput:focus {{
    border-color: {accent};
    background: {input_bg};
}}

#LoginButton {{
    background: {accent};
    color: #ffffff;
    border: none;
    border-radius: 4px;
    font-size: 15px;
    font-weight: bold;
    padding: 11px;
}}

#LoginButton:hover {{
    background: {accent_hover};
}}

#LoginButton:disabled {{
    background: {accent_disabled};
}}

#LoginError {{
    color: #fa5151;
    font-size: 12px;
}}

QCheckBox {{
    spacing: 5px;
    color: {text_secondary};
    font-size: 12px;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
}}

/* 裸 QLabel（QFormLayout 行标签等）默认黑字，暗色下看不见；有自己
   setStyleSheet / objectName 规则的标签会覆盖这条，不受影响 */
QLabel {{
    color: {text_primary};
    background: transparent;
}}

QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
    border: none;
}}
QScrollBar::handle:vertical {{
    background: {scrollbar_idle};
    /* 透明描边把滑块收成静置 4px 的细条，悬停时放宽到 6px */
    border: 3px solid transparent;
    border-radius: 2px;
    background-clip: content-box;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{
    background: {scrollbar};
    border: 2px solid transparent;
    border-radius: 3px;
}}
QScrollBar::handle:vertical:pressed {{
    background: {scrollbar_hover};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
    border: none;
}}
QScrollBar::sub-page:vertical, QScrollBar::add-page:vertical {{
    background: transparent;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 0;
    border: none;
}}
QScrollBar::handle:horizontal {{
    background: {scrollbar_idle};
    border: 3px solid transparent;
    border-radius: 2px;
    background-clip: content-box;
    min-width: 28px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {scrollbar};
    border: 2px solid transparent;
    border-radius: 3px;
}}
QScrollBar::handle:horizontal:pressed {{
    background: {scrollbar_hover};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
    border: none;
}}
QScrollBar::sub-page:horizontal, QScrollBar::add-page:horizontal {{
    background: transparent;
}}

#QuoteBar {{
    background: {quote_bg};
    border-left: 3px solid {accent};
}}

QMenu {{
    background: {input_bg};
    border: 1px solid {border};
    border-radius: 6px;
    padding: 4px;
}}
QMenu::item {{
    background: transparent;
    color: {text_primary};
    padding: 6px 22px 6px 10px;
    border-radius: 4px;
    margin: 1px 31px;
}}
QMenu::item:selected {{
    background: {conv_hover};
}}
QMenu::item:disabled {{
    color: {text_secondary};
    background: transparent;
}}
QMenu::separator {{
    height: 1px;
    background: {border};
    margin: 4px 10px;
}}
"""


def _darken(hex_color: str, factor: float = 0.15) -> str:
    """将颜色变暗（用于 hover/press 效果）。"""
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[:2], 16), int(hex_color[2:4], 16), int(hex_color[4:], 16)
    r = max(0, int(r * (1 - factor)))
    g = max(0, int(g * (1 - factor)))
    b = max(0, int(b * (1 - factor)))
    return f"#{r:02x}{g:02x}{b:02x}"


def _lighten(hex_color: str, factor: float = 0.3) -> str:
    """将颜色变亮（用于 disabled 效果）。"""
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[:2], 16), int(hex_color[2:4], 16), int(hex_color[4:], 16)
    r = min(255, int(r + (255 - r) * factor))
    g = min(255, int(g + (255 - g) * factor))
    b = min(255, int(b + (255 - b) * factor))
    return f"#{r:02x}{g:02x}{b:02x}"


class ThemeManager:
    """管理应用主题：亮色/暗色 + 自定义主色调 + 字体。"""

    LIGHT = "light"
    DARK = "dark"

    def __init__(self):
        self._theme = self.LIGHT
        self._accent = "#07C160"
        self._font_family = "Microsoft YaHei"
        self._font_size = 13

    # ------------------------------------------------------------------ #
    # 属性
    # ------------------------------------------------------------------ #
    @property
    def theme(self) -> str:
        return self._theme

    @theme.setter
    def theme(self, value: str):
        self._theme = value if value in (self.LIGHT, self.DARK) else self.LIGHT

    @property
    def accent(self) -> str:
        return self._accent

    @accent.setter
    def accent(self, hex_color: str):
        self._accent = hex_color

    @property
    def font_family(self) -> str:
        return self._font_family

    @font_family.setter
    def font_family(self, name: str):
        self._font_family = name

    @property
    def font_size(self) -> int:
        return self._font_size

    @font_size.setter
    def font_size(self, size: int):
        self._font_size = max(10, min(24, size))

    @property
    def palette(self) -> Dict[str, str]:
        """当前主题色板，供不走 QSS 的绘制路径（如气泡 delegate）取色。"""
        return _DARK if self._theme == self.DARK else _LIGHT

    # ------------------------------------------------------------------ #
    # QSS 生成
    # ------------------------------------------------------------------ #
    def generate_qss(self) -> str:
        """根据当前主题设置生成完整 QSS。"""
        palette = _DARK if self._theme == self.DARK else _LIGHT
        fmt = dict(palette)
        fmt["accent"] = self._accent
        fmt["accent_hover"] = _darken(self._accent)
        fmt["accent_press"] = _darken(self._accent, 0.25)
        fmt["accent_disabled"] = _lighten(self._accent)
        fmt["font_family"] = self._font_family
        fmt["font_size"] = self._font_size
        return _QSS_TEMPLATE.format(**fmt)

    def apply(self, app):
        """应用主题到 QApplication。"""
        from . import icons
        qss = self.generate_qss()
        app.setStyleSheet(qss)
        # 矢量图标的描边色跟随主题文字色（图标缓存按颜色分键，会自动失效）
        palette = _DARK if self._theme == self.DARK else _LIGHT
        icons.set_theme_colors(palette["text_primary"])
        icons.set_status_colors({
            k[7:]: v for k, v in palette.items() if k.startswith("status_")
        })

    def color(self, key: str, default: str = "") -> str:
        """当前主题色板中某语义键的颜色（供委托等非 QSS 绘制取色）。

        key 不在色板里时返回 default；default 为空则回退到亮色板里该键的值，
        保证取色在色板调整/缺键时不崩。
        """
        palette = _DARK if self._theme == self.DARK else _LIGHT
        if key in palette:
            return palette[key]
        if default:
            return default
        return _LIGHT.get(key, "#000000")

    # ------------------------------------------------------------------ #
    # 序列化
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        return {
            "theme": self._theme,
            "accent_color": self._accent,
            "font_family": self._font_family,
            "font_size": self._font_size,
        }

    def from_dict(self, d: dict):
        self._theme = d.get("theme", self.LIGHT)
        self._accent = d.get("accent_color", "#07C160")
        self._font_family = d.get("font_family", "Microsoft YaHei")
        self._font_size = int(d.get("font_size", 13))
