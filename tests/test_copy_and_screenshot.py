"""复制为图片 与 截图流程 测试。

- 复制为图片：纯文本消息按渲染样式画成 PNG 放入剪贴板。
- 截图：最小化→捕获→还原→发送文件路径（QTimer 时序用 mock 抽平）。
GUI 测试，offscreen 渲染，不联网。
运行：python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtGui import QPixmap, QImage, QColor  # noqa: E402
from PyQt5.QtCore import QPoint, QPointF, QRect  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from core.models import Message, User  # noqa: E402
from ui.chat_view import ChatView  # noqa: E402
from ui import main_window as mw  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _sender():
    return User(id="u1", username="user1", name="用户1", avatar_url="")


class TestCopyAsImage(unittest.TestCase):
    def setUp(self):
        self.view = ChatView()
        self.view.resize(640, 420)
        self.view.set_room("r")
        self.msg = Message(id="m1", room_id="r", sender=_sender(),
                           timestamp=1700000000.0, msg_type="text",
                           text="**加粗** 和普通文字")
        self.view.model.prepend_messages_immediate([self.msg])

    def test_copy_text_message_writes_pixmap_to_clipboard(self):
        QApplication.clipboard().clear()
        self.view._copy_msg_as_image(self.msg)
        pm = QApplication.clipboard().pixmap()
        self.assertFalse(pm.isNull())
        self.assertGreater(pm.width(), 0)
        self.assertGreater(pm.height(), 0)


class TestScreenshotFlow(unittest.TestCase):
    def test_on_screenshot_stages_selection_into_input(self):
        """把 QTimer.singleShot 抽平成立即执行，验证
        最小化→抓屏→显示遮罩→选中后插入输入框草稿→还原 的完整接线。
        截图直接 insert_screenshot_image 内联进输入框，不直接发送。
        """
        from PyQt5.QtCore import QTimer

        input_obj = mock.Mock()
        window = mock.Mock()
        window.chat_area.input = input_obj

        png = QPixmap(20, 20)
        png.fill(QColor("blue"))

        with mock.patch.object(QTimer, "singleShot",
                               side_effect=lambda _ms, fn: fn()), \
                mock.patch.object(mw.QGuiApplication, "primaryScreen") as screen:
            scr = mock.Mock()
            scr.grabWindow.return_value = png
            scr.geometry.return_value = QRect(0, 0, 100, 100)
            screen.return_value = scr
            mw.MainWindow._on_screenshot(window)
            overlay = window._capture_overlay
            self.assertIsNotNone(overlay)
            # 模拟框选完成的 selected 信号
            overlay.selected.emit(png.toImage())

        window.showMinimized.assert_called_once()
        window.showNormal.assert_called_once()
        window.activateWindow.assert_called_once()
        # 截图内联进输入框而非直接发送
        input_obj.insert_screenshot_image.assert_called_once()
        input_obj.send_file.emit.assert_not_called()

    def test_on_screenshot_cancel_restores_without_staging(self):
        """Esc/右键取消：还原窗口、不插入草稿、不发送。"""
        from PyQt5.QtCore import QTimer

        input_obj = mock.Mock()
        window = mock.Mock()
        window.chat_area.input = input_obj

        png = QPixmap(20, 20)
        png.fill(QColor("blue"))

        with mock.patch.object(QTimer, "singleShot",
                               side_effect=lambda _ms, fn: fn()), \
                mock.patch.object(mw.QGuiApplication, "primaryScreen") as screen:
            scr = mock.Mock()
            scr.grabWindow.return_value = png
            scr.geometry.return_value = QRect(0, 0, 100, 100)
            screen.return_value = scr
            mw.MainWindow._on_screenshot(window)
            overlay = window._capture_overlay
            overlay.cancelled.emit()

        window.showMinimized.assert_called_once()
        window.showNormal.assert_called_once()
        window.activateWindow.assert_called_once()
        input_obj.insert_screenshot_image.assert_not_called()


class TestScreenshotAnnotate(unittest.TestCase):
    """框选后进入标注态：工具条 / 作画提交 / 撤销 / 出口信号。"""

    @staticmethod
    def _img_bytes(img):
        b = img.constBits()
        return bytes(bytearray(b.asarray(img.sizeInBytes())))

    def _overlay(self):
        from ui.screenshot_capture import ScreenshotOverlay
        pm = QPixmap(120, 120)
        pm.fill(QColor("blue"))
        ov = ScreenshotOverlay(pm)
        ov.resize(120, 120)
        self.addCleanup(ov.deleteLater)
        return ov

    def _select(self, ov):
        """合成拖拽事件完成框选（30,30)-(90,90)。"""
        from PyQt5.QtCore import QEvent, QPointF, Qt
        from PyQt5.QtGui import QMouseEvent
        def ev(t, pos):
            return QMouseEvent(t, QPointF(pos), QPointF(pos),
                               Qt.LeftButton, Qt.NoButton, Qt.NoModifier)
        ov.mousePressEvent(ev(QEvent.MouseButtonPress, QPoint(30, 30)))
        ov.mouseMoveEvent(ev(QEvent.MouseMove, QPoint(90, 90)))
        ov.mouseReleaseEvent(ev(QEvent.MouseButtonRelease, QPoint(90, 90)))

    def test_selection_enters_annotate_with_toolbar(self):
        ov = self._overlay()
        self.assertEqual(ov._mode, "select")
        self._select(ov)
        self.assertEqual(ov._mode, "annotate")
        self.assertIsNotNone(ov._canvas)
        self.assertEqual(ov._canvas.width(), 61)  # 61px（含边界）
        self.assertIsNotNone(ov._toolbar)
        self.assertEqual(len(ov._tool_btns), 5, "5 个作画工具可选中")

    def test_draw_rect_commits_and_undo_reverts(self):
        ov = self._overlay()
        self._select(ov)
        before = self._img_bytes(ov._canvas)
        ov._set_tool("rect")
        self._select(ov)  # 在成图上再拖一框（坐标即选区内叠加）
        self.assertEqual(len(ov._undo_stack), 1)
        after = self._img_bytes(ov._canvas)
        self.assertNotEqual(before, after, "作画必须改变画布")
        ov._undo()
        self.assertEqual(ov._undo_stack, [])
        self.assertEqual(self._img_bytes(ov._canvas), before,
                         "撤销必须逐字节还原画布")

    def test_undo_limit_and_empty_undo_noop(self):
        ov = self._overlay()
        self._select(ov)
        ov._undo()  # 空栈不崩
        from ui.screenshot_capture import _UNDO_LIMIT
        for _ in range(_UNDO_LIMIT + 3):
            ov._commit_shape(("rect", QPointF(1, 1), QPointF(10, 10)))
        self.assertEqual(len(ov._undo_stack), _UNDO_LIMIT)

    def test_done_emits_selected_canvas(self):
        ov = self._overlay()
        self._select(ov)
        got = []
        ov.selected.connect(got.append)
        ov._done()
        self.assertEqual(len(got), 1)
        self.assertIs(got[0], ov._canvas)

    def test_copy_puts_canvas_in_clipboard(self):
        ov = self._overlay()
        self._select(ov)
        QApplication.clipboard().clear()
        ov._copy_to_clipboard()
        pm = QApplication.clipboard().pixmap()
        self.assertFalse(pm.isNull())
        self.assertEqual(pm.width(), ov._canvas.width())

    def test_ocr_saves_temp_png_and_emits(self):
        ov = self._overlay()
        self._select(ov)
        paths, closed = [], []
        ov.ocr_requested.connect(paths.append)
        ov.closed.connect(lambda: closed.append(1))
        ov._ocr()
        self.assertEqual(len(paths), 1)
        self.assertTrue(os.path.isfile(paths[0]))
        img = QImage(paths[0])
        self.assertFalse(img.isNull())
        self.assertEqual(img.width(), ov._canvas.width())
        self.assertTrue(closed)
        os.remove(paths[0])

    def test_text_commit_pushes_undo(self):
        ov = self._overlay()
        self._select(ov)
        stack_before = len(ov._undo_stack)
        ov._commit_shape(("text", QPointF(5, 15), "备注"))
        self.assertEqual(len(ov._undo_stack), stack_before + 1)

    def test_right_click_in_annotate_cancels(self):
        ov = self._overlay()
        self._select(ov)
        cancelled = []
        ov.cancelled.connect(lambda: cancelled.append(1))
        from PyQt5.QtCore import QEvent, QPointF, Qt
        from PyQt5.QtGui import QMouseEvent
        ev = QMouseEvent(QEvent.MouseButtonPress, QPointF(1, 1),
                         QPointF(1, 1), Qt.RightButton, Qt.NoButton,
                         Qt.NoModifier)
        ov.mousePressEvent(ev)
        self.assertTrue(cancelled)
        self.assertFalse(ov._emit_closed_on_close)


if __name__ == "__main__":
    unittest.main()
