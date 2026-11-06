"""Emoji 选择器面板：网格浏览 + 中英文搜索，点击插入/回传 emoji。

含「表情包」页签：列出本地收藏的自定义表情（storage_root/stickers/），
点击经 sticker_selected 信号按文件发送；页内「+」可从本地文件导入。

搜索走 emoji 库的全量名字表（英文）加本文件的精选中文词表：emoji 库
只提供 CLDR 英文名，中文只能自己维护——只给精选表情配中文同义词，
全量表按英文名兜底，避免为几千个字符硬写翻译。
"""
from __future__ import annotations

import os
import time

import emoji

from PyQt5.QtCore import QEvent, QPoint, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QIcon, QMovie, QPixmap
from PyQt5.QtWidgets import (
    QApplication, QDesktopWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QPushButton, QLineEdit, QLabel, QMenu, QScrollArea,
    QToolButton, QFileDialog,
)

# 常用 emoji 按类别分组
EMOJI_CATEGORIES = {
    "😀 笑脸": [
        "😀", "😃", "😄", "😁", "😆", "😅", "🤣", "😂",
        "🙂", "🙃", "😉", "😊", "😇", "🥰", "😍", "😘",
        "😋", "😛", "😜", "🤪", "😝", "🤑", "🤗", "🤭",
        "😐", "😑", "😶", "😏", "😒", "🙄", "😬", "😮",
    ],
    "👋 手势": [
        "👋", "🤚", "🖐", "✋", "🖖", "👌", "🤏", "✌",
        "🤞", "🤟", "🤘", "🤙", "👈", "👉", "👆", "👇",
        "☝", "👍", "👎", "✊", "👊", "🤛", "🤜", "👏",
        "🙌", "👐", "🤲", "🤝", "🙏", "💪",
    ],
    "❤ 心形": [
        "❤", "🧡", "💛", "💚", "💙", "💜", "🖤", "🤍",
        "💔", "❣", "💕", "💞", "💓", "💗", "💖", "💘",
        "💝", "💟",
    ],
    "🎉 物品": [
        "🎉", "🎊", "🎈", "🎁", "🎀", "🏆", "🏅", "🥇",
        "⭐", "🌟", "✨", "💫", "🔥", "💯", "✅", "❌",
        "⚡", "💡", "📌", "📎", "🔗", "💻", "📱", "📷",
    ],
    "🐱 动物": [
        "🐱", "🐶", "🐭", "🐹", "🐰", "🦊", "🐻", "🐼",
        "🐨", "🐯", "🦁", "🐮", "🐷", "🐸", "🐵", "🐔",
        "🐧", "🐦", "🦄", "🐝", "🦋", "🐢", "🐍", "🐙",
    ],
    "🍎 食物": [
        "🍎", "🍐", "🍊", "🍋", "🍌", "🍉", "🍇", "🍓",
        "🫐", "🍈", "🍒", "🍑", "🥭", "🍔", "🍕", "🍟",
        "🌭", "🍿", "🧂", "🥚", "🍞", "🥐", "☕", "🍵",
    ],
}

