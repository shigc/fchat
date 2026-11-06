"""右键菜单图标：用 QPainter 现画矢量小图标。

项目本身没有任何图标资源（无 .qrc / 无 PNG·SVG 目录），也不想为此引入
第三方素材，所以按 16x16 网格用描边方式画。图标颜色跟随主题，
亮/暗色下都能看清；生成结果按 (名字, 尺寸, 颜色) 缓存。
"""
from __future__ import annotations

from typing import Dict, Tuple

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

# 网格边长：所有画法都在 0.._GRID 坐标系里
_GRID = 16.0
# 超采样倍数，避免高分屏下发虚
_SS = 3

_text_color = QColor("#3b3b3b")
_stroke = 1.4

_CACHE: Dict[Tuple[str, int, str], QIcon] = {}

# 用户状态点：语义色由 ThemeManager 注入（随亮/暗主题），key 同 theme 色板
# 的 status_online/... 后缀（departed = 离职/停用）
_STATUS_COLORS: Dict[str, str] = {}
_STATUS_PX_CACHE: Dict[Tuple[str, int], QPixmap] = {}


def set_status_colors(colors: dict):
    """由 ThemeManager 在应用主题时调用；colors 是 {status_key: 颜色}。"""
    global _STATUS_COLORS
    _STATUS_COLORS = dict(colors or {})
    _STATUS_PX_CACHE.clear()


def status_pixmap(key: str, size: int = 10) -> QPixmap:
    """用户状态点：语义色实心圆 + 白描边（与头像底色隔开）；departed 加白色斜杠。"""
    pm = _STATUS_PX_CACHE.get((key, size))
    if pm is not None:
        return pm
    color = QColor(_STATUS_COLORS.get(key)
                   or _STATUS_COLORS.get("offline")
                   or "#b8b8b8")
    px = int(size * _SS)
    pm = QPixmap(px, px)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(px / _GRID, px / _GRID)
    ring = QPen(Qt.white)
    ring.setWidthF(1.6)
    p.setPen(ring)
    p.setBrush(color)
    p.drawEllipse(QRectF(2.8, 2.8, 10.4, 10.4))
    if key == "departed":
        ring2 = QPen(Qt.white)
        ring2.setWidthF(2.4)
        p.setPen(ring2)
        p.setBrush(Qt.NoBrush)
        _line(p, 5.4, 10.6, 10.6, 5.4)
    p.end()
    pm.setDevicePixelRatio(_SS)
    _STATUS_PX_CACHE[(key, size)] = pm
    return pm


def set_theme_colors(text, stroke: float = 1.4):
    """由 ThemeManager 在应用主题时调用，让图标跟着亮/暗色走。

    text 可以是十六进制字符串或 QColor（theme.py 不想依赖 Qt 类型，
    两种都接下来最省事）。
    """
    global _text_color, _stroke
    _text_color = text if isinstance(text, QColor) else QColor(str(text))
    _stroke = stroke
    _CACHE.clear()


def icon(name: str, size: int = 16, color=None, stroke: float | None = None) -> QIcon:
    """取一个矢量图标；名字未知时返回空图标（菜单会退化成纯文字）。

    color 显式给定（hex 字符串）时用该色渲染且不动全局主题色——供同一
    界面需要两种色态的场景（如导航按钮未选中灰 / 选中白）。stroke 覆盖
    全局描边宽（16px 网格单位）：1.4 的默认值在右键菜单里够用，但导航
    这类并排文本的场景细线会显得发灰发浅，需要加粗到与文字同视觉重量。"""
    global _text_color, _stroke
    c = _text_color if color is None else QColor(str(color))
    s = _stroke if stroke is None else stroke
    key = (name, size, c.name(), s)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    drawer = _DRAWERS.get(name)
    if drawer is None:
        return QIcon()
    old, old_s, _text_color, _stroke = _text_color, _stroke, c, s
    try:
        pm = _render(drawer, size)
    finally:
        _text_color, _stroke = old, old_s
    result = QIcon(pm)
    _CACHE[key] = result
    return result


