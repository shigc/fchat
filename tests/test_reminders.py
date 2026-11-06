"""消息定时提醒：右键菜单、配置持久化、到期轮询触发托盘通知。

offscreen；不联网。
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QPoint  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402
from core.models import Message, User  # noqa: E402
from ui.chat_view import ChatView  # noqa: E402
import ui.main_window as mw  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _msg(text="记得看这份报告", msg_type="text"):
    sender = User(id="u1", username="alice", name="Alice")
    return Message(id="m1", room_id="r1", text=text, msg_type=msg_type,
                   sender=sender)


class _StubWin:
    """只喂 _on_remind_requested/_check_reminders 用到的属性。"""
    _active_room_id = "r1"
    _conversations = {}
    _tray = None

    def __init__(self):
        self.status_msgs = []
        # _sync_remind_map 的落点（mock：记下每次刷入的标记表）
        self.chat_area = mock.Mock()

    def statusBar(self):
        return self

    def showMessage(self, text, ms=0):
        self.status_msgs.append(text)

    def remind_map(self):
        args = self.chat_area.chat_view.model.set_remind_map.call_args
        return args.args[0] if args else None

    # 新增的 MainWindow 方法依赖链走真实现，stub 只补委托
    def _sync_remind_map(self):
        mw.MainWindow._sync_remind_map(self)

    def _remove_reminders(self, rid, msg_id):
        return mw.MainWindow._remove_reminders(self, rid, msg_id)


class RemindMenuTest(unittest.TestCase):

    def setUp(self):
        self.view = ChatView()

    def tearDown(self):
        self.view.deleteLater()

    def _open_menu(self, msg):
        """弹菜单（mock exec 不会真弹）。所有 addMenu 共享同一返回 mock。"""
        with mock.patch("ui.chat_view.QMenu") as MQ:
            self.view._show_context_menu(msg, QPoint(0, 0))
        calls = MQ.return_value.addMenu.call_args_list
        # addMenu(icon, title)：图标在首参，标题在次参
        self.assertEqual(
            len([c for c in calls if c.args and c.args[-1] == "提醒"]), 1)
        return MQ.return_value.addMenu.return_value

    def test_remind_submenu_with_three_options(self):
        remind_menu = self._open_menu(_msg())
        labels = [c.args[0] for c in remind_menu.addAction.call_args_list]
        self.assertEqual(labels, ["1 小时后", "3 小时后", "明天 9 点"])

    def test_system_message_has_no_remind_menu(self):
        with mock.patch("ui.chat_view.QMenu") as MQ:
            self.view._show_context_menu(_msg(msg_type="system"), QPoint(0, 0))
        titles = [c.args[-1] for c in MQ.return_value.addMenu.call_args_list
                  if c.args]
        self.assertNotIn("提醒", titles)

    def test_pick_option_emits_message_and_due(self):
        got = []
        self.view.remind_requested.connect(lambda m, d: got.append((m, d)))
        msg = _msg()
        remind_menu = self._open_menu(msg)
        callbacks = [c.args[0]
                     for c in remind_menu.addAction.return_value.triggered
                     .connect.call_args_list]
        for cb in callbacks:
            cb(False)
        self.assertEqual(len(got), 3)
        self.assertTrue(all(m is msg for m, _ in got))
        dues = [d for _, d in got]
        self.assertEqual(dues, sorted(dues))
        now = time.time()
        self.assertAlmostEqual(dues[0] - now, 3600, delta=5)
        self.assertAlmostEqual(dues[1] - now, 3 * 3600, delta=5)
        self.assertGreater(dues[2], now)

    def test_remind_due_tomorrow_nine(self):
        import datetime
        due = ChatView._remind_due("tomorrow9")
        lt = time.localtime(due)
        self.assertEqual(lt.tm_hour, 9)
        self.assertEqual(lt.tm_min, 0)
        self.assertEqual(datetime.date.fromtimestamp(due),
                         datetime.date.fromtimestamp(time.time())
                         + datetime.timedelta(days=1))

    def test_cancel_action_only_with_reminder(self):
        """已设提醒的消息右键有「取消提醒」，没设的没有。"""
        with mock.patch("ui.chat_view.QMenu") as MQ:
            self.view._show_context_menu(_msg(), QPoint(0, 0))
        labels = [c.args[-1] for c in MQ.return_value.addAction.call_args_list
                  if c.args]
        self.assertNotIn("取消提醒", labels)
        self.view._model.set_remind_map({"m1": time.time() + 60})
        with mock.patch("ui.chat_view.QMenu") as MQ:
            self.view._show_context_menu(_msg(), QPoint(0, 0))
        labels = [c.args[-1] for c in MQ.return_value.addAction.call_args_list
                  if c.args]
        self.assertIn("取消提醒", labels)

    def test_cancel_action_emits_message(self):
        got = []
        self.view.remind_cancel_requested.connect(got.append)
        self.view._model.set_remind_map({"m1": time.time() + 60})
        made = []

        def _add(*a, **k):
            act = mock.Mock()
            made.append((a, act))
            return act

        with mock.patch("ui.chat_view.QMenu") as MQ:
            MQ.return_value.addAction.side_effect = _add
            self.view._show_context_menu(_msg(), QPoint(0, 0))
        act = next(act for a, act in made if a[-1] == "取消提醒")
        cbs = [c.args[0] for c in act.triggered.connect.call_args_list]
        self.assertEqual(len(cbs), 1)
        cbs[0](False)
        self.assertEqual(got, [_msg()])

    def test_time_marker_shows_remind_icon(self):
        msg = _msg()
        # 标记是 delegate 的时间行机制，数据源在 model 的 _remind_map
        self.assertEqual(self.view._delegate._time_markers(msg), [])
        self.view._model.set_remind_map({"m1": time.time() + 60})
        self.assertEqual(
            [k for k, _p in self.view._delegate._time_markers(msg)],
            ["icon"])


class ReminderPersistTest(unittest.TestCase):
    """配置整表替换 + 其他局部保存器不得抹掉 reminders。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = self._tmp.name
        self._orig = (config.CONFIG_DIR, config.CONFIG_FILE, config.KEY_FILE)
        config.CONFIG_DIR = os.path.join(tmp, "cfgdir")
        config.CONFIG_FILE = os.path.join(config.CONFIG_DIR, "config.yaml")
        config.KEY_FILE = os.path.join(config.CONFIG_DIR, "secret.key")

    def tearDown(self):
        (config.CONFIG_DIR, config.CONFIG_FILE,
         config.KEY_FILE) = self._orig
        self._tmp.cleanup()

    def test_roundtrip(self):
        due = time.time() + 3600
        config.save_reminders([{"rid": "r1", "msg_id": "m1",
                                "text": "hi", "due": due}])
        loaded = config.load_config()["reminders"]
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["rid"], "r1")
        self.assertAlmostEqual(loaded[0]["due"], due)

    def test_other_savers_keep_reminders(self):
        config.save_reminders([{"rid": "r1", "msg_id": "m1",
                                "text": "hi", "due": 1.0}])
        config.save_auto_login(True)
        self.assertEqual(len(config.load_config()["reminders"]), 1)


