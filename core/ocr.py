"""离线图片文字识别（RapidOCR / PP-OCRv4 ONNX）。

只在**本机**推理：不联网、不把图片字节交给任何服务端，所以绕开了
「AI 网关不支持多模态模型」的限制（实测网关该 token
只有 3 个纯文本模型，带图请求直接 400 not a multimodal model）。

工程约束：
- 引擎（onnxruntime session）非线程安全，且首次加载模型要 1~2 秒，
  所以进程内单例 + 一把串行锁；调用方一律放后台线程
  （`ui/api_workers.run_async`），绝不能在 Qt 槽里直接跑——CPU 上一次
  识别约 1~8 秒，会把主线程冻住。
- **依赖 main.py 顶部先于 PyQt5 预载系统 VC++ 运行库**：PyQt5/Qt5/bin
  捆的老 msvcp140/vcruntime140 会占用 DLL 解析，onnxruntime 的 pyd
  初始化直接失败（DLL 初始化例程失败），顺序错了整个功能不可用。
- `rapidocr-onnxruntime` 是可选依赖（单独放在 requirements-ocr.txt，
  模型随 wheel 自带约 15.8MB）。没装时 `is_available()` 为假、`recognize()` 抛
  `OcrUnavailable`，由 UI 提示补装，不能因为缺依赖就崩。
- 识别结果里中英文/数字之间的空格会丢（PP-OCR 识别模型不输出空格），
  属已知特性，不做还原。同理：检测器偶尔把**上下相邻两行**并成一个框
  （等宽终端截图的紧排表格里出现），框内没有字符级坐标，排版还原救不
  回来，只能整框照抄。
"""
from __future__ import annotations

import logging
import os
import statistics
import threading
from dataclasses import dataclass, field
from typing import List

log = logging.getLogger(__name__)

_MODULE = "rapidocr_onnxruntime"


class OcrUnavailable(Exception):
    """依赖没装或模型加载失败：属于「功能不可用」，UI 给安装指引。"""


class OcrError(Exception):
    """图片读不了或推理失败。"""


@dataclass
class OcrLine:
    text: str
    score: float = 0.0
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0

    @property
    def _char_w(self) -> float:
        """半角字符当量宽度：CJK 全角按 2 倍宽计，用来反推空格个数。"""
        if not self.w:
            return 0.0
        disp = sum(2 if ord(ch) > 0x2E80 else 1 for ch in self.text) or 1
        return self.w / disp


# ---- 版式还原阈值（相对量，与截图分辨率无关） ---- #
_ROW_OVERLAP = 0.4   # 垂直重叠达到较小框高的该比例：判定同一视觉行
_ROW_YC = 0.6        # 且 y 中心差不超过较小框高的该倍数（防高框跨行误并）
_BLANK_GAP = 0.6     # 行距超过该倍数行高：补一个空行（还原段落/表格空隙）
_SPACE_GAP = 0.15    # 框间距小于该倍数字宽：视为连排，不加空格
_MAX_SPACES = 24     # 缩进/框间空格数上限，防异常框把行顶飞


