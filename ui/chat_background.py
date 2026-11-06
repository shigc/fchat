"""聊天背景预设与渲染。

配置里只存一个字符串：""=主题默认底色、``preset:<id>``=内置预设、
其它=本地图片路径。预设全部用 QPainter 现画（项目不引入图片资源），
同一款预设按亮/暗主题各带一套配色，切换主题时自动换图。

纹理按逻辑像素定间距，渲染时乘以 devicePixelRatio，高 DPI 屏上
点线不会因为放大而糊成一片。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Tuple

from PyQt5.QtCore import QPointF, Qt
from PyQt5.QtGui import QColor, QLinearGradient, QPainter, QPen, QPixmap

PRESET_PREFIX = "preset:"


@dataclass(frozen=True)
class BackgroundPreset:
    """一款预设：kind 决定参数含义，light/dark 各一套。"""

    id: str
    name: str
    kind: str  # solid / gradient / dots / grid / stripes / speckle
    light: Tuple
    dark: Tuple


# 纯色 / 渐变取两端低饱和色；纹理参数 = (底色, 纹理色, 间距, alpha)
PRESETS: Tuple[BackgroundPreset, ...] = (
    BackgroundPreset("solid_mist", "雾白", "solid",
                     ("#F5F6F7",), ("#1E1F21",)),
    BackgroundPreset("solid_sand", "暖砂", "solid",
                     ("#F4EFE7",), ("#24201A",)),
    BackgroundPreset("solid_sage", "鼠尾草", "solid",
                     ("#E8EFE9",), ("#1C231E",)),
    BackgroundPreset("solid_blue", "雾蓝", "solid",
                     ("#E7EFF4",), ("#1A2126",)),
    BackgroundPreset("solid_rose", "藕粉", "solid",
                     ("#F5ECEF",), ("#241C1F",)),
    BackgroundPreset("solid_lilac", "紫灰", "solid",
                     ("#EFECF3",), ("#211E26",)),
    BackgroundPreset("grad_dawn", "晨雾", "gradient",
                     ("#E9F1F5", "#F7F2EC"), ("#1A2025", "#221F1B")),
    BackgroundPreset("grad_lime", "青柠雾", "gradient",
                     ("#E8F1EA", "#F5F7F0"), ("#19231D", "#20261F")),
    BackgroundPreset("grad_dusk", "暮霞", "gradient",
                     ("#F7EEE9", "#EEEAF4"), ("#241E1B", "#211D26")),
    BackgroundPreset("grad_lake", "湖光", "gradient",
                     ("#E5EFF2", "#EDF2EF"), ("#182226", "#1D2422")),
    BackgroundPreset("grad_warm", "暖灰", "gradient",
                     ("#F3EFEA", "#E9E6E2"), ("#221F1B", "#1A1816")),
    BackgroundPreset("pat_dots", "细点阵", "dots",
                     ("#F4F6F7", "#8A9BA8", 20, 38),
                     ("#1F2123", "#8FA3AE", 20, 30)),
    BackgroundPreset("pat_grid", "细网格", "grid",
                     ("#F5F4F1", "#B9B2A6", 24, 26),
                     ("#211F1C", "#6E675C", 24, 32)),
    BackgroundPreset("pat_stripes", "斜纹", "stripes",
                     ("#F3F5F4", "#A9B8B2", 18, 24),
                     ("#1E211F", "#5F726A", 18, 28)),
    BackgroundPreset("pat_speckle", "细砂", "speckle",
                     ("#F5F3EF", "#B7AFA2", 12, 30),
                     ("#1F1E1C", "#8C8579", 12, 28)),
)

PRESET_BY_ID = {p.id: p for p in PRESETS}

# 只保留最近一张原图：窗口拖动时 resize 会反复渲染，不能每次读盘
_image_cache = {"key": None, "pixmap": None}


def preset_value(preset_id: str) -> str:
    return PRESET_PREFIX + preset_id


def theme_mode(theme) -> str:
    """取 "light"/"dark"；兼容注入 ThemeManager 或字符串。"""
    if isinstance(theme, str):
        return "dark" if theme == "dark" else "light"
    mode = getattr(theme, "theme", "light")
    return "dark" if mode == "dark" else "light"


def render_background(value: str, width: int, height: int,
                      theme="light", dpr: float = 1.0) -> Optional[QPixmap]:
    """把配置值渲染成视口尺寸的背景图。

    返回 None 表示没有背景图（默认底色 / 未知预设 / 图片失效），
    由调用方回落到主题 chat_bg 纯色。
    """
    w = max(1, int(round(width * dpr)))
    h = max(1, int(round(height * dpr)))
    value = value or ""
    pixmap = None
    if value.startswith(PRESET_PREFIX):
        preset = PRESET_BY_ID.get(value[len(PRESET_PREFIX):])
        if preset is not None:
            pixmap = _render_preset(preset, w, h, theme_mode(theme), dpr)
    elif value and os.path.isfile(value):
        pixmap = _render_image(value, w, h)
    if pixmap is not None:
        pixmap.setDevicePixelRatio(dpr)
    return pixmap


def _render_preset(preset: BackgroundPreset, w: int, h: int,
                   mode: str, dpr: float) -> QPixmap:
    colors = preset.dark if mode == "dark" else preset.light
    pm = QPixmap(w, h)
    if preset.kind == "solid":
        pm.fill(QColor(colors[0]))
        return pm

    painter = QPainter(pm)
    try:
        if preset.kind == "gradient":
            grad = QLinearGradient(0, 0, w, h)
            grad.setColorAt(0.0, QColor(colors[0]))
            grad.setColorAt(1.0, QColor(colors[1]))
            painter.fillRect(0, 0, w, h, grad)
        else:
            base, ink, step, alpha = colors
            painter.fillRect(0, 0, w, h, QColor(base))
            painter.setRenderHint(QPainter.Antialiasing)
            step_px = max(4.0, float(step) * dpr)
            color = QColor(ink)
            color.setAlpha(int(alpha))
            if preset.kind == "dots":
                _paint_dots(painter, w, h, step_px, color, dpr)
            elif preset.kind == "grid":
                _paint_grid(painter, w, h, step_px, color, dpr)
            elif preset.kind == "stripes":
                _paint_stripes(painter, w, h, step_px, color, dpr)
            else:
                _paint_speckle(painter, w, h, step_px, color)
    finally:
        painter.end()
    return pm


def _paint_dots(painter: QPainter, w: int, h: int, step: float,
                color: QColor, dpr: float):
    # 点直径只有 1~2px，抗锯齿椭圆每帧上万个太慢；方点在这个尺寸下
    # 肉眼无差别，drawPoint 快一个量级
    painter.setPen(QPen(color, max(1.0, 1.6 * dpr)))
    y = step / 2
    row = 0
    while y < h + step:
        x = step / 2 + (step / 2 if row % 2 else 0.0)
        while x < w + step:
            painter.drawPoint(QPointF(x, y))
            x += step
        y += step
        row += 1


def _paint_grid(painter: QPainter, w: int, h: int, step: float,
                color: QColor, dpr: float):
    pen = QPen(color)
    pen.setWidthF(max(1.0, dpr))
    painter.setPen(pen)
    x = step
    while x < w:
        painter.drawLine(QPointF(x, 0), QPointF(x, h))
        x += step
    y = step
    while y < h:
        painter.drawLine(QPointF(0, y), QPointF(w, y))
        y += step


def _paint_stripes(painter: QPainter, w: int, h: int, step: float,
                   color: QColor, dpr: float):
    pen = QPen(color)
    pen.setWidthF(max(1.0, dpr))
    painter.setPen(pen)
    x = -h
    while x < w + h:
        painter.drawLine(QPointF(x, 0), QPointF(x + h, h))
        x += step


def _paint_speckle(painter: QPainter, w: int, h: int, step: float,
                   color: QColor):
    """细砂纹理：固定线性同余种子，保证同一尺寸每次生成一致。"""
    painter.setPen(QPen(color, max(1, int(step / 12))))
    seed = 0x9E3779B9
    count = max(1, int(w * h / (step * step)))
    for _ in range(count):
        seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
        x = seed % w
        seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
        y = seed % h
        painter.drawPoint(x, y)


def _render_image(path: str, w: int, h: int) -> Optional[QPixmap]:
    """本地图片 cover 裁剪 + 深色罩，保证白/暗气泡与文字对比度。"""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    key = (path, mtime)
    if _image_cache["key"] != key:
        src = QPixmap(path)
        _image_cache["key"] = key if not src.isNull() else None
        _image_cache["pixmap"] = src if not src.isNull() else None
    src = _image_cache["pixmap"]
    if src is None or src.isNull():
        return None
    scaled = src.scaled(w, h, Qt.KeepAspectRatioByExpanding,
                        Qt.SmoothTransformation)
    canvas = QPixmap(w, h)
    painter = QPainter(canvas)
    painter.drawPixmap((w - scaled.width()) // 2,
                       (h - scaled.height()) // 2, scaled)
    painter.fillRect(canvas.rect(), QColor(0, 0, 0, 55))
    painter.end()
    return canvas
