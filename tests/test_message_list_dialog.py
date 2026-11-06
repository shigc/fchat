"""置顶 / 星标消息列表弹窗：加载/空/失败三态、点击回传消息。

异步任务替换成同步执行（不联网），offscreen 渲染。
"""
import os
import sys
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication, QLabel  # noqa: E402

from core.models import Message, User  # noqa: E402
from ui.message_list_dialog import MessageListDialog  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _sync_run_async(task, on_done=None, on_fail=None, parent=None, **_kw):
    """把 run_async 换成同步调用，便于断言三态。"""
    try:
        result = task()
    except Exception as exc:  # noqa: BLE001 —— 测试里原样交给 on_fail
        if on_fail is not None:
            on_fail(exc)
    else:
        if on_done is not None:
            on_done(result)
    return mock.Mock()


def _msg(mid="m1", text="你好", sender="小明", mtype="text"):
    return Message(id=mid, room_id="r1", text=text,
                   sender=User(username="u", name=sender),
                   timestamp=time.time(), msg_type=mtype)


class MessageListDialogTest(unittest.TestCase):

    def _make(self, client, kind="pinned"):
        with mock.patch("ui.message_list_dialog.run_async",
                        side_effect=_sync_run_async):
            return MessageListDialog(client, "r1", kind)

    def test_pinned_success_fills_list(self):
        client = mock.Mock()
        client.get_pinned_messages.return_value = [_msg("m1"), _msg("m2")]
        dlg = self._make(client)
        client.get_pinned_messages.assert_called_once_with("r1", 50)
        self.assertEqual(dlg._list.count(), 2)
        self.assertFalse(dlg._list.isHidden())
        self.assertTrue(dlg._status.isHidden())
        labels = [lbl.text() for lbl in dlg._row_labels]
        self.assertTrue(any("小明" in t for t in labels), labels)
        dlg.deleteLater()

    def test_starred_uses_starred_endpoint(self):
        client = mock.Mock()
        client.get_starred_messages.return_value = [_msg("m1")]
        dlg = self._make(client, kind="starred")
        client.get_starred_messages.assert_called_once_with("r1", 50)
        client.get_pinned_messages.assert_not_called()
        self.assertEqual(dlg._list.count(), 1)
        dlg.deleteLater()

    def test_empty_shows_hint(self):
        client = mock.Mock()
        client.get_pinned_messages.return_value = []
        dlg = self._make(client)
        self.assertEqual(dlg._status.text(), "暂无置顶消息")
        self.assertTrue(dlg._list.isHidden())
        self.assertTrue(dlg._retry_btn.isHidden())
        dlg.deleteLater()

    def test_failure_shows_retry(self):
        client = mock.Mock()
        client.get_pinned_messages.side_effect = RuntimeError("boom")
        dlg = self._make(client)
        self.assertIn("加载失败", dlg._status.text())
        self.assertFalse(dlg._retry_btn.isHidden())
        self.assertTrue(dlg._list.isHidden())
        dlg.deleteLater()

    def test_retry_recovers(self):
        client = mock.Mock()
        client.get_pinned_messages.side_effect = RuntimeError("boom")
        dlg = self._make(client)
        client.get_pinned_messages.side_effect = None
        client.get_pinned_messages.return_value = [_msg("m1")]
        with mock.patch("ui.message_list_dialog.run_async",
                        side_effect=_sync_run_async):
            dlg._load()
        self.assertEqual(dlg._list.count(), 1)
        self.assertTrue(dlg._retry_btn.isHidden())
        self.assertTrue(dlg._status.isHidden())
        dlg.deleteLater()

    def test_no_client_shows_error(self):
        dlg = self._make(None)
        self.assertIn("尚未登录", dlg._status.text())
        self.assertFalse(dlg._retry_btn.isHidden())
        dlg.deleteLater()

    def test_click_emits_message_and_accepts(self):
        client = mock.Mock()
        client.get_pinned_messages.return_value = [_msg("m7")]
        dlg = self._make(client)
        got = []
        dlg.message_selected.connect(got.append)
        with mock.patch.object(dlg, "accept") as accept:
            dlg._list.itemClicked.emit(dlg._list.item(0))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].id, "m7")
        accept.assert_called_once()
        dlg.deleteLater()

    def test_row_widget_shows_body_without_hover(self):
        """正文要直接可见；曾只把多行文本塞进 item，默认委托整体省略，
        只能靠 tooltip 才能看到内容。"""
        client = mock.Mock()
        client.get_pinned_messages.return_value = [
            _msg("m1", text="需要直接显示的消息正文")]
        dlg = self._make(client)
        row = dlg._list.itemWidget(dlg._list.item(0))
        self.assertIsNotNone(row)
        # item 文本必须为空：非空会被默认委托画在行部件下面形成重影
        self.assertEqual(dlg._list.item(0).text(), "")
        texts = [lbl.text() for lbl in row.findChildren(QLabel)]
        self.assertTrue(any("需要直接显示的消息正文" in t for t in texts),
                        texts)
        dlg.deleteLater()

    def test_long_text_is_truncated_and_newlines_flattened(self):
        client = mock.Mock()
        client.get_pinned_messages.return_value = [
            _msg("m1", text="第一行\n" + "x" * 200)]
        dlg = self._make(client)
        body = dlg._list.item(0).toolTip()
        self.assertNotIn("\n", body)
        self.assertTrue(body.endswith("…"))
        self.assertLessEqual(len(body), 81)
        dlg.deleteLater()


if __name__ == "__main__":
    unittest.main()