class ReminderRuntimeTest(unittest.TestCase):

    def setUp(self):
        self.win = _StubWin()

    def test_on_remind_requested_persists(self):
        with mock.patch("config.load_config", return_value={"reminders": []}), \
                mock.patch("config.save_reminders") as save:
            mw.MainWindow._on_remind_requested(self.win, _msg(), 123.0)
        saved = save.call_args.args[0]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["rid"], "r1")
        self.assertAlmostEqual(saved[0]["due"], 123.0)

    def test_check_reminders_fires_toast_and_cleans(self):
        now = time.time()
        reminders = [
            {"rid": "due1", "msg_id": "m1", "text": "到期1", "due": now - 5},
            {"rid": "due2", "msg_id": "m2", "text": "", "due": now},
            {"rid": "future", "msg_id": "m3", "text": "未到", "due": now + 999},
        ]
        tray = mock.Mock()
        self.win._tray = tray

        class _C:
            name = "房间甲"
        self.win._conversations = {"due1": _C()}
        with mock.patch("config.load_config",
                        return_value={"reminders": reminders}), \
                mock.patch("config.save_reminders") as save:
            mw.MainWindow._check_reminders(self.win)
        fired = [c.args for c in tray.show_message_toast.call_args_list]
        self.assertEqual([f[2] for f in fired], ["due1", "due2"])
        self.assertEqual(fired[0][0], "房间甲")
        kept = save.call_args.args[0]
        self.assertEqual([r["rid"] for r in kept], ["future"])

    def test_check_reminders_no_tray_falls_back_statusbar(self):
        self.win._tray = None
        with mock.patch("config.load_config",
                        return_value={"reminders": [
                            {"rid": "r1", "msg_id": "m1", "text": "到点了",
                             "due": time.time() - 1}]}), \
                mock.patch("config.save_reminders"):
            mw.MainWindow._check_reminders(self.win)
        self.assertTrue(any("到点了" in s for s in self.win.status_msgs))

    def test_on_remind_requested_replaces_duplicate(self):
        """同一条消息重复设提醒按覆盖处理，不堆重复项。"""
        old = {"rid": "r1", "msg_id": "m1", "text": "旧",
               "due": time.time() + 60}
        due = time.time() + 3600
        # 两次读配置：_remove_reminders 读旧表、_sync_remind_map 读新表
        with mock.patch("config.load_config", side_effect=[
                {"reminders": [old]},
                {"reminders": [{"rid": "r1", "msg_id": "m1", "text": "t",
                                "due": due}]}]), \
                mock.patch("config.save_reminders") as save:
            mw.MainWindow._on_remind_requested(self.win, _msg(), due)
        saved = save.call_args.args[0]
        self.assertEqual(len(saved), 1)
        self.assertAlmostEqual(saved[0]["due"], due)
        # 标记表同步刷入模型
        mapping = self.win.remind_map()
        self.assertEqual(mapping, {"m1": due})

    def test_on_remind_cancel_requested_removes(self):
        now = time.time()
        reminders = [
            {"rid": "r1", "msg_id": "m1", "text": "a", "due": now + 60},
            {"rid": "r1", "msg_id": "m2", "text": "b", "due": now + 60},
        ]
        # _remove_reminders 与 _sync_remind_map 各读一次配置：第二次读到
        # 删除后的表（save 是 mock，不会真的改文件）
        with mock.patch("config.load_config", side_effect=[
                {"reminders": reminders},
                {"reminders": [reminders[1]]}]), \
                mock.patch("config.save_reminders") as save:
            mw.MainWindow._on_remind_cancel_requested(self.win, _msg())
        kept = save.call_args.args[0]
        self.assertEqual([r["msg_id"] for r in kept], ["m2"])
        self.assertEqual(self.win.remind_map(), {"m2": now + 60})
        self.assertTrue(any("已取消提醒" in s for s in self.win.status_msgs))

    def test_sync_remind_map_filters_expired(self):
        now = time.time()
        with mock.patch("config.load_config",
                        return_value={"reminders": [
                            {"rid": "r1", "msg_id": "m1", "text": "",
                             "due": now + 60},
                            {"rid": "r1", "msg_id": "m2", "text": "",
                             "due": now - 1}]}):
            mw.MainWindow._sync_remind_map(self.win)
        self.assertEqual(self.win.remind_map(), {"m1": now + 60})