def _render(drawer, size: int) -> QPixmap:
    px = int(size * _SS)
    pm = QPixmap(px, px)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(px / _GRID, px / _GRID)
    pen = QPen(_text_color)
    pen.setWidthF(_stroke)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    drawer(p)
    p.end()
    pm.setDevicePixelRatio(_SS)
    return pm


def _fill(p: QPainter, color: QColor = None):
    """切到填充样式（画实心小图形时用），画完由调用方复原。"""
    p.setPen(Qt.NoPen)
    p.setBrush(color or _text_color)


def _line(p: QPainter, x1, y1, x2, y2):
    p.drawLine(QPointF(x1, y1), QPointF(x2, y2))


def _arrow_head(p: QPainter, tip: QPointF, dx: float, dy: float, arm: float = 3.0):
    """在 tip 处画箭头。dx/dy 是箭头的**行进方向**，两翼画在来路一侧。"""
    import math
    length = math.hypot(dx, dy) or 1.0
    ux, uy = dx / length, dy / length
    for side in (1.0, -1.0):
        wx = tip.x() - ux * arm + (-uy) * arm * 0.6 * side
        wy = tip.y() - uy * arm + ux * arm * 0.6 * side
        _line(p, tip.x(), tip.y(), wx, wy)


# ---------------------------------------------------------------------- #
# 各个图标的画法
# ---------------------------------------------------------------------- #
def _pin(p: QPainter):
    """置顶：一枚图钉（圆帽 + 收拢到尖端的两条边）。"""
    p.drawEllipse(QRectF(4.8, 1.8, 6.4, 6.4))
    _line(p, 5.9, 7.3, 8.0, 13.8)
    _line(p, 10.1, 7.3, 8.0, 13.8)
    _line(p, 6.6, 9.9, 9.4, 9.9)


def _slash(p: QPainter):
    _line(p, 1.8, 14.2, 14.2, 1.8)


def _unpin(p: QPainter):
    """取消置顶：图钉 + 斜杠。"""
    _pin(p)
    _slash(p)


def _star_points():
    """五角星顶点：外圈 10 个点，内半径按黄金比例缩进。"""
    import math
    pts = []
    for i in range(10):
        radius = 6.0 if i % 2 == 0 else 2.6
        ang = math.radians(-90 + i * 36)
        pts.append(QPointF(8.0 + radius * math.cos(ang),
                          8.0 + radius * math.sin(ang)))
    return pts


def _favorite(p: QPainter):
    """收藏：实心五角星（收藏态高亮）。"""
    _fill(p)
    p.drawPolygon(_star_points())
    p.setPen(QPen(_text_color, _stroke))
    p.setBrush(Qt.NoBrush)


def _unfavorite(p: QPainter):
    """未收藏：描边五角星。"""
    p.drawPolygon(_star_points())


def _edit(p: QPainter):
    """编辑：斜置铅笔（笔杆 + 笔尖）。"""
    p.drawPolygon([QPointF(3.0, 13.0), QPointF(4.2, 9.4),
                   QPointF(10.8, 2.8), QPointF(13.2, 5.2),
                   QPointF(6.6, 11.8)])
    _line(p, 4.2, 9.4, 6.6, 11.8)


def _leave(p: QPainter):
    """退出频道：门框 + 向外的箭头。"""
    p.drawLine(QPointF(9.5, 2.2), QPointF(3.5, 2.2))
    p.drawLine(QPointF(3.5, 2.2), QPointF(3.5, 13.8))
    p.drawLine(QPointF(3.5, 13.8), QPointF(9.5, 13.8))
    _line(p, 6.5, 8.0, 14.0, 8.0)
    _arrow_head(p, QPointF(14.0, 8.0), 1.0, 0.0)


