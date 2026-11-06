"""可流式、可取消的 AI 后台任务。

`run_async` 只支持「结束后一次性回调」，AI 需要打字机式增量与真正的
停止按钮，因此单独提供一个任务类：

- worker 住在独立 QThread，`AITask` 住在主线程；增量/状态/完成都经
  QueuedConnection 投递，界面槽永远在主线程执行。
- 取消通过 threading.Event 传给 `AIClient.chat(cancel=...)`，流式读取
  每块都会检查，因此点「停止」后数百毫秒内即收尾。
- 任务持有者是对话框这类 close 即销毁的对象时，父对象 destroyed 先于
  子对象删除触发：把自身移出父树并挂进 api_workers 的保活表，避免
  "QThread: Destroyed while thread is still running"。

任务函数签名 `job(worker)`，worker 提供：
    worker.cancel_event   传给 AIClient.chat / collect_room_context
    worker.is_cancelled() 长任务自行轮询
    worker.emit_delta(s)  追加的流式正文
    worker.emit_status(s) 采集/请求阶段的人话状态
返回值经 done 信号送出（可为 str 或携带额外信息的 dict）。
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from PyQt5.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from core.ai_client import AICancelled

from . import api_workers

log = logging.getLogger(__name__)


class AIWorker(QObject):
    """在后台线程执行 job；所有信号都由主线程接收。"""

    delta = pyqtSignal(str)
    status = pyqtSignal(str)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, job: Callable):
        super().__init__()
        self._job = job
        self._cancel = threading.Event()

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel

    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self, *_):
        self._cancel.set()

    def emit_delta(self, text):
        if text:
            self.delta.emit(str(text))

    def emit_status(self, text):
        if text:
            self.status.emit(str(text))

    def run(self):
        try:
            result = self._job(self)
        except AICancelled:
            self.cancelled.emit()
        except Exception as e:  # noqa: BLE001
            if self._cancel.is_set():
                self.cancelled.emit()
            else:
                log.exception("AI 任务失败")
                self.failed.emit(str(e) or type(e).__name__)
        else:
            if self._cancel.is_set():
                self.cancelled.emit()
            else:
                self.done.emit(result)


class AITask(QObject):
    """一次 AI 任务的生命周期管理者（创建于主线程）。"""

    delta = pyqtSignal(str)
    status = pyqtSignal(str)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, job: Callable, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._finished = False
        self._parent_name = type(parent).__name__ if parent is not None else ""

        self._thread = QThread(self)
        self._worker = AIWorker(job)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)

        # 先中转到主线程，再触发业务信号；连接顺序保证 _finish 最后执行
        self._worker.delta.connect(self._relay_delta)
        self._worker.status.connect(self._relay_status)
        self._worker.done.connect(self._relay_done)
        self._worker.failed.connect(self._relay_failed)
        self._worker.cancelled.connect(self._relay_cancelled)
        self._worker.done.connect(self._finish)
        self._worker.failed.connect(self._finish)
        self._worker.cancelled.connect(self._finish)

        if parent is not None:
            parent.destroyed.connect(self._on_parent_destroyed)
        self._thread.start()

    # ------------------------------------------------------------------ #
    # 控制
    # ------------------------------------------------------------------ #
    def cancel(self, *_):
        """请求取消（线程安全，可重复调用）。"""
        try:
            self._worker.cancel()
        except RuntimeError:
            pass

    def is_cancelled(self) -> bool:
        try:
            return self._worker.is_cancelled()
        except RuntimeError:
            return True

    def is_finished(self) -> bool:
        try:
            return self._finished or not self._thread.isRunning()
        except RuntimeError:
            return True

    def wait_finished(self, timeout_ms: int) -> bool:
        """供退出流程等待线程结束；勿在普通交互里调用。"""
        try:
            return self._thread.wait(timeout_ms)
        except RuntimeError:
            return True

    # ------------------------------------------------------------------ #
    # 主线程中转与收尾
    # ------------------------------------------------------------------ #
    @pyqtSlot(str)
    def _relay_delta(self, text):
        self.delta.emit(text)

    @pyqtSlot(str)
    def _relay_status(self, text):
        self.status.emit(text)

    @pyqtSlot(object)
    def _relay_done(self, result):
        self.done.emit(result)

    @pyqtSlot(str)
    def _relay_failed(self, err):
        self.failed.emit(err)

    @pyqtSlot()
    def _relay_cancelled(self):
        self.cancelled.emit()

    @pyqtSlot()
    def _finish(self):
        if self._finished:
            return
        self._finished = True
        try:
            self._thread.quit()
            self._thread.wait()
        except RuntimeError:
            pass
        self._worker.deleteLater()
        api_workers.unregister_detached(self)
        self.deleteLater()
        log.debug("AI 任务清理完成")

    def _on_parent_destroyed(self, *_):
        if self._finished:
            return
        self.cancel()
        try:
            self._thread.quit()
        except RuntimeError:
            return
        self.setParent(None)
        api_workers.register_detached(self)
        log.info("AI 任务所属 %s 已销毁，线程已解绑收尾",
                 self._parent_name or "对象")


def detach_unfinished_tasks(parent: QObject) -> int:
    """主窗口退出时解绑仍在跑的 AI 任务（同 api_workers 的处理）。"""
    n = 0
    for task in parent.findChildren(AITask):
        if task.is_finished():
            continue
        task.cancel()
        try:
            task._thread.quit()
        except RuntimeError:
            continue
        task.setParent(None)
        api_workers.register_detached(task)
        n += 1
    return n