class ReminderDialogTest(unittest.TestCase):
    """提醒列表弹窗：空态、行数、取消/跳转信号。"""

    def setUp(self):
        from ui.reminders_dialog import RemindersDialog
        self.dlg = RemindersDialog()

    def tearDown(self):
        self.dlg.deleteLater()

    def test_empty_state(self):
        self.dlg.set_reminders([])
        self.assertTrue(self.dlg._empty.isVisibleTo(self.dlg))
        self.assertFalse(self.dlg._scroll.isVisibleTo(self.dlg))

    def test_rows_built_and_signals(self):
        due = time.time() + 3600
        rows = [
            {"rid": "r1", "msg_id": "m1", "text": "记得看报告",
             "due": due, "room_name": "项目群"},
            {"rid": "r2", "msg_id": "m2", "text": "", "due": due + 60,
             "room_name": ""},
        ]
        self.dlg.set_reminders(rows)
        self.assertFalse(self.dlg._empty.isVisibleTo(self.dlg))
        from ui.reminders_dialog import _RemindRow
        row_widgets = [self.dlg._rows_lay.itemAt(i).widget()
                       for i in range(self.dlg._rows_lay.count())]
        rows_only = [w for w in row_widgets if isinstance(w, _RemindRow)]
        self.assertEqual(len(rows_only), 2)

        opened, cancelled = [], []
        self.dlg.open_requested.connect(lambda r, m: opened.append((r, m)))
        self.dlg.cancel_requested.connect(
            lambda r, m: cancelled.append((r, m)))
        rows_only[0].open_requested.emit("r1", "m1")
        rows_only[1].cancel_requested.emit("r2", "m2")
        self.assertEqual(opened, [("r1", "m1")])
        self.assertEqual(cancelled, [("r2", "m2")])

    def test_row_cancel_button_emits(self):
        due = time.time() + 60
        self.dlg.set_reminders([{"rid": "r1", "msg_id": "m1",
                                 "text": "x", "due": due,
                                 "room_name": "群"}])
        from ui.reminders_dialog import _RemindRow
        rows = [self.dlg._rows_lay.itemAt(i).widget()
                for i in range(self.dlg._rows_lay.count())]
        row = next(w for w in rows if isinstance(w, _RemindRow))
        got = []
        self.dlg.cancel_requested.connect(lambda r, m: got.append((r, m)))
        from PyQt5.QtWidgets import QPushButton
        btn = next(w for w in row.findChildren(QPushButton)
                   if w.text() == "取消")
        btn.click()
        self.assertEqual(got, [("r1", "m1")])


if __name__ == "__main__":
    unittest.main()