def _delete(p: QPainter):
    """删除会话：垃圾桶。"""
    _line(p, 3.0, 4.4, 13.0, 4.4)          # 盖沿
    _line(p, 6.4, 4.4, 6.4, 2.6)
    _line(p, 6.4, 2.6, 9.6, 2.6)
    _line(p, 9.6, 2.6, 9.6, 4.4)
    p.drawArc(QRectF(4.4, 10.4, 7.2, 6.0), 0, 180 * 16)   # 桶底
    _line(p, 4.6, 5.0, 5.2, 12.0)          # 桶身两侧
    _line(p, 11.4, 5.0, 10.8, 12.0)


def _copy(p: QPainter):
    """复制：两页相叠（后一页用淡色描边区分层次）。"""
    faded = QColor(_text_color)
    faded.setAlpha(120)
    p.setPen(QPen(faded, _stroke))
    p.drawRoundedRect(QRectF(6.0, 2.2, 7.4, 9.2), 1.4, 1.4)
    p.setPen(QPen(_text_color, _stroke))
    p.drawRoundedRect(QRectF(2.6, 5.0, 7.6, 9.0), 1.4, 1.4)


def _quote(p: QPainter):
    """引用回复：气泡 + 指向线。"""
    p.drawRoundedRect(QRectF(2.2, 2.4, 11.6, 7.6), 2.2, 2.2)
    p.drawPolygon([QPointF(5.0, 9.8), QPointF(6.6, 13.2), QPointF(8.0, 9.8)])
    _line(p, 4.8, 5.2, 11.2, 5.2)
    _line(p, 4.8, 7.4, 9.0, 7.4)


def _recall(p: QPainter):
    """撤回：U 形回退箭头。"""
    p.drawArc(QRectF(4.4, 2.2, 9.2, 8.6), 0, 180 * 16)     # 顶部半圆
    _line(p, 13.6, 6.5, 13.6, 10.4)                        # 右侧下行
    _line(p, 4.4, 6.5, 4.4, 11.6)                         # 左侧下行
    _arrow_head(p, QPointF(4.4, 13.8), 0.0, 1.0, arm=3.4)  # 箭头朝下


def _forward(p: QPainter):
    """转发：右折箭头。"""
    p.drawPolyline([QPointF(2.6, 13.0), QPointF(2.6, 8.2),
                    QPointF(12.0, 8.2)])
    _arrow_head(p, QPointF(12.6, 8.2), 1.0, 0.0, arm=3.4)


def _save_base(p: QPainter):
    """保存类图标共用的向下箭头 + 托盘。"""
    _line(p, 8.0, 2.0, 8.0, 9.4)
    _arrow_head(p, QPointF(8.0, 10.0), 0.0, 1.0, arm=3.2)
    _line(p, 2.8, 13.6, 13.2, 13.6)
    _line(p, 2.8, 13.6, 2.8, 11.0)
    _line(p, 13.2, 13.6, 13.2, 11.0)


def _save_image(p: QPainter):
    """保存图片：箭头 + 照片角标。"""
    _save_base(p)
    p.drawRoundedRect(QRectF(9.4, 1.4, 5.0, 4.0), 1.0, 1.0)
    _fill(p)
    p.drawEllipse(QRectF(10.6, 2.4, 1.2, 1.2))
    p.setPen(QPen(_text_color, _stroke))
    p.setBrush(Qt.NoBrush)


def _save_file(p: QPainter):
    """保存文件：箭头 + 文档角标。"""
    _save_base(p)
    p.drawPolyline([QPointF(9.6, 1.4), QPointF(12.6, 1.4),
                    QPointF(14.2, 3.0), QPointF(14.2, 5.6),
                    QPointF(9.6, 5.6), QPointF(9.6, 1.4)])
    _line(p, 12.5, 1.5, 12.5, 3.1)


def _save_video(p: QPainter):
    """保存视频：箭头 + 播放三角角标。"""
    _save_base(p)
    p.drawRoundedRect(QRectF(9.4, 1.4, 5.0, 4.0), 1.0, 1.0)
    _fill(p)
    p.drawPolygon([QPointF(10.8, 2.3), QPointF(10.8, 4.5), QPointF(13.0, 3.4)])
    p.setPen(QPen(_text_color, _stroke))
    p.setBrush(Qt.NoBrush)


