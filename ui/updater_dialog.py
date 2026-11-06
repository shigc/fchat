"""更新下载进度弹窗：微信式无边框卡片 + 进度条 + 取消。

下载在 QThread 中流式进行，进度经 worker 信号投递到主线程（worker 信号
连接到本对话框的 pyqtSlot，跨线程自动 QueuedConnection，与 api_workers
同款做法）。取消用 threading.Event：progress 回调里检测到即抛
DownloadCancelled，请求循环随之终止，.part 残件下次下载时覆盖。
"""
from __future__ import annotations

import logging
import threading

from PyQt5.QtCore import QObject, QThread, pyqtSignal, pyqtSlot
from PyQt5.QtWidgets import QProgressBar

from core import updater
from .dialog_base import FramelessCardDialog
from .widgets import WxLabel, WxPushButton

log = logging.getLogger(__name__)


def _fmt_size(n: int) -> str:
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n} B"


class _DownloadWorker(QObject):
    """工作线程执行 download_installer，进度/结果经信号回主线程。"""

    progress = pyqtSignal(int, int)
    finished_ok = pyqtSignal(str)   # 安装包最终路径
    failed = pyqtSignal(str)        # 错误信息；用户取消发 DownloadCancelled 文案

    def __init__(self, url: str, dest_dir: str, parent=None):
        super().__init__(parent)
        self._url = url
        self._dest_dir = dest_dir
        self._cancel_evt = threading.Event()

    def cancel(self):
        self._cancel_evt.set()

    def run(self):
        try:
            def _cb(done: int, total: int):
                if self._cancel_evt.is_set():
                    raise updater.DownloadCancelled()
                self.progress.emit(done, total)

            path = updater.download_installer(self._url, self._dest_dir, _cb)
        except updater.DownloadCancelled:
            self.failed.emit("已取消")
        except Exception as e:  # noqa: BLE001
            log.warning("更新包下载失败：%s", e)
            self.failed.emit(str(e))
        else:
            self.finished_ok.emit(path)


class UpdateProgressDialog(FramelessCardDialog):
    """模态下载进度弹窗。exec_() 返回 Accept 表示下载完成，路径在 .file_path。"""

    def __init__(self, url: str, theme=None, parent=None):
        super().__init__("正在下载更新", theme, parent, width=440)
        self.file_path = ""
        self.error = ""
        self._user_cancelled = False

        self._hint = WxLabel("发现新版本，正在下载安装包…", self._theme)
        self._hint.set_variant("hint")
        self._hint.setWordWrap(True)
        self._layout.addWidget(self._hint)

        self._bar = QProgressBar(self)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(8)
        self._layout.addWidget(self._bar)

        self._status = WxLabel("准备下载…", self._theme)
        self._status.set_variant("hint")
        self._layout.addWidget(self._status)

        cancel_row = self._row_right()
        self._cancel_btn = WxPushButton("取消", self._theme)
        self._cancel_btn.clicked.connect(self.reject)
        cancel_row.addWidget(self._cancel_btn)
        cancel_row.addStretch()
        self._layout.addLayout(cancel_row)

        self._thread = QThread(self)
        self._worker = _DownloadWorker(url, updater.updates_dir())
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_ok)
        self._worker.failed.connect(self._on_fail)
        self._worker.finished_ok.connect(self._cleanup)
        self._worker.failed.connect(self._cleanup)
        self._thread.start()

    def _row_right(self):
        from PyQt5.QtWidgets import QHBoxLayout
        row = QHBoxLayout()
        row.setSpacing(8)
        return row

    @pyqtSlot(int, int)
    def _on_progress(self, done: int, total: int):
        try:
            if total > 0:
                self._bar.setMaximum(total)
                self._bar.setValue(done)
                self._status.setText(
                    f"{_fmt_size(done)} / {_fmt_size(total)}")
            else:
                # 服务端没给 Content-Length：不确定总量，用忙碌指示
                self._bar.setMaximum(0)
                self._bar.setValue(0)
                self._status.setText(f"已下载 {_fmt_size(done)}")
        except Exception:  # noqa: BLE001
            log.exception("更新进度回调异常")

    @pyqtSlot(str)
    def _on_ok(self, path: str):
        try:
            self.file_path = path
            self._status.setText("下载完成")
            self.accept()
        except Exception:  # noqa: BLE001
            log.exception("更新下载完成回调异常")

    @pyqtSlot(str)
    def _on_fail(self, err: str):
        try:
            self.error = err
            # 用户已请求关闭（可能线程还卡在网络读取上）：任何失败结果
            # 都视为取消完成，直接关窗；否则展示错误等用户手动关闭
            if self._user_cancelled or err == "已取消":
                self._finish_close()
            else:
                self._status.setText(f"下载失败：{err}")
        except Exception:  # noqa: BLE001
            log.exception("更新下载失败回调异常")

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
        """取消下载并关窗。

        线程可能正阻塞在网络读取（读超时最长 30s），绝不能在主线程
        wait() 等它——置取消标志后让 failed 信号回来时再真正关窗。
        """
        self._user_cancelled = True
        self._worker.cancel()
        if self._thread.isRunning():
            self._cancel_btn.setEnabled(False)
            self._status.setText("正在取消…")
            return
        self._finish_close()

    def _finish_close(self):
        self._cleanup()
        super().reject()

    def closeEvent(self, event):
        self.reject()
        event.accept()
