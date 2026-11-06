"""右键菜单「复制链接」：附件消息取 file_url 原址，图文混排取内联图链接。

相对路径按 image_loader 补全成绝对地址；纯文字消息不出该菜单项。

运行：python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QPoint  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from core.models import Message, User  # noqa: E402
from ui import chat_view as cv  # noqa: E402
from ui.chat_view import ChatView, RoleMessage  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _sender():
    return User(id="u1", username="alice", name="张三", avatar_url="")


def _msg(mid, msg_type="text", text="", file_url=""):
    return Message(id=mid, room_id="r", is_mine=False, sender=_sender(),
                   timestamp=1700000000.0, msg_type=msg_type, text=text,
                   status="sent", file_url=file_url)


class CopyLinkTest(unittest.TestCase):

    def setUp(self):
        self.view = ChatView()
        self.view.resize(640, 420)
        self.view.set_room("r")
        self.loader = mock.Mock()
        self.loader.resolve.side_effect = lambda u: (
            u if u.startswith("http") else "https://s" + u)
        self.view.image_loader = self.loader

    def tearDown(self):
        self.view.deleteLater()

    def _append(self, msg):
        self.view.model.append_message(msg)

    def test_image_msg_copies_resolved_file_url(self):
        self._append(_msg("m1", msg_type="image",
                          file_url="/file-upload/abc.png"))
        self.assertEqual(self.view._copyable_links(
            self.view.model.get_message(0)), ["https://s/file-upload/abc.png"])

    def test_file_and_video_and_quote_copy_file_url(self):
        for i, t in enumerate(("file", "video", "audio", "quote")):
            self._append(_msg(f"m{i}", msg_type=t, file_url="/f/x"))
            self.assertEqual(self.view._copyable_links(
                self.view.model.get_message(i)), ["https://s/f/x"])

    def test_text_with_inline_images(self):
        self._append(_msg("mt", text="看图 ![a](/file-upload/i1.png) 和 "
                                    "![b](https://x/i2.png)"))
        links = self.view._copyable_links(self.view.model.get_message(0))
        self.assertEqual(links, ["https://s/file-upload/i1.png",
                                 "https://x/i2.png"])

    def test_plain_text_has_no_link(self):
        self._append(_msg("mp", text="只有文字"))
        self.assertEqual(self.view._copyable_links(
            self.view.model.get_message(0)), [])

    def test_menu_shows_action_and_copies_clipboard(self):
        self._append(_msg("m1", msg_type="image",
                          file_url="/file-upload/abc.png"))
        msg = self.view.model.get_message(0)
        menus = []
        with mock.patch.object(cv.QMenu, "exec_",
                               lambda self, pos: menus.append(self)):
            self.view._show_context_menu(msg, QPoint(0, 0))
        self.assertEqual(len(menus), 1)
        acts = [a for a in menus[0].actions() if a.text() == "复制链接"]
        self.assertEqual(len(acts), 1)
        acts[0].triggered.emit()
        self.assertEqual(_APP.clipboard().text(),
                         "https://s/file-upload/abc.png")

    def test_menu_no_link_action_for_plain_text(self):
        self._append(_msg("mp", text="只有文字"))
        menus = []
        with mock.patch.object(cv.QMenu, "exec_",
                               lambda self, pos: menus.append(self)):
            self.view._show_context_menu(
                self.view.model.get_message(0), QPoint(0, 0))
        self.assertFalse([a for a in menus[0].actions()
                          if a.text() == "复制链接"])


if __name__ == "__main__":
    unittest.main()