# ---------------------------------------------------------------------- #
# 设置页图标（图标尺寸小，配色简单，保持描边风格）
# ---------------------------------------------------------------------- #
def _general(p: QPainter):
    """通用：齿轮（两个同心圆 + 八条短辐条）。"""
    p.drawEllipse(QRectF(6.2, 6.2, 3.6, 3.6))
    p.drawEllipse(QRectF(4.2, 4.2, 7.6, 7.6))
    import math
    for i in range(8):
        a = math.pi / 4 * i
        x0, y0 = 8.0 + 4.0 * math.cos(a), 8.0 + 4.0 * math.sin(a)
        x1, y1 = 8.0 + 5.3 * math.cos(a), 8.0 + 5.3 * math.sin(a)
        _line(p, x0, y0, x1, y1)


def _appearance(p: QPainter):
    """外观：调色板（圆盘，左下缺口处一个取样点）。"""
    p.drawRoundedRect(QRectF(2.6, 2.8, 10.8, 10.8), 5.4, 5.4)
    _fill(p)
    p.drawEllipse(QRectF(4.6, 4.8, 1.8, 1.8))
    p.drawEllipse(QRectF(8.4, 11.0, 1.8, 1.8))
    p.setPen(QPen(_text_color, _stroke))
    p.setBrush(Qt.NoBrush)


def _font(p: QPainter):
    """字体：字母 A。"""
    _line(p, 3.6, 13.4, 7.6, 2.6)
    _line(p, 11.6, 13.4, 7.6, 2.6)
    _line(p, 5.4, 8.4, 9.8, 8.4)


def _shortcuts(p: QPainter):
    """快捷键：键盘（圆角矩形 + 键位网格）。"""
    p.drawRoundedRect(QRectF(1.8, 3.4, 12.4, 9.2), 1.6, 1.6)
    for x, y in ((4.0, 6.4), (8.0, 6.4), (12.0, 6.4),
                 (4.0, 9.4), (8.0, 9.4), (12.0, 9.4)):
        p.drawEllipse(QRectF(x, y, 1.2, 1.2))


def _about(p: QPainter):
    """关于：信息圈 + i。"""
    p.drawEllipse(QRectF(2.6, 2.6, 10.8, 10.8))
    _fill(p)
    p.drawEllipse(QRectF(7.6, 5.0, 1.2, 1.2))
    _line(p, 8.0, 7.0, 8.0, 11.2)
    p.setPen(QPen(_text_color, _stroke))
    p.setBrush(Qt.NoBrush)


def _storage(p: QPainter):
    """存储：数据库椭圆柱（三层）。"""
    p.drawEllipse(QRectF(4.0, 2.2, 8.0, 3.0))
    _line(p, 4.0, 3.7, 4.0, 12.3)
    _line(p, 12.0, 3.7, 12.0, 12.3)
    p.drawArc(QRectF(4.0, 10.8, 8.0, 3.0), 180 * 16, 180 * 16)
    p.drawArc(QRectF(4.0, 6.5, 8.0, 3.0), 180 * 16, 180 * 16)


def _status(p: QPainter):
    """状态：人形 + 状态圆点。"""
    p.drawEllipse(QRectF(6.0, 2.4, 4.0, 4.0))          # 头
    p.drawArc(QRectF(4.0, 7.0, 8.0, 6.0), 0, 180 * 16)  # 肩
    _fill(p)
    p.drawEllipse(QRectF(11.0, 10.0, 3.0, 3.0))         # 状态点
    p.setPen(QPen(_text_color, _stroke))
    p.setBrush(Qt.NoBrush)


def _logout(p: QPainter):
    """退出登录：向左门箭头（区别于 leave 的通道）。"""
    p.drawLine(QPointF(4.0, 8.0), QPointF(15.0, 8.0))
    _line(p, 3.2, 2.6, 7.4, 8.0)
    _line(p, 7.4, 8.0, 3.2, 13.4)
    p.drawLine(QPointF(5.6, 2.2), QPointF(2.2, 2.2))
    p.drawLine(QPointF(2.2, 2.2), QPointF(2.2, 13.8))
    p.drawLine(QPointF(2.2, 13.8), QPointF(5.6, 13.8))


