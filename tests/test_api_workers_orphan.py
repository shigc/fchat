"""回归：父对象先销毁时，run_async 任务必须解绑而不是连带销毁 QThread。

真机 2026-10-20 报 "QThread: Destroyed while thread is still running"：
ProfileCard / RoomInfoCard 是 WA_DeleteOnClose 浮卡，关闭即析构；其
enrich() 发起的 run_async(parent=卡片) 若还在网络请求中，会随父对象
一起被删除。这里直接验证解绑路径与回调丢弃语义。
"""
import os
import sys
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QEvent, QObject  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from ui.api_workers import _detached_tasks, run_async  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _spin_until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _APP.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return False


class ParentDestroyedTest(unittest.TestCase):

    def test_task_survives_parent_deletion(self):
        gate = threading.Event()
        done_calls = []

        parent = QObject()
        task = run_async(lambda: (gate.wait(5.0), "ok")[1],
                         on_done=done_calls.append, parent=parent)
        self.assertIs(task.parent(), parent)

        # destroyed 信号在子对象删除前发出，槽里必须已把任务移出父树
        parent.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)

        self.assertIsNone(task.parent())
        self.assertIn(task, _detached_tasks)
        self.assertTrue(task._thread.isRunning())

        gate.set()
        self.assertTrue(_spin_until(lambda: task.is_finished()))
        self.assertTrue(_spin_until(lambda: task not in _detached_tasks))
        # 回调引用的是已销毁的界面对象，不能再跑
        self.assertEqual(done_calls, [])

    def test_finished_task_is_not_detached(self):
        """线程已结束（清理信号尚未跑到）时父对象销毁：不需要保活。"""
        gate = threading.Event()
        gate.set()
        parent = QObject()
        task = run_async(lambda: "ok", on_done=None, parent=parent)
        self.assertTrue(_spin_until(lambda: task.is_finished()))
        parent.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.assertNotIn(task, _detached_tasks)


if __name__ == "__main__":
    unittest.main()
