"""日志系统：文件轮转 + Python 异常钩子 + Qt 消息路由 + 硬崩溃转储。

日志位置：<存储根目录>/logs/fchat.log（5MB x 3 个轮转备份，目录来源
见 core.paths；config.yaml/密钥本身永远留在系统默认配置目录）
崩溃转储：<存储根目录>/logs/crash.log（faulthandler，段错误/闪退时
         由 C 层直接写入各线程 Python 栈，进程被杀也能留证据）

在 main() 最早处调用 setup_logging()，之后所有模块
logging.getLogger(__name__) 即可。
"""
from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import os
import sys
import traceback

from core.paths import logs_dir

# LOG_DIR 必须保留为模块级常量：文件句柄随启动即打开，运行期迁移
# 存储路径只影响下一次启动（运行中不热切日志文件）
LOG_DIR = logs_dir()
LOG_FILE = os.path.join(LOG_DIR, "fchat.log")
CRASH_FILE = os.path.join(LOG_DIR, "crash.log")

_MAX_BYTES = 5 * 1024 * 1024
_BACKUPS = 3

# faulthandler 需要长期持有的文件句柄
_crash_fh = None


def _qt_message_handler(mode, context, message):
    """把 Qt 内部警告/错误（如 QPixmap 内存分配失败）路由进日志。"""
    log = logging.getLogger("qt")
    if mode >= 3:       # QtCriticalMsg / QtFatalMsg
        log.error("Qt: %s (%s:%s in %s)", message,
                  context.file, context.line, context.function)
    elif mode == 1:     # QtWarningMsg
        log.warning("Qt: %s", message)
    else:               # QtDebugMsg / QtInfoMsg
        log.debug("Qt: %s", message)


def _excepthook(exc_type, exc, tb):
    """未捕获 Python 异常：完整栈写入日志（含本地变量摘要）。"""
    try:
        # 限制栈深度防止 RecursionError 时格式化本身再爆栈
        formatted = "".join(traceback.format_exception(exc_type, exc, tb, limit=50))
    except BaseException:
        formatted = f"{exc_type.__name__}: {exc}"
    try:
        logging.getLogger("crash").critical("未捕获异常：%s", formatted)
    except BaseException:
        # 爆栈时 logging 内部处理会再次递归失败，退化为短消息直落 stderr
        try:
            sys.stderr.write(f"未捕获异常：{exc_type.__name__}: {exc}\n")
        except BaseException:
            pass
    try:
        _orig_excepthook(exc_type, exc, tb)
    except BaseException:
        pass


_orig_excepthook = sys.excepthook


def setup_logging(level=logging.INFO):
    """初始化全局日志。必须在 QApplication 创建前调用。"""
    global _crash_fh
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except OSError:
        pass

    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    try:
        rh = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUPS,
            encoding="utf-8")
        rh.setFormatter(fmt)
        root.addHandler(rh)
    except OSError:
        pass

    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(ch)

    # websocket-client 库每次握手失败都打 ERROR，重连风暴时会刷屏；
    # DDP 连接问题在 core.realtime 已有带退避的 WARNING 记录，这里静音
    logging.getLogger("websocket").setLevel(logging.CRITICAL)

    # 1) 未捕获 Python 异常
    sys.excepthook = _excepthook

    # 2) Qt 内部消息（QPixmap OOM 等 Python 捕获不到的错误）
    try:
        from PyQt5.QtCore import qInstallMessageHandler
        qInstallMessageHandler(_qt_message_handler)
    except Exception:  # noqa: BLE001
        pass

    # 3) 硬崩溃（段错误/abort）转储：进程被杀也能留下 Python 栈
    try:
        _crash_fh = open(CRASH_FILE, "a", encoding="utf-8", buffering=1)
        faulthandler.enable(file=_crash_fh, all_threads=True)
    except (OSError, RuntimeError):
        _crash_fh = None

    log = logging.getLogger("app")
    log.info("=" * 60)
    log.info("Fchat 启动 pid=%s python=%s", os.getpid(), sys.version.split()[0])
    return log