# ---------------------------------------------------------------------- #
# 输入框工具栏图标（发送/附件/表情/截图/预览）
# ---------------------------------------------------------------------- #
def _attach(p: QPainter):
    """附件：回形针。"""
    p.translate(0, -1.0)  # 光学中心偏下约 1.5px，上移 1 网格与其它图标共线
    p.drawPolyline([QPointF(2.6, 13.6),
                    QPointF(10.6, 5.4),
                    QPointF(4.6, 4.4), QPointF(4.6, 12.8),
                    QPointF(11.6, 12.8), QPointF(11.6, 6.0)])


def _emoji(p: QPainter):
    """表情：笑脸。"""
    p.drawEllipse(QRectF(2.5, 2.5, 11.0, 11.0))
    _fill(p)
    p.drawEllipse(QRectF(6.0, 6.2, 1.5, 1.5))
    p.drawEllipse(QRectF(8.9, 6.2, 1.5, 1.5))
    p.setPen(QPen(_text_color, _stroke))
    p.setBrush(Qt.NoBrush)
    p.drawArc(QRectF(5.4, 8.0, 5.6, 4.4), 20 * 16, 140 * 16)


def _screenshot(p: QPainter):
    """截图：相框 + 中间的山形取景。"""
    p.drawRoundedRect(QRectF(2.4, 3.0, 11.2, 10.0), 1.2, 1.2)
    _fill(p)
    p.drawEllipse(QRectF(4.6, 5.2, 1.6, 1.6))
    p.drawPolygon([QPointF(4.0, 11.6), QPointF(7.6, 6.2), QPointF(11.2, 11.6)])
    p.setPen(QPen(_text_color, _stroke))
    p.setBrush(Qt.NoBrush)


def _preview(p: QPainter):
    """预览：放大镜。"""
    p.translate(0, 2.0)  # 光学中心偏上约 3px，下移 2 网格与其它图标共线
    p.drawEllipse(QRectF(2.8, 2.0, 8.6, 8.6))
    _line(p, 9.8, 8.2, 13.8, 5.6)


def _send(p: QPainter):
    """发送：纸飞机。"""
    p.drawPolygon([QPointF(2.4, 8.2), QPointF(13.6, 2.4),
                   QPointF(10.6, 13.6), QPointF(8.6, 9.2)])
    _line(p, 2.4, 8.2, 8.6, 9.2)
    _line(p, 8.6, 9.2, 13.6, 2.4)


def _plus(p: QPainter):
    """新建：加号。"""
    _line(p, 8.0, 3.0, 8.0, 13.0)
    _line(p, 3.0, 8.0, 13.0, 8.0)


# ---------------------------------------------------------------------- #
# 房间面板图标（标题栏三按钮：成员/文件/聊天记录）
# ---------------------------------------------------------------------- #
def _members(p: QPainter):
    """成员：两人剪影（前实后人 + 右后淡色人）。"""
    faded = QColor(_text_color)
    faded.setAlpha(120)
    p.setPen(QPen(faded, _stroke))
    p.drawEllipse(QRectF(9.4, 3.4, 4.0, 4.0))
    p.drawArc(QRectF(8.6, 8.2, 5.8, 4.6), 40 * 16, 140 * 16)
    p.setPen(QPen(_text_color, _stroke))
    p.drawEllipse(QRectF(3.4, 2.6, 5.2, 5.2))
    p.drawArc(QRectF(2.2, 8.4, 7.6, 6.2), 0, 180 * 16)


def _files(p: QPainter):
    """文件：带折角的文档页。"""
    p.drawPolyline([QPointF(4.2, 2.2), QPointF(9.6, 2.2),
                    QPointF(12.4, 5.0), QPointF(12.4, 13.8),
                    QPointF(4.2, 13.8), QPointF(4.2, 2.2)])
    _line(p, 9.6, 2.2, 9.6, 5.0)
    _line(p, 9.6, 5.0, 12.4, 5.0)
    _line(p, 6.2, 8.0, 10.6, 8.0)
    _line(p, 6.2, 10.4, 10.6, 10.4)


