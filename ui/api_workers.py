"""通用后台任务助手：把同步 API 调用移出 UI 主线程。

用法：
    run_async(lambda: client.get_history(rid),
              on_done=self._on_history_loaded,
              on_fail=self._on_history_failed)

每次调用创建一次性 QThread；结果/错误信号在**主线程**回调，
回调执行完毕后自动 quit + wait + deleteLater 清理线程与 worker。

线程安全要点（曾导致 access violation 闪退的坑）：
ApiWorker 在工作线程中 emit 信号。PyQt 对连接到"普通 Python 函数"
（无 QObject 接收者）的信号默认走 DirectConnection，即回调会在
**工作线程**中执行——回调里一旦操作 QWidget 就与主线程竞争，
随机引发 access violation（典型表现为打开会话/滚动加载后闪退）。
因此回调必须连接到本模块 _AsyncTask（创建于主线程的 QObject）的
pyqtSlot，PyQt 判定接收者线程 != 发射线程后自动改走 QueuedConnection，
保证 on_done/on_fail/清理全部在主线程执行。
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from PyQt5.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

log = logging.getLogger(__name__)


class ApiWorker(QObject):
    """在后台线程执行传入的 callable。"""

    done = pyqtSignal(object)   # 正常结果
    failed = pyqtSignal(str)    # 错误信息

    def __init__(self, fn: Callable, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            result = self._fn()
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))
        else:
            self.done.emit(result)


class _AsyncTask(QObject):
    """一次 run_async 任务的主线程接收器 + 生命周期管理者。

    创建于主线程（parent 挂主窗口），worker 信号连接到本对象的
    pyqtSlot 后均为 QueuedConnection → 回调必然在主线程执行。
    """

    def __init__(self, fn: Callable,
                 on_done: Optional[Callable],
                 on_fail: Optional[Callable],
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self._on_done = on_done
        self._on_fail = on_fail
        self._finished = False
        # destroyed 槽里 parent 的 Python 包装可能已不可用，提前记下类型名
        self._parent_name = type(parent).__name__ if parent is not None else ""

        self._thread = QThread(self)
        self._worker = ApiWorker(fn)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)

        # 连接顺序即调用顺序：先业务回调，后清理
        self._worker.done.connect(self._handle_done)
        self._worker.failed.connect(self._handle_fail)
        self._worker.done.connect(self._finish)
        self._worker.failed.connect(self._finish)
        # 资料卡/弹窗这类父对象会被 close 即销毁（WA_DeleteOnClose），
        # 任务必须赶在被连带删除前解绑（见 _on_parent_destroyed）
        if parent is not None:
            parent.destroyed.connect(self._on_parent_destroyed)
        self._thread.start()

    def _on_parent_destroyed(self, *_):
        """父对象先于任务销毁时把线程解绑出去。

        QObject 析构会连带删除子对象，连同仍在运行的 QThread，报
        "QThread: Destroyed while thread is still running"。destroyed
        信号在子对象删除**之前**发出，此刻 setParent(None) 可以把自身
        移出父树，再由模块级 _detached_tasks 保活到线程自然结束。
        结果回调引用的是已销毁的界面对象，一并丢弃。
        """
        if self._finished or self.is_finished():
            return
        try:
            self._thread.quit()
        except RuntimeError:
            return
        self.setParent(None)
        self._on_done = None
        self._on_fail = None
        _detached_tasks.append(self)
        log.info("后台任务所属 %s 已销毁，线程已解绑收尾",
                 self._parent_name or "对象")

    @pyqtSlot(object)
    def _handle_done(self, result):
        try:
            if self._on_done:
                self._on_done(result)
        except Exception:  # noqa: BLE001
            # 回调里的异常不能吞：记入日志（未捕获钩子兜底不了槽内异常）
            log.exception("run_async on_done 回调异常")

    @pyqtSlot(str)
    def _handle_fail(self, err: str):
        try:
            if self._on_fail:
                self._on_fail(err)
        except Exception:  # noqa: BLE001
            log.exception("run_async on_fail 回调异常")

    @pyqtSlot()
    def _finish(self):
        """主线程中停线程并释放对象（此时业务回调已执行完）。"""
        self._finished = True
        self._thread.quit()
        self._thread.wait()
        self._worker.deleteLater()
        self.deleteLater()
        try:
            _detached_tasks.remove(self)
        except ValueError:
            pass
        log.debug("run_async 任务清理完成")

    def wait_finished(self, timeout_ms: int) -> bool:
        """阻塞等待任务线程结束（主窗口退出时的兜底，勿在别处调用）。"""
        try:
            return self._thread.wait(timeout_ms)
        except RuntimeError:
            # C++ 对象已被 deleteLater 销毁，线程必然早已结束
            return True

    def is_finished(self) -> bool:
        try:
            return not self._thread.isRunning()
        except RuntimeError:
            return True


# 退出时放弃等待的任务被解绑出窗口后由本表持有强引用：防止没有引用导致
# Python 包装提前 GC（同样会销毁还在跑的线程）；线程结束经 _finish 自清
_detached_tasks: list = []


def register_detached(task) -> None:
    """把已解绑的任务挂进保活表（供其他线程任务模块复用收尾机制）。"""
    _detached_tasks.append(task)


def unregister_detached(task) -> None:
    try:
        _detached_tasks.remove(task)
    except ValueError:
        pass


def detach_unfinished_tasks(parent: QObject) -> int:
    """把 parent 下未结束的任务解绑出去（退出放弃等待时用），返回数量。

    主窗口销毁会连带销毁还活着的子 QThread，报
    "QThread: Destroyed while thread is still running"；解绑后线程由
    模块级引用保活直到自然结束。这里直接 quit 线程事件循环（线程安全）：
    worker 还在跑则循环在 run() 返回后退出，已跑完则立即退出——不能
    依赖信号再触发清理，worker 的完成信号可能早已发过（结果排队回主
    线程，而主线程事件循环随退出关闭，排队的 _finish 保证跑不到），
    错过这次发射线程就会永远卡在事件循环里。
    """
    n = 0
    for task in parent.findChildren(_AsyncTask):
        if task.is_finished():
            continue
        task._thread.quit()
        task.setParent(None)
        _detached_tasks.append(task)
        n += 1
    return n


def drain_detached(timeout_ms: int = 3000) -> int:
    """app.exec_() 返回后等待解绑的后台线程结束，返回未结束的数量。

    detach 只是 quit 线程事件循环，worker 正阻塞在网络调用上时 run()
    返回不了；解释器析构时销毁还在跑的 QThread 就是那条
    "QThread: Destroyed while thread is still running" ERROR 的来源。
    此时主线程已无事可做，按总预算依次等待；到点未结束的条目保留在
    _detached_tasks 里（入口随即 os._exit，不触发析构，不会报错）。
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    unfinished = 0
    for task in list(_detached_tasks):
        left = int(max(0.0, deadline - time.monotonic()) * 1000)
        if not task.wait_finished(left):
            unfinished += 1
    if not unfinished:
        _detached_tasks.clear()
    return unfinished


def run_async(fn: Callable,
              on_done: Optional[Callable] = None,
              on_fail: Optional[Callable] = None,
              parent: Optional[QObject] = None):
    """在线程中执行 fn，结束后回到**主线程**回调。

    - on_done(result)：fn 正常返回时调用（主线程）
    - on_fail(str)：fn 抛异常时调用（主线程）
    线程生命周期完全自管理（一次性线程，完成后自动清理）。
    """
    task = _AsyncTask(fn, on_done, on_fail, parent)
    return task
