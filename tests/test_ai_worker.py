"""ui.ai_worker 测试：流式增量、状态、完成、失败、取消、父对象销毁。

offscreen + 真实 QThread；不联网。
"""
import os
import sys
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

from core.ai_client import AICancelled  # noqa: E402
from ui.ai_worker import AITask  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def spin_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _APP.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    _APP.processEvents()
    return predicate()


class AITaskTest(unittest.TestCase):
    def test_streams_deltas_status_and_done(self):
        def job(worker):
            worker.emit_status("准备中")
            worker.emit_delta("你")
            time.sleep(0.02)
            worker.emit_delta("好")
            return "你好"

        task = AITask(job)
        deltas, statuses, dones, fails = [], [], [], []
        task.delta.connect(deltas.append)
        task.status.connect(statuses.append)
        task.done.connect(dones.append)
        task.failed.connect(fails.append)

        self.assertTrue(spin_until(lambda: bool(dones)))
        self.assertEqual("".join(deltas), "你好")
        self.assertEqual(statuses, ["准备中"])
        self.assertEqual(dones, ["你好"])
        self.assertEqual(fails, [])
        self.assertTrue(spin_until(task.is_finished))

    def test_empty_delta_and_status_not_emitted(self):
        def job(worker):
            worker.emit_delta("")
            worker.emit_status("")
            worker.emit_delta(None)
            return "ok"

        task = AITask(job)
        deltas, statuses, dones = [], [], []
        task.delta.connect(deltas.append)
        task.status.connect(statuses.append)
        task.done.connect(dones.append)
        self.assertTrue(spin_until(lambda: bool(dones)))
        self.assertEqual(deltas, [])
        self.assertEqual(statuses, [])

    def test_failure_emits_failed(self):
        def job(_worker):
            raise RuntimeError("网关超时")

        task = AITask(job)
        fails, dones = [], []
        task.failed.connect(fails.append)
        task.done.connect(dones.append)
        self.assertTrue(spin_until(lambda: bool(fails)))
        self.assertEqual(fails, ["网关超时"])
        self.assertEqual(dones, [])
        self.assertTrue(spin_until(task.is_finished))

    def test_cancel_raises_ai_cancelled(self):
        def job(worker):
            while not worker.is_cancelled():
                time.sleep(0.01)
            raise AICancelled("已取消")

        task = AITask(job)
        cancelled = []
        task.cancelled.connect(lambda: cancelled.append(True))
        task.done.connect(lambda _r: self.fail("取消不应触发 done"))
        task.cancel()
        self.assertTrue(spin_until(lambda: bool(cancelled)))
        self.assertTrue(task.is_cancelled())
        self.assertTrue(spin_until(task.is_finished))

    def test_cancel_after_natural_exception_reports_cancelled(self):
        def job(worker):
            worker.cancel()
            raise RuntimeError("连接中断")

        task = AITask(job)
        cancelled, fails = [], []
        task.cancelled.connect(lambda: cancelled.append(True))
        task.failed.connect(fails.append)
        self.assertTrue(spin_until(lambda: bool(cancelled)))
        self.assertEqual(fails, [])

    def test_cancel_without_job_checking_is_harmless(self):
        def job(_worker):
            return "done"

        task = AITask(job)
        dones = []
        task.done.connect(dones.append)
        self.assertTrue(spin_until(lambda: bool(dones)))
        task.cancel()  # 已完成后再取消不应崩
        task.cancel()
        self.assertTrue(spin_until(task.is_finished))

    def test_job_receives_cancel_event(self):
        seen = {}

        def job(worker):
            seen["event"] = worker.cancel_event
            return "ok"

        task = AITask(job)
        dones = []
        task.done.connect(dones.append)
        self.assertTrue(spin_until(lambda: bool(dones)))
        self.assertTrue(seen["event"].is_set() is False)
        task.cancel()
        self.assertTrue(seen["event"].is_set())

    def test_parent_destroyed_does_not_kill_running_thread(self):
        def job(worker):
            # 父对象销毁会触发 cancel；长任务据此尽快退出
            for _ in range(500):
                if worker.is_cancelled():
                    raise AICancelled("已取消")
                time.sleep(0.01)
            return "finished"

        parent = QWidget()
        task = AITask(job, parent=parent)
        _APP.processEvents()
        parent.deleteLater()
        # deleteLater 的 DeferredDelete 事件要显式派发才会真正销毁对象。
        # 只派发 parent：全局派发会顺手销毁前面测试遗留的窗口/面板，
        # Qt 在析构期回调 Python 委托时曾导致整个进程 qFatal（退出码 127）。
        from PyQt5.QtCore import QCoreApplication, QEvent
        QCoreApplication.sendPostedEvents(parent, QEvent.DeferredDelete)
        _APP.processEvents()
        self.assertTrue(spin_until(task.is_cancelled))
        self.assertTrue(spin_until(task.is_finished))

    def test_parentless_task_finishes(self):
        task = AITask(lambda _w: "x")
        dones = []
        task.done.connect(dones.append)
        self.assertTrue(spin_until(lambda: bool(dones)))
        self.assertTrue(spin_until(task.is_finished))


if __name__ == "__main__":
    unittest.main()