def _search(p: QPainter):
    """聊天记录搜索：放大镜（居中，区别于 preview 的输入栏光学对齐）。"""
    p.drawEllipse(QRectF(2.8, 2.8, 8.2, 8.2))
    _line(p, 10.4, 10.4, 13.6, 13.6)


def _arrow_down(p: QPainter):
    """跳到底部：向下箭头。"""
    _line(p, 8.0, 2.6, 8.0, 12.2)
    p.drawPolyline([QPointF(3.6, 8.2), QPointF(8.0, 12.6),
                    QPointF(12.4, 8.2)])


# ---------------------------------------------------------------------- #
# 截图标注工具条
# ---------------------------------------------------------------------- #
def _rect(p: QPainter):
    """矩形：空心方框。"""
    p.drawRect(QRectF(3.0, 4.0, 10.0, 8.0))


def _ellipse(p: QPainter):
    """椭圆：空心圆。"""
    p.drawEllipse(QRectF(3.0, 4.0, 10.0, 8.0))


def _mark_unread(p: QPainter):
    """标为未读：实心圆点（未读红点语义）。"""
    _fill(p)
    p.drawEllipse(QRectF(4.0, 4.0, 8.0, 8.0))


def _arrow(p: QPainter):
    """箭头：斜向线 + 尖端两翼。"""
    _line(p, 3.4, 12.6, 12.4, 3.6)
    _line(p, 12.4, 3.6, 12.4, 8.2)
    _line(p, 12.4, 3.6, 7.8, 3.6)


def _pen(p: QPainter):
    """画笔：斜置笔杆 + 笔尖。"""
    p.drawPolyline([QPointF(2.8, 13.2), QPointF(3.8, 10.2),
                    QPointF(10.2, 3.8), QPointF(12.2, 5.8),
                    QPointF(5.8, 12.2), QPointF(2.8, 13.2)])


def _text(p: QPainter):
    """文字：字母 T。"""
    _line(p, 3.6, 3.8, 12.4, 3.8)
    _line(p, 8.0, 3.8, 8.0, 12.6)
    _line(p, 5.6, 12.6, 10.4, 12.6)


def _remind(p: QPainter):
    """定时提醒：表盘 + 时针分针。"""
    p.drawEllipse(QRectF(2.9, 2.9, 10.2, 10.2))
    _line(p, 8.0, 5.2, 8.0, 8.0)
    _line(p, 8.0, 8.0, 10.6, 9.6)


def _undo(p: QPainter):
    """撤销：左弯回转箭头。"""
    p.drawPolyline([QPointF(5.4, 4.6), QPointF(2.6, 7.4),
                    QPointF(5.4, 10.2)])
    p.drawArc(QRectF(2.6, 4.2, 10.8, 9.0), -60 * 16, 190 * 16)


def _check(p: QPainter):
    """完成：对勾。"""
    p.drawPolyline([QPointF(2.8, 8.6), QPointF(6.6, 12.4),
                    QPointF(13.2, 4.0)])


def _ocr(p: QPainter):
    """文字识别：三行文字 + 右下角放大镜（比 search 多了正文行）。"""
    faded = QColor(_text_color)
    faded.setAlpha(170)
    p.setPen(QPen(faded, _stroke))
    _line(p, 2.4, 3.6, 11.2, 3.6)
    _line(p, 2.4, 6.4, 9.4, 6.4)
    _line(p, 2.4, 9.2, 7.2, 9.2)
    p.setPen(QPen(_text_color, _stroke))
    p.drawEllipse(QRectF(7.6, 7.8, 4.6, 4.6))
    _line(p, 11.6, 11.8, 14.2, 14.4)


