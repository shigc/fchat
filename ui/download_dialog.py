"""文件下载进度弹窗：微信式无边框卡片 + 实时进度条 + 取消。

**非模态**：下载期间主窗口仍可操作/切换（旧版用模态 exec_ 会挡住主程序）。
下载在 QThread 中流式进行，进度经 worker 信号投递到主线程（worker 信号连接到
本对话框的 pyqtSlot，跨线程自动 QueuedConnection，与 updater_dialog /
api_workers 同款做法）。写入 .part 再原子改名，取消或失败清理残件。取消用
threading.Event：进度回调里检测到即抛，请求循环随之终止（网络读最长阻塞到
超时，故取消不 wait 线程，等 failed 回来再真正关窗）。

进度总量优先取响应头 Content-Length；服务端（经反向代理/chunked）不给时回退
到调用方传入的已知文件大小（消息附件的 file_size），这样百分比与完整大小
仍能显示；两者都拿不到才退化为不确定总量的忙碌指示。
"""
from __future__ import annotations

import logging
import os
import threading

from PyQt5.QtCore import QObject, Qt, QThread, pyqtSignal, pyqtSlot
from PyQt5.QtWidgets import QHBoxLayout, QProgressBar

from core.url_guard import (
    MAX_REDIRECT_HOPS, is_redirect, redirect_target, same_origin)
from .dialog_base import FramelessCardDialog
from .widgets import WxLabel, WxPushButton, _color

log = logging.getLogger(__name__)

# force_close 后调用方会清空 _save_dialogs；弹窗是无父顶层窗口，若这里
# 不留强引用，Python GC 会连同仍在跑的 QThread 一起析构并报
# "QThread: Destroyed while thread is still running"。进程随后经
# main.py 的 os._exit 退出，不需要回收。
_closing_dialogs: list = []


def _fmt_size(n: int) -> str:
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n} B"


class _Cancelled(Exception):
    """用户请求取消（内部控制流，不外泄到 failed 文案之外）。"""


class _FileDownloadWorker(QObject):
    """工作线程流式下载文件到 .part 再原子改名，进度/结果经信号回主线程。"""

    progress = pyqtSignal(int, int)   # (已下载字节, 总字节；总量未知为 0)
    finished_ok = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, url: str, dest: str, known_total: int = 0,
                 headers=None, verify=True, parent=None):
        super().__init__(parent)
        self._url = url
        self._dest = dest
        try:
            self._known_total = int(known_total or 0)
        except (TypeError, ValueError):
            self._known_total = 0
        self._headers = headers or {}
        self._verify = verify
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    @staticmethod
    def _remove(path):
        try:
            os.remove(path)
        except OSError:
            pass

    def run(self):
        import requests
        part = self._dest + ".part"
        try:
            d = os.path.dirname(self._dest)
            if d:
                os.makedirs(d, exist_ok=True)
            # 跳转手动跟，不用 allow_redirects：requests 跨主机只剥
            # Authorization，附件用的 X-Auth-Token/X-User-Id 会跟着 302
            # 原样送到别的主机。跳到同源就保留原请求头，跳出去就丢掉。
            target, headers = self._url, self._headers
            for _ in range(MAX_REDIRECT_HOPS + 1):
                with requests.get(target, stream=True, timeout=60,
                                  headers=headers,
                                  verify=self._verify,
                                  allow_redirects=False) as resp:
                    nxt = redirect_target(resp, target)
                    if nxt:
                        headers = self._headers if same_origin(
                            nxt, target) else {}
                        target = nxt
                        continue
                    if is_redirect(resp.status_code):
                        raise OSError("重定向没有给出可下载的目标地址")
                    resp.raise_for_status()
                    try:
                        total = int(resp.headers.get("Content-Length") or 0)
                    except (TypeError, ValueError):
                        total = 0
                    if total <= 0:
                        total = self._known_total   # 头部没给总量 → 用已知文件大小
                    done = 0
                    with open(part, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=64 * 1024):
                            if self._cancel.is_set():
                                raise _Cancelled()
                            if not chunk:
                                continue
                            f.write(chunk)
                            done += len(chunk)
                            self.progress.emit(done, total)
                    break
            else:
                raise OSError(f"重定向超过 {MAX_REDIRECT_HOPS} 跳")
            os.replace(part, self._dest)
        except _Cancelled:
            self._remove(part)
            self.failed.emit("已取消")
        except Exception as e:  # noqa: BLE001 —— 网络/磁盘异常降级为 failed 文案
            self._remove(part)
            log.warning("文件下载失败：%s", e)
            self.failed.emit(str(e))
        else:
            self.finished_ok.emit()