# 精选表情的中文同义词（空格分隔，搜索按子串命中）。
# 只覆盖 EMOJI_CATEGORIES 里的字符；类别词表给整类兜底，比如搜「动物」。
_ZH_NAMES = {
    # 笑脸
    "😀": "咧嘴笑 开心 笑 高兴 哈哈",
    "😃": "大笑 开心 笑",
    "😄": "开心 笑 高兴",
    "😁": "露齿笑 开心",
    "😆": "眯眼笑 哈哈",
    "😅": "苦笑 出汗 尴尬",
    "🤣": "笑翻 笑哭 哈哈",
    "😂": "笑哭 眼泪 哈哈",
    "🙂": "微笑 呵呵",
    "🙃": "倒脸 无奈",
    "😉": "眨眼 调皮",
    "😊": "害羞 微笑 开心",
    "😇": "天使 无辜",
    "🥰": "爱心 喜欢 幸福",
    "😍": "花痴 喜欢 爱",
    "😘": "飞吻 亲亲",
    "😋": "好吃 馋",
    "😛": "吐舌 调皮",
    "😜": "吐舌 捣蛋 调皮",
    "🤪": "疯狂 搞怪",
    "😝": "吐舌 眯眼",
    "🤑": "钱 发财 贪财",
    "🤗": "拥抱 抱抱",
    "🤭": "偷笑 捂嘴",
    "😐": "无语 面无表情",
    "😑": "面无表情 无语",
    "😶": "沉默 无声",
    "😏": "得意 坏笑",
    "😒": "不满 鄙视",
    "🙄": "翻白眼 无语",
    "😬": "尴尬 龇牙",
    "😮": "惊讶 吃惊",
    # 手势
    "👋": "挥手 再见 你好",
    "🤚": "举手 手背",
    "🖐": "手掌 五 停止",
    "✋": "举手 停止 掌",
    "🖖": "瓦肯 星际",
    "👌": "好的 没问题 ok 可以",
    "🤏": "一点点 捏",
    "✌": "胜利 耶 剪刀手",
    "🤞": "好运 祈祷 交叉",
    "🤟": "爱你 摇滚 手势",
    "🤘": "摇滚 金属",
    "🤙": "电话 联系 六",
    "👈": "左 指",
    "👉": "右 指",
    "👆": "上 指",
    "👇": "下 指",
    "☝": "一 指 上",
    "👍": "赞 点赞 好 棒 同意 顶",
    "👎": "踩 差评 不好 反对",
    "✊": "拳头 加油 力量",
    "👊": "碰拳 拳头 出击",
    "🤛": "左拳 碰拳",
    "🤜": "右拳 碰拳",
    "👏": "鼓掌 拍手 掌声",
    "🙌": "欢呼 举手 庆祝",
    "👐": "摊手 张开",
    "🤲": "双手 捧 祈祷",
    "🤝": "握手 合作 同意",
    "🙏": "感谢 谢谢 拜托 祈祷",
    "💪": "加油 肌肉 强壮 努力",
    # 心形
    "❤": "爱心 爱 喜欢 红心",
    "🧡": "橙心 爱",
    "💛": "黄心 爱",
    "💚": "绿心 爱",
    "💙": "蓝心 爱",
    "💜": "紫心 爱",
    "🖤": "黑心 爱",
    "🤍": "白心 爱",
    "💔": "心碎 失恋 难过",
    "❣": "心 感叹 爱",
    "💕": "两心 爱 喜欢",
    "💞": "旋转心 爱",
    "💓": "心跳 爱",
    "💗": "爱心 喜欢",
    "💖": "闪亮心 爱",
    "💘": "一箭穿心 爱",
    "💝": "礼物心 爱",
    "💟": "心 装饰 爱",
    # 物品
    "🎉": "庆祝 派对 彩带 恭喜",
    "🎊": "彩带 庆祝 派对",
    "🎈": "气球 庆祝 派对",
    "🎁": "礼物 礼品 惊喜",
    "🎀": "蝴蝶结 礼物",
    "🏆": "奖杯 冠军 胜利",
    "🏅": "奖牌 荣誉",
    "🥇": "金牌 第一 冠军",
    "⭐": "星 星星 收藏",
    "🌟": "星星 闪亮 发光",
    "✨": "闪光 魔法 闪亮",
    "💫": "眩晕 星星",
    "🔥": "火 热门 燃烧 厉害",
    "💯": "满分 一百 厉害",
    "✅": "对 正确 完成 勾 通过",
    "❌": "错 错误 叉 取消 不行",
    "⚡": "闪电 电 快 能量",
    "💡": "灯泡 想法 主意 灵感",
    "📌": "图钉 固定 重要",
    "📎": "回形针 附件",
    "🔗": "链接 连接",
    "💻": "电脑 笔记本 工作",
    "📱": "手机 电话 移动",
    "📷": "相机 拍照 照片",
    # 动物
    "🐱": "猫 猫咪",
    "🐶": "狗 小狗 狗狗",
    "🐭": "老鼠 鼠",
    "🐹": "仓鼠 鼠",
    "🐰": "兔子 兔",
    "🦊": "狐狸",
    "🐻": "熊",
    "🐼": "熊猫",
    "🐨": "考拉 树袋熊",
    "🐯": "老虎 虎",
    "🦁": "狮子",
    "🐮": "牛 奶牛",
    "🐷": "猪",
    "🐸": "青蛙 蛙",
    "🐵": "猴子 猴",
    "🐔": "鸡 公鸡",
    "🐧": "企鹅",
    "🐦": "鸟",
    "🦄": "独角兽 奇幻",
    "🐝": "蜜蜂 蜂",
    "🦋": "蝴蝶",
    "🐢": "乌龟 龟",
    "🐍": "蛇",
    "🐙": "章鱼",
    # 食物
    "🍎": "苹果 水果",
    "🍐": "梨 水果",
    "🍊": "橘子 橙子 水果",
    "🍋": "柠檬 水果",
    "🍌": "香蕉 水果",
    "🍉": "西瓜 水果",
    "🍇": "葡萄 水果",
    "🍓": "草莓 水果",
    "🫐": "蓝莓 水果",
    "🍈": "甜瓜 水果",
    "🍒": "樱桃 水果",
    "🍑": "桃子 水果",
    "🥭": "芒果 水果",
    "🍔": "汉堡 快餐 吃",
    "🍕": "披萨 快餐 吃",
    "🍟": "薯条 快餐 吃",
    "🌭": "热狗 快餐",
    "🍿": "爆米花 电影",
    "🧂": "盐 调味",
    "🥚": "蛋 鸡蛋 食物",
    "🍞": "面包 食物",
    "🥐": "牛角包 面包",
    "☕": "咖啡 喝 提神",
    "🍵": "茶 喝茶",
}

# 类别级关键词：搜「动物」「手势」整类都命中
_CATEGORY_KEYWORDS = {
    "😀 笑脸": "笑脸 表情 情绪 脸 smile face",
    "👋 手势": "手势 手 动作 hand gesture",
    "❤ 心形": "心形 心 爱 喜欢 heart love",
    "🎉 物品": "物品 东西 物件 object item",
    "🐱 动物": "动物 animal",
    "🍎 食物": "食物 吃的 美食 food",
}