def _disp_len(text: str) -> int:
    """半角字符当量长度：CJK 全角按 2 计。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def _global_char_w(lines: List[OcrLine]) -> float:
    """全图统一的半角字符当量宽度（按字符数加权平均）。

    PP-OCR 检测框宽度误差大，单框自估字宽在同一字体下能从 8.7 跳到
    14.7（单字符框最离谱），空格数随之忽多忽少、表格列对不齐；全局
    统计用长框（≥3 当量字符）摊薄单框误差。无合格框时返回 0，调用方
    退回单框自估。"""
    total_w = 0.0
    total_n = 0
    for line in lines:
        n = _disp_len(line.text)
        if n >= 3 and line.w > 0:
            total_w += line.w
            total_n += n
    return total_w / total_n if total_n else 0.0


def _box_pad(lines: List[OcrLine], gw: float) -> float:
    """检测框相对文字的典型外扩量（中位数，钳到非负）。

    框边缘吃掉列间空白，直接用框间隙算空格会偏少；框外扩量近似恒定，
    按中位数补回。按加权均值构造时总和为 0（短框偏正、长框偏负），
    中位数恰好反映该偏差。"""
    if gw <= 0:
        return 0.0
    vals = [line.w - _disp_len(line.text) * gw
            for line in lines
            if _disp_len(line.text) >= 4 and line.w > 0]
    if not vals:
        return 0.0
    return max(0.0, statistics.median(vals))


def _group_rows(lines: List[OcrLine]) -> List[List[OcrLine]]:
    """检测框聚成视觉行：PP-OCR 把一行常拆成多个框，直接按 y 排会把
    同一行的碎片与相邻行交错。判定只对行内首框做，够用且便宜；再叠加
    y 中心邻近条件，防止高框（检测框常比行距高）把相邻行的矮框吸进来。"""
    rows: List[List[OcrLine]] = []
    for line in sorted(lines, key=lambda l: (l.y, l.x)):
        for row in rows:
            base = row[0]
            min_h = min(base.h, line.h or base.h or 1)
            overlap = (min(base.y + base.h, line.y + line.h)
                       - max(base.y, line.y))
            yc = abs((base.y + base.h / 2) - (line.y + line.h / 2))
            if overlap > _ROW_OVERLAP * min_h and yc <= _ROW_YC * min_h:
                row.append(line)
                break
        else:
            rows.append([line])
    return rows


def _format_layout(lines: List[OcrLine]) -> str:
    """按检测框坐标重建版式：行序、行内横向空格、段间空行、行首缩进。"""
    lines = list(lines)
    gw = _global_char_w(lines)
    pad = _box_pad(lines, gw)
    out = []
    prev_bottom = None
    for row in _group_rows(lines):
        row.sort(key=lambda l: l.x)
        top = row[0]
        h = top.h or 1.0
        if prev_bottom is not None and top.y - prev_bottom > _BLANK_GAP * h:
            out.append("")
        indent = 0
        unit0 = gw or top._char_w
        if unit0:
            indent = min(_MAX_SPACES, max(0, round(top.x / unit0)))
        buf = " " * indent
        right = None
        for line in row:
            unit = gw or line._char_w
            if right is not None and unit:
                gap = line.x - right + pad
                if gap > _SPACE_GAP * unit:
                    n = max(1, min(round(gap / unit), _MAX_SPACES))
                    buf += " " * n
            buf += line.text
            right = line.x + line.w
        out.append(buf)
        prev_bottom = max(l.y + l.h for l in row)
    return "\n".join(out)


@dataclass
class OcrResult:
    lines: List[OcrLine] = field(default_factory=list)

    @property
    def text(self) -> str:
        """尽量还原原始排版：视觉行顺序、横向空格、空行、行首缩进。"""
        return _format_layout([l for l in self.lines if l.text])

    def __len__(self) -> int:
        return len(self.lines)


_engine = None
# 建引擎与推理共用：onnxruntime session 并发调用会崩，串行最省事
_engine_lock = threading.Lock()


def is_available() -> bool:
    """依赖是否可导入（只查模块是否存在，不加载模型）。"""
    import importlib.util
    try:
        return importlib.util.find_spec(_MODULE) is not None
    except (ImportError, ValueError):
        return False


def is_loaded() -> bool:
    """引擎是否已建好（预热完成/识别过至少一次）。只用于决定提示语：
    已加载再提示「首次加载模型稍慢」会误导用户以为每张图都这么慢。
    读到的可能是预热线程刚写入的旧值，提示语场景无所谓。"""
    return _engine is not None


def _load_rapidocr():
    """引擎导入唯一入口：测试注入 fake 直接 patch 本函数，不碰 sys.modules
    ——预热定时器的并发真实 import 会被 import 机器按名无条件覆盖/删除该键
    （放置与失败清理都不管键里现在存的是谁），patch.dict 注入的 fake
    会被冲掉，from-import 落回真实模块。"""
    from rapidocr_onnxruntime import RapidOCR
    return RapidOCR


def _get_engine():
    global _engine
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is None:
            try:
                RapidOCR = _load_rapidocr()
            except Exception as e:  # noqa: BLE001
                raise OcrUnavailable(
                    f"未安装离线文字识别组件（{_MODULE}）：{e}") from e
            try:
                _engine = RapidOCR()
            except Exception as e:  # noqa: BLE001
                # 模型文件缺失/损坏同样按不可用处理，别当成识别失败
                raise OcrUnavailable(f"识别模型加载失败：{e}") from e
            log.info("OCR 引擎已加载（离线 PP-OCRv4）")
    return _engine


def reset_engine():
    """丢掉引擎单例（单测换 fake 引擎 / 释放内存时用）。"""
    global _engine
    with _engine_lock:
        _engine = None


def warmup() -> bool:
    """提前加载模型，避免首次识别多等 1~2 秒。

    只加载引擎不推理；依赖没装或加载失败一律静默返回 False（预热失败
    不该弹任何东西，真正识别时再按既有路径报「缺少组件」）。onnxruntime
    session 非线程安全，调用方必须放后台线程（run_async）。"""
    if not is_available():
        return False
    try:
        _get_engine()
        return True
    except Exception:  # noqa: BLE001
        log.info("OCR 引擎预热失败（首次识别时再加载）", exc_info=True)
        return False


def recognize(path: str) -> OcrResult:
    """识别一张本地图片。path 可以是无扩展名的磁盘缓存文件（按文件头解码）。"""
    if not path:
        raise OcrError("没有可识别的图片")
    if not os.path.isfile(path):
        raise OcrError("图片文件不存在或已被清理")
    engine = _get_engine()
    with _engine_lock:
        try:
            # 返回值是 (结果, 各阶段耗时列表)：第二项不是错误位，
            # 当成 err 判会每帧都误报「识别失败」（rapidocr 1.4 实测）
            out, elapse = engine(path)
        except OcrUnavailable:
            raise
        except Exception as e:  # noqa: BLE001
            raise OcrError(f"识别失败：{e}") from e
    lines: List[OcrLine] = []
    for item in out or []:
        try:
            box, text, score = item
        except (TypeError, ValueError):
            continue
        xs = [float(p[0]) for p in box] if box else [0.0]
        ys = [float(p[1]) for p in box] if box else [0.0]
        text = str(text or "").strip()
        if not text:
            continue
        lines.append(OcrLine(text=text, score=float(score or 0.0),
                             x=min(xs), y=min(ys),
                             w=max(xs) - min(xs), h=max(ys) - min(ys)))
    # 行序按左上角粗排；真正的视觉行聚合与空格/空行还原在 text 里做
    lines.sort(key=lambda l: (l.y, l.x))
    log.info("OCR 完成 行数=%d 字符=%d 推理耗时=%s", len(lines),
             sum(len(l.text) for l in lines), elapse)
    return OcrResult(lines=lines)