class FileDownloadDialog(FramelessCardDialog):
    """非模态下载进度弹窗。用 show() 打开，完成/失败经信号通知调用方。

    信号：succeeded(dest) 下载完成；failed(err) 失败或取消（取消文案为「已取消」）。
    成功自动关窗，失败保留窗口显示错误、取消键变「关闭」。进度条随主题取 accent 色。
    """

    succeeded = pyqtSignal(str)   # 目标路径
    failed = pyqtSignal(str)      # 错误/取消文案

    def __init__(self, url: str, dest: str, name: str = "", total: int = 0,
                 headers=None, verify=True, theme=None, parent=None,
                 center_on=None):
        super().__init__("正在下载", theme, parent, width=420)
        self.dest = dest
        self.ok = False
        self.error = ""
        self._user_cancelled = False
        self._closing = False
        # 不认 parent 作窗口归属：无 parent 才是独立顶层窗口，切到主程序时
        # 能被压到主窗口之后（有 parent 的子窗口会被窗口管理器钉在最上层）。
        # center_on 只用来算初始居中位置（取其顶层主窗口中心）。
        self._center_on = center_on

        # 非模态：不阻塞主窗口（基类默认 setModal(True)，这里覆盖）
        self.setModal(False)
        self.setWindowModality(Qt.NonModal)

        self._name = WxLabel(name or os.path.basename(dest), self._theme)
        self._name.setWordWrap(True)
        self._layout.addWidget(self._name)

        self._bar = QProgressBar(self)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(8)
        self._bar.setRange(0, 100 if total > 0 else 0)
        self._bar.setValue(0)
        self._layout.addWidget(self._bar)

        init = f"共 {_fmt_size(total)}，准备下载…" if total > 0 else "准备下载…"
        self._status = WxLabel(init, self._theme)
        self._status.set_variant("hint")
        self._layout.addWidget(self._status)

        row = QHBoxLayout()
        row.addStretch()
        self._cancel_btn = WxPushButton("取消", self._theme)
        self._cancel_btn.clicked.connect(self.reject)
        row.addWidget(self._cancel_btn)
        self._layout.addLayout(row)

        self._theme_widgets.extend([self._name, self._status, self._cancel_btn])
        self._apply_bar_style()

        self._thread = QThread(self)
        self._worker = _FileDownloadWorker(url, dest, total, headers, verify)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_ok)
        self._worker.failed.connect(self._on_fail)
        self._worker.finished_ok.connect(self._cleanup)
        self._worker.failed.connect(self._cleanup)
        self._thread.start()

    def _apply_bar_style(self):
        t = self._theme
        accent = getattr(t, "accent", None) or _color(t, "accent", "#07C160")
        track = _color(t, "input_border", "#e0e0e0")
        self._bar.setStyleSheet(
            f"QProgressBar{{background:{track};border:none;border-radius:4px;}}"
            f"QProgressBar::chunk{{background:{accent};border-radius:4px;}}")

    def set_theme(self, theme):
        super().set_theme(theme)
        self._apply_bar_style()

    def showEvent(self, event):
        # 弹窗无 widget parent（独立顶层窗口，才能被主窗口压到后面），基类
        # 只能按屏幕居中；这里改按 center_on 的顶层主窗口居中，落在程序正中。
        super().showEvent(event)
        ref = self._center_on
        win = ref.window() if ref is not None else None
        if win is not None and win is not self and win.isVisible():
            geo = self.frameGeometry()
            geo.moveCenter(win.frameGeometry().center())
            self.move(geo.topLeft())

    @pyqtSlot(int, int)
    def _on_progress(self, done: int, total: int):
        try:
            if total > 0:
                pct = min(100, int(done * 100 / total))
                self._bar.setRange(0, 100)
                self._bar.setValue(pct)
                self._status.setText(
                    f"{_fmt_size(done)} / {_fmt_size(total)}    {pct}%")
            else:
                # 头部与已知大小都没有：总量未知，进度条改忙碌指示
                self._bar.setRange(0, 0)
                self._status.setText(f"已下载 {_fmt_size(done)}")
        except Exception:  # noqa: BLE001
            log.exception("下载进度回调异常")

    @pyqtSlot()
    def _on_ok(self):
        try:
            self.ok = True
            self._bar.setRange(0, 100)
            self._bar.setValue(100)
            self._status.setText("下载完成")
            self.succeeded.emit(self.dest)
            self._finish_close()
        except Exception:  # noqa: BLE001
            log.exception("下载完成回调异常")

    @pyqtSlot(str)
    def _on_fail(self, err: str):
        try:
            self.error = err
            self.failed.emit(err)
            # 用户已请求取消（线程可能还卡在网络读上）：任何失败结果都视为
            # 取消完成直接关窗；否则展示错误、取消键变「关闭」等用户手动关。
            if self._user_cancelled or err == "已取消":
                self._finish_close()
            else:
                self._bar.setRange(0, 100)
                self._status.setText(f"下载失败：{err}")
                self._cancel_btn.setText("关闭")
                self._cancel_btn.setEnabled(True)
                # 独立窗口可能已被主窗口压到后面：出错时提到前面，否则用户
                # 看不到错误也点不到「关闭」，窗口会一直隐形挂着。
                self.show()
                self.raise_()
                self.activateWindow()
        except Exception:  # noqa: BLE001
            log.exception("下载失败回调异常")

    @pyqtSlot()
    def _cleanup(self):
        """业务回调执行完毕后停线程（顺序同 api_workers._AsyncTask）。"""
        try:
            self._thread.quit()
            self._thread.wait()
            self._worker.deleteLater()
        except RuntimeError:
            pass

    def reject(self, *_):
        """取消下载并关窗（关闭按钮 / 取消键 / closeEvent 统一入口）。

        线程可能正阻塞在网络读取（最长到 60s 超时），绝不能在主线程 wait()
        等它——置取消标志后让 failed 信号回来时再真正关窗。
        """
        self._user_cancelled = True
        self._worker.cancel()
        if self._thread.isRunning():
            self._cancel_btn.setEnabled(False)
            self._status.setText("正在取消…")
            return
        self._finish_close()

    def _finish_close(self):
        if self._closing:
            return
        self._closing = True
        self._cleanup()
        self.hide()
        self.deleteLater()

    def closeEvent(self, event):
        # 自行决定何时真正关（线程在跑先取消、等 failed 回来再关），
        # 一律忽略 Qt 默认关闭，改走 reject。
        event.ignore()
        self.reject()

    def force_close(self):
        """主窗口关闭时用：取消下载并立即隐藏，不等线程。

        本弹窗是无父的独立顶层窗口，会被计入 quitOnLastWindowClosed 的
        「主窗口」——主窗口关闭时若它还可见，app 不会退出。主窗口 closeEvent
        里 hide 掉它即可让 lastWindowClosed 触发；残留线程交给 main.py 的
        os._exit 收尾，绝不能在退出路径上 wait()（可能阻塞到 60s 超时）。
        """
        self._user_cancelled = True
        try:
            self._worker.cancel()
        except RuntimeError:
            pass
        self._closing = True
        self.hide()
        # 调用方紧接着 clear() 列表：这里必须留强引用保活到进程结束
        if self not in _closing_dialogs:
            _closing_dialogs.append(self)