_CELL = 36
_COLS = 8
_CELL_GAP = 4
_SEARCH_LIMIT = 120
# 「点按钮关闭」窗口：Tool 窗失活自关后按钮 click 才回放，这次点击已是
# 关闭动作，不能再弹回来（与 room_panels 的 _TOGGLE_GRACE 同一语义）
_TOGGLE_GRACE = 0.3
_HINT_DEFAULT = "悬停查看名称，点击插入"
_HINT_STICKER = "点击发送表情包"


class StickerPreview(QWidget):
    """表情包悬停预览浮窗：gif 用 QMovie 保持动画，随悬停显隐。

    Tool 窗挂在 picker 下（盖在主窗口上），但**不激活自己**——激活会
    令面板失活自关；显隐完全由 picker 在单元格 Enter/Leave 时控制。
    """

    def __init__(self, theme, parent=None):
        super().__init__(
            parent,
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        panel = theme_color(theme, "conv_bg", "#ffffff")
        border = theme_color(theme, "border", "#e0e0e0")
        sub = theme_color(theme, "text_secondary", "#999999")
        self.setStyleSheet(
            f"QWidget{{background:{panel};border:1px solid {border};"
            f"border-radius:8px;}}"
            f"QLabel{{border:none;background:transparent;}}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 8)
        lay.setSpacing(6)
        self._img = QLabel()
        self._img.setAlignment(Qt.AlignCenter)
        lay.addWidget(self._img)
        self._tip = QLabel()
        self._tip.setAlignment(Qt.AlignCenter)
        self._tip.setStyleSheet(f"color:{sub};font-size:12px;")
        lay.addWidget(self._tip)
        self._movie = None

    def set_sticker(self, path: str, usage_text: str) -> bool:
        """换成要预览的表情；图片读不出返回 False（悬停时静默不弹）。"""
        if self._movie is not None:
            self._movie.stop()
            self._movie = None
        self._img.setMovie(None)
        if path.lower().endswith(".gif"):
            movie = QMovie(path)
            movie.jumpToFrame(0)
            movie.setScaledSize(movie.currentPixmap().size().scaled(
                QSize(320, 320), Qt.KeepAspectRatio))
            self._img.setMovie(movie)
            movie.start()
            self._movie = movie
        else:
            pm = QPixmap(path)
            if pm.isNull():
                return False
            self._img.setPixmap(pm.scaled(
                320, 320, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self._tip.setText(usage_text)
        self.adjustSize()
        return True


def theme_color(theme, key: str, default: str) -> str:
    """从 ThemeManager 取语义色，取不到按默认兜底。"""
    if theme is not None:
        try:
            return theme.color(key, default) or default
        except Exception:  # noqa: BLE001 —— 取色失败降级默认
            pass
    return default

# 精选字符顺序 + 每个字符所属类别的关键词，模块加载时算一次
_CURATED_CHARS = []
_CATEGORY_KEYWORDS_BY_CHAR = {}
for _cat_name, _cat_chars in EMOJI_CATEGORIES.items():
    for _char in _cat_chars:
        if _char not in _CATEGORY_KEYWORDS_BY_CHAR:
            _CURATED_CHARS.append(_char)
            _CATEGORY_KEYWORDS_BY_CHAR[_char] = _CATEGORY_KEYWORDS.get(
                _cat_name, "")

# emoji 库全量英文名索引（char -> 可搜索文本），首次搜索时构建
_EN_INDEX = None


def _english_index() -> dict:
    """char -> 小写英文名/别名（下划线同时展开成空格）。

    肤色变体不单列：搜 thumbs 只出基础款，免得同款五个色号刷屏。
    """
    global _EN_INDEX
    if _EN_INDEX is not None:
        return _EN_INDEX
    index = {}
    for char, data in emoji.EMOJI_DATA.items():
        if not isinstance(data, dict):
            continue
        en = str(data.get("en") or "")
        if "skin_tone" in en:
            continue
        names = [en]
        names.extend(data.get("alias") or [])
        parts = []
        for name in names:
            name = str(name or "").strip(":").lower()
            if not name:
                continue
            parts.append(name)
            parts.append(name.replace("_", " ").replace("-", " "))
        if parts:
            index[char] = " ".join(parts)
    _EN_INDEX = index
    return index


def emoji_tooltip(char: str) -> str:
    """悬停提示：中文名在前、英文名兜底。

    原生 QToolTip 是系统深色浮窗，短码打头又小又难认；这里只留可读的
    名称文本，显示位置统一由面板底部的提示行负责。
    """
    data = emoji.EMOJI_DATA.get(char) or {}
    name = str(data.get("en") or "").strip(":").replace("_", " ")
    zh = _ZH_NAMES.get(char, "").split(" ")[0]
    parts = [p for p in (zh, name) if p]
    return " · ".join(parts) or char


def search_emojis(query: str) -> list:
    """按中文/英文/短码搜索 emoji；精选在前，最多 _SEARCH_LIMIT 个。"""
    query = str(query or "").strip().strip(":").lower()
    terms = [t for t in query.split() if t]
    if not terms:
        return []
    en_index = _english_index()
    results = []
    seen = set()

    def _hit(char: str) -> bool:
        names = en_index.get(char, "")
        zh = " ".join((_ZH_NAMES.get(char, ""),
                       _CATEGORY_KEYWORDS_BY_CHAR.get(char, "")))
        for term in terms:
            if term == char:
                continue
            if term not in names and term not in zh:
                return False
        return True

    for char in _CURATED_CHARS:
        if _hit(char):
            results.append(char)
            seen.add(char)
            if len(results) >= _SEARCH_LIMIT:
                return results
    for char in en_index:
        if char in seen or not _hit(char):
            continue
        results.append(char)
        if len(results) >= _SEARCH_LIMIT:
            break
    return results


class EmojiPicker(QWidget):
    """Emoji 弹出选择器（无边框 Tool 窗，避免 Popup 阻断中文 IME）。"""

    emoji_selected = pyqtSignal(str)   # 选中的 emoji 字符
    sticker_selected = pyqtSignal(str)  # 选中的表情包图片路径

    def __init__(self, parent=None, theme=None, close_on_select=False):
        # 不用 Qt.Popup：Windows 上 IME 无法在 Popup 窗口合成输入（搜索框
        # 打不了中文）。改用无边框 Tool 窗，失活时自关（见 changeEvent）
        # 模拟「点外部关闭」；WindowStaysOnTopHint 保证盖在主窗口上。
        super().__init__(
            parent,
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        # 同一面板两用：输入框的 emoji 插入（选后保持打开）与消息回应的
        # 添加（选后立即关闭并回传字符）
        if theme is None:
            # 输入框那条路径创建面板时拿不到 ThemeManager，沿父链取主窗口
            # 的实例，暗色下才不会弹出一块白板
            node = parent
            while node is not None and theme is None:
                theme = getattr(node, "theme_manager", None)
                node = node.parent()
        self._theme = theme
        self._close_on_select = close_on_select
        self._mode = "emoji"
        self._closed_at = 0.0
        self._dialog_open = False
        # 悬停预览浮窗（懒建，单元格 Enter/Leave 控制显隐）
        self._hover_preview = None
        self.setObjectName("EmojiPickerRoot")
        # QWidget 子类默认不画 QSS 背景，必须显式开启样式化背景
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedSize(352, 420)
        self.setStyleSheet(self._build_qss())

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        # 页签：Emoji / 表情包
        mode_lay = QHBoxLayout()
        mode_lay.setSpacing(4)
        self.emoji_tab_btn = QPushButton("Emoji")
        self.sticker_tab_btn = QPushButton("表情包")
        for btn in (self.emoji_tab_btn, self.sticker_tab_btn):
            btn.setObjectName("EmojiTab")
            btn.setCheckable(True)
            btn.setFixedHeight(24)
            btn.setCursor(Qt.PointingHandCursor)
            mode_lay.addWidget(btn)
        mode_lay.addStretch()
        self.import_btn = QPushButton("+ 导入")
        self.import_btn.setObjectName("EmojiImportBtn")
        self.import_btn.setFixedHeight(24)
        self.import_btn.setCursor(Qt.PointingHandCursor)
        self.import_btn.setToolTip("从本地图片导入表情包")
        self.import_btn.clicked.connect(self._on_import_clicked)
        mode_lay.addWidget(self.import_btn)
        lay.addLayout(mode_lay)
        self.emoji_tab_btn.clicked.connect(
            lambda _=False: self.set_mode("emoji"))
        self.sticker_tab_btn.clicked.connect(
            lambda _=False: self.set_mode("sticker"))

        # emoji 专属头部（搜索框 + 分类标签），表情包页隐藏
        self._emoji_header = QWidget()
        eh = QVBoxLayout(self._emoji_header)
        eh.setContentsMargins(0, 0, 0, 0)
        eh.setSpacing(6)
        lay.addWidget(self._emoji_header)

        # 搜索框
        self.search = QLineEdit()
        self.search.setObjectName("EmojiSearch")
        self.search.setPlaceholderText("搜索 emoji（中文 / English）")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._on_search)
        eh.addWidget(self.search)

        # 分类标签
        self._cat_bar = QWidget()
        cat_lay = QHBoxLayout(self._cat_bar)
        cat_lay.setContentsMargins(0, 0, 0, 0)
        cat_lay.setSpacing(6)
        self._cat_buttons = []
        for cat_name in EMOJI_CATEGORIES:
            btn = QPushButton(cat_name.split(" ")[0])  # 取 emoji 作为按钮
            btn.setObjectName("EmojiCatBtn")
            btn.setFixedSize(32, 32)
            btn._cat_name = cat_name
            btn.installEventFilter(self)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda checked, c=cat_name: self._scroll_to_cat(c))
            cat_lay.addWidget(btn)
            self._cat_buttons.append(btn)
        cat_lay.addStretch()
        eh.addWidget(self._cat_bar)

        # emoji 网格（滚动区域）
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("border:none;background:transparent;")

        self._container = None
        self._grid = None
        # 分类页缓存：首次搜索时整块销毁 150+ 个带 emoji 字体的按钮要卡
        # 0.7s 左右，切回分类还要再建一遍；页面建好后留着复用
        self._cat_page = None
        self._cat_grid = None
        lay.addWidget(self._scroll, 1)

        # 悬停提示固定占底部一行：原生 QToolTip 是系统深色浮窗，字小、
        # 贴着鼠标还会盖住附近表情，改为面板内随主题着色的提示条
        self.hint = QLabel(_HINT_DEFAULT)
        self.hint.setObjectName("EmojiHint")
        self.hint.setFixedHeight(20)
        lay.addWidget(self.hint)

        self.set_mode("emoji")

    # ------------------------------------------------------------------ #
    # 页签切换
    # ------------------------------------------------------------------ #
    def set_mode(self, mode: str):
        self._hide_hover_preview()
        self._mode = "sticker" if mode == "sticker" else "emoji"
        self.emoji_tab_btn.setChecked(self._mode == "emoji")
        self.sticker_tab_btn.setChecked(self._mode == "sticker")
        self._emoji_header.setVisible(self._mode == "emoji")
        self.import_btn.setVisible(self._mode == "sticker")
        if self._mode == "emoji":
            self._populate_grid(self.search.text())
        else:
            self._populate_stickers()
        self._set_hint(self._default_hint())

    def _reset_grid(self):
        """换一个全新的容器 + 布局再重新填充。

        QGridLayout 在 takeAt 清空后行/列数仍保留旧值（实测停在 31×8），
        新加的控件会被摆到旧行列对应的位置，搜索结果的几个表情就散落在
        面板中间而不是从左上角开始。行列数没有重置接口，只能整棵换掉。
        分类页例外：只摘下来留着复用，不能销毁（见 __init__ 的缓存说明）。
        """
        cur = self._scroll.widget()
        if cur is not None:
            self._scroll.takeWidget()
            if cur is not self._cat_page:
                cur.deleteLater()
        self._container = QWidget()
        self._container.setObjectName("EmojiGrid")
        self._grid = QGridLayout(self._container)
        self._grid.setSpacing(_CELL_GAP)
        self._grid.setContentsMargins(4, 4, 4, 4)
        self._scroll.setWidget(self._container)
        self._scroll.verticalScrollBar().setValue(0)

    def _show_page(self, page, grid):
        if self._scroll.widget() is page:
            self._grid = grid
        else:
            cur = self._scroll.widget()
            if cur is not None:
                self._scroll.takeWidget()
                if cur is not self._cat_page:
                    cur.deleteLater()
            self._container = page
            self._grid = grid
            self._scroll.setWidget(page)
        self._scroll.verticalScrollBar().setValue(0)

    def _set_hint(self, text: str):
        self.hint.setText(text)

    def _default_hint(self) -> str:
        return _HINT_STICKER if self._mode == "sticker" else _HINT_DEFAULT

    def _stretch_grid(self, cols: int, row: int):
        """把多余宽高都挤到末尾的空列/空行。

        QGridLayout 会把容器多余宽度平均分给各列，固定尺寸的格子因此被
        推开（2 个搜索结果也会分居左右半区）；末尾留一列弹性空间才贴左。
        """
        self._grid.setColumnStretch(cols, 1)
        self._grid.setRowStretch(row, 1)

    def _add_hint(self, text: str):
        tip = QLabel(text)
        tip.setObjectName("EmojiCatTitle")
        tip.setAlignment(Qt.AlignCenter)
        self._grid.addWidget(tip, 0, 0, 1, _COLS)
        self._stretch_grid(_COLS, 1)

    def _populate_stickers(self):
        """表情包网格：图片按钮 + 空态提示。懒加载 QPixmap（44px 缩放）。"""
        self._hide_hover_preview()
        self._reset_grid()
        self._set_hint(_HINT_STICKER)
        from core.stickers import list_stickers
        paths = list_stickers()
        if not paths:
            self._add_hint("还没有收藏表情\n右键聊天里的图片消息可收藏")
            return
        row = 0
        col = 0
        cols = 6
        for path in paths:
            btn = QToolButton()
            btn.setObjectName("EmojiCell")
            btn.setFixedSize(44, 44)
            btn._sticker_name = os.path.basename(path)
            btn._sticker_path = path
            btn.installEventFilter(self)
            pm = QPixmap(path)
            if pm.isNull():
                continue
            btn.setIcon(QIcon(pm))
            btn.setIconSize(btn.size())
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(
                lambda _=False, p=path: self._on_sticker_clicked(p))
            # 右键预览/删除；CustomContextMenu 才能拿到格内坐标换全局
            btn.setContextMenuPolicy(Qt.CustomContextMenu)
            btn.customContextMenuRequested.connect(
                lambda pos, b=btn, p=path:
                    self._show_sticker_menu(p, b.mapToGlobal(pos)))
            self._grid.addWidget(btn, row, col)
            col += 1
            if col >= cols:
                col = 0
                row += 1
        self._stretch_grid(cols, row + 1)

    def _on_sticker_clicked(self, path: str):
        # 先计数再发送：下次打开面板按使用频率重排（本次打开内不重排，
        # 连续点选时网格在脚下乱跳没法用）
        from core.stickers import record_sticker_use
        try:
            record_sticker_use(path)
        except OSError:  # noqa: BLE001 —— 计数失败不影响发送
            pass
        self.sticker_selected.emit(path)
        if self._close_on_select:
            self.close()

    def _show_sticker_menu(self, path: str, global_pos: QPoint):
        # 预览改为悬停自动弹出，右键只留删除
        menu = QMenu(self)
        delete_act = menu.addAction("删除")
        chosen = menu.exec_(global_pos)
        if chosen is delete_act:
            self._delete_sticker(path)

    def _show_hover_preview(self, path: str, btn):
        from core.stickers import usage_count
        if self._hover_preview is None:
            self._hover_preview = StickerPreview(self._theme, parent=self)
        if not self._hover_preview.set_sticker(
                path,
                f"{os.path.basename(path)} · 已用 {usage_count(path)} 次"):
            return
        # 贴在单元格右侧，右侧放不下贴左侧；垂直对齐单元格并钳回屏内
        left = btn.mapToGlobal(btn.rect().topLeft())
        right = btn.mapToGlobal(btn.rect().topRight())
        screen = QDesktopWidget().availableGeometry(left)
        x = right.x() + 8
        if x + self._hover_preview.width() > screen.right() + 1:
            x = left.x() - self._hover_preview.width() - 8
        x = min(max(screen.left(), x),
                screen.right() - self._hover_preview.width() + 1)
        y = min(max(screen.top(), left.y()),
                screen.bottom() - self._hover_preview.height() + 1)
        self._hover_preview.move(x, y)
        self._hover_preview.show()
        self._hover_preview.raise_()

    def _hide_hover_preview(self):
        if self._hover_preview is not None:
            self._hover_preview.hide()

    def _delete_sticker(self, path: str):
        from ui.confirm_dialog import confirm
        name = os.path.basename(path)
        # 确认弹窗是另一顶层窗，会触发本面板失活自关，先豁免
        self._dialog_open = True
        try:
            ok = confirm(self, self._theme, "删除表情",
                         f"确定删除「{name}」吗？", danger=True, ok_text="删除")
        finally:
            self._dialog_open = False
        self.raise_()
        self.activateWindow()
        if not ok:
            return
        from core.stickers import delete_sticker
        try:
            delete_sticker(path)
        except OSError as e:
            self._show_error_hint(f"删除失败：{e}")
            return
        self._populate_stickers()

    def _show_error_hint(self, text: str):
        """底部提示行临时显示错误（danger 色），4 秒后恢复。"""
        self.hint.setText(text)
        self.hint.setStyleSheet(
            f"color:{self._c('danger_fg', '#c62828')};"
            "background:transparent;font-size:12px;")
        QTimer.singleShot(4000, self._clear_error_hint)

    def _clear_error_hint(self):
        try:
            self.hint.setStyleSheet("")
            self._set_hint(self._default_hint())
        except RuntimeError:
            # 面板已析构时 4 秒回调才到，不用管
            pass

    _IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
    _IMG_FILTER = "图片 (*.png *.jpg *.jpeg *.gif *.bmp *.webp)"

    def _on_import_clicked(self, *_):
        menu = QMenu(self)
        files_act = menu.addAction("选择图片…（可多选）")
        folder_act = menu.addAction("选择文件夹…")
        chosen = menu.exec_(self.import_btn.mapToGlobal(
            QPoint(0, self.import_btn.height())))
        if chosen is files_act:
            self._import_files_dialog()
        elif chosen is folder_act:
            self._import_folder_dialog()

    def _import_files_dialog(self):
        self._dialog_open = True
        try:
            paths, _ = QFileDialog.getOpenFileNames(
                self, "选择图片（可按住 Ctrl 多选）", "", self._IMG_FILTER)
        finally:
            self._dialog_open = False
        self._reactivate_after_dialog()
        if paths:
            self._import_many(paths)

    def _import_folder_dialog(self):
        self._dialog_open = True
        try:
            d = QFileDialog.getExistingDirectory(self, "选择文件夹")
        finally:
            self._dialog_open = False
        self._reactivate_after_dialog()
        if not d:
            return
        paths = [os.path.join(d, f) for f in sorted(os.listdir(d))
                 if os.path.splitext(f)[1].lower() in self._IMG_EXTS
                 and os.path.isfile(os.path.join(d, f))]
        if not paths:
            self._show_error_hint("该文件夹里没有可导入的图片")
            return
        self._import_many(paths)

    def _reactivate_after_dialog(self):
        # 原生文件对话框会让 Tool 窗失活，回来后要抢回激活态
        self.raise_()
        self.activateWindow()

    def _import_many(self, paths):
        """批量导入：先 Qt 解码验证（core 只按魔数归类，损坏图片会「导入
        成功」却显示为空），再逐张入库；结果汇总到底部提示行。"""
        from core.stickers import save_sticker
        new = 0
        dup = 0
        errors = []
        for p in paths:
            if QPixmap(p).isNull() and not p.lower().endswith(".gif"):
                errors.append(f"{os.path.basename(p)} 不是有效图片")
                continue
            try:
                _, created = save_sticker(p, report_new=True)
            except OSError as e:
                errors.append(f"{os.path.basename(p)}: {e}")
                continue
            # 同内容不同文件名在 core 按内容 md5 去重，只算一张
            if created:
                new += 1
            else:
                dup += 1
        if new:
            self._populate_stickers()
        if errors:
            head = f"已导入 {new} 张，失败 {len(errors)} 张" if new \
                else "导入失败"
            if dup:
                head += f"，跳过重复 {dup} 张"
            tail = f"：{errors[0]}" + (" 等" if len(errors) > 1 else "")
            self._show_error_hint(head + tail)
        elif new:
            self._set_hint(f"已导入 {new} 张表情"
                           + (f"，{dup} 张重复已跳过" if dup else ""))
        elif dup:
            self._set_hint(f"{dup} 张均与已有表情重复，未新增")

    def _c(self, key: str, default: str) -> str:
        return theme_color(self._theme, key, default)

    def _build_qss(self) -> str:
        """面板与子控件样式统一从主题语义色生成（暗色下不能用白底）。

        子控件用 objectName 选择器，重建的网格/分类按钮自动继承；
        emoji 字体族必须写在每条显示 emoji 的规则里，否则部分控件会退化成
        黑色单色字形。
        """
        emoji_font = ('"Segoe UI Emoji","Apple Color Emoji",'
                      '"Noto Color Emoji"')
        c = {
            "panel": self._c("conv_bg", "#ffffff"),
            "border": self._c("border", "#e0e0e0"),
            "text": self._c("text_primary", "#191919"),
            "sub": self._c("text_secondary", "#999999"),
            "hover": self._c("conv_hover", "#eef3f4"),
            "input_bg": self._c("input_bg", "#ffffff"),
            "input_border": self._c("input_border", "#e0e0e0"),
            "focus": self._c("link_strong", "#1256b8"),
            "scroll": self._c("scrollbar", "#c9c9c9"),
        }
        return (
            "QWidget#EmojiPickerRoot{background:%(panel)s;"
            "border:1px solid %(border)s;border-radius:8px;}"
            "QPushButton#EmojiTab{background:transparent;color:%(sub)s;"
            "border:none;border-radius:6px;padding:2px 14px;"
            "font-size:13px;}"
            "QPushButton#EmojiTab:hover{background:%(hover)s;}"
            "QPushButton#EmojiTab:checked{background:%(hover)s;"
            "color:%(text)s;}"
            "QPushButton#EmojiImportBtn{background:%(input_bg)s;"
            "color:%(text)s;border:1px solid %(input_border)s;"
            "border-radius:6px;padding:2px 10px;font-size:12px;}"
            "QPushButton#EmojiImportBtn:hover{background:%(hover)s;}"
            "QLineEdit#EmojiSearch{background:%(input_bg)s;color:%(text)s;"
            "border:1px solid %(input_border)s;border-radius:6px;"
            "padding:6px 8px;font-size:13px;}"
            "QLineEdit#EmojiSearch:focus{border:1px solid %(focus)s;}"
            "QLabel#EmojiCatTitle{color:%(sub)s;font-size:12px;"
            "background:transparent;padding:6px 2px 2px 2px;}"
            "QPushButton#EmojiCatBtn{background:transparent;border:none;"
            "font-family:%(emoji_font)s;font-size:18px;"
            "border-radius:7px;}"
            "QPushButton#EmojiCatBtn:hover{background:%(hover)s;}"
            "QPushButton#EmojiCell{background:transparent;border:none;"
            "font-family:%(emoji_font)s;font-size:24px;"
            "border-radius:8px;}"
            "QPushButton#EmojiCell:hover{background:%(hover)s;}"
            "QPushButton#EmojiCell:pressed{background:%(border)s;}"
            "QToolButton#EmojiCell{background:transparent;border:none;"
            "border-radius:8px;}"
            "QToolButton#EmojiCell:hover{background:%(hover)s;}"
            "QLabel#EmojiHint{color:%(sub)s;font-size:12px;"
            "background:transparent;padding:0px 2px;}"
            "QWidget#EmojiGrid{background:transparent;}"
            "QScrollBar:vertical{background:transparent;width:6px;"
            "border:none;}"
            "QScrollBar::handle:vertical{background:%(scroll)s;"
            "border-radius:3px;min-height:24px;}"
            "QScrollBar::add-line:vertical,"
            "QScrollBar::sub-line:vertical{height:0px;background:none;"
            "border:none;}"
            "QScrollBar::add-page:vertical,"
            "QScrollBar::sub-page:vertical{background:none;}"
        ) % dict(c, emoji_font=emoji_font)

    def _make_emoji_button(self, emoji_char: str) -> QPushButton:
        btn = QPushButton(emoji_char)
        btn.setObjectName("EmojiCell")
        btn.setFixedSize(_CELL, _CELL)
        btn.setCursor(Qt.PointingHandCursor)
        btn._emoji_char = emoji_char
        btn.installEventFilter(self)
        btn.clicked.connect(
            lambda checked, e=emoji_char: self._on_emoji_clicked(e))
        return btn

    def _fill_emoji_row(self, chars, row: int = 0, col: int = 0):
        """把字符铺进网格；返回下一段可用的 (row, col)。"""
        for emoji_char in chars:
            self._grid.addWidget(self._make_emoji_button(emoji_char),
                                 row, col)
            col += 1
            if col >= _COLS:
                col = 0
                row += 1
        if col:
            row += 1
            col = 0
        return row, col

    def _show_category_page(self):
        """分类页只建一次；之后搜索/切页都复用同一份网格。"""
        if self._cat_page is not None:
            self._show_page(self._cat_page, self._cat_grid)
            return
        self._reset_grid()
        row = 0
        for cat_name, emojis in EMOJI_CATEGORIES.items():
            label = QLabel(f"  {cat_name}")
            label.setObjectName("EmojiCatTitle")
            self._grid.addWidget(label, row, 0, 1, _COLS)
            row += 1
            row, _col = self._fill_emoji_row(emojis, row, 0)
            row += 1
        self._stretch_grid(_COLS, row)
        self._cat_page = self._container
        self._cat_grid = self._grid

    def _populate_grid(self, filter_text: str = ""):
        """填充 emoji 网格：无搜索词按分类浏览，有搜索词出扁平结果。"""
        query = (filter_text or "").strip()
        self._set_hint(self._default_hint())
        # 搜索时隐藏分类条，既省空间也避免「分类按钮对结果无效」的错觉
        self._cat_bar.setVisible(not query)
        if not query:
            self._show_category_page()
            return

        self._reset_grid()
        chars = search_emojis(query)
        if not chars:
            self._add_hint("没有找到相关 emoji\n试试「点赞」「开心」「thumbs」")
            return
        row, _col = self._fill_emoji_row(chars)
        self._stretch_grid(_COLS, row)

    def _on_emoji_clicked(self, emoji_char: str):
        self.emoji_selected.emit(emoji_char)
        if self._close_on_select:
            self.close()

    def _on_search(self, text: str):
        self._populate_grid(text)

    # ------------------------------------------------------------------ #
    # 悬停提示 / 打开关闭
    # ------------------------------------------------------------------ #
    def eventFilter(self, obj, e):
        # 悬停提示走面板底部固定行；网格按钮数量多，统一在这里转发。
        # 表情格悬停同时弹大图预览，移出即收。
        if e.type() == QEvent.Enter:
            char = getattr(obj, "_emoji_char", "")
            cat = getattr(obj, "_cat_name", "")
            sticker = getattr(obj, "_sticker_path", "")
            if char:
                self._set_hint(emoji_tooltip(char))
            elif cat:
                self._set_hint(cat)
            elif sticker:
                self._set_hint(os.path.basename(sticker))
                self._show_hover_preview(sticker, obj)
        elif e.type() == QEvent.Leave:
            self._set_hint(self._default_hint())
            self._hide_hover_preview()
        return super().eventFilter(obj, e)

    def open_at(self, pos: QPoint):
        """在全局坐标 pos（面板左上角）打开；贴屏幕边时钳回屏内。"""
        screen = QDesktopWidget().availableGeometry(pos)
        x = min(max(screen.left(), pos.x()),
                screen.right() - self.width() + 1)
        y = min(max(screen.top(), pos.y()),
                screen.bottom() - self.height() + 1)
        self.move(x, y)
        self.show()
        self.raise_()
        # Tool 窗不会像 popup 那样自动抓键盘，必须显式激活才能输入中文
        self.activateWindow()
        if self._mode == "emoji":
            self.search.setFocus()
            self.search.selectAll()

    def was_recently_closed(self) -> bool:
        return (time.monotonic() - self._closed_at) < _TOGGLE_GRACE

    def changeEvent(self, e):
        # 模拟 popup 的「点外部关闭」：面板失活即关。豁免嵌套
        # Qt.Popup（右键菜单等）与原生文件对话框导致的失活。
        super().changeEvent(e)
        if (e.type() == QEvent.ActivationChange
                and not self.isActiveWindow()
                and not self._dialog_open
                and QApplication.activePopupWidget() is None):
            self.close()
            self._hide_hover_preview()

    def hideEvent(self, e):
        self._closed_at = time.monotonic()
        # 面板失活自关/换房关闭时预览是独立 Tool 窗，不跟着隐，需手动收
        self._hide_hover_preview()
        super().hideEvent(e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(e)

    def _scroll_to_cat(self, cat_name: str):
        # 清空搜索回到分类视图（搜索词为空时 clear 不发信号，需手动重建）
        had_query = bool(self.search.text())
        self.search.clear()
        if not had_query:
            self._populate_grid("")
        # 重建网格的布局要到后续事件循环才落位，此刻子控件 y 和滚动条
        # range 都是旧值，直接在同一个调用栈里 setValue 会被钳成 0
        self._scroll_to_cat_when_ready(cat_name, 3)

    def _scroll_to_cat_when_ready(self, cat_name: str, attempts: int):
        bar = self._scroll.verticalScrollBar()
        margin_top = self._grid.contentsMargins().top()
        for i in range(self._grid.count()):
            item = self._grid.itemAt(i)
            w = item.widget()
            if w and isinstance(w, QLabel) and cat_name in w.text():
                target = max(0, w.y() - 4)
                # 重建后未布局时所有子控件都堆在 (0,0)，首行不可能贴到
                # 内容边距之上；y 已更新但 range 仍为 0 也是同一类中间态
                if w.y() < margin_top or (target > 0 and bar.maximum() <= 0):
                    break
                # 按标签实际位置滚动：行高随字号/字体变化，固定步长会偏
                bar.setValue(target)
                return
        if attempts > 0:
            QTimer.singleShot(
                0, lambda: self._scroll_to_cat_when_ready(cat_name,
                                                          attempts - 1))
