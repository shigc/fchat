"""消息列表模型与代理：虚拟化渲染，仅绘制可见区域。

MessageListModel 持有消息数据，支持分批插入以避免大量消息阻塞 UI。
MessageDelegate 负责每行的绘制（头像、气泡、三角、时间标签等）和尺寸计算。
"""
from __future__ import annotations

import logging
import math
import time as _time
from collections import OrderedDict
from typing import List, Optional

from PyQt5.QtCore import (
    Qt, QSize, QRect, QRectF, QPoint, QModelIndex, QTimer, pyqtSignal,
    QAbstractListModel, QSortFilterProxyModel, QUrl, QVariant,
    QLineF, QEvent, QPointF,
)
from PyQt5.QtGui import (
    QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen,
    QBrush, QTextDocument, QTextOption, QTextCursor, QTextCharFormat,
    QAbstractTextDocumentLayout, QPalette, QPixmap, QPolygon,
    QPolygonF, QLinearGradient,
)
from PyQt5.QtWidgets import (
    QStyledItemDelegate, QStyle, QApplication, QToolTip,
)

from core.models import Message, User
from core.messages import reaction_display
from core import markdown
from .widgets import format_chat_time
from .avatar_loader import canon_url
from .icons import icon

log = logging.getLogger(__name__)


def _same_system_event(prev: Optional[Message], cur: Message) -> bool:
    """两条相邻系统消息是否同一次服务端重复落库。"""
    if prev is None or prev.msg_type != "system" or cur.msg_type != "system":
        return False
    if (prev.text or "") != (cur.text or ""):
        return False
    pu = prev.sender.username if prev.sender else ""
    cu = cur.sender.username if cur.sender else ""
    # sender 缺失时宁可多显示，也不能把不同人的动作误合并
    if not pu or pu != cu:
        return False
    return abs(cur.timestamp - prev.timestamp) <= _SYSTEM_DUP_WINDOW


def _reaction_parts(key: str, count: int):
    """chip 文本拆成 (emoji, 人数)：emoji 与人数用不同字号绘制。

    reaction_band 的宽高预算与 _paint_reactions 必须共用这一份拆分，
    否则文字会溢出 chip 或被裁掉。
    """
    return reaction_display(key), (str(count) if count > 1 else "")


def _reaction_is_emoji(text: str) -> bool:
    """短码解析失败时显示的是原始 :name:，要退回正文小字体绘制。"""
    return bool(text) and not text.startswith(":")


# ---------------------------------------------------------------------------
# 自定义角色
# ---------------------------------------------------------------------------
RoleMessage = Qt.UserRole + 1
RoleIsMine = Qt.UserRole + 2
RoleShowTimeLabel = Qt.UserRole + 3
RoleShowAvatar = Qt.UserRole + 4
RoleShowName = Qt.UserRole + 5
RoleIsSystem = Qt.UserRole + 6
RoleRowHeight = Qt.UserRole + 7

# 头像颜色盘
_AVATAR_COLORS = [
    "#07C160", "#10AEFF", "#FA9D3B", "#FA5151", "#986EE5",
    "#00A86B", "#E64340", "#5B8FF9", "#F0A73A", "#576B95",
]

# ---------------------------------------------------------------------------
# 布局常量
# ---------------------------------------------------------------------------
_MARGIN_L = 10
_MARGIN_R = 10
_MARGIN_V = 4
_AVATAR_SIZE = 40
_AVATAR_GAP = 8
_BUBBLE_PAD = 8
_BUBBLE_MAX_W = 560
_BUBBLE_RADIUS = 10
_TRI_W = 6
_TRI_H = 14
_NAME_H = 28
_TIME_GAP = 300
# 表情回应 chip：单行布局，单 chip 超高与行高公式的唯一来源（reaction_band）
_REACTION_CHIP_GAP = 4
_REACTION_CHIP_PAD = 7
_REACTION_BAND_TOP = 4
_REACTION_COUNT_GAP = 3
# emoji 用独立字号/字体族：跟随正文字号会小到看不清，Windows 上非 emoji
# 字体族的回退渲染也容易丢色
_REACTION_FONT = 14
_EMOJI_FONT_FAMILY = "Segoe UI Emoji"
# 服务端重复落库的系统消息折叠窗口：一次动作（真机见「离开频道」）会被
# 服务端写成多条独立消息（id 连续、ts 相差数毫秒），推送层的
# (id, _updatedAt) 去重拦不住；显示层按相邻同人同文本 + 时间差折叠。
_SYSTEM_DUP_WINDOW = 2.0
_FONT_SIZE = 14
_SMALL_FONT = 12
_TINY_FONT = 11
_IMG_W, _IMG_H = 240, 180
# 图片显示尺寸约束：保持比例，限制在该范围内
_IMG_MIN_W, _IMG_MIN_H = 80, 60
_IMG_MAX_W, _IMG_MAX_H = 320, 400
# 逐条转发消息内容上方「转发的消息」标记行高度（高度计算与绘制共用）
_FORWARD_TAG_H = 18
# 引用块文件行图标边长（高度计算与绘制共用）
_QFILE_ICON = 14
# 气泡下方状态图标（时钟/单勾/双勾/红叉）与时间文本的间距（绘制与命中同源）
_STATUS_ICON = 12
_STATUS_GAP = 6
# 时间行内的小标记（已编辑/置顶/星标）：不占行高，与时间文本同一基线，
# 只在 _paint_message_row 里画，不参与 layout_rects
_TIME_MARK_ICON = 12
_TIME_MARK_GAP = 3
# 超过该成员数的群（全公司广播频道）不显示「N人未读」：数字无参考价值，
# 名单浮窗也渲染不动
UNREAD_COUNT_LIMIT = 500
# 未读分隔线占用的行首高度：行高、绘制与点击命中都必须走这一个常量
_UNREAD_DIVIDER_H = 28


def _calc_image_display_size(orig_w: int, orig_h: int) -> tuple:
    """根据原始尺寸计算显示尺寸（保持比例，约束在范围内）。"""
    if orig_w <= 0 or orig_h <= 0:
        return _IMG_W, _IMG_H
    w, h = orig_w, orig_h
    # 先缩到最大范围
    if w > _IMG_MAX_W or h > _IMG_MAX_H:
        k = min(_IMG_MAX_W / w, _IMG_MAX_H / h)
        w, h = int(w * k), int(h * k)
    # 再放到最小范围（小图放大）
    if w < _IMG_MIN_W and h < _IMG_MIN_H:
        k = max(_IMG_MIN_W / w, _IMG_MIN_H / h)
        w, h = int(w * k), int(h * k)
    return max(_IMG_MIN_W, w), max(_IMG_MIN_H, h)


def _inline_display_size(orig_w: int, orig_h: int) -> tuple:
    """内联 markdown 图显示尺寸：宽度不超过内联占位宽 240 并保持比例。"""
    from core.markdown import _INLINE_IMG_W
    if orig_w <= 0 or orig_h <= 0:
        return _INLINE_IMG_W, 180
    k = _INLINE_IMG_W / orig_w
    return _INLINE_IMG_W, max(40, int(round(orig_h * k)))


def _is_image_like(msg: Message) -> bool:
    """图片消息或带封面的视频消息：无气泡边框，图片矩形就是气泡矩形。"""
    return msg.msg_type == "image" or (msg.msg_type == "video" and msg.cover_url)


def _paint_dpr(painter: QPainter) -> float:
    """取绘制设备逻辑→物理缩放系数（HiDPI）。绘制设备里可能不是 widget。"""
    dev = painter.device()
    if dev is not None and hasattr(dev, "devicePixelRatioF"):
        dpr = dev.devicePixelRatioF()
        if dpr and dpr > 0:
            return float(dpr)
    return 1.0


def _system_bubble_metrics(text: str, row_width: int, font: QFont):
    """系统消息气泡的 (宽, 高)。行高与绘制必须同源，否则气泡底部被裁。"""
    fm = QFontMetrics(font)
    max_w = max(60, row_width - _MARGIN_V * 2)
    return min(fm.horizontalAdvance(text) + 24, max_w), fm.height() + 4


def _format_file_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}TB"


def _full_time_text(ts: float) -> str:
    """气泡悬停 tooltip 的完整日期时间（列表里只显时分，日期靠悬停）。"""
    return _time.strftime("%Y-%m-%d %H:%M", _time.localtime(ts))


_FILE_DEFAULT_NAME = {"file": "文件", "audio": "语音消息", "video": "视频消息"}

_CAPTION_GAP = 4  # 图片与其下方说明文字的间距

_FILE_CARD_W = 260      # 卡片内容区基础宽（外层气泡再加 _BUBBLE_PAD）
_FILE_CARD_MAX_W = 340  # 长文件名的宽度上限，超限由绘制端省略 + tooltip
_FILE_BADGE = 36        # 徽标边长
_FILE_BADGE_RADIUS = 6
_FILE_NAME_GAP = 4      # 文件名行与大小行的间距
_FILE_TEXT_GAP = 10     # 徽标与文本列的间距
_BADGE_FG = "#ffffff"

# 扩展名 → 徽标底色。文件类型语义色（Office 同款）跨主题稳定，徽标自带
# 底色在暗色气泡上同样成立，故不挂 ThemeManager。
_FILE_BADGE_COLORS = {
    "doc": "#4a7dbf", "docx": "#4a7dbf", "txt": "#4a7dbf",
    "md": "#4a7dbf", "rtf": "#4a7dbf",
    "xls": "#3f9e6b", "xlsx": "#3f9e6b", "csv": "#3f9e6b",
    "ppt": "#d9822b", "pptx": "#d9822b",
    "pdf": "#d9534f",
    "zip": "#bf8f30", "rar": "#bf8f30", "7z": "#bf8f30",
    "tar": "#bf8f30", "gz": "#bf8f30",
    "png": "#3aa3a3", "jpg": "#3aa3a3", "jpeg": "#3aa3a3",
    "gif": "#3aa3a3", "bmp": "#3aa3a3", "webp": "#3aa3a3",
    "mp3": "#8e6bbf", "wav": "#8e6bbf", "flac": "#8e6bbf", "m4a": "#8e6bbf",
    "mp4": "#5a67c7", "avi": "#5a67c7", "mkv": "#5a67c7", "mov": "#5a67c7",
    "exe": "#6b7f99", "dll": "#6b7f99", "msi": "#6b7f99",
    "py": "#6b7f99", "c": "#6b7f99", "cpp": "#6b7f99",
    "h": "#6b7f99", "js": "#6b7f99",
}
# 无扩展名时的兜底（标签, 底色）
_FILE_BADGE_DEFAULT = {
    "file": ("文件", "#8a9299"),
    "audio": ("音频", "#8e6bbf"),
    "video": ("视频", "#5a67c7"),
}


def _file_badge(msg: Message) -> tuple:
    """文件卡徽标 (标签, 底色)：按扩展名取类别色，无扩展名给类别词。"""
    name = msg.file_name or msg.text or ""
    ext = ""
    if "." in name:
        cand = name.rsplit(".", 1)[1].strip().lower()
        if cand and len(cand) <= 8 and cand.isalnum():
            ext = cand
    if ext:
        return ext[:4].upper(), _FILE_BADGE_COLORS.get(ext, "#8a9299")
    return _FILE_BADGE_DEFAULT.get(msg.msg_type, _FILE_BADGE_DEFAULT["file"])


def _file_display_name(msg: Message) -> str:
    """文件卡第一行的文件名，宽度计算与绘制必须同源。"""
    return msg.file_name or msg.text or _FILE_DEFAULT_NAME.get(
        msg.msg_type, "文件")


def _file_card_size_text(msg: Message) -> str:
    """文件卡第二行文本：有大小显大小，没有则显类别词。"""
    return (_format_file_size(msg.file_size) if msg.file_size
            else _FILE_DEFAULT_NAME.get(msg.msg_type, "文件"))


def _fmt_mmss(ms: int) -> str:
    s = max(0, int(ms)) // 1000
    return f"{s // 60}:{s % 60:02d}"


def _badge_fit_font(base: QFont, label: str, badge_w: int) -> QFont:
    """徽标标签字体：放不下时按宽度比例直接算目标像素字号。

    扩展名最长 4 字符（DOCX/XLSX），small 字号下必然超出 36px 方块。
    像素缩放近似线性，按比例换算一步到位，再用循环收敛兜住度量非线性
    （下限 7px：再小可读性崩坏，宁可溢出也不无限缩）。
    """
    font = QFont(base)
    base_px = font.pixelSize()
    if base_px <= 0:
        base_px = QFontMetrics(font).height()
    fm = QFontMetrics(font)
    adv = fm.horizontalAdvance(label)
    limit = badge_w - 8
    if adv > limit:
        px = max(7, int(limit * base_px / adv))
        font.setPixelSize(px)
        fm = QFontMetrics(font)
        while fm.horizontalAdvance(label) > badge_w - 4 and px > 7:
            px -= 1
            font.setPixelSize(px)
            fm = QFontMetrics(font)
    return font


def _file_card_h(font: QFont, small_font: QFont) -> int:
    """文件卡内容高：徽标与两行文本取大者，模型行高与委托绘制同源。"""
    fm = QFontMetrics(font)
    fs = QFontMetrics(small_font)
    return max(_FILE_BADGE, fm.height() + _FILE_NAME_GAP + fs.height())


def _elide_one_line(font: QFont, text: str, width: int) -> str:
    fm = QFontMetrics(font)
    if fm.horizontalAdvance(text) > width:
        # 中间省略：文件名场景保住头尾（扩展名在尾部）
        return fm.elidedText(text, Qt.ElideMiddle, width)
    return text


def _avatar_color(name: str) -> QColor:
    return QColor(_AVATAR_COLORS[abs(hash(name or "?")) % len(_AVATAR_COLORS)])


def _merge_image_url(entry: dict) -> str:
    """返回合并转发条目的图片 URL，该条应画图而非显示文本；否则返回空串。

    方案二图片卡的 text 恰好是「右键打开链接查看原图」链接（或为空），此时才
    判定为图片条目、只画图不显示文本；带真实配文的 legacy 条目（att 带
    image_url 且 text 是正文）仍按文本渲染（可选中），避免误伤旧格式。
    """
    att = entry.get("att") or {}
    url = att.get("image_url") or ""
    if not url:
        return ""
    text = (entry.get("text") or "").strip()
    if text and text != f"[右键打开链接查看原图]({url})":
        return ""
    return url