# ---------------------------------------------------------------------- #
# 目录面板图标（目录按钮 + 行图标：频道/团队）
# ---------------------------------------------------------------------- #
def _directory(p: QPainter):
    """目录：三条横线 + 左端小圆点（通讯录/索引观感）。"""
    faded = QColor(_text_color)
    faded.setAlpha(160)
    for y in (4.2, 8.0, 11.8):
        _fill(p, faded)
        p.drawEllipse(QRectF(2.4, y - 1.1, 2.2, 2.2))
        p.setPen(QPen(_text_color, _stroke))
        p.setBrush(Qt.NoBrush)
        _line(p, 6.4, y, 13.6, y)


def _hash(p: QPainter):
    """频道：井号（两竖两横，微斜更像官方 #）。"""
    _line(p, 6.0, 2.4, 4.6, 13.6)
    _line(p, 11.4, 2.4, 10.0, 13.6)
    _line(p, 2.8, 6.2, 13.4, 6.2)
    _line(p, 2.6, 10.0, 13.2, 10.0)


def _team(p: QPainter):
    """团队：旗帜（旗杆 + 波浪旗面）。"""
    _line(p, 3.6, 2.2, 3.6, 14.0)
    _line(p, 3.6, 2.8, 9.0, 2.8)
    p.drawArc(QRectF(7.0, 2.2, 5.6, 4.0), -90 * 16, 180 * 16)
    _line(p, 9.0, 6.2, 4.0, 6.2)
    _line(p, 3.6, 4.4, 4.0, 4.4)


def _question(p: QPainter):
    """确认弹窗：圆圈 + 问号（钩弧顺时针从左上绕到正下方，接竖笔 + 圆点）。"""
    p.drawEllipse(QRectF(2.6, 2.6, 10.8, 10.8))
    p.drawArc(QRectF(5.6, 4.6, 4.8, 4.8), 150 * 16, -240 * 16)
    _line(p, 8.0, 9.4, 8.0, 10.6)
    _fill(p)
    p.drawEllipse(QRectF(7.4, 11.7, 1.3, 1.3))
    p.setPen(QPen(_text_color, _stroke))
    p.setBrush(Qt.NoBrush)


def _close(p: QPainter):
    """关闭：斜十字。"""
    _line(p, 3.8, 3.8, 12.2, 12.2)
    _line(p, 12.2, 3.8, 3.8, 12.2)


def _min(p: QPainter):
    """最小化：底部横线。横线只有一笔，墨迹天然比双笔画图标少，
    笔画加粗到与其它图标视觉等重。"""
    old = p.pen()
    pen = QPen(old.color(), old.widthF() * 1.5)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    _line(p, 3.5, 11.6, 12.5, 11.6)
    p.setPen(old)


def _max(p: QPainter):
    """最大化：方框。"""
    p.drawRoundedRect(QRectF(3.5, 3.5, 9.0, 9.0), 1.0, 1.0)


def _restore(p: QPainter):
    """还原：前小方框 + 右上 L 形背框（叠放窗口）。"""
    _line(p, 6.2, 2.6, 13.4, 2.6)
    _line(p, 13.4, 2.6, 13.4, 9.8)
    p.drawRoundedRect(QRectF(2.6, 5.4, 8.0, 8.0), 1.0, 1.0)


def _more(p: QPainter):
    """更多菜单：竖三点。"""
    _fill(p)
    for y in (3.2, 8.0, 12.8):
        p.drawEllipse(QRectF(6.6, y - 1.4, 2.8, 2.8))
    p.setPen(QPen(_text_color, _stroke))
    p.setBrush(Qt.NoBrush)


def _chats(p: QPainter):
    """会话：对话气泡 + 左下尾巴 + 两个省略号点（微信式导航图标）。"""
    p.drawRoundedRect(QRectF(2.4, 2.8, 11.2, 8.6), 2.4, 2.4)
    p.drawPolygon([QPointF(4.6, 11.2), QPointF(6.8, 11.2),
                   QPointF(5.2, 13.6)])
    _fill(p)
    p.drawEllipse(QRectF(5.4, 6.3, 1.4, 1.4))
    p.drawEllipse(QRectF(9.2, 6.3, 1.4, 1.4))
    p.setPen(QPen(_text_color, _stroke))
    p.setBrush(Qt.NoBrush)


def _e2e_on(p: QPainter):
    """E2E 已开启：闭合挂锁（锁梁两腿都插进锁体）。"""
    p.drawRoundedRect(QRectF(3.4, 7.0, 9.2, 6.8), 1.6, 1.6)
    p.drawArc(QRectF(5.2, 2.2, 5.6, 6.4), 0, 180 * 16)
    _line(p, 5.2, 5.4, 5.2, 7.0)
    _line(p, 10.8, 5.4, 10.8, 7.0)
    _fill(p)
    p.drawEllipse(QRectF(7.3, 9.6, 1.4, 1.4))
    p.setPen(QPen(_text_color, _stroke))
    p.setBrush(Qt.NoBrush)


def _e2e_off(p: QPainter):
    """E2E 已关闭：开钩挂锁（锁梁右腿抬起，离开锁体）。"""
    p.drawRoundedRect(QRectF(3.4, 7.0, 9.2, 6.8), 1.6, 1.6)
    p.drawArc(QRectF(5.8, 1.6, 4.8, 4.8), 0, 180 * 16)
    _line(p, 5.8, 4.0, 5.8, 7.0)
    _line(p, 10.6, 4.0, 10.6, 2.2)
    _fill(p)
    p.drawEllipse(QRectF(7.3, 9.6, 1.4, 1.4))
    p.setPen(QPen(_text_color, _stroke))
    p.setBrush(Qt.NoBrush)


def _ai(p: QPainter):
    """AI：双四角星（主星 + 右上小星，代表生成式助手）。"""
    p.drawPolyline([
        QPointF(6.4, 2.4), QPointF(7.7, 6.5), QPointF(11.8, 7.8),
        QPointF(7.7, 9.1), QPointF(6.4, 13.2), QPointF(5.1, 9.1),
        QPointF(1.0, 7.8), QPointF(5.1, 6.5), QPointF(6.4, 2.4),
    ])
    p.drawPolyline([
        QPointF(12.2, 2.2), QPointF(12.8, 4.0), QPointF(14.6, 4.6),
        QPointF(12.8, 5.2), QPointF(12.2, 7.0), QPointF(11.6, 5.2),
        QPointF(9.8, 4.6), QPointF(11.6, 4.0), QPointF(12.2, 2.2),
    ])


_DRAWERS = {
    "pin": _pin,
    "unpin": _unpin,
    "edit": _edit,
    "favorite": _favorite,
    "unfavorite": _unfavorite,
    "leave": _leave,
    "delete": _delete,
    "copy": _copy,
    "quote": _quote,
    "recall": _recall,
    "forward": _forward,
    "save_image": _save_image,
    "save_file": _save_file,
    "save_video": _save_video,
    "general": _general,
    "appearance": _appearance,
    "font": _font,
    "shortcuts": _shortcuts,
    "about": _about,
    "storage": _storage,
    "status": _status,
    "logout": _logout,
    "attach": _attach,
    "emoji": _emoji,
    "screenshot": _screenshot,
    "preview": _preview,
    "send": _send,
    "plus": _plus,
    "members": _members,
    "files": _files,
    "search": _search,
    "arrow_down": _arrow_down,
    "rect": _rect,
    "ellipse": _ellipse,
    "mark_unread": _mark_unread,
    "arrow": _arrow,
    "pen": _pen,
    "text": _text,
    "remind": _remind,
    "undo": _undo,
    "check": _check,
    "ocr": _ocr,
    "directory": _directory,
    "ai": _ai,
    "hash": _hash,
    "team": _team,
    "question": _question,
    "close": _close,
    "min": _min,
    "max": _max,
    "restore": _restore,
    "more": _more,
    "chats": _chats,
    # 侧栏「设置」导航复用齿轮（与设置页「通用」同形）
    "settings": _general,
    "e2e_on": _e2e_on,
    "e2e_off": _e2e_off,
}