# ============================================================================
# MessageListModel
# ============================================================================
class MessageListModel(QAbstractListModel):
    """消息数据模型。支持分批延迟插入以保持 UI 响应。"""

    batch_finished = pyqtSignal()

    TIME_GAP = _TIME_GAP
    BATCH_SIZE = 50
    BATCH_INTERVAL_MS = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        self._messages: List[Message] = []
        self._last_ts = 0.0
        self._show_name = False
        self._view_width = 0
        self._height_cache: dict = {}
        # 消息 id -> 行号 索引：消除 find_message_row/update_message/
        # remove_message 的 O(n) 线性扫描（消息密集房间实时推送热点）。
        # 与原 find_message_row 语义一致：重复 id 保留第一个（setdefault）。
        self._row_by_id: dict = {}
        # QTextDocument 缓存（LRU）：消除滚动时对 markdown 消息的重复
        # setHtml 解析。键含内容+宽度，内容不可变的纯函数 doc 无需显式
        # 失效；build_text_doc 依赖可变 msg + 内联图尺寸，在编辑/内联图
        # 就绪时失效。上限防内存泄漏。
        self._doc_cache = OrderedDict()
        self._DOC_CACHE_LIMIT = 200
        self._font = QFont()
        self._font.setPointSize(_FONT_SIZE)
        self._small_font = QFont()
        self._small_font.setPointSize(_SMALL_FONT)
        self._tiny_font = QFont()
        self._tiny_font.setPointSize(_TINY_FONT)
        self._reaction_font = QFont()
        self._reaction_font.setFamily(_EMOJI_FONT_FAMILY)
        self._reaction_font.setPointSize(_REACTION_FONT)
        self._image_loader = None
        # 当前登录用户名：标记「我点过的」回应 chip（只影响配色，不影响几何）
        self._my_username = ""

        # 已读回执的渲染状态。权威数据在 main_window._receipts——消息对象
        # 会被 append_message 整体替换、历史缓存另持引用，回执不能挂 Message 上。
        self._room_type = "d"          # "d" 私聊 / "c"/"p" 群聊
        self._member_count = 0         # 群成员数（不含自己，未知为 0）
        self._receipts_supported = True  # 服务端明确拒绝后置 False（降级）
        self._receipts_by_msg: dict = {}   # msg_id -> [receipt dict]

        # 定时提醒标记：msg_id -> 到期 epoch。提醒是本地数据（config 落盘），
        # 权威在 main_window——消息对象会被 append/rebuild 整体替换，
        # 不能挂 Message 上（同 _receipts_by_msg 的道理）
        self._remind_map: dict = {}

        # markdown 内联图：msg_id -> [inline dict]；resolved url -> 引用该图的消息 id 集合
        # （供 image_ready 到货后失效这些行的高度缓存并重绘）
        self._inline_map: dict = {}
        self._inline_url_msgs: dict = {}
        # resolved url -> (显示宽, 显示高)：真实尺寸加载后用于重写 <img> 占位
        self._inline_sizes: dict = {}

        # 文本选区（doc 内字符位置）：sel_start == sel_end 表示无选区。
        # 放在模型上，委托绘制选区高亮、ChatView 管理命中与复制。
        # 合并卡片每条内容各是一个 doc，sel_merge_idx 指明选区属于哪一条。
        self.sel_msg_id: Optional[str] = None
        self.sel_start = 0
        self.sel_end = 0
        self.sel_merge_idx: int = -1

        # 点击引用块跳转后的原消息行高亮（flash_row 设置，QTimer 清除）
        self.flash_msg_id: Optional[str] = None

        # 未读分隔线所在消息 id：这一行顶部多出 _UNREAD_DIVIDER_H，
        # 委托绘制线与文字，不插入合成行（模型索引/回执外推都不受影响）
        self._unread_divider_id = ""

        # 分批加载
        self._pending: List[Message] = []
        self._batch_active = False
        self._batch_timer = QTimer()
        self._batch_timer.setInterval(self.BATCH_INTERVAL_MS)
        self._batch_timer.setSingleShot(True)
        self._batch_timer.timeout.connect(self._flush_batch)

    # ---- QAbstractListModel 接口 ---- #
    def rowCount(self, parent=QModelIndex()):
        return len(self._messages)

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self._messages):
            return None
        msg = self._messages[index.row()]
        if role in (Qt.DisplayRole, Qt.EditRole, RoleMessage):
            return msg
        if role == RoleIsMine:
            return msg.is_mine
        if role == RoleIsSystem:
            return msg.msg_type == "system"
        if role == RoleShowTimeLabel:
            return self._show_time_label(index.row())
        if role == RoleShowAvatar:
            return self._show_avatar(index.row())
        if role == RoleShowName:
            return self._show_name_row(index.row())
        if role == RoleRowHeight:
            return self._get_height(index.row())
        return None

    # ---- 公共方法 ---- #
    def set_show_name(self, show: bool):
        self._show_name = show
        self._height_cache.clear()

    def set_my_username(self, username: str):
        """登录用户变化：回应 chip 的「我点过」配色要重画（几何不变）。"""
        username = username or ""
        if username != self._my_username:
            self._my_username = username
            self._refresh_all_rows()

    def set_unread_divider(self, msg_id: str):
        """未读分隔线放在哪条消息行首；空串表示不显示。

        行高变化必须清高度缓存并 layoutChanged：dataChanged 不足以让
        QListView 重新询问 sizeHint，下一行会被压在旧高度上。
        """
        msg_id = msg_id or ""
        if msg_id == self._unread_divider_id:
            return
        self._unread_divider_id = msg_id
        self._height_cache.clear()
        self.layoutChanged.emit()

    # ---- 表情回应（几何唯一入口） ---- #
    def reaction_band(self, msg: Message, max_width: int):
        """单行回应 chip 布局：(chips, band_height)。

        chip 元素为 (key, count, mine, 相对 band 左上角的矩形)；溢出时最后
        一个 chip 是 ("", 隐藏条数, False, rect)，由绘制侧渲染成 +N。
        行高、绘制与点击命中共用本方法，任何一处单独改都会错位。
        """
        reactions = msg.reactions or {}
        if not reactions or max_width <= 0:
            return [], 0
        emoji_fm = QFontMetrics(self._reaction_font)
        small_fm = QFontMetrics(self._small_font)
        chip_h = max(emoji_fm.height(), small_fm.height()) + 4
        entries = []
        for key, info in reactions.items():
            usernames = (info or {}).get("usernames") or []
            if not usernames:
                continue
            entries.append((str(key), len(usernames),
                            bool(self._my_username)
                            and self._my_username in usernames))
        if not entries:
            return [], 0

        def _chip_w(key: str, count: int) -> int:
            emo, cnt = _reaction_parts(key, count)
            font_metrics = emoji_fm if _reaction_is_emoji(emo) else small_fm
            w = font_metrics.horizontalAdvance(emo) + _REACTION_CHIP_PAD * 2
            if cnt:
                w += _REACTION_COUNT_GAP + small_fm.horizontalAdvance(cnt)
            return w

        def _overflow_w(hidden: int) -> int:
            return (small_fm.horizontalAdvance(f"+{hidden}")
                    + _REACTION_CHIP_PAD * 2)

        widths = [_chip_w(k, n) for k, n, _m in entries]

        def _total(n: int, overflow: int) -> int:
            w = sum(widths[:n]) + _REACTION_CHIP_GAP * max(0, n - 1)
            if overflow:
                w += _REACTION_CHIP_GAP + _overflow_w(overflow)
            return w

        shown = len(entries)
        overflow = 0
        while shown > 0 and _total(shown, overflow) > max_width:
            shown -= 1
            overflow = len(entries) - shown

        chips = []
        x = 0
        for i in range(shown):
            key, count, mine = entries[i]
            w = widths[i]
            chips.append((key, count, mine,
                          QRect(x, _REACTION_BAND_TOP, w, chip_h)))
            x += w + _REACTION_CHIP_GAP
        if overflow:
            w = _overflow_w(overflow)
            chips.append(("", overflow, False,
                          QRect(x, _REACTION_BAND_TOP, w, chip_h)))
        return chips, chip_h + _REACTION_BAND_TOP

    def reaction_band_width(self, msg: Message, max_width: int) -> int:
        chips, _h = self.reaction_band(msg, max_width)
        return chips[-1][3].right() + 1 if chips else 0

    # ---- 已读回执渲染状态 ---- #
    def set_room_context(self, room_type: str, member_count: int):
        self._room_type = room_type
        self._member_count = member_count
        self._refresh_all_rows()

    def set_receipts_supported(self, supported: bool):
        if self._receipts_supported != supported:
            self._receipts_supported = supported
            self._refresh_all_rows()

    def set_read_receipts(self, msg_id: str, receipts: list):
        self._receipts_by_msg[msg_id] = list(receipts or [])
        # 单调外推：本行及更早的自己消息状态都可能跟着变；行高不变，
        # 不碰 _height_cache，只触发重绘
        row = self.find_message_row(msg_id)
        if row >= 0:
            self.dataChanged.emit(self.index(0), self.index(row))

    def _refresh_all_rows(self):
        if self._messages:
            self.dataChanged.emit(self.index(0),
                                  self.index(len(self._messages) - 1))

    def status_spec(self, msg: Message, row: int):
        """状态判定唯一入口（委托绘制与点击命中都走它）。

        返回 (kind, unread)：kind ∈ clock / sent / read / failed /
        unread_count / none。"""
        if msg.msg_type == "system":
            return "none", 0
        # 多人和某人的私聊 type 仍是 "d" 但成员多于两人，按群聊样式显示
        # 未读数；成员数未知（0，列表还没拉到）时先按 1 对 1 画图标
        group_like = self._room_type != "d" or self._member_count > 1
        if not group_like:
            if not msg.is_mine:
                return "none", 0
            if msg.status == "sending":
                return "clock", 0
            if msg.status == "failed":
                return "failed", 0
            if not self._receipts_supported:
                return "sent", 0
            rs = self._effective_receipts(row)
            if rs is None:
                return "sent", 0
            return ("read", 0) if rs else ("sent", 0)
        # 群聊/多人私聊：自己消息的瞬态（发送中/失败）优先于未读数；
        # 其余所有消息（含别人发的）显示未读人数——成员数与回执都齐了
        # 才有数字。超大群（如全公司广播频道）未读数无意义、名单也
        # 渲染不动，超过阈值不显示
        if msg.is_mine and msg.status == "sending":
            return "clock", 0
        if msg.is_mine and msg.status == "failed":
            return "failed", 0
        if self._member_count > UNREAD_COUNT_LIMIT:
            return "none", 0
        if not self._receipts_supported or self._member_count <= 0:
            return "none", 0
        rs = self._effective_receipts(row)
        if rs is None:
            return "none", 0
        read_ids = {r.get("userId") for r in rs if isinstance(r, dict)}
        n = self._member_count - len(read_ids)
        if n <= 0:
            # 全员已读：自己消息画已读双勾（对齐私聊语义）；别人消息
            # 的已读状态不属于自己，不显示
            return ("read", 0) if msg.is_mine else ("none", 0)
        return "unread_count", n

    def _effective_receipts(self, row: int):
        """本行有已拉取的回执数据时直接用——空名单对本行是准确结论
        （确实还没人读过本条）。否则单调外推：向行尾（时间更新方向）
        找第一条**非空**名单的非系统消息，其回执作用于本行——读过新的
        必然读过旧的（服务端已读是房间级单调状态，与发送者无关，别人
        发的消息同样外推）。
        外推时空名单必须跳过：最新消息还没人读（剔除自己后剩 []）时，
        空名单对更早的消息不含任何单调信息（没读过最新的不代表没读过
        旧的），拿它当结论会把明明已读的旧消息全部误判成未读。
        找不到返回 None。"""
        m = self._messages[row]
        if m.msg_type != "system":
            rs = self._receipts_by_msg.get(m.id)
            if rs is not None:
                return rs
        for i in range(row + 1, len(self._messages)):
            m = self._messages[i]
            if m.msg_type != "system":
                rs = self._receipts_by_msg.get(m.id)
                if rs:
                    return rs
        return None

    def get_message(self, row: int) -> Optional[Message]:
        if 0 <= row < len(self._messages):
            return self._messages[row]
        return None

    def find_message_row(self, message_id: str) -> int:
        # O(1) 索引查找；回退线性扫描兜底（索引与 _messages 理论上严格同步，
        # 兜底仅防御性，正常不触发）
        row = self._row_by_id.get(message_id, -1)
        if row >= 0 and row < len(self._messages) \
                and self._messages[row].id == message_id:
            return row
        for i, m in enumerate(self._messages):
            if m.id == message_id:
                return i
        return -1

    def _rebuild_row_index(self):
        """整体重建 id->行号 索引。顶部插入/删除使行号整体偏移时调用。
        重复 id 保留第一个（setdefault），与原 find_message_row 语义一致。"""
        self._row_by_id = {}
        for i, m in enumerate(self._messages):
            self._row_by_id.setdefault(m.id, i)

    def set_remind_map(self, mapping: dict):
        """整体替换提醒标记并重绘。只影响时间行标记（不进行高计算）。"""
        self._remind_map = dict(mapping or {})
        if self._messages:
            self.dataChanged.emit(self.index(0),
                                  self.index(len(self._messages) - 1))

    def remind_for(self, message_id: str) -> float:
        return float(self._remind_map.get(message_id) or 0.0)

    def clear(self):
        self.beginResetModel()
        self._messages.clear()
        self._last_ts = 0.0
        self._height_cache.clear()
        self._row_by_id.clear()
        self._doc_cache.clear()
        self._pending.clear()
        self._batch_timer.stop()
        self._batch_active = False
        self._inline_map.clear()
        self._inline_url_msgs.clear()
        self._receipts_by_msg.clear()
        self._member_count = 0
        self._room_type = "d"
        self._unread_divider_id = ""
        self.endResetModel()

    def append_message(self, message: Message):
        # 同一 id 已存在则视为同一条（乐观回显后收到服务端回推、或编辑后
        # 重推），原位更新而不是重复追加——否则会出现"发一条显示两条"。
        row = self.find_message_row(message.id)
        if row >= 0:
            self._messages[row] = message
            self._drop_inline(message.id)
            self._height_cache.pop(message.id, None)
            idx = self.index(row)
            self.dataChanged.emit(idx, idx)
            return
        if message.room_id and self._messages and self._messages[-1].room_id != message.room_id:
            return
        if self._messages and _same_system_event(self._messages[-1], message):
            return
        row = len(self._messages)
        prev_id = self._messages[-1].id if self._messages else None
        self.beginInsertRows(QModelIndex(), row, row)
        self._messages.append(message)
        self._last_ts = message.timestamp
        # 仅失效新增行和可能受影响的上一行
        self._height_cache.pop(message.id, None)
        if prev_id:
            self._height_cache.pop(prev_id, None)
        # 登记索引（append 是尾部追加，row 即尾部；重复 id 保留第一个）
        self._row_by_id.setdefault(message.id, row)
        self.endInsertRows()

    def prepend_messages_batched(self, messages: List[Message]):
        """分批延迟插入：将消息放入队列，由定时器逐批添加到模型。"""
        self._pending.extend(messages)
        self._batch_active = True
        self._batch_timer.start()

    def _collapse_system_dupes(self, messages: List[Message]) -> List[Message]:
        """折叠相邻的同一次系统事件（列表保持传入顺序）。"""
        out = []
        for m in messages:
            if out and _same_system_event(out[-1], m):
                continue
            out.append(m)
        return out

    def prepend_messages_immediate(self, messages: List[Message]):
        """立即插入（用于少量消息）。"""
        if not messages:
            return
        messages = self._collapse_system_dupes(messages)
        # 与已在屏上的首条相接处也可能是一对重复（翻页/重建边界）
        if self._messages and messages and _same_system_event(
                messages[-1], self._messages[0]):
            messages.pop()
        if not messages:
            return
        old_first_id = self._messages[0].id if self._messages else None
        self.beginInsertRows(QModelIndex(), 0, len(messages) - 1)
        self._messages = list(messages) + self._messages
        if self._messages:
            self._last_ts = self._messages[-1].timestamp
        # 缓存按 id 记录，顶部插入只可能改变原首行的时间标签/名字展示
        if old_first_id:
            self._height_cache.pop(old_first_id, None)
        # 顶部插入使所有现有行号 +N，重建索引（低频批量操作，O(n) 可接受）
        self._rebuild_row_index()
        self.endInsertRows()

    def _flush_batch(self):
        if not self._pending:
            self._batch_active = False
            self.batch_finished.emit()
            return
        count = min(self.BATCH_SIZE, len(self._pending))
        batch = self._pending[-count:]
        self._pending = self._pending[:-count]
        # batch 是时间倒序（新→旧），反转后为旧→新
        batch.reverse()
        batch = self._collapse_system_dupes(batch)
        if self._messages and batch and _same_system_event(
                batch[-1], self._messages[0]):
            batch.pop()
        if batch:
            old_first_id = self._messages[0].id if self._messages else None
            self.beginInsertRows(QModelIndex(), 0, len(batch) - 1)
            self._messages = batch + self._messages
            if old_first_id:
                self._height_cache.pop(old_first_id, None)
            # 顶部插入使现有行号 +N，重建索引
            self._rebuild_row_index()
            self.endInsertRows()
        if self._pending:
            self._batch_timer.start()
        else:
            log.debug("分批加载完成，共 %d 条消息", len(self._messages))
            self._batch_active = False
            self.batch_finished.emit()

    def update_message(self, message: Message):
        for i, m in enumerate(self._messages):
            if m.id == message.id:
                self._messages[i] = message
                self._drop_inline(message.id)
                self._height_cache.pop(message.id, None)
                idx = self.index(i)
                self.dataChanged.emit(idx, idx)
                return

    def remove_message(self, message_id: str) -> int:
        for i, m in enumerate(self._messages):
            if m.id == message_id:
                self._drop_inline(message_id)
                self.beginRemoveRows(QModelIndex(), i, i)
                self._messages.pop(i)
                # 缓存按 id 记录：被删行的 id 作废；后一行的时间标签/名字
                # 展示因前一行消失而可能变化，一并失效
                self._height_cache.pop(message_id, None)
                if i < len(self._messages):
                    self._height_cache.pop(self._messages[i].id, None)
                # 删除使 i 之后的行号 -1，重建索引（删除是低频操作）
                self._rebuild_row_index()
                self.endRemoveRows()
                return i
        return -1

    def all_messages(self) -> List[Message]:
        return list(self._messages)

    # ---- 高度计算（延迟计算 + 缓存） ---- #
    def invalidate_height_cache(self):
        self._height_cache.clear()
        # 宽度/字体变化会使 doc 换行结果改变，缓存一并清空
        self._doc_cache.clear()

    def set_fonts(self, font: QFont):
        self._font = font
        self._small_font = QFont(font)
        self._small_font.setPointSize(max(_SMALL_FONT, font.pointSize() - 1))
        self._tiny_font = QFont(font)
        self._tiny_font.setPointSize(max(_TINY_FONT, font.pointSize() - 2))
        self._reaction_font = QFont(font)
        self._reaction_font.setFamily(_EMOJI_FONT_FAMILY)
        self._reaction_font.setPointSize(max(_REACTION_FONT, font.pointSize()))
        self._height_cache.clear()
        # 字体变化使所有 doc 需重建
        self._doc_cache.clear()

    def set_image_loader(self, loader):
        self._image_loader = loader
        if loader is not None:
            loader.image_ready.connect(self._on_inline_image_ready)

    def _on_inline_image_ready(self, resolved_url: str):
        """内联图到货：记录真实显示尺寸，失效受影响行的高度缓存并重绘。"""
        msgs = self._inline_url_msgs.get(resolved_url)
        if not msgs:
            return
        if self._image_loader is not None:
            orig = self._image_loader.original_size(resolved_url)
            if orig:
                self._inline_sizes[resolved_url] = _inline_display_size(*orig)
        for mid in list(msgs):
            self._height_cache.pop(mid, None)
            # 内联图尺寸变化，doc 需重建以注入新尺寸/资源
            self._drop_msg_docs(mid)
            row = self.find_message_row(mid)
            if row < 0:
                continue
            idx = self.index(row)
            self.dataChanged.emit(idx, idx)

    def _get_image_display_size(self, msg: Message) -> tuple:
        """获取图片消息的显示尺寸 (w, h)。优先用已加载的原始尺寸，否则返回默认。"""
        raw = msg.file_url or msg.cover_url or ""
        if raw and self._image_loader:
            orig = self._image_loader.original_size(raw)
            if orig:
                return _calc_image_display_size(orig[0], orig[1])
        return _IMG_W, _IMG_H

    def _merge_image_size(self, url: str, content_w: int) -> tuple:
        """合并转发图片条目的显示尺寸 (w, h)。

        优先按原始尺寸缩放，超出内容宽度时等比收缩；未加载原始尺寸时回退
        默认占位（宽度不超过内容宽）。高度/绘制/点击三处必须共用同一尺寸。
        """
        if url and self._image_loader:
            orig = self._image_loader.original_size(url)
            if orig:
                w, h = _calc_image_display_size(orig[0], orig[1])
                if w > content_w:
                    k = content_w / w
                    w, h = int(w * k), max(40, int(h * k))
                return max(40, w), max(40, h)
        return min(_IMG_W, max(40, content_w)), _IMG_H

    def _get_height(self, row: int) -> int:
        if row >= len(self._messages):
            return 0
        msg = self._messages[row]
        # 缓存按消息 id 记录：按行号记录时顶部插行使行号全部移位，
        # 每批历史插入都要全量重算行高，滚动路径上逐行卡顿
        h = self._height_cache.get(msg.id)
        if h is None:
            h = self._calc_height(msg, row)
            self._height_cache[msg.id] = h
        return h

    def _available_text_width(self, row: int) -> int:
        """计算当前行可用的文本宽度，考虑视口宽度和头像区域。
        始终预留头像空间，保证连续消息气泡对齐。"""
        vw = self._view_width if self._view_width > 0 else 800
        # 始终预留头像 + 间距，使连续消息气泡位置一致
        avatar_w = _AVATAR_SIZE
        avatar_gap = _AVATAR_GAP
        max_bubble_w = vw - _MARGIN_L - _MARGIN_R - avatar_w - avatar_gap - _TRI_W
        bubble_w = min(_BUBBLE_MAX_W, max_bubble_w)
        return max(50, bubble_w - _BUBBLE_PAD * 2)

    def _text_height(self, text: str, available_width: int) -> int:
        if not text:
            return QFontMetrics(self._font).height()
        doc = QTextDocument()
        doc.setDefaultFont(self._font)
        doc.setPlainText(text)
        doc.setDocumentMargin(0)
        doc.setTextWidth(max(50, available_width))
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        doc.setDefaultTextOption(opt)
        return int(doc.size().height())

    def build_text_doc(self, msg: Message) -> QTextDocument:
        """构建文本消息的 QTextDocument（字体 + 换行选项，不含宽度）。

        markdown / @提及 / 纯文本三条路径的唯一入口：高度计算、气泡宽度、
        委托绘制、点击命中都必须用它。内容或换行模式出现两套，就会出现
        行高不符溢出、点击命不中 anchor（都是踩过的坑）。
        """
        text = msg.text or msg.display_text or ""
        # 内容指纹：text + 提及序列（影响 markdown 高亮/替换）。内联图尺寸
        # 变化由 _on_inline_image_ready 走 _drop_msg_docs 失效，不在此指纹内。
        # 缓存的是未设宽度的模板 doc，调用方使用前 setTextWidth（现有调用方
        # text_content_width / text_bubble_metrics / _build_text_doc 均已设置），
        # 主线程顺序执行共享安全。
        mentions_key = tuple(
            (m.get("username"), m.get("name"))
            for m in (msg.mentions or []))
        key = ("msg", msg.id, text, msg.display_text, mentions_key)
        doc = self._doc_cache_get(key)
        if doc is not None:
            return doc
        doc = QTextDocument()
        doc.setDefaultFont(self._font)
        doc.setDocumentMargin(0)
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        doc.setDefaultTextOption(opt)
        if markdown.looks_like_markdown(text):
            html_text = markdown.md_to_html(text)
            if msg.mentions:
                html_text = markdown.highlight_mentions(html_text, msg.mentions)
            # markdown 内联图：重写 <img> 占位尺寸为已加载的真实尺寸，
            # 登记等待表，并收集已就绪图以便注入资源（见 _prepare_inline_images）
            html_text = self._prepare_inline_images(msg, html_text)
            doc.setHtml(html_text)
            self._inject_inline_resources(doc, msg.id)
        elif msg.mentions:
            doc.setHtml(markdown.apply_mentions_plain(text, msg.mentions))
        else:
            doc.setPlainText(text)
        self._doc_cache_put(key, doc)
        return doc

    # ---- markdown 内联图 ---- #
    def _drop_inline(self, msg_id: str):
        """消息被删/内容替换时清掉其内联图索引与 doc 缓存。"""
        # 内容替换后 build_text_doc 缓存作废（内联图尺寸/文本都可能变）
        self._drop_msg_docs(msg_id)
        entries = self._inline_map.pop(msg_id, None)
        if entries:
            for ent in entries:
                resolved = ent.get("resolved")
                if resolved:
                    msgs = self._inline_url_msgs.get(resolved)
                    if msgs:
                        msgs.discard(msg_id)
                        if not msgs:
                            self._inline_url_msgs.pop(resolved, None)

    # ---- QTextDocument 缓存（LRU） ---- #
    def _doc_cache_get(self, key):
        """取缓存 doc；命中则提升为最近使用。"""
        doc = self._doc_cache.get(key)
        if doc is not None:
            self._doc_cache.move_to_end(key)
        return doc

    def _doc_cache_put(self, key, doc):
        """写入缓存 doc，超上限淘汰最久未用。"""
        if key in self._doc_cache:
            self._doc_cache.move_to_end(key)
        self._doc_cache[key] = doc
        while len(self._doc_cache) > self._DOC_CACHE_LIMIT:
            self._doc_cache.popitem(last=False)

    def _drop_msg_docs(self, msg_id: str):
        """移除某条消息的全部 build_text_doc 缓存项（内容/内联图变化时）。
        纯函数 doc（quote/merge/caption）键不含 msg_id，内容不可变无需清理。"""
        if not msg_id:
            return
        prefix = ("msg", msg_id)
        stale = [k for k in self._doc_cache if k[:2] == prefix]
        for k in stale:
            self._doc_cache.pop(k, None)

    def _inline_entries(self, msg: Message) -> list:
        """取该消息的内联图列表（懒解析 + 缓存），空返回 []。"""
        if msg.id in self._inline_map:
            return self._inline_map[msg.id]
        text = msg.text or msg.display_text or ""
        entries = markdown.inline_image_urls(text) if text else []
        self._inline_map[msg.id] = entries
        return entries

    def _prepare_inline_images(self, msg: Message, html_text: str) -> str:
        """登记内联图加载：解析 url、登记反向索引、发起加载请求，
        并把已加载图像的 <img> 占位尺寸改写为真实显示尺寸。

        尺寸改写直接操作 html 属性：md_to_html 输出固定格式
        <img src=... alt=... width=W height=H>，用正则按 src 定位改写
        width/height 可靠；QTextImageFormat.setWidth/setHeight 再
        setCharFormat 对已存在的 image 字符无效（改动不持久），不可用。
        未加载的图保持占位 240x180，到货后（_on_inline_image_ready 触发
        dataChanged 重绘）再走本函数改写为真实尺寸。
        """
        import re
        loader = self._image_loader
        entries = self._inline_entries(msg)
        if not entries:
            return html_text
        rewrites = []  # (src, w, h)
        for ent in entries:
            resolved = ent["resolved"] if "resolved" in ent else None
            if not resolved:
                if not loader:
                    continue
                # 与 image_ready 的发射键保持一致：需要 canonical 形式，
                # 否则 URL 含空格/非 ASCII 时对不上（见 avatar_loader.canon_url）
                resolved = canon_url(loader.resolve(ent["url"]))
                ent["resolved"] = resolved
                # 已加载过的图同步取真实尺寸，不必等 image_ready 回投
                orig = loader.original_size(resolved)
                if orig:
                    self._inline_sizes[resolved] = _inline_display_size(*orig)
            # 登记 url -> msg 反向索引，供 image_ready 到货回调定位要重绘的行
            self._inline_url_msgs.setdefault(resolved, set()).add(msg.id)
            if loader:
                size = self._inline_sizes.get(resolved)
                if size:
                    rewrites.append((ent["src"], size[0], size[1]))
                elif resolved not in self._inline_sizes:
                    # 未加载：触发请求（pixmap_for 未命中会发起并返回 None）
                    loader.pixmap_for(resolved, ent["w"])
        if not rewrites:
            return html_text

        def _rewrite(m: re.Match) -> str:
            tag = m.group(0)
            w, h = re.match(r'\s+width="([^"]*)"', tag), None
            # 组装每处 src 对应的 (w,h)；这里 tag 只含单一 src，按 src 匹配
            for src, nw, nh in rewrites:
                if src in tag:
                    tag = re.sub(r'\s+width="[^"]*"', ' width="%d"' % nw, tag)
                    tag = re.sub(r'\s+height="[^"]*"', ' height="%d"' % nh, tag)
                    break
            return tag

        return re.sub(r'<img[^>]*>', _rewrite, html_text)

    def _inject_inline_resources(self, doc: QTextDocument, msg_id: str):
        """把已加载的内联图 pixmap 写进 doc（按 html src 原样作资源键）。

        高度计算与委托绘制都走 build_text_doc，故此处与占位/真实尺寸天然
        同源。QTextDocument 的 imageName 就是 html 里 <img src> 原样（含
        相对路径），所以资源键必须用 src 而非绝对化后的 resolved（否则查
        不到不渲染）。真实尺寸已在 _prepare_inline_images 里改写到 html。
        """
        loader = self._image_loader
        if not loader:
            return
        entries = {e["src"]: e for e in self._inline_map.get(msg_id, [])}
        if not entries:
            return
        from PyQt5.QtGui import QTextCursor
        cur = QTextCursor(doc)
        cur.movePosition(cur.Start)
        while True:
            fmt = cur.charFormat()
            if fmt.isImageFormat():
                src = fmt.toImageFormat().name()
                ent = entries.get(src)
                if ent:
                    resolved = ent.get("resolved")
                    pm = (loader.pixmap_for(resolved, ent["w"])
                          if resolved else None)
                    if pm is not None and not pm.isNull():
                        doc.addResource(QTextDocument.ImageResource,
                                        QUrl(src), pm)
            if cur.atEnd():
                break
            cur.movePosition(cur.NextCharacter)

    def text_content_width(self, msg: Message, max_content_w: int) -> int:
        """文字气泡的内容宽度：单行理想宽度与 max_content_w 取小。

        必须基于替换提及后的文档测宽——@username 显示成更长的中文名，
        用原文测出的气泡宽会让绘制时换行，行高溢出盖住下一行。
        """
        doc = self.build_text_doc(msg)
        doc.setTextWidth(-1)
        ideal = doc.idealWidth()
        if ideal <= 0:
            return max(50, max_content_w)
        # ceil+1：round 会向下丢 0.x px，绘制可用宽度比排版需求窄一点，
        # 无空格长 token（FCLCLOCK_… 这类）就会整词折行
        return max(50, min(int(math.ceil(ideal)) + 1, max_content_w))

    def text_bubble_metrics(self, msg: Message, max_content_w: int):
        """文字气泡的 (内容宽, 内容高)，宽度与高度出自同一文档，
        保证行高计算与委托绘制的换行完全一致。"""
        doc = self.build_text_doc(msg)
        # 同一文档先量理想宽（关换行），再以之为宽换行量高。
        # 复用一个 doc 而非调用 text_content_width 再重建：字体/换行变化会
        # 整表重算行高（每行一次），markdown 行重建文档要重跑渲染，双重
        # 构建会让大字库会话的布局明显卡顿（冻结）。
        doc.setTextWidth(-1)
        ideal = doc.idealWidth()
        if ideal <= 0:
            content_w = max(50, max_content_w)
        else:
            content_w = max(50, min(int(math.ceil(ideal)) + 1, max_content_w))
        doc.setTextWidth(content_w)
        return content_w, int(doc.size().height())

    # ---- 引用块（高度与绘制同源） ---- #
    # 图片引用缩略图外框：宽 150、高 64（图按比例缩放居中，不裁切）。
    # 外框尺寸固定，图片加载前后行高不变；56px 的旧方图装整张截图时
    # 只剩一条看不清内容的细带，必须放大。
    QUOTE_THUMB_W = 168
    QUOTE_THUMB_H = 64
    QUOTE_MEDIA_GAP = 10       # 缩略图与作者/说明文字列的间距
    # 引用块内边距，绘制与高度计算共用（左缘留给 3px 竖条）
    QUOTE_PAD_TOP = 8
    QUOTE_PAD_BOTTOM = 8
    QUOTE_PAD_LEFT = 12
    QUOTE_PAD_RIGHT = 10

    def quote_block_text(self, msg: Message) -> str:
        """引用块显示文本：作者：内容；被引用内容缺失时占位。"""
        desc = msg.description or ""
        if not desc:
            return "引用消息"
        author = msg.quote_author or ""
        return f"{author}：{desc}" if author else desc

    def quote_caption(self, msg: Message) -> str:
        """图片/视频引用缩略图下的说明文字：desc 剥掉占位前缀后的正文。

        display_text 的「[图片] 图片」「[视频] 视频」是无说明时的占位，
        剥完只剩占位词就不显示。无前缀时 desc 本身可能就是原消息正文
        （引用图片消息时 _on_quote_message 优先传原文），仅图片/视频引用
        （quote_image_url 存在）才当说明用——文本引用的 description 是
        原话，永远不进这个分支。"""
        desc = (msg.description or "").strip()
        cap = ""
        for prefix in ("[图片] ", "[视频] "):
            if desc.startswith(prefix):
                cap = desc[len(prefix):].strip()
                break
        if not cap and (msg.quote_image_url
                        or msg.msg_type in ("image", "video")):
            cap = desc
        return "" if cap in ("", "图片", "视频") else cap

    def image_caption(self, msg: Message) -> str:
        """图片气泡下方的说明文字。

        官方客户端的图文混排 msg 为空、说明在附件 description；本应用
        mediaConfirm 路径的说明在 msg 正文（「图片」是解析层的无说明占位）。"""
        if msg.msg_type != "image":
            return ""
        cap = (msg.description or "").strip()
        if not cap:
            text = (msg.text or "").strip()
            if text and text != "图片":
                cap = text
        return cap

    def _caption_doc(self, text: str, width: int) -> QTextDocument:
        """图片说明的换行文档（主字体，与文本消息观感一致）。"""
        key = ("doc", "cap", text, width)
        doc = self._doc_cache_get(key)
        if doc is not None:
            return doc
        doc = QTextDocument()
        doc.setDefaultFont(self._font)
        doc.setDocumentMargin(0)
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        doc.setDefaultTextOption(opt)
        doc.setPlainText(text)
        doc.setTextWidth(max(50, width))
        self._doc_cache_put(key, doc)
        return doc

    def image_bubble_w(self, msg: Message, img_w: int,
                       max_bubble_w: int) -> int:
        """图片消息的气泡宽：无说明就是图片宽；有说明时气泡按说明的
        自然宽度撑开，上限与文本气泡一致（_BUBBLE_MAX_W 与视口取小，
        宽窗口下说明不能比文本气泡还长），下限是图片宽。返回值是说明
        换行/绘制/命中共用的唯一宽度。"""
        w = min(img_w, max_bubble_w)
        cap = self.image_caption(msg)
        if not cap:
            return w
        doc = self._caption_doc(cap, 10 ** 6)
        natural = int(doc.idealWidth())
        cap_max = min(_BUBBLE_MAX_W, max_bubble_w)
        return max(w, min(natural, cap_max))

    def image_caption_h(self, msg: Message, bubble_w: int) -> int:
        """图片说明文字高度（含与图片的间距）；无说明返回 0。

        bubble_w 必须与 layout_rects 的图片气泡宽（image_bubble_w）
        同源，两边换行宽度不同就会裁字或留白。"""
        if msg.msg_type != "image":
            return 0
        cap = self.image_caption(msg)
        if not cap:
            return 0
        doc = self._caption_doc(cap, bubble_w)
        return _CAPTION_GAP + int(doc.size().height())

    def quote_file_display(self, msg: Message) -> str:
        """文件引用显示的文件名：优先解析/补抓拿到的原始文件名。"""
        if msg.quote_file_name:
            return msg.quote_file_name
        desc = msg.description or ""
        if desc.startswith("[文件] "):
            return desc[len("[文件] "):] or "文件"
        return ""

    def _quote_doc(self, text: str, width: int) -> QTextDocument:
        """引用块小字文档（按给定宽度换行）。"""
        key = ("doc", "q", text, width)
        doc = self._doc_cache_get(key)
        if doc is not None:
            return doc
        doc = QTextDocument()
        doc.setDefaultFont(self._small_font)
        doc.setDocumentMargin(0)
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        doc.setDefaultTextOption(opt)
        doc.setPlainText(text)
        doc.setTextWidth(max(50, width))
        self._doc_cache_put(key, doc)
        return doc

    def _quote_small_doc(self, text: str, avail_w: int) -> QTextDocument:
        """引用块通栏小字文档（宽度与绘制侧左右内边距一致）。"""
        return self._quote_doc(
            text, avail_w - self.QUOTE_PAD_LEFT - self.QUOTE_PAD_RIGHT)

    def _quote_block_doc(self, msg: Message, avail_w: int) -> QTextDocument:
        return self._quote_small_doc(self.quote_block_text(msg), avail_w)

    def quote_media_metrics(self, msg: Message, avail_w: int) -> dict:
        """图片引用块的内部几何（相对卡片左上角）。

        缩略图在左、作者与说明在右侧文字列；外框尺寸固定，图片加载前后
        行高不变。高度计算与委托绘制只能从这里取数，两边各算一套会错位。
        """
        fm = QFontMetrics(self._small_font)
        inner_w = max(50, avail_w - self.QUOTE_PAD_LEFT - self.QUOTE_PAD_RIGHT)
        # 窄窗口下缩略图让位给文字列，但不小于 40（外框高度不变）
        thumb_w = max(40, min(self.QUOTE_THUMB_W,
                              inner_w - self.QUOTE_MEDIA_GAP - 72))
        col_x = self.QUOTE_PAD_LEFT + thumb_w + self.QUOTE_MEDIA_GAP
        col_w = max(50, avail_w - self.QUOTE_PAD_RIGHT - col_x)
        author_h = fm.height() if msg.quote_author else 0
        cap = self.quote_caption(msg)
        cap_doc = self._quote_doc(cap, col_w) if cap else None
        cap_h = int(cap_doc.size().height()) if cap_doc is not None else 0
        text_h = author_h + ((4 + cap_h) if cap_h else 0)
        row_h = max(self.QUOTE_THUMB_H, text_h)
        # 文字比缩略图矮时在列内垂直居中，避免作者行孤零零贴在卡片顶
        offset = max(0, (row_h - text_h) // 2)
        author_y = self.QUOTE_PAD_TOP + offset
        return {
            "height": self.QUOTE_PAD_TOP + row_h + self.QUOTE_PAD_BOTTOM,
            "thumb": QRect(self.QUOTE_PAD_LEFT, self.QUOTE_PAD_TOP,
                           thumb_w, self.QUOTE_THUMB_H),
            "col_x": col_x,
            "col_w": col_w,
            "author_y": author_y,
            "author_h": author_h,
            "cap_y": author_y + author_h + (4 if author_h else 0),
            "cap_doc": cap_doc,
        }

    def quote_block_height(self, msg: Message, avail_w: int) -> int:
        """引用块完整高度。

        文本引用：内容自动换行不截断；图片引用：缩略图与作者/说明列的
        较高者；文件引用：作者行 + 文件行。三种结构各自与委托绘制严格
        同源，行高不符就会裁切或重叠。"""
        fm = QFontMetrics(self._small_font)
        if msg.quote_image_url:
            return self.quote_media_metrics(msg, avail_w)["height"]
        if self.quote_file_display(msg):
            h = self.QUOTE_PAD_TOP + self.QUOTE_PAD_BOTTOM
            if msg.quote_author:
                h += fm.height() + 2
            return h + fm.height()
        doc = self._quote_block_doc(msg, avail_w)
        return int(doc.size().height()) + self.QUOTE_PAD_TOP \
            + self.QUOTE_PAD_BOTTOM

    def _small_doc(self, text: str, width: int) -> QTextDocument:
        """用小型字体构建自动换行文档，供合并卡片内容排版与高度计算共用。"""
        key = ("doc", "small", text, width)
        doc = self._doc_cache_get(key)
        if doc is not None:
            return doc
        doc = QTextDocument()
        doc.setDefaultFont(self._small_font)
        doc.setDocumentMargin(0)
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        doc.setDefaultTextOption(opt)
        doc.setPlainText(text)
        doc.setTextWidth(max(50, width))
        self._doc_cache_put(key, doc)
        return doc

    def _merge_content_doc(self, content: str, width: int) -> QTextDocument:
        """合并卡片条目内容的文档：走 markdown 渲染，把附件的 [文件名](完整URL)
        变成可点击锚点（其它客户端与本应用都用同一种链接形式）。

        高度计算与绘制、点击命中必须共用这一个 doc；字体/宽度/换行任一不同，
        就会行高错位或锚点命不中（同 build_text_doc 的约束）。
        """
        key = ("doc", "merge", content, width)
        doc = self._doc_cache_get(key)
        if doc is not None:
            return doc
        doc = QTextDocument()
        doc.setDefaultFont(self._small_font)
        doc.setDocumentMargin(0)
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        doc.setDefaultTextOption(opt)
        if markdown.looks_like_markdown(content):
            doc.setHtml(markdown.md_to_html(content))
        else:
            doc.setPlainText(content)
        doc.setTextWidth(max(50, width))
        self._doc_cache_put(key, doc)
        return doc

    def merge_content_layout(self, msg: Message, content_rect: QRect) -> list:
        """合并卡片每条内容的命中布局：[(内容矩形, 内容 doc, entry), ...]。

        与 merge_block_height / _paint_merge_content 同源（同一 y 递进），供
        视口点击命中锚点 / 文本选区定位使用。doc 为 None 表示该条无内容。
        """
        entries = msg.forward_list or []
        out = []
        if not entries:
            return out
        fm = QFontMetrics(self._small_font)
        name_h = fm.height()
        x = content_rect.left()
        w = content_rect.width()
        # 头部行「聊天记录 · N 条」占 24，偏移与 _paint_merge_content 的
        # y += 24 + 2 一致
        y = content_rect.top() + 24 + 2
        for e in entries:
            y += name_h + 2
            content = e.get("text") or ""
            image_url = _merge_image_url(e)
            if image_url:
                # 图片条目：不渲染文本，只占图片矩形（高度与绘制/行高同源）
                _w, img_h = self._merge_image_size(image_url, w)
                out.append({"rect": QRect(x, y, w, img_h), "doc": None, "entry": e})
                y += img_h + 2
            elif content:
                doc = self._merge_content_doc(content, w)
                dh = int(doc.size().height())
                out.append({"rect": QRect(x, y, w, dh), "doc": doc, "entry": e})
                y += dh + 2
            else:
                out.append({"rect": QRect(x, y, w, 0), "doc": None, "entry": e})
            y += 5
        return out

    def merge_block_height(self, msg: Message, avail_w: int) -> int:
        """合并转发卡片完整高度：头部 + 每条（发送人/时间 + 内容，自动换行）。"""
        entries = msg.forward_list or []
        if not entries:
            return 34
        fm = QFontMetrics(self._small_font)
        name_h = fm.height()
        h = 24  # 卡片头部「聊天记录 · N 条」行
        for e in entries:
            row_h = name_h + 2
            content = e.get("text") or ""
            image_url = _merge_image_url(e)
            if image_url:
                # 图片条目：高为图片显示高度，与绘制/点击布局同源
                _w, img_h = self._merge_image_size(image_url, avail_w)
                row_h += img_h + 2
            elif content:
                row_h += self._merge_content_doc(content, avail_w).size().height() + 2
            h += row_h + 5
        return int(h) + 4

    def _calc_bubble_content_h(self, msg: Message, row: int = -1) -> int:
        """计算气泡内容高度（含底部表情回应条）。"""
        avail_w = self._available_text_width(row) if row >= 0 \
            else _BUBBLE_MAX_W - _BUBBLE_PAD * 2
        reactions_h = self.reaction_band(msg, avail_w)[1]

        if _is_image_like(msg):
            # 图片无气泡边框：图片 + 下方说明（图文混排）+ 回应条
            img_w, img_h = self._get_image_display_size(msg)
            vw = self._view_width if self._view_width > 0 else 800
            # 说明的换行宽度与 layout_rects 的图片气泡宽同源
            max_bubble = vw - _MARGIN_L - _MARGIN_R \
                - _AVATAR_SIZE - _AVATAR_GAP - _TRI_W
            bubble_w = self.image_bubble_w(msg, img_w, max_bubble)
            return img_h + self.image_caption_h(msg, bubble_w) + reactions_h
        if msg.msg_type in ("file", "audio", "video"):
            # 文件卡：徽标与两行文本取大者，绘制走同一 _file_card_h
            return _file_card_h(self._font, self._small_font) + reactions_h

        if msg.msg_type == "merge":
            # 合并转发卡片：高度与绘制同源（merge_block_height / _paint_merge_content）
            return self.merge_block_height(msg, avail_w) + reactions_h

        if msg.msg_type == "quote":
            # 引用块完整显示不截断，高度走与绘制同源的 quote_block_height
            quote_h = self.quote_block_height(msg, avail_w)
            reply_text = msg.text or ""
            # 引用回复高度必须与绘制一致：走同一文档构建
            reply_h = 0
            if reply_text:
                reply_msg = Message(text=reply_text, mentions=msg.mentions)
                _, reply_h = self.text_bubble_metrics(reply_msg, avail_w)
            return quote_h + reply_h + reactions_h

        _w, h = self.text_bubble_metrics(msg, avail_w)
        # 逐条转发内容上方有「转发的消息」标记，高度计算与绘制共用同一常量，
        # 只加一边会出现内容被裁或溢出
        if msg.is_forwarded:
            h += _FORWARD_TAG_H
        return h + reactions_h

    def time_label_h(self) -> int:
        """居中时间标签行高：上留 8 + 字体高 + 下留 4。

        不能用硬编码常量：中文字体 12pt 的 QFontMetrics.height 就接近
        18-20px，固定 16 的绘制矩形会把标签底部裁掉一截，用户调大
        字体后更明显。委托绘制（_paint_time_label）走同一度量。
        """
        return QFontMetrics(self._small_font).height() + 12

    def _calc_height(self, msg: Message, row: int) -> int:
        divider_h = (_UNREAD_DIVIDER_H
                     if msg.id and msg.id == self._unread_divider_id else 0)
        if msg.msg_type == "system":
            time_h = self.time_label_h() if self._show_time_label(row) else 0
            text = msg.display_text or msg.text or ""
            _, bubble_h = _system_bubble_metrics(
                text, self._view_width or 800, self._small_font)
            return time_h + _MARGIN_V * 2 + bubble_h + divider_h

        time_h = self.time_label_h() if self._show_time_label(row) else 0
        show_avatar = self._show_avatar(row)
        show_name = self._show_name_row(row)
        name_h = _NAME_H if show_name else 0
        bubble_content_h = self._calc_bubble_content_h(msg, row)
        bubble_h = bubble_content_h + _BUBBLE_PAD * 2

        avatar_h = _AVATAR_SIZE if show_avatar else 0
        time_row_h = QFontMetrics(self._tiny_font).height() + 2
        content_h = name_h + bubble_h + time_row_h
        body_h = max(avatar_h, content_h)
        return time_h + _MARGIN_V * 2 + body_h + 8 + divider_h

    def _show_time_label(self, row: int) -> bool:
        if row <= 0 or row >= len(self._messages):
            return False
        msg = self._messages[row]
        prev = self._messages[row - 1]
        return (msg.timestamp - prev.timestamp) > self.TIME_GAP

    def _show_avatar(self, row: int) -> bool:
        if row >= len(self._messages):
            return False
        if row == 0:
            return True
        msg = self._messages[row]
        prev = self._messages[row - 1]
        if prev.msg_type == "system" or msg.msg_type == "system":
            return True
        return prev.sender != msg.sender or prev.is_mine != msg.is_mine

    def _show_name_row(self, row: int) -> bool:
        if not self._show_name or row >= len(self._messages):
            return False
        msg = self._messages[row]
        if msg.is_mine or msg.msg_type == "system":
            return False
        if row == 0:
            return True
        prev = self._messages[row - 1]
        if prev.msg_type == "system":
            return True
        return prev.sender != msg.sender


# ============================================================================
# MessageDelegate
# ============================================================================
class MessageDelegate(QStyledItemDelegate):
    """绘制消息行：头像、气泡、三角、时间标签等。"""

    def __init__(self, model: MessageListModel, parent=None, palette: dict = None):
        super().__init__(parent)
        self.model_ref = model
        self.image_loader = None
        self.avatar_loader = None
        # 主题取色器（ThemeManager）；未注入时回退到亮色默认，保证测试可运行
        self._theme = None
        # 兼容旧 color palette 机制（保留 set_palette/_col，渲染统一走 _c）
        self._palette = palette
        # url -> 等待该资源的消息 id（用 id 而非行号：插行不会让回调打错行）
        self._img_pending: dict = {}
        # 透明图片主色渐变垫底缓存：(url, w, h, dpr) -> QPixmap（见 _sticker_backdrop）
        self._backdrop_cache: dict = {}
        # 缓存视口宽度，避免 sizeHint 中使用不可靠的 option.rect.width()
        self._view_width = 0
        # 点击按压反馈：气泡叠深色罩层片刻后还原（flash() 设置）
        self._flash_id = ""
        self._flash_until = 0.0
        self._flash_gen = 0
        # 音频内联播放状态 {msg_id: (playing, pos_ms, dur_ms)}；
        # ChatView 持播放器驱动，delegate 只管绘制/命中
        self._audio_states: dict = {}
        if parent and hasattr(parent, "viewport"):
            # 必须监听 viewport 自己的 Resize：装在 view 上时过滤时机早于
            # QAbstractScrollArea 改 viewport 尺寸，读到的永远是旧宽度
            # （滞后一拍），缩窗后行宽/行高全部错位、右侧内容点击失效
            parent.viewport().installEventFilter(self)
            self._view_width = parent.viewport().width()
            self.model_ref._view_width = self._view_width

    def set_theme(self, theme) -> None:
        """注入 ThemeManager，让气泡/文字等按当前主题取色。"""
        self._theme = theme

    # ---- 点击按压反馈（flash） ---- #
    def _flash_repaint(self):
        try:
            view = self.parent()
        except RuntimeError:
            # flash 的延时还原回调可能在委托析构后才触发（测试/快速关窗
            # 场景），此时 C++ 对象已删，异常逃出 Qt 事件循环会终止进程
            return
        if view is not None and hasattr(view, "viewport"):
            try:
                view.viewport().update()
            except RuntimeError:
                pass

    def flash(self, msg_id: str, ms: int = 180):
        """点击反馈：该消息气泡叠一层深色罩，ms 毫秒后还原（按钮式按压）。

        连续点击以最后一次为准：_flash_gen 让旧的还原定时器失效。
        """
        if not msg_id:
            return
        self._flash_id = msg_id
        self._flash_until = _time.monotonic() + ms / 1000.0
        self._flash_gen += 1
        gen = self._flash_gen
        self._flash_repaint()
        QTimer.singleShot(ms, lambda: self._flash_done(gen))

    def _flash_done(self, gen: int):
        if gen != self._flash_gen:
            return
        self._flash_id = ""
        self._flash_repaint()

    def _flash_active(self, msg_id: str) -> bool:
        return (bool(msg_id) and msg_id == self._flash_id
                and _time.monotonic() < self._flash_until)

    # ---- 悬停 tooltip（文件名放不下显全名；其余气泡显完整日期时间） ---- #
    def helpEvent(self, event, view, option, index):
        msg = index.data(RoleMessage)
        if event.type() != QEvent.ToolTip or msg is None:
            return super().helpEvent(event, view, option, index)
        if msg.msg_type in ("file", "audio", "video"):
            bubble, _avatar, _img = self.layout_rects(index, option.rect)
            text_w = (bubble.width() - _BUBBLE_PAD * 2
                      - _FILE_BADGE - _FILE_TEXT_GAP)
            name = _file_display_name(msg)
            if (text_w > 0 and not bubble.isNull()
                    and QFontMetrics(self.model_ref._font)
                    .horizontalAdvance(name) > text_w):
                QToolTip.showText(event.globalPos(), name, view)
                return True
            return super().helpEvent(event, view, option, index)
        bubble, _avatar, _img = self.layout_rects(index, option.rect)
        if not bubble.isNull() and bubble.contains(event.pos()):
            QToolTip.showText(event.globalPos(),
                              _full_time_text(msg.timestamp), view)
            return True
        return super().helpEvent(event, view, option, index)

    def _c(self, key: str, default: str) -> QColor:
        """从当前主题取语义色（缺键回退），未注入主题时用亮色默认。"""
        if self._theme is not None:
            return QColor(self._theme.color(key, default))
        fallback = {
            "system_bubble_fill": "#e8e8e8",
            "system_bubble_text": "#999999",
            "sender_name": "#888888",
            "bubble_other_border": "#e0e0e0",
            "time_label": "#b0b0b0",
            "text_tertiary": "#666666",
            "link_strong": "#1256b8",
            "selection_bg": "#3399ff",
            "selection_text": "#ffffff",
            "image_placeholder": "#d8d8d8",
            "flash_highlight": "#ffe58f",
            "chat_bg": "#f5f5f5",
            "bubble_mine": "#ffffff",
            "bubble_other": "#ffffff",
            "text_primary": "#191919",
            "quote_bg": "#f0f0f0",
            "quote_border": "#c0c0c0",
            "reaction_bg": "#f0f2f5",
            "reaction_border": "#dfe3e8",
            "reaction_mine_bg": "#e3f2fd",
            "reaction_mine_text": "#1256b8",
            "mention_highlight": "#fff3d6",
        }
        return QColor(fallback.get(key, default))


    # ---- 异步资源注入 ---- #
    def set_image_loader(self, loader):
        self.image_loader = loader
        if loader is not None:
            loader.image_ready.connect(self._on_resource_ready)

    def set_avatar_loader(self, loader):
        self.avatar_loader = loader
        if loader is not None:
            loader.avatar_ready.connect(self._on_resource_ready)

    def set_palette(self, palette: dict):
        """切换主题色板并触发整表重绘。"""
        self._palette = palette
        if self.model_ref is not None:
            self.model_ref.layoutChanged.emit()

    def _col(self, key: str, fallback: str) -> QColor:
        """取色：优先主题色板，缺键或未接主题时回退默认浅色。"""
        if self._palette:
            return QColor(self._palette.get(key, fallback))
        return QColor(fallback)

    def clear_pending_resources(self):
        """切房间时丢掉等待表，旧房间的到货通知不再找行。"""
        self._img_pending.clear()
        self._backdrop_cache.clear()

    def _on_resource_ready(self, url: str):
        """图片/头像到货：按消息 id 找回所在行并重绘。"""
        ids = self._img_pending.pop(url, set())
        for mid in ids:
            row = self.model_ref.find_message_row(mid)
            if row < 0:
                continue
            # 图片到货可能改变气泡尺寸，失效高度缓存
            self.model_ref._height_cache.pop(mid, None)
            idx = self.model_ref.index(row, 0)
            self.model_ref.dataChanged.emit(idx, idx)

    # ---- 尺寸 ---- #
    def sizeHint(self, option: QStyle.OptionViewItem, index: QModelIndex) -> QSize:
        h = index.data(RoleRowHeight)
        if h is None:
            h = 60
        # 确保获取视口宽度
        if self._view_width <= 0:
            view = self.parent()
            if view and hasattr(view, 'viewport'):
                w = view.viewport().width()
                if w > 0:
                    self._view_width = w
        # 如果还是 0，使用 option.rect.width() 作为最后手段
        w = self._view_width if self._view_width > 0 else option.rect.width()
        # 如果还是 0，使用默认宽度
        if w <= 0:
            w = 600  # 默认宽度
        return QSize(w, h)

    def eventFilter(self, obj, event):
        from PyQt5.QtCore import QEvent
        if event.type() == QEvent.Resize:
            view = self.parent()
            if view and hasattr(view, 'viewport'):
                new_w = view.viewport().width()
                if new_w != self._view_width:
                    self._view_width = new_w
                    self.model_ref._view_width = new_w
                    self.model_ref.invalidate_height_cache()
                    self.model_ref.layoutChanged.emit()
        return super().eventFilter(obj, event)

    def initStyleOption(self, option: QStyle.OptionViewItem, index: QModelIndex):
        super().initStyleOption(option, index)
        option.state &= ~QStyle.State_Selected
        option.state &= ~QStyle.State_HasFocus
        option.state &= ~QStyle.State_MouseOver

    # ---- 绘制 ---- #
    def paint(self, painter: QPainter, option: QStyle.OptionViewItem,
              index: QModelIndex):
        # 更新视口宽度（paint 时视图肯定已显示）
        if self._view_width <= 0:
            view = self.parent()
            if view and hasattr(view, 'viewport'):
                w = view.viewport().width()
                if w > 0:
                    self._view_width = w

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)

        # 行背景透明：底色/背景图由 ChatView 视口 palette 统一绘制，
        # 逐行填色会盖住背景图，且滚动时背景会跟着行跑
        msg = index.data(RoleMessage)
        if msg is None:
            painter.restore()
            return

        # 跳转原消息后的临时高亮（flash_row 设置，约 900ms 后清除）
        if msg.id and msg.id == self.model_ref.flash_msg_id:
            highlight = QColor(self._c("flash_highlight", "#ffe58f"))
            highlight.setAlpha(102)
            painter.fillRect(option.rect, highlight)

        # 多选转发模式：勾选行整行着色 + 右侧小对勾，未选中行加左缘细线提示可勾
        view = self.parent()
        if view is not None and getattr(view, "ms_active", lambda: False)():
            if getattr(view, "ms_is_selected", lambda _m: False)(msg.id):
                sel = QColor("#4a90d9")
                sel.setAlpha(38)
                painter.fillRect(option.rect, sel)
                painter.setPen(QPen(QColor("#4a90d9"), 2))
                chk = QRect(option.rect.right() - 16, option.rect.top() + 6, 12, 12)
                painter.drawRoundedRect(chk, 3, 3)
                painter.setPen(QPen(Qt.white, 2))
                painter.drawLine(chk.left() + 2, chk.center().y(),
                                 chk.left() + 5, chk.bottom() - 2)
                painter.drawLine(chk.left() + 5, chk.bottom() - 2,
                                 chk.right() - 1, chk.top() + 4)
            else:
                painter.setPen(QPen(QColor("#4a90d9"), 2))
                painter.drawLine(option.rect.left() + 2, option.rect.top() + 2,
                                 option.rect.left() + 2, option.rect.bottom() - 2)

        is_system = msg.msg_type == "system"
        # @我 / @all：淡色整行底 + 左缘竖条（纯视觉，不参与行高与命中）
        if not is_system and not msg.is_mine and (
                getattr(msg, "mentions_me", False)
                or getattr(msg, "mention_all", False)):
            tint = QColor(self._c("mention_highlight", "#ff9f2e"))
            tint.setAlpha(30)
            painter.fillRect(option.rect, tint)
            painter.fillRect(option.rect.left(), option.rect.top(),
                             3, option.rect.height(),
                             QColor(self._c("mention_highlight", "#ff9f2e")))
        if msg.id and msg.id == self.model_ref._unread_divider_id:
            self._paint_unread_divider(painter, option.rect)
        if is_system:
            self._paint_system_row(painter, option.rect, msg, index)
        else:
            self._paint_message_row(painter, option.rect, msg, index)

        # 点击按压反馈：气泡/图片上叠深色罩，flash() 的定时器到点还原
        if self._flash_active(msg.id):
            bubble, _avatar, img = self.layout_rects(index, option.rect)
            target = img if not img.isNull() else bubble
            if not target.isNull():
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(0, 0, 0, 36))
                painter.drawRoundedRect(
                    target, 4 if not img.isNull() else 8, 8)

        painter.restore()

    def _divider_offset(self, index: QModelIndex) -> int:
        """本行顶部未读分隔线占用的高度：绘制与布局唯一的取数入口。"""
        msg = index.data(RoleMessage)
        if msg is not None and msg.id and \
                msg.id == self.model_ref._unread_divider_id:
            return _UNREAD_DIVIDER_H
        return 0

    def _paint_unread_divider(self, painter: QPainter, rect: QRect):
        """行首「以下为新消息」：左右细线 + 中间文字，不挡消息内容。"""
        fm = QFontMetrics(self.model_ref._small_font)
        cy = rect.top() + _UNREAD_DIVIDER_H // 2
        text = "以下为新消息"
        text_w = fm.horizontalAdvance(text)
        cx = rect.center().x()
        painter.save()
        painter.setPen(QPen(QColor(self._c("border", "#e7e7e7")), 1))
        left_end = cx - text_w // 2 - 10
        right_start = cx + text_w // 2 + 10
        if left_end > rect.left() + 16:
            painter.drawLine(rect.left() + 16, cy, left_end, cy)
        if right_start < rect.right() - 16:
            painter.drawLine(right_start, cy, rect.right() - 16, cy)
        painter.setPen(QColor(self._c("time_label", "#b0b0b0")))
        painter.setFont(self.model_ref._small_font)
        painter.drawText(QRect(cx - text_w // 2, cy - fm.height() // 2,
                               text_w, fm.height()),
                         Qt.AlignCenter, text)
        painter.restore()

    # ------------------------------------------------------------------ #
    # 系统消息行
    # ------------------------------------------------------------------ #
    def _paint_system_row(self, painter: QPainter, rect: QRect,
                          msg: Message, index: QModelIndex):
        y = rect.top() + self._divider_offset(index)
        if index.data(RoleShowTimeLabel):
            self._paint_time_label(painter, rect, msg.timestamp, y)
            y += self.model_ref.time_label_h()

        text = msg.display_text or msg.text or ""
        fm = QFontMetrics(self.model_ref._small_font)
        bubble_w, text_h = _system_bubble_metrics(
            text, rect.width(), self.model_ref._small_font)
        x = rect.left() + (rect.width() - bubble_w) // 2
        bubble_rect = QRect(x, y + _MARGIN_V, bubble_w, text_h)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(self._c("system_bubble_fill", "#e8e8e8")))
        painter.drawRoundedRect(bubble_rect, 8, 8)

        painter.setPen(QColor(self._c("system_bubble_text", "#999999")))
        painter.setFont(self.model_ref._small_font)
        if fm.horizontalAdvance(text) + 24 > bubble_w:
            text = fm.elidedText(text, Qt.ElideRight, bubble_w - 24)
        painter.drawText(bubble_rect, Qt.AlignCenter, text)

    def layout_rects(self, index: QModelIndex, row_rect: QRect):
        """算出一行的 (气泡矩形, 头像矩形, 图片内容矩形)。

        绘制和点击命中测试都必须走这里，两边各算一套迟早会错位。
        返回的都是视口坐标，与 QAbstractItemView.visualRect 同一坐标系；
        不适用的矩形返回 null。
        """
        msg = index.data(RoleMessage)
        if msg is None or msg.msg_type == "system":
            return QRect(), QRect(), QRect()

        y = row_rect.top() + self._divider_offset(index)
        if index.data(RoleShowTimeLabel):
            y += self.model_ref.time_label_h()
        y += _MARGIN_V

        is_mine = bool(index.data(RoleIsMine))
        show_avatar = bool(index.data(RoleShowAvatar))
        show_name = bool(index.data(RoleShowName))
        # 始终预留头像空间，保证连续消息气泡对齐
        avatar_w = _AVATAR_SIZE
        avatar_gap = _AVATAR_GAP
        name_h = _NAME_H if show_name else 0

        bubble_content_h = self.model_ref._calc_bubble_content_h(msg, index.row())

        # 气泡最大宽度
        max_bubble_w = (row_rect.width() - _MARGIN_L - _MARGIN_R
                        - avatar_w - avatar_gap - _TRI_W)
        image_like = _is_image_like(msg)
        if image_like:
            # 图片无气泡边框：气泡就是图片（+ 下方说明/回应条），不加内边距；
            # 有说明时气泡按说明自然宽度撑开，窄图说明不被挤窄
            img_w, _img_h = self.model_ref._get_image_display_size(msg)
            bubble_w = self.model_ref.image_bubble_w(msg, img_w, max_bubble_w)
            bubble_h = bubble_content_h
        else:
            bubble_w = max(50, min(_BUBBLE_MAX_W, max_bubble_w))
            # 根据内容收紧
            bubble_w = min(bubble_w,
                           self._calc_bubble_width(msg, bubble_w) + _BUBBLE_PAD * 2)
            bubble_h = bubble_content_h + _BUBBLE_PAD * 2
        # 回应条单行不换行：气泡太窄装不下时按 band 宽度撑开（与绘制同宽）
        if msg.reactions:
            band_max_w = bubble_w if image_like \
                else max(50, max_bubble_w - _BUBBLE_PAD * 2)
            band_w = self.model_ref.reaction_band_width(msg, band_max_w)
            need = band_w if image_like else band_w + _BUBBLE_PAD * 2
            bubble_w = min(max_bubble_w, max(bubble_w, need))

        if is_mine:
            bubble_x = row_rect.right() - _MARGIN_R - avatar_w - avatar_gap - _TRI_W - bubble_w
        else:
            bubble_x = row_rect.left() + _MARGIN_R + avatar_w + avatar_gap + _TRI_W
        bubble_rect = QRect(bubble_x, y + name_h, bubble_w, bubble_h)

        if show_avatar:
            if is_mine:
                ax = bubble_rect.right() + _TRI_W + _AVATAR_GAP
            else:
                ax = row_rect.left() + _MARGIN_R
            avatar_rect = QRect(ax, bubble_rect.top(), _AVATAR_SIZE, _AVATAR_SIZE)
        else:
            avatar_rect = QRect()

        if _is_image_like(msg):
            img_w, img_h = self.model_ref._get_image_display_size(msg)
            image_rect = QRect(bubble_rect.left(), bubble_rect.top(),
                               min(img_w, bubble_rect.width()), img_h)
        else:
            image_rect = QRect()
        return bubble_rect, avatar_rect, image_rect

    def quote_block_rect(self, index: QModelIndex, row_rect: QRect) -> QRect:
        """引用块矩形（视口坐标），供点击命中跳转。非引用消息返回 null。"""
        msg = index.data(RoleMessage)
        if msg is None or msg.msg_type != "quote":
            return QRect()
        bubble_rect, _avatar, _img = self.layout_rects(index, row_rect)
        if bubble_rect.isNull():
            return QRect()
        content = bubble_rect.adjusted(_BUBBLE_PAD, _BUBBLE_PAD,
                                       -_BUBBLE_PAD, -_BUBBLE_PAD)
        quote_h = self.model_ref.quote_block_height(msg, content.width())
        return QRect(content.left(), content.top(),
                     content.width(), min(quote_h, content.height()))

    # ------------------------------------------------------------------ #
    # 常规消息行
    # ------------------------------------------------------------------ #
    def _paint_message_row(self, painter: QPainter, rect: QRect,
                           msg: Message, index: QModelIndex):
        y = rect.top() + self._divider_offset(index)
        if index.data(RoleShowTimeLabel):
            self._paint_time_label(painter, rect, msg.timestamp, y)
            y += self.model_ref.time_label_h()

        is_mine = msg.is_mine
        show_name = bool(index.data(RoleShowName))
        bubble_rect, avatar_rect, _image_rect = self.layout_rects(index, rect)

        # 时间戳行高度（使用实际字体度量）
        tiny_fm = QFontMetrics(self.model_ref._tiny_font)
        time_row_h = tiny_fm.height() + 2
        bubble_x, bubble_w = bubble_rect.left(), bubble_rect.width()

        # 绘制发送者名称（在气泡上方）
        if show_name:
            sender_name = msg.sender.name or msg.sender.username or "?"
            painter.setPen(QColor(self._c("sender_name", "#888888")))
            painter.setFont(self.model_ref._small_font)
            name_rect = QRect(bubble_x, bubble_rect.top() - _NAME_H,
                              bubble_w, _NAME_H)
            painter.drawText(name_rect, Qt.AlignLeft | Qt.AlignTop, sender_name)

        # 绘制气泡背景（图片类无边框，图片自身铺满气泡矩形）
        if not _is_image_like(msg):
            if is_mine:
                bubble_color = QColor(self._c("bubble_mine", "#ffffff"))
                painter.setPen(Qt.NoPen)
            else:
                bubble_color = QColor(self._c("bubble_other", "#ffffff"))
                painter.setPen(QPen(QColor(self._c("bubble_other_border", "#e0e0e0")), 1))
            painter.setBrush(bubble_color)
            painter.drawRoundedRect(bubble_rect, _BUBBLE_RADIUS, _BUBBLE_RADIUS)

            # 绘制三角
            if is_mine:
                tri_color = QColor(self._c("bubble_mine", "#ffffff"))
                painter.setPen(Qt.NoPen)
            else:
                tri_color = QColor(self._c("bubble_other", "#ffffff"))
                painter.setPen(QPen(QColor(self._c("bubble_other_border", "#e0e0e0")), 1))
            painter.setBrush(tri_color)
            self._paint_triangle(painter, bubble_rect, is_mine, tri_color)

        # 绘制头像（与气泡顶部对齐）
        if not avatar_rect.isNull():
            sender_name = msg.sender.name or msg.sender.username or "?"
            self._paint_avatar(painter, avatar_rect.left(), avatar_rect.top(),
                               sender_name,
                               msg.sender.avatar_url if msg.sender else "",
                               msg_id=msg.id)

        # 绘制气泡内容
        self._paint_bubble_content(painter, bubble_rect, msg)

        # 绘制时间 + 状态（在气泡下方）。状态块几何与点击命中都来自
        # _status_chunk，两边必须同源（同 layout_rects 的约定）。
        time_y = bubble_rect.bottom() + 2
        time_text = format_chat_time(msg.timestamp)
        painter.setFont(self.model_ref._tiny_font)
        painter.setPen(QColor(self._c("time_label", "#b0b0b0")))
        chunk_rect, kind, unread = self._status_chunk(index, rect)
        if is_mine:
            # 时间右缘止于状态块左侧；矩形宽度不能跟着气泡收窄：
            # 窄气泡装不下全格式日期，drawText 会按矩形裁成 "2026-08-"
            right = (chunk_rect.left() - _STATUS_GAP
                     if not chunk_rect.isNull() else bubble_x + bubble_w)
            text_w = tiny_fm.horizontalAdvance(time_text)
            time_w = max(bubble_w, text_w)
            time_x = max(rect.left(), right - time_w)
            time_rect = QRect(time_x, time_y, right - time_x, time_row_h)
            painter.drawText(time_rect, Qt.AlignRight | Qt.AlignTop, time_text)
            # 标记贴右对齐的时间文字左侧。time_rect 左缘在宽气泡时是气泡
            # 左缘，用它定位会把图标甩到气泡另一端（真机截图问题）。
            marker_x = max(rect.left(),
                           right - text_w - _STATUS_GAP
                           - self._time_markers_width(tiny_fm, msg))
            self._paint_time_markers(painter, msg, marker_x,
                                     time_y, time_row_h)
            self._draw_chunk(painter, chunk_rect, kind, unread)
        elif not chunk_rect.isNull():
            # 别人消息的未读文本：时间左对齐于气泡左缘，未读文本在其右
            # （与 _status_chunk 的几何同源）
            text_w = tiny_fm.horizontalAdvance(time_text)
            painter.drawText(QRect(bubble_x, time_y, text_w, time_row_h),
                             Qt.AlignLeft | Qt.AlignTop, time_text)
            self._paint_time_markers(
                painter, msg, chunk_rect.right() + _STATUS_GAP,
                time_y, time_row_h)
            self._draw_chunk(painter, chunk_rect, kind, unread)
        else:
            # 保持左缘与气泡对齐，向右扩展（不越出行）
            text_w = tiny_fm.horizontalAdvance(time_text)
            time_w = min(max(bubble_w, text_w), rect.right() - bubble_x)
            time_rect = QRect(bubble_x, time_y, time_w, time_row_h)
            painter.drawText(time_rect, Qt.AlignLeft | Qt.AlignTop, time_text)
            self._paint_time_markers(
                painter, msg, bubble_x + text_w + _STATUS_GAP,
                time_y, time_row_h)

    def _time_markers(self, msg: Message) -> list:
        """时间行标记：(kind, payload)；kind 为 icon/text。"""
        items = []
        if getattr(msg, "pinned", False):
            items.append(("icon", "pin"))
        if getattr(msg, "starred", False):
            items.append(("icon", "favorite"))
        if self.model_ref._remind_map.get(msg.id):
            items.append(("icon", "remind"))
        if getattr(msg, "edited_at", 0):
            items.append(("text", "已编辑"))
        return items

    def _time_markers_width(self, fm: QFontMetrics, msg: Message) -> int:
        items = self._time_markers(msg)
        if not items:
            return 0
        total = sum(_TIME_MARK_ICON if kind == "icon"
                    else fm.horizontalAdvance(payload)
                    for kind, payload in items)
        return total + _TIME_MARK_GAP * (len(items) - 1)

    def _paint_time_markers(self, painter: QPainter, msg: Message,
                            x: int, y: int, row_h: int):
        """时间旁画「已编辑」与置顶/星标图标；几何不进入行高计算。"""
        items = self._time_markers(msg)
        if not items:
            return
        painter.save()
        painter.setFont(self.model_ref._tiny_font)
        for kind, payload in items:
            if kind == "icon":
                rect = QRect(x, y + (row_h - _TIME_MARK_ICON) // 2,
                             _TIME_MARK_ICON, _TIME_MARK_ICON)
                icon(payload, _TIME_MARK_ICON).paint(painter, rect)
                x += _TIME_MARK_ICON + _TIME_MARK_GAP
            else:
                fm = QFontMetrics(self.model_ref._tiny_font)
                w = fm.horizontalAdvance(payload)
                painter.setPen(QColor(self._c("text_tertiary", "#666666")))
                painter.drawText(QRect(x, y, w, row_h),
                                 Qt.AlignLeft | Qt.AlignTop, payload)
                x += w + _TIME_MARK_GAP
        painter.restore()

    def _draw_chunk(self, painter, chunk_rect, kind, unread):
        if kind == "unread_count":
            painter.setPen(QColor(self._c("text_tertiary", "#666666")))
            painter.drawText(chunk_rect, Qt.AlignLeft | Qt.AlignTop,
                             f"{unread}人未读")
        elif not chunk_rect.isNull():
            self._draw_status_icon(painter, kind, chunk_rect)

    def _status_chunk(self, index: QModelIndex, row_rect: QRect):
        """气泡下方状态块的 (rect, kind, unread)——绘制与点击命中的唯一
        几何来源（同 layout_rects 约定）。无状态时 rect 为 null。"""
        msg = index.data(RoleMessage)
        if msg is None:
            return QRect(), "none", 0
        kind, unread = self.model_ref.status_spec(msg, index.row())
        if kind == "none":
            return QRect(), kind, 0
        bubble, _avatar, _image = self.layout_rects(index, row_rect)
        if bubble.isNull():
            return QRect(), kind, 0
        tiny_fm = QFontMetrics(self.model_ref._tiny_font)
        time_y = bubble.bottom() + 2
        row_h = tiny_fm.height() + 2
        right = bubble.left() + bubble.width()
        if kind == "unread_count" and not msg.is_mine:
            # 别人消息气泡在左侧：时间左对齐于气泡左缘，未读文本紧跟
            # 时间右侧。放气泡右缘会把时间挤进行左缘裁掉
            t_w = tiny_fm.horizontalAdvance(format_chat_time(msg.timestamp))
            x = bubble.left() + t_w + _STATUS_GAP
            w = tiny_fm.horizontalAdvance(f"{unread}人未读")
            w = max(0, min(w, row_rect.right() - x))
            return QRect(x, time_y, w, row_h), kind, unread
        if kind == "unread_count":
            w = tiny_fm.horizontalAdvance(f"{unread}人未读")
            return (QRect(max(row_rect.left(), right - w), time_y, w, row_h),
                    kind, unread)
        return QRect(right - _STATUS_ICON,
                     time_y + (row_h - _STATUS_ICON) // 2,
                     _STATUS_ICON, _STATUS_ICON), kind, 0

    def status_rect(self, index: QModelIndex, row_rect: QRect) -> QRect:
        """「N人未读」的点击矩形（视口坐标）。仅群聊未读文本块可点，
        私聊的图标不可点。"""
        chunk_rect, kind, _unread = self._status_chunk(index, row_rect)
        return chunk_rect if kind == "unread_count" else QRect()

    def failed_status_rect(self, index: QModelIndex, row_rect: QRect) -> QRect:
        """失败红叉的点击矩形（重新发送）；非 failed 返回 null。"""
        chunk_rect, kind, _unread = self._status_chunk(index, row_rect)
        return chunk_rect if kind == "failed" else QRect()

    # ---- 音频内联播放（状态由 ChatView 驱动） ---- #
    def set_audio_state(self, msg_id: str, playing: bool, pos_ms: int,
                        dur_ms: int):
        self._audio_states[msg_id] = (playing, pos_ms, dur_ms)

    def clear_audio_state(self, msg_id: str, dur_ms: int = 0):
        """停止某条音频的播放态：进度复位，保留已知时长供第二行显示。"""
        self._audio_states[msg_id] = (False, 0, dur_ms)

    def audio_btn_rect(self, index, row_rect: QRect) -> QRect:
        """音频卡播放按钮的点击矩形（视口坐标）；非音频消息返回 null。

        与 _paint_audio_content 的按钮矩形同源（同一 content_rect 算法），
        两边各算一套迟早错位。
        """
        msg = index.data(RoleMessage)
        if msg is None or msg.msg_type != "audio":
            return QRect()
        bubble, _avatar, _img = self.layout_rects(index, row_rect)
        if bubble.isNull():
            return QRect()
        content = bubble.adjusted(_BUBBLE_PAD, _BUBBLE_PAD,
                                  -_BUBBLE_PAD, -_BUBBLE_PAD)
        _chips, reactions_h = self.model_ref.reaction_band(
            msg, max(50, content.width()))
        if reactions_h:
            content.setHeight(max(0, content.height() - reactions_h))
        return QRect(content.left(),
                     content.top() + (content.height() - _FILE_BADGE) // 2,
                     _FILE_BADGE, _FILE_BADGE)

    def reaction_rects(self, index: QModelIndex, row_rect: QRect) -> list:
        """回应 chip 的 (key, 视口矩形)；与绘制共用 reaction_band 布局。"""
        msg = index.data(RoleMessage)
        if msg is None or msg.msg_type == "system" or not msg.reactions:
            return []
        bubble, _avatar, _image = self.layout_rects(index, row_rect)
        if bubble.isNull():
            return []
        image_like = _is_image_like(msg)
        chips, _h = self.model_ref.reaction_band(
            msg, max(50, bubble.width()
                     - (0 if image_like else _BUBBLE_PAD * 2)))
        if not chips:
            return []
        if image_like:
            left, width = bubble.left(), bubble.width()
            _, img_h = self.model_ref._get_image_display_size(msg)
            top = bubble.top() + img_h + 1
        else:
            left = bubble.left() + _BUBBLE_PAD
            width = bubble.width() - _BUBBLE_PAD * 2
            top = bubble.bottom() - _BUBBLE_PAD - _h + 1
        out = []
        for key, _count, _mine, r in chips:
            if not key:
                continue
            rr = r.translated(left, top).intersected(
                QRect(left, top, max(0, width), _h))
            if rr.width() > 0:
                out.append((key, rr))
        return out

    def _draw_status_icon(self, painter: QPainter, kind: str, rect: QRect):
        """0..16 网格矢量现画状态图标（颜色随主题）。不走 ui/icons.py：
        那是全局单色缓存机制，这里颜色按状态变化且随行内联绘制。"""
        color_key = {"clock": "status_sent", "sent": "status_sent",
                     "read": "status_read", "failed": "status_failed",
                     }.get(kind, "status_sent")
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(QColor(self._c(color_key, "#b0b0b0")), 2.0)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.translate(rect.topLeft())
        painter.scale(rect.width() / 16.0, rect.height() / 16.0)
        if kind == "clock":
            painter.drawEllipse(QRectF(3.2, 3.2, 9.6, 9.6))
            painter.drawLine(QLineF(8, 8, 8, 4.6))
            painter.drawLine(QLineF(8, 8, 10.4, 9.2))
        elif kind == "read":
            painter.drawLine(QLineF(1.8, 8.6, 5.0, 11.8))
            painter.drawLine(QLineF(5.0, 11.8, 10.2, 4.6))
            painter.drawLine(QLineF(5.8, 8.6, 9.0, 11.8))
            painter.drawLine(QLineF(9.0, 11.8, 14.2, 4.6))
        elif kind == "failed":
            painter.drawLine(QLineF(4.2, 4.2, 11.8, 11.8))
            painter.drawLine(QLineF(11.8, 4.2, 4.2, 11.8))
        else:  # sent 单勾
            painter.drawLine(QLineF(3.2, 8.6, 6.8, 12.2))
            painter.drawLine(QLineF(6.8, 12.2, 12.8, 4.6))
        painter.restore()

    # ------------------------------------------------------------------ #
    # 气泡内容绘制
    # ------------------------------------------------------------------ #
    def _calc_bubble_content_h(self, msg: Message, row: int = -1) -> int:
        return self.model_ref._calc_bubble_content_h(msg, row)

    def _calc_bubble_width(self, msg: Message, max_w: int) -> int:
        """根据内容计算气泡实际宽度（不超过 max_w）。"""
        if _is_image_like(msg):
            img_w, _ = self.model_ref._get_image_display_size(msg)
            return img_w
        if msg.msg_type in ("file", "audio", "video"):
            # 文件卡自适应宽：短名保持基础宽，长名在上限内撑开完整显示，
            # 仍放不下时绘制端省略 + 悬停 tooltip 显示全名。
            # 返回的是内容宽（外层再加 2*_BUBBLE_PAD）
            fm = QFontMetrics(self.model_ref._font)
            fsm = QFontMetrics(self.model_ref._small_font)
            natural = (_FILE_BADGE + _FILE_TEXT_GAP
                       + max(fm.horizontalAdvance(_file_display_name(msg)),
                             fsm.horizontalAdvance(_file_card_size_text(msg))))
            w = max(_FILE_CARD_W, min(natural, _FILE_CARD_MAX_W))
            return min(w, max_w - _BUBBLE_PAD * 2)

        text = msg.text or msg.display_text or ""
        if not text:
            return 60
        if msg.msg_type in ("quote", "merge"):
            # 引用块 / 合并转发卡片宽度不可预知，一律用满宽，避免行被裁
            return max_w
        # 宽度与行高出自同一文档（提及替换后的文本比原文长）
        return self.model_ref.text_content_width(msg, max_w - _BUBBLE_PAD * 2)

    def _paint_bubble_content(self, painter: QPainter, rect: QRect,
                              msg: Message):
        # 底部回应条：chip 布局只从 reaction_band 取，与行高/命中共源；
        # 图片类 band 占满气泡宽（图片无内边距），其余避开左右内边距
        image_like = _is_image_like(msg)
        band_max_w = (rect.width() if image_like
                      else max(50, rect.width() - _BUBBLE_PAD * 2))
        chips, reactions_h = self.model_ref.reaction_band(msg, band_max_w)
        if image_like:
            # 图文混排：图片 + 下方说明文字（官方客户端同款），无说明时
            # 高度为 0，分支合并
            cap = self.model_ref.image_caption(msg) \
                if msg.msg_type == "image" else ""
            cap_h = 0
            cap_doc = None
            if cap:
                cap_doc = self.model_ref._caption_doc(cap, rect.width())
                cap_h = _CAPTION_GAP + int(cap_doc.size().height())
            img_h = max(1, rect.height() - reactions_h - cap_h)
            self._paint_image(painter,
                              QRect(rect.left(), rect.top(), rect.width(),
                                    img_h),
                              msg, is_video=msg.msg_type == "video")
            y = rect.top() + img_h
            if cap_doc is not None:
                y += _CAPTION_GAP
                painter.save()
                painter.translate(rect.left(), y)
                # 说明也支持文本选区（命中/取文本在 ChatView 同源构建）
                self._draw_doc_with_selection(painter, cap_doc, msg)
                painter.restore()
            band_rect = QRect(rect.left(), y + 1,
                              rect.width(), reactions_h)
            self._paint_reactions(painter, band_rect, chips)
            return

        content_rect = rect.adjusted(_BUBBLE_PAD, _BUBBLE_PAD,
                                     -_BUBBLE_PAD, -_BUBBLE_PAD)
        if reactions_h:
            # 内容区上让出 band，文件/音频等 AlignVCenter 不会压到 chip 上
            content_rect.setHeight(
                max(0, content_rect.height() - reactions_h))
        self._paint_bubble_body(painter, content_rect, msg)
        band_rect = QRect(rect.left() + _BUBBLE_PAD,
                          rect.bottom() - _BUBBLE_PAD - reactions_h + 1,
                          rect.width() - _BUBBLE_PAD * 2, reactions_h)
        self._paint_reactions(painter, band_rect, chips)

    def _paint_bubble_body(self, painter: QPainter, content_rect: QRect,
                           msg: Message):
        if msg.msg_type == "file":
            self._paint_file_content(painter, content_rect, msg)
            return

        if msg.msg_type == "audio":
            # 音频：专属卡片（播放钮 + 进度行）；无封面视频仍走文件卡
            self._paint_audio_content(painter, content_rect, msg)
            return

        if msg.msg_type == "video":
            # 无封面的视频与文件同款卡片（带封面视频走上方图片分支）
            self._paint_file_content(painter, content_rect, msg)
            return

        if msg.msg_type == "quote":
            self._paint_quote_content(painter, content_rect, msg)
            return

        if msg.msg_type == "merge":
            self._paint_merge_content(painter, content_rect, msg)
            return

        # 文字消息 — 使用 QTextDocument 渲染，与高度计算使用相同布局引擎
        text = msg.text or msg.display_text or ""
        if not text:
            return

        # 逐条转发：内容上方画「转发的消息」标记行，正文下移（与高度计算
        # 的 _FORWARD_TAG_H 保持同源）
        text_y = content_rect.top()
        if msg.is_forwarded:
            tag_rect = QRect(content_rect.left(), text_y,
                             content_rect.width(), _FORWARD_TAG_H)
            painter.setPen(QColor(self._c("text_tertiary", "#666666")))
            painter.setFont(self.model_ref._small_font)
            painter.drawText(tag_rect, Qt.AlignLeft | Qt.AlignVCenter,
                             "「转发的消息」")
            text_y += _FORWARD_TAG_H

        painter.setPen(QColor(self._c("text_primary", "#191919")))
        painter.setFont(self.model_ref._font)

        # 构建文档（统一处理 markdown + @提及 + 自动换行）
        doc = self._build_text_doc(msg, content_rect.width())
        painter.save()
        painter.translate(content_rect.left(), text_y)
        self._draw_doc_with_selection(painter, doc, msg)
        painter.restore()

    def _paint_reactions(self, painter: QPainter, rect: QRect, chips):
        if not chips or rect.height() <= 0:
            return
        model = self.model_ref
        emoji_fm = QFontMetrics(model._reaction_font)
        small_fm = QFontMetrics(model._small_font)
        for key, count, mine, chip_rect in chips:
            r = chip_rect.translated(rect.left(), rect.top())
            r = r.intersected(rect)
            if r.width() <= 1 or r.height() <= 1:
                continue
            bg = QColor(self._c(
                "reaction_mine_bg" if mine else "reaction_bg",
                "#e3f2fd" if mine else "#f0f2f5"))
            painter.setPen(QPen(
                QColor(self._c("reaction_border", "#dfe3e8")), 1))
            painter.setBrush(bg)
            painter.drawRoundedRect(QRectF(r).adjusted(0.5, 0.5, -0.5, -0.5),
                                    r.height() / 2, r.height() / 2)
            text_color = QColor(self._c(
                "reaction_mine_text" if mine else "text_secondary",
                "#1256b8" if mine else "#666666"))
            inner = r.adjusted(_REACTION_CHIP_PAD, 0, -_REACTION_CHIP_PAD, 0)
            if not key:
                painter.setFont(model._small_font)
                painter.setPen(text_color)
                painter.drawText(inner, Qt.AlignCenter, f"+{count}")
                continue
            emo, cnt = _reaction_parts(key, count)
            if not _reaction_is_emoji(emo):
                painter.setFont(model._small_font)
                painter.setPen(text_color)
                painter.drawText(inner, Qt.AlignCenter,
                                 f"{emo} {cnt}".strip())
                continue
            emo_w = emoji_fm.horizontalAdvance(emo)
            if emo_w <= 0 or inner.width() < emo_w:
                continue
            painter.setFont(model._reaction_font)
            painter.setPen(text_color)
            painter.drawText(QRect(inner.left(), r.top(), emo_w, r.height()),
                             Qt.AlignLeft | Qt.AlignVCenter, emo)
            if cnt:
                painter.setFont(model._small_font)
                painter.setPen(text_color)
                painter.drawText(inner, Qt.AlignRight | Qt.AlignVCenter,
                                 cnt)

    def _draw_doc_plain(self, painter: QPainter, doc,
                        color_key: str = "text_primary",
                        default: str = "#191919"):
        """带主题文字色的文档绘制。

        doc.drawContents 用的是 QApplication 全局调色板（黑字），不理会
        painter 的 pen——暗色主题只切 QSS 不动全局调色板，直接调它会画出
        深底黑字（对比度不足）。所有绘制消息文本的地方都必须走本方法或
        _draw_doc_with_selection。"""
        ctx = QAbstractTextDocumentLayout.PaintContext()
        ctx.palette.setColor(QPalette.Text, QColor(self._c(color_key, default)))
        doc.documentLayout().draw(painter, ctx)

    def _draw_doc_with_selection(self, painter: QPainter, doc, msg: Message):
        """绘制文档；本消息有文本选区时高亮选中段（文本消息与引用回复
        共用，选区位置存模型 sel_start/sel_end）。"""
        sel = self.model_ref
        ctx = QAbstractTextDocumentLayout.PaintContext()
        ctx.palette.setColor(QPalette.Text, QColor(self._c("text_primary", "#191919")))
        ctx.palette.setColor(QPalette.Highlight, QColor(self._c("selection_bg", "#3399ff")))
        ctx.palette.setColor(QPalette.HighlightedText, QColor(self._c("selection_text", "#ffffff")))
        if (sel.sel_msg_id == msg.id and sel.sel_start != sel.sel_end
                and sel.sel_merge_idx < 0):
            max_pos = max(0, doc.characterCount() - 1)
            a = min(sel.sel_start, sel.sel_end, max_pos)
            b = max(sel.sel_start, sel.sel_end)
            b = min(b, max_pos)
            if a < b:
                cursor = QTextCursor(doc)
                cursor.setPosition(a)
                cursor.setPosition(b, QTextCursor.KeepAnchor)
                # PyQt5 的 Selection 是字段式结构体，不能像 PyQt6 那样
                # 用 Selection(cursor, format) 构造（会直接 TypeError，
                # 气泡只剩背景、文字全丢）
                selection = QAbstractTextDocumentLayout.Selection()
                selection.cursor = cursor
                # Qt5 下 format 不设背景笔刷就没有高亮（不会回落到
                # palette.highlight），背景/前景都要显式给
                fmt = QTextCharFormat()
                fmt.setBackground(QColor(self._c("selection_bg", "#3399ff")))
                fmt.setForeground(QColor(self._c("selection_text", "#ffffff")))
                selection.format = fmt
                ctx.selections = [selection]
        doc.documentLayout().draw(painter, ctx)

    def _build_text_doc(self, msg: Message, width: int) -> QTextDocument:
        """构建与绘制一致的 QTextDocument（内容统一来自模型 build_text_doc）。"""
        doc = self.model_ref.build_text_doc(msg)
        doc.setTextWidth(max(50, width))
        return doc

    def _paint_quote_content(self, painter: QPainter, rect: QRect,
                             msg: Message):
        reply = msg.text or ""
        model = self.model_ref
        fm = QFontMetrics(model._small_font)

        # 引用块高度与换行必须走模型 quote_block_height / _quote_block_doc
        # 同一来源，行高不符就会裁掉长引用
        quote_h = model.quote_block_height(msg, rect.width())
        quote_rect = QRect(rect.left(), rect.top(), rect.width(), quote_h)

        # 灰色引用背景 + 细边框（浅色截图与灰底靠边框区分）
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(self._c("quote_bg", "#f0f0f0")))
        painter.drawRoundedRect(quote_rect, 6, 6)
        border = QColor(self._c("quote_border", "#c0c0c0"))
        border.setAlpha(110)
        painter.setPen(QPen(border, 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(
            QRectF(quote_rect).adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)

        # 左侧竖线（上下内缩，不压卡片圆角）
        painter.setPen(QPen(QColor(self._c("quote_border", "#c0c0c0")), 3))
        painter.drawLine(quote_rect.left() + 2, quote_rect.top() + 8,
                         quote_rect.left() + 2, quote_rect.bottom() - 8)

        painter.setFont(model._small_font)
        if msg.quote_image_url:
            # 微信式：缩略图在左，作者/说明在右列，说明完整换行
            metrics = model.quote_media_metrics(msg, rect.width())
            thumb_rect = metrics["thumb"].translated(quote_rect.topLeft())
            self._paint_quote_thumb(painter, thumb_rect, msg)
            if msg.quote_author:
                # 作者比说明淡一档，缩略图右侧两行才有层次
                painter.setPen(QColor(self._c("text_secondary", "#999999")))
                painter.drawText(
                    QRect(quote_rect.left() + metrics["col_x"],
                          quote_rect.top() + metrics["author_y"],
                          metrics["col_w"], metrics["author_h"]),
                    Qt.AlignLeft | Qt.AlignTop, msg.quote_author)
            if metrics["cap_doc"] is not None:
                painter.save()
                painter.translate(quote_rect.left() + metrics["col_x"],
                                  quote_rect.top() + metrics["cap_y"])
                self._draw_doc_plain(painter, metrics["cap_doc"],
                                     "text_tertiary", "#666666")
                painter.restore()
        else:
            x = quote_rect.left() + model.QUOTE_PAD_LEFT
            y = quote_rect.top() + model.QUOTE_PAD_TOP
            inner_w = rect.width() - model.QUOTE_PAD_LEFT - model.QUOTE_PAD_RIGHT
            painter.setPen(QColor(self._c("text_tertiary", "#666666")))
            file_name = model.quote_file_display(msg)
            if file_name:
                # 文件引用：作者行 + 文件图标 + 文件名（单行省略）
                if msg.quote_author:
                    painter.drawText(QRect(x, y, inner_w, fm.height()),
                                     Qt.AlignLeft | Qt.AlignTop,
                                     msg.quote_author)
                    y += fm.height() + 2
                self._paint_quote_file_line(
                    painter, QRect(x, y, inner_w, fm.height()), file_name)
            else:
                # 文本引用（完整换行绘制）
                doc = model._quote_block_doc(msg, rect.width())
                painter.save()
                painter.translate(x, y)
                self._draw_doc_plain(painter, doc,
                                     "text_tertiary", "#666666")
                painter.restore()
        y = quote_rect.bottom() + 4

        if reply:
            reply_rect = QRect(rect.left(), y, rect.width(),
                               rect.bottom() - y + 1)
            painter.setPen(QColor(self._c("text_primary", "#191919")))
            painter.setFont(self.model_ref._font)
            # 引用回复的文本也走统一的文档构建（支持 markdown + 提及），
            # 选区高亮与文本消息同一套；判定用原消息 id（reply_msg 是
            # 临时对象没有 id），doc 内容同为 msg.text，位置一致
            reply_msg = Message(text=reply, mentions=msg.mentions)
            doc = self._build_text_doc(reply_msg, reply_rect.width())
            painter.save()
            painter.translate(reply_rect.topLeft())
            self._draw_doc_with_selection(painter, doc, msg)
            painter.restore()

    def _paint_quote_thumb(self, painter: QPainter, box: QRect,
                           msg: Message):
        """引用块缩略图：按比例缩放后居中放进固定外框，不裁切。

        外框尺寸来自 quote_media_metrics，图片未到货画满框占位，到货
        重绘不改变行高。"""
        url = ""
        if self.image_loader:
            url = canon_url(self.image_loader.resolve(msg.quote_image_url))
        pm = None
        if url and self.image_loader:
            pm = self.image_loader.pixmap_for(url, box.width())
        if pm is not None and not pm.isNull():
            # 同 _paint_image：按物理像素缩放并回写 DPR，避免缩略图发糊
            dpr = _paint_dpr(painter)
            scaled = pm.scaled(int(box.width() * dpr),
                               int(box.height() * dpr),
                               Qt.KeepAspectRatio, Qt.SmoothTransformation)
            scaled.setDevicePixelRatio(dpr)
            dw = int(round(scaled.width() / dpr))
            dh = int(round(scaled.height() / dpr))
            target = QRect(box.left() + (box.width() - dw) // 2,
                           box.top() + (box.height() - dh) // 2, dw, dh)
            painter.save()
            path = QPainterPath()
            path.addRoundedRect(QRectF(target), 4, 4)
            painter.setClipPath(path)
            painter.drawPixmap(target, scaled)
            painter.restore()
            # 截图多是浅色，贴灰底会糊成一片，补一圈细边压轮廓
            border = QColor(self._c("quote_border", "#c0c0c0"))
            border.setAlpha(150)
            painter.save()
            painter.setPen(QPen(border, 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(
                QRectF(target).adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
            painter.restore()
            return
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(self._c("image_placeholder", "#d8d8d8")))
        painter.drawRoundedRect(box, 4, 4)
        failed = bool(url) and self.image_loader.is_failed(url, box.width())
        if url and not failed:
            self._img_pending.setdefault(url, set()).add(msg.id)

    def _paint_quote_file_line(self, painter: QPainter, rect: QRect,
                               name: str):
        """引用块里的文件行：文件图标 + 单行省略的文件名。"""
        pm = icon("files").pixmap(_QFILE_ICON, _QFILE_ICON)
        painter.drawPixmap(
            rect.left(), rect.top() + (rect.height() - pm.height()) // 2, pm)
        text_rect = rect.adjusted(_QFILE_ICON + 4, 0, 0, 0)
        text = _elide_one_line(self.model_ref._small_font, name,
                               text_rect.width())
        painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, text)

    def _paint_merge_content(self, painter: QPainter, rect: QRect,
                             msg: Message):
        """绘制合并转发「聊天记录」卡片。高度来自 merge_block_height，同源。"""
        entries = msg.forward_list or []
        x = rect.left()
        y = rect.top()
        fm = QFontMetrics(self.model_ref._small_font)
        name_h = fm.height()

        # 头部行：聊天记录 · N 条
        painter.setPen(QColor(self._c("text_tertiary", "#666666")))
        painter.setFont(self.model_ref._small_font)
        head_rect = QRect(x, y, rect.width(), 20)
        painter.drawText(head_rect, Qt.AlignLeft | Qt.AlignTop,
                         f"聊天记录 · {len(entries)} 条")
        y += 24 + 2
        sep = QColor(self._c("quote_border", "#c0c0c0"))
        sep.setAlpha(120)

        for idx, e in enumerate(entries):
            sender = (e.get("sender") or "未知").strip()
            ts = e.get("ts") or 0.0
            ts_text = (e.get("ts_text") or "").strip()
            content = e.get("text") or ""
            # 发送人 + 时间 一行灰字；优先使用发送端写好的可读时间文本
            name_line = sender
            if ts_text:
                name_line = f"{sender}  {ts_text}"
            elif ts:
                try:
                    name_line = f"{sender}  {format_chat_time(float(ts))}"
                except (ValueError, OSError):
                    name_line = sender
            painter.setPen(QColor(self._c("sender_name", "#888888")))
            painter.setFont(self.model_ref._small_font)
            painter.drawText(QRect(x, y, rect.width(), name_h),
                             Qt.AlignLeft | Qt.AlignTop, name_line)
            y += name_h + 2

            image_url = _merge_image_url(e)
            if image_url:
                # 图片条目：只画图，不渲染 text（高度来自 merge_block_height 同源）
                img_h = self.model_ref._merge_image_size(
                    image_url, rect.width())[1]
                self._paint_merge_image(
                    painter, QRect(x, y, rect.width(), img_h), image_url,
                    msg.id)
                y += img_h + 2
            elif content.strip():
                doc = self.model_ref._merge_content_doc(content, rect.width())
                doc_h = int(doc.size().height())
                content_rect = QRect(x, y, rect.width(), doc_h)
                painter.save()
                painter.translate(content_rect.topLeft())
                ctx = QAbstractTextDocumentLayout.PaintContext()
                ctx.palette.setColor(
                    QPalette.Text,
                    QColor(self._c("text_primary", "#191919")))
                sel = self.model_ref
                if (sel.sel_msg_id == msg.id and sel.sel_merge_idx == idx
                        and sel.sel_start != sel.sel_end):
                    max_pos = max(0, doc.characterCount() - 1)
                    a = min(sel.sel_start, sel.sel_end, max_pos)
                    b = min(max(sel.sel_start, sel.sel_end), max_pos)
                    if a < b:
                        cursor = QTextCursor(doc)
                        cursor.setPosition(a)
                        cursor.setPosition(b, QTextCursor.KeepAnchor)
                        fmt = QTextCharFormat()
                        fmt.setBackground(
                            QColor(self._c("selection_bg", "#3399ff")))
                        fmt.setForeground(
                            QColor(self._c("selection_text", "#ffffff")))
                        selection = QAbstractTextDocumentLayout.Selection()
                        selection.cursor = cursor
                        selection.format = fmt
                        ctx.selections = [selection]
                doc.documentLayout().draw(painter, ctx)
                painter.restore()
                y += doc_h + 2

            # 条目间细分隔线
            if idx < len(entries) - 1:
                painter.setPen(QPen(sep, 1))
                painter.drawLine(x, y, x + rect.width(), y)
                y += 5

    def _paint_merge_image(self, painter: QPainter, rect: QRect,
                           image_url: str, merge_msg_id: str):
        """绘制合并转发条目里的图片，未到货时画占位并登记等待。

        尺寸来自模型 _merge_image_size（与行高同源），图片加载逻辑与
        _paint_image 一致；到货后经 _img_pending 的 merge_msg_id 找回整条
        合并消息行重绘。
        """
        img_w, img_h = self.model_ref._merge_image_size(
            image_url, rect.width())
        img_rect = QRect(rect.left(), rect.top(), img_w, img_h)
        url = ""
        if image_url and self.image_loader:
            url = canon_url(self.image_loader.resolve(image_url))

        pm = None
        if url and self.image_loader:
            pm = self.image_loader.pixmap_for(url, img_w)
        if pm is not None and not pm.isNull():
            # 同 _paint_image：按物理像素缩放并回写 DPR，避免缩略图被上采样发糊
            dpr = _paint_dpr(painter)
            scaled = pm.scaled(int(img_w * dpr), int(img_h * dpr),
                               Qt.KeepAspectRatio, Qt.SmoothTransformation)
            scaled.setDevicePixelRatio(dpr)
            dw = int(scaled.width() / dpr)
            dh = int(scaled.height() / dpr)
            target = QRect(
                img_rect.left() + (img_rect.width() - dw) // 2,
                img_rect.top() + (img_rect.height() - dh) // 2,
                dw, dh)
            painter.save()
            path = QPainterPath()
            path.addRoundedRect(QRectF(img_rect), 4, 4)
            painter.setClipPath(path)
            painter.drawPixmap(target, scaled)
            painter.restore()
            return

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(self._c("image_placeholder", "#d8d8d8")))
        painter.drawRoundedRect(img_rect, 4, 4)
        failed = bool(url) and self.image_loader.is_failed(url, img_w)
        label = "📷 图片加载失败" if failed else "📷 图片加载中…"
        painter.setPen(QColor(self._c("text_tertiary", "#666666")))
        painter.setFont(self.model_ref._small_font)
        painter.drawText(img_rect, Qt.AlignCenter, label)
        if url and not failed and merge_msg_id:
            self._img_pending.setdefault(url, set()).add(merge_msg_id)

    def _paint_file_content(self, painter: QPainter, rect: QRect,
                            msg: Message):
        """文件卡：左侧类型徽标 + 右侧文件名/大小两行。

        高度来自 _file_card_h、宽度来自 _calc_bubble_width，三者同源，
        只改一边会裁剪或错位。
        """
        model = self.model_ref

        # 徽标：圆角方块垂直居中，类别底色 + 白字
        badge = QRect(rect.left(),
                      rect.top() + (rect.height() - _FILE_BADGE) // 2,
                      _FILE_BADGE, _FILE_BADGE)
        label, bg = _file_badge(msg)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(bg))
        painter.drawRoundedRect(badge, _FILE_BADGE_RADIUS, _FILE_BADGE_RADIUS)
        painter.setPen(QColor(_BADGE_FG))
        painter.setFont(_badge_fit_font(model._small_font, label,
                                        badge.width()))
        painter.drawText(badge, Qt.AlignCenter, label)

        # 文本列：文件名（ElideMiddle 保扩展名）+ 大小灰字
        text_x = badge.right() + 1 + _FILE_TEXT_GAP
        text_w = rect.right() + 1 - text_x
        if text_w <= 8:
            return
        fm = QFontMetrics(model._font)
        fsm = QFontMetrics(model._small_font)
        total_h = fm.height() + _FILE_NAME_GAP + fsm.height()
        top = rect.top() + (rect.height() - total_h) // 2
        name_rect = QRect(text_x, top, text_w, fm.height())
        size_rect = QRect(text_x, top + fm.height() + _FILE_NAME_GAP,
                          text_w, fsm.height())
        painter.setPen(QColor(self._c("text_primary", "#191919")))
        painter.setFont(model._font)
        painter.drawText(name_rect, Qt.AlignLeft | Qt.AlignVCenter,
                         _elide_one_line(model._font,
                                         _file_display_name(msg), text_w))
        size = _file_card_size_text(msg)
        painter.setPen(QColor(self._c("text_secondary", "#666666")))
        painter.setFont(model._small_font)
        painter.drawText(size_rect, Qt.AlignLeft | Qt.AlignVCenter, size)

    def _paint_audio_content(self, painter: QPainter, rect: QRect,
                             msg: Message):
        """音频卡：左圆形播放/暂停钮 + 右文件名/进度行。

        高度与文件卡同源（_file_card_h），进度条画在第二行内，行高不变；
        按钮矩形与 audio_btn_rect 命中共源。
        """
        model = self.model_ref
        playing, pos_ms, dur_ms = self._audio_states.get(
            msg.id, (False, 0, 0))

        # 播放钮：紫色圆底（沿用音频徽标类别色），白三角/白双竖条
        btn = QRect(rect.left(),
                    rect.top() + (rect.height() - _FILE_BADGE) // 2,
                    _FILE_BADGE, _FILE_BADGE)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#8e6bbf"))
        painter.drawEllipse(btn)
        painter.setBrush(QColor(_BADGE_FG))
        if playing:
            bar_w = btn.width() * 0.10
            bar_h = btn.height() * 0.42
            top = btn.top() + (btn.height() - bar_h) / 2
            x1 = btn.left() + btn.width() * 0.30
            painter.drawRect(QRectF(x1, top, bar_w, bar_h))
            painter.drawRect(QRectF(x1 + bar_w * 2.2, top, bar_w, bar_h))
        else:
            cx, cy = btn.center().x(), btn.center().y()
            r = btn.width() * 0.20
            painter.drawPolygon(QPolygonF([
                QPointF(cx - r * 0.6, cy - r),
                QPointF(cx - r * 0.6, cy + r),
                QPointF(cx + r * 1.2, cy)]))

        # 文本列：文件名行同文件卡；第二行 = 细进度条 + 时长
        text_x = btn.right() + 1 + _FILE_TEXT_GAP
        text_w = rect.right() + 1 - text_x
        if text_w <= 8:
            return
        fm = QFontMetrics(model._font)
        fsm = QFontMetrics(model._small_font)
        total_h = fm.height() + _FILE_NAME_GAP + fsm.height()
        top = rect.top() + (rect.height() - total_h) // 2
        name_rect = QRect(text_x, top, text_w, fm.height())
        size_rect = QRect(text_x, top + fm.height() + _FILE_NAME_GAP,
                          text_w, fsm.height())
        painter.setPen(QColor(self._c("text_primary", "#191919")))
        painter.setFont(model._font)
        painter.drawText(name_rect, Qt.AlignLeft | Qt.AlignVCenter,
                         _elide_one_line(model._font,
                                         _file_display_name(msg), text_w))

        if dur_ms > 0:
            text = (_fmt_mmss(pos_ms) + " / " + _fmt_mmss(dur_ms)
                    if pos_ms > 0 else _fmt_mmss(dur_ms))
            bar_w = max(20, text_w - fsm.horizontalAdvance(text) - 8)
            bar_h = 3
            bar_y = size_rect.top() + (size_rect.height() - bar_h) // 2
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(self._c("input_border", "#e0e0e0")))
            painter.drawRoundedRect(QRect(text_x, bar_y, bar_w, bar_h),
                                    1, 1)
            ratio = max(0.0, min(1.0, pos_ms / float(dur_ms)))
            if ratio > 0:
                painter.setBrush(QColor("#8e6bbf"))
                painter.drawRoundedRect(
                    QRect(text_x, bar_y, max(2, int(bar_w * ratio)), bar_h),
                    1, 1)
            painter.setPen(QColor(self._c("text_secondary", "#666666")))
            painter.setFont(model._small_font)
            painter.drawText(
                QRect(text_x + bar_w + 8, size_rect.top(),
                      text_w - bar_w - 8, size_rect.height()),
                Qt.AlignRight | Qt.AlignVCenter, text)
        else:
            painter.setPen(QColor(self._c("text_secondary", "#666666")))
            painter.setFont(model._small_font)
            painter.drawText(size_rect, Qt.AlignLeft | Qt.AlignVCenter,
                             "语音消息")

    def _paint_image(self, painter: QPainter, rect: QRect, msg: Message,
                     is_video: bool = False):
        """画图片/视频封面缩略图，未到货时画占位并登记等待。"""
        img_w, img_h = self.model_ref._get_image_display_size(msg)
        img_rect = QRect(rect.left(), rect.top(), img_w, img_h)
        url = ""
        raw = (msg.cover_url if is_video else msg.file_url) or ""
        if raw and self.image_loader:
            url = canon_url(self.image_loader.resolve(raw))

        pm = None
        if url and self.image_loader:
            pm = self.image_loader.pixmap_for(url, img_w)
        if pm is not None and not pm.isNull():
            # HiDPI 下按物理像素缩放并回写 DPR：缩略图已按 _HIDPI_SCALE
            # 冗余生成，这里若再降回逻辑尺寸（img_w×img_h）只会丢掉高清
            # 冗余，被屏幕二次上采样变糊（之前发糊的根因）。
            dpr = _paint_dpr(painter)
            scaled = pm.scaled(int(img_w * dpr), int(img_h * dpr),
                               Qt.KeepAspectRatio, Qt.SmoothTransformation)
            scaled.setDevicePixelRatio(dpr)
            dw = int(scaled.width() / dpr)
            dh = int(scaled.height() / dpr)
            target = QRect(
                img_rect.left() + (img_rect.width() - dw) // 2,
                img_rect.top() + (img_rect.height() - dh) // 2,
                dw, dh)
            # 圆角裁剪，与加载中/失败的占位块观感一致
            painter.save()
            path = QPainterPath()
            path.addRoundedRect(QRectF(img_rect), 4, 4)
            painter.setClipPath(path)
            if pm.hasAlphaChannel():
                # 透明底图片（表情包等）垫「主色渐变」底，与官方客户端一致
                # （真机截图逐像素拟合：官方垫底与贴纸形状无关、明暗分布
                # 同模糊自像负相关，是左亮主色→右暗的横向光晕）；不透明
                # 图片 hasAlphaChannel 为 False 不垫。
                backdrop = self._sticker_backdrop(
                    url or msg.id, pm, img_w, img_h, dpr)
                if backdrop is not None and not backdrop.isNull():
                    painter.drawPixmap(img_rect.topLeft(), backdrop)
            painter.drawPixmap(target, scaled)
            painter.restore()
            return

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(self._c("image_placeholder", "#d8d8d8")))
        painter.drawRoundedRect(img_rect, 4, 4)
        failed = bool(url) and self.image_loader.is_failed(url, img_w)
        if is_video:
            label = "🎬 封面加载失败" if failed else "🎬 封面加载中…"
        else:
            label = "📷 图片加载失败" if failed else "📷 图片加载中…"
        painter.setPen(QColor(self._c("text_tertiary", "#666666")))
        painter.setFont(self.model_ref._small_font)
        painter.drawText(img_rect, Qt.AlignCenter, label)
        if url and not failed:
            self._img_pending.setdefault(url, set()).add(msg.id)

    # 官方垫底的亮度走向（真机截图按 x 十等分实测，值=主色占比）；
    # Qt 渐变在 stop 间线性插值，用多 stop 逼近这条缓 S 衰减曲线
    _BACKDROP_STOPS = (
        (0.0, 1.0), (0.2, 0.85), (0.4, 0.5),
        (0.6, 0.16), (0.8, 0.05), (1.0, 0.04),
    )

    def _sticker_backdrop(self, key: str, pm: QPixmap,
                          img_w: int, img_h: int, dpr: float):
        """透明底图片（表情包）的主色渐变垫底（官方客户端同款）。

        官方垫底不是模糊自像：真机截图逐像素拟合证明其明暗分布与贴纸
        形状无关（左亮卡其→右黑，与居中的脸负相关），是取图片主色画的
        横向渐变光晕。这里取不透明像素均值作主色，亮端固定在左缘——
        官方的渐变方向是否随图片变化未验证（只有一个样本），先按固定
        方向实现。
        """
        k = (key, int(img_w), int(img_h), round(dpr, 3))
        hit = self._backdrop_cache.get(k)
        if hit is not None:
            return hit
        dom = self._dominant_color(pm)
        w = max(1, int(img_w * dpr))
        h = max(1, int(img_h * dpr))
        out = QPixmap(w, h)
        grad = QLinearGradient(0, 0, w, 0)
        for pos, f in self._BACKDROP_STOPS:
            grad.setColorAt(pos, QColor(
                int(dom[0] * f), int(dom[1] * f), int(dom[2] * f)))
        p = QPainter(out)
        p.fillRect(0, 0, w, h, grad)
        p.end()
        out.setDevicePixelRatio(dpr)
        if len(self._backdrop_cache) >= 64:
            self._backdrop_cache.clear()
        self._backdrop_cache[k] = out
        return out

    def _dominant_color(self, pm: QPixmap):
        """不透明像素的均值色（垫底主色）。缩到 64px 再算，控成本。"""
        img = pm.toImage()
        if img.width() > 64 or img.height() > 64:
            img = img.scaled(64, 64, Qt.KeepAspectRatio,
                             Qt.SmoothTransformation)
        # 分开赋值：Cython 3.3.0 在「多个纯对象目标的链式赋值」上生成
        # 代码有缺陷，最后一项 n 会留成 NULL，n += 1 直接访问违例
        # （打包版 1.3.x 启动即崩的根因）。源码模式不触发，别合并回去。
        rs = 0
        gs = 0
        bs = 0
        n = 0
        for y in range(img.height()):
            for x in range(img.width()):
                c = img.pixelColor(x, y)
                if c.alpha() < 128:
                    continue
                rs += c.red()
                gs += c.green()
                bs += c.blue()
                n += 1
        if not n:
            return (0, 0, 0)
        return (rs // n, gs // n, bs // n)

    # ------------------------------------------------------------------ #
    # 辅助绘制
    # ------------------------------------------------------------------ #
    def _paint_triangle(self, painter: QPainter, bubble_rect: QRect,
                        is_mine: bool, color: QColor):
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        if is_mine:
            # 自己的消息：三角在气泡右侧，指向右侧头像
            x = bubble_rect.right()
            y_mid = bubble_rect.top() + 14
            tri = QPolygon([
                QPoint(x, y_mid),
                QPoint(x, y_mid + _TRI_H),
                QPoint(x + _TRI_W, y_mid + _TRI_H // 2 + 2),
            ])
        else:
            # 对方的消息：三角在气泡左侧，指向左侧头像
            x = bubble_rect.left() - _TRI_W
            y_mid = bubble_rect.top() + 14
            tri = QPolygon([
                QPoint(x + _TRI_W, y_mid),
                QPoint(x + _TRI_W, y_mid + _TRI_H),
                QPoint(x, y_mid + _TRI_H // 2 + 2),
            ])
        painter.drawPolygon(tri)

    def _paint_avatar(self, painter: QPainter, x: int, y: int,
                      name: str, avatar_url: str = "", msg_id: str = ""):
        """画圆形头像；未到货时画首字占位并登记等待。"""
        rect = QRect(x, y, _AVATAR_SIZE, _AVATAR_SIZE)
        pm = None
        if avatar_url and self.avatar_loader:
            pm = self.avatar_loader.pixmap_for(avatar_url)

        painter.save()
        if pm is not None and not pm.isNull():
            # 撑满再裁，避免非正方形头像被拉扁；多余部分由圆形裁剪兜住
            path = QPainterPath()
            path.addEllipse(QRectF(rect))
            painter.setClipPath(path)
            sq = pm.scaled(_AVATAR_SIZE, _AVATAR_SIZE,
                           Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            painter.drawPixmap(rect.left() + (rect.width() - sq.width()) // 2,
                               rect.top() + (rect.height() - sq.height()) // 2,
                               sq)
            painter.restore()
            return

        # 未缓存：绘制占位（彩色方形圆角+首字）
        color = _avatar_color(name)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(QRectF(rect),
                                _AVATAR_SIZE // 5, _AVATAR_SIZE // 5)

        letter = (name[0].upper()) if name else "?"
        painter.setPen(QColor("white"))
        font = QFont()
        font.setPointSize(max(9, _AVATAR_SIZE // 2))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignCenter, letter)
        painter.restore()

        if (avatar_url and msg_id and self.avatar_loader
                and not self.avatar_loader.is_failed(avatar_url)):
            self._img_pending.setdefault(canon_url(avatar_url), set()).add(msg_id)

    def _paint_time_label(self, painter: QPainter, rect: QRect,
                          ts: float, y: int):
        text = format_chat_time(ts)
        painter.setPen(QColor(self._c("time_label", "#b0b0b0")))
        painter.setFont(self.model_ref._small_font)
        # 高度用字体度量：中文字体实际高度大于固定值，写死会把底部裁掉
        fm = QFontMetrics(self.model_ref._small_font)
        label_rect = QRect(rect.left(), y + 8, rect.width(), fm.height())
        painter.drawText(label_rect, Qt.AlignHCenter | Qt.AlignTop, text)

    def _text_height(self, text: str, available_width: int) -> int:
        if not text:
            return QFontMetrics(self.model_ref._font).height()
        doc = QTextDocument()
        doc.setDefaultFont(self.model_ref._font)
        doc.setPlainText(text)
        doc.setDocumentMargin(0)
        doc.setTextWidth(max(50, available_width))
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        doc.setDefaultTextOption(opt)
        return int(doc.size().height())
