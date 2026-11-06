"""离线 OCR 的 UI 链路：右键菜单取图、查看器按钮、结果对话框、主窗口编排。

offscreen；下载与识别一律 mock，不联网、不加载真模型。
运行：python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PyQt5.QtGui import QMouseEvent  # noqa: E402
from PyQt5.QtWidgets import QApplication, QDialog  # noqa: E402

import config  # noqa: E402
from core.models import Message, User  # noqa: E402
from ui import chat_view as cv  # noqa: E402
from ui.chat_view import ChatView  # noqa: E402
from ui.image_viewer import ImageViewer  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.ocr_dialog import OcrResultDialog, show_ocr_result  # noqa: E402

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


class _FakeLoader:
    def __init__(self, cached=None):
        self.auth_token = ""
        self.auth_uid = ""
        self._cached = cached

    def resolve(self, url):
        return url if url.startswith("http") else "https://s" + url

    def cached_original_path(self, url):
        return self._cached

    def cached_original(self, url):
        return None

    def cached_pixmap(self, url):
        return None


def _mock_loader(cached=None):
    """ChatView 的 image_loader 替身：Mock 兜住气泡渲染会调到的其他方法。"""
    loader = mock.Mock()
    loader.resolve.side_effect = lambda u: (
        u if str(u).startswith("http") else "https://s" + str(u))
    loader.cached_original_path.return_value = cached
    return loader


# --------------------------------------------------------------------- #
# 右键菜单
# --------------------------------------------------------------------- #
class ChatViewOcrMenuTest(unittest.TestCase):

    def setUp(self):
        self.view = ChatView()
        self.view.resize(640, 420)
        self.view.set_room("r")
        self.view.image_loader = _mock_loader()
        self.emitted = []
        self.view.ocr_requested.connect(self.emitted.append)

    def tearDown(self):
        self.view.deleteLater()

    def _menu(self, msg):
        self.view.model.append_message(msg)
        menus = []
        with mock.patch.object(cv.QMenu, "exec_",
                               lambda self, pos: menus.append(self)):
            self.view._show_context_menu(self.view.model.get_message(0),
                                        QPoint(0, 0))
        return menus[0]

    def test_image_message_has_single_entry_with_absolute_url(self):
        menu = self._menu(_msg("m1", msg_type="image",
                               file_url="/file-upload/a.png"))
        acts = [a for a in menu.actions() if a.text() == "识别文字"]
        self.assertEqual(len(acts), 1)
        self.assertIsNone(acts[0].menu())
        acts[0].triggered.emit()
        self.assertEqual(self.emitted, ["https://s/file-upload/a.png"])

    def test_inline_images_become_submenu(self):
        menu = self._menu(_msg("m2", text="看 ![a](/file-upload/i1.png) "
                                          "和 ![b](https://x/i2.png)"))
        acts = [a for a in menu.actions() if a.text() == "识别文字"]
        self.assertEqual(len(acts), 1)
        labels = [a.text() for a in acts[0].menu().actions()]
        self.assertEqual(labels, ["图片 1", "图片 2"])
        for act in acts[0].menu().actions():
            act.triggered.emit()
        self.assertEqual(self.emitted, ["https://s/file-upload/i1.png",
                                       "https://x/i2.png"])

    def test_plain_text_has_no_entry(self):
        menu = self._menu(_msg("m3", text="只有文字"))
        self.assertFalse([a for a in menu.actions() if a.text() == "识别文字"])

    def test_non_image_attachment_has_no_entry(self):
        for i, t in enumerate(("file", "video", "audio")):
            self.view.model.append_message(_msg(f"m{i}", msg_type=t,
                                                file_url="/f/x"))
            self.assertEqual(self.view._ocr_targets(
                self.view.model.get_message(i)), [])

    def test_targets_skip_relative_when_no_loader(self):
        self.view.image_loader = None
        self.view.model.append_message(_msg("m9", msg_type="image",
                                            file_url="https://x/a.png"))
        self.assertEqual(self.view._ocr_targets(self.view.model.get_message(0)),
                         ["https://x/a.png"])


class LocalImagePathTest(unittest.TestCase):

    def setUp(self):
        self.view = ChatView()
        self.view.set_room("r")

    def tearDown(self):
        self.view.deleteLater()

    def test_disk_original_cache_wins(self):
        self.view.image_loader = _mock_loader(cached=r"C:\cache\images\abc")
        ready = mock.Mock()
        self.view.local_image_path("https://s/x/a.png", ready, mock.Mock())
        ready.assert_called_once_with(r"C:\cache\images\abc")

    def test_cache_miss_downloads_to_file_cache(self):
        self.view.image_loader = _mock_loader(cached=None)
        with mock.patch.object(ChatView, "_ensure_cached_file") as ensure, \
                mock.patch("core.paths.files_dir",
                           return_value=os.path.join(os.environ.get("TEMP", "."),
                                                     "ocr_test_files")):
            self.view.local_image_path("/file-upload/x/a.png",
                                       mock.Mock(), mock.Mock())
        url, dest = ensure.call_args.args[:2]
        self.assertEqual(url, "https://s/file-upload/x/a.png")
        self.assertTrue(dest.endswith("a.png"), dest)

    def test_empty_url_fails_fast(self):
        self.view.image_loader = _mock_loader(cached=None)
        fail = mock.Mock()
        self.view.local_image_path("", mock.Mock(), fail)
        fail.assert_called_once_with("没有可识别的图片")


# --------------------------------------------------------------------- #
# 图片查看器
# --------------------------------------------------------------------- #
class ImageViewerOcrTest(unittest.TestCase):

    def setUp(self):
        patcher = mock.patch.object(ImageViewer, "_start_load")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_toolbar_button_emits_current_url(self):
        viewer = ImageViewer("/file-upload/x/a.png", _FakeLoader())
        self.addCleanup(viewer.deleteLater)
        self.assertTrue(viewer._ocr_btn.text())
        emitted = []
        viewer.ocr_requested.connect(emitted.append)
        viewer._ocr_btn.click()
        self.assertEqual(emitted, ["/file-upload/x/a.png"])
        self.assertIn("识别", viewer._hint)

    def test_no_url_only_hints(self):
        viewer = ImageViewer("", None)
        self.addCleanup(viewer.deleteLater)
        emitted = []
        viewer.ocr_requested.connect(emitted.append)
        viewer._request_ocr()
        self.assertEqual(emitted, [])
        self.assertIn("没有可识别", viewer._hint)


# --------------------------------------------------------------------- #
# 结果对话框
# --------------------------------------------------------------------- #
class OcrResultDialogTest(unittest.TestCase):

    def _dialog(self, text="第一行\n第二行"):
        dlg = OcrResultDialog(None, None, text=text)
        self.addCleanup(dlg.deleteLater)
        return dlg

    def test_shows_text_and_counts(self):
        dlg = self._dialog()
        self.assertEqual(dlg.plain_text, "第一行\n第二行")
        self.assertIn("2 行", dlg._count.text())
        # 字数只算非空白字符：两行各 3 字
        self.assertIn("6 字", dlg._count.text())

    def test_editing_is_possible_and_updates_actions(self):
        dlg = self._dialog()
        dlg._view.setPlainText("改过的")
        self.assertEqual(dlg.plain_text, "改过的")
        self.assertTrue(dlg._insert_btn.isEnabled())

    def test_empty_text_disables_actions(self):
        dlg = self._dialog("   ")
        self.assertFalse(dlg._copy_btn.isEnabled())
        self.assertFalse(dlg._insert_btn.isEnabled())

    def test_insert_returns_text_and_accepts(self):
        dlg = self._dialog()
        dlg._insert_btn.click()
        self.assertEqual(dlg.inserted_text, "第一行\n第二行")
        self.assertEqual(dlg.result(), QDialog.Accepted)

    def test_copy_puts_text_on_clipboard(self):
        dlg = self._dialog()
        dlg._copy_btn.click()
        self.assertEqual(_APP.clipboard().text(), "第一行\n第二行")
        self.assertIn("已复制", dlg._status.text())

    def test_close_rejects_without_text(self):
        dlg = self._dialog()
        dlg._close_btn_bottom.click()
        self.assertEqual(dlg.result(), QDialog.Rejected)
        self.assertIsNone(dlg.inserted_text)

    def test_helper_returns_inserted_text(self):
        with mock.patch.object(OcrResultDialog, "exec_",
                               lambda self: self._on_insert() or 1):
            self.assertEqual(show_ocr_result(None, None, "内容"), "内容")

    def test_helper_returns_none_when_closed(self):
        with mock.patch.object(OcrResultDialog, "exec_", lambda self: 0):
            self.assertIsNone(show_ocr_result(None, None, "内容"))

    def test_dialog_is_resizable(self):
        dlg = self._dialog()
        self.assertEqual(dlg.minimumWidth(), 560)
        dlg.resize(720, 640)
        self.assertEqual((dlg.width(), dlg.height()), (720, 640))

    def test_edge_drag_resizes_via_frameless_border(self):
        """无边框窗口没有系统边框：用户拖右下边沿也要能放大。"""
        dlg = self._dialog()
        dlg.resize(560, 520)
        dlg.show()
        self.addCleanup(dlg.close)
        QApplication.processEvents()
        before = (dlg.width(), dlg.height())
        start = QPoint(dlg.width() - 2, dlg.height() - 2)
        for type_, pos, buttons in (
                (QEvent.MouseButtonPress, start, Qt.LeftButton),
                (QEvent.MouseMove, start + QPoint(40, 24), Qt.LeftButton),
                (QEvent.MouseButtonRelease, start + QPoint(40, 24),
                 Qt.NoButton)):
            QApplication.sendEvent(dlg, QMouseEvent(
                type_, QPointF(pos), QPointF(pos),
                QPointF(dlg.mapToGlobal(pos)),
                Qt.LeftButton, buttons, Qt.NoModifier))
        self.assertEqual((dlg.width(), dlg.height()),
                         (before[0] + 40, before[1] + 24))

    def test_stay_on_top_flag_follows_argument(self):
        # 查看器是置顶全屏窗：结果窗不置顶会被压在其后（真机实测）
        plain = self._dialog()
        self.assertFalse(
            bool(plain.windowFlags() & Qt.WindowStaysOnTopHint))
        dlg = OcrResultDialog(None, None, text="x", stay_on_top=True)
        self.addCleanup(dlg.deleteLater)
        self.assertTrue(bool(dlg.windowFlags() & Qt.WindowStaysOnTopHint))


# --------------------------------------------------------------------- #
# 主窗口编排
# --------------------------------------------------------------------- #
class _NoLogin(MainWindow):
    def _try_auto_login(self):
        self.auto_login_skipped = True


class _Result:
    def __init__(self, text):
        self.text = text


class MainWindowOcrTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = self._tmp.name
        self._orig = (config.CONFIG_DIR, config.CONFIG_FILE,
                      config.KEY_FILE, config.LEGACY_INI_FILE)
        config.CONFIG_DIR = os.path.join(tmp, "cfgdir")
        config.CONFIG_FILE = os.path.join(config.CONFIG_DIR, "config.yaml")
        config.KEY_FILE = os.path.join(config.CONFIG_DIR, "secret.key")
        config.LEGACY_INI_FILE = os.path.join(tmp, "config.ini")
        self.win = _NoLogin()
        self.win._active_room_id = "r1"
        self.win.client = mock.Mock()
        self.win.client.me.username = "me"
        self.win._room_messages["r1"] = [
            _msg("m1", msg_type="image", file_url="https://s/a.png")]

    def tearDown(self):
        self.win.realtime and self.win.realtime.stop()
        self.win.deleteLater()
        (config.CONFIG_DIR, config.CONFIG_FILE,
         config.KEY_FILE, config.LEGACY_INI_FILE) = self._orig
        self._tmp.cleanup()

    def test_chat_view_signal_is_connected(self):
        self.assertTrue(self.win.chat_area.chat_view.receivers(
            self.win.chat_area.chat_view.ocr_requested))

    def test_missing_dependency_notifies_install_hint(self):
        with mock.patch("core.ocr.is_available", return_value=False), \
                mock.patch("ui.main_window.notify") as notify:
            self.win.chat_area.chat_view.ocr_requested.emit("https://s/a.png")
        self.assertEqual(notify.call_args.args[2], "缺少识别组件")
        self.assertIn("rapidocr", notify.call_args.args[3])

    def test_busy_request_is_queued_out(self):
        self.win._ocr_task = object()
        with mock.patch.object(ChatView, "local_image_path") as resolve, \
                mock.patch("core.ocr.is_available", return_value=True):
            self.win.chat_area.chat_view.ocr_requested.emit("https://s/a.png")
        resolve.assert_not_called()

    def _hint_after_request(self, loaded: bool) -> str:
        with mock.patch.object(ChatView, "local_image_path"), \
                mock.patch("core.ocr.is_available", return_value=True), \
                mock.patch("core.ocr.is_loaded", return_value=loaded):
            self.win.chat_area.chat_view.ocr_requested.emit("https://s/a.png")
        return self.win.statusBar().currentMessage()

    def test_first_recognition_hint_mentions_slow_model_load(self):
        msg = self._hint_after_request(loaded=False)
        self.assertIn("正在本机识别文字", msg)
        self.assertIn("首次加载模型稍慢", msg)

    def test_loaded_engine_drops_slow_hint(self):
        """引擎已预热/识别过：再提示「首次加载稍慢」会误导。"""
        msg = self._hint_after_request(loaded=True)
        self.assertIn("正在本机识别文字", msg)
        self.assertNotIn("首次", msg)

    def _drive_to_recognize(self, ready_path=r"C:\cache\images\abc"):
        """点菜单 → 取到本地路径 → 提交后台识别，返回 (run_async 参数)。"""
        captured = {}

        def fake_local(url, on_ready, on_fail=None):
            captured["url"] = url
            on_ready(ready_path)

        def fake_run_async(fn, on_done, on_fail, parent=None):
            captured["job"] = fn
            captured["on_done"] = on_done
            captured["on_fail"] = on_fail
            captured["parent"] = parent
            return "task"

        with mock.patch.object(self.win.chat_area.chat_view,
                               "local_image_path",
                               new=fake_local), \
                mock.patch("core.ocr.is_available", return_value=True), \
                mock.patch("ui.api_workers.run_async", new=fake_run_async):
            self.win.chat_area.chat_view.ocr_requested.emit("https://s/a.png")
        return captured

    def test_full_flow_recognizes_then_inserts_input(self):
        captured = self._drive_to_recognize()
        self.assertEqual(captured["url"], "https://s/a.png")
        self.assertEqual(self.win._ocr_task, "task")
        # 后台跑的必须是 core.ocr.recognize(本地路径)
        with mock.patch("core.ocr.recognize", return_value=_Result("识别结果")) as rec:
            self.assertIs(captured["job"](), rec.return_value)
        rec.assert_called_once_with(r"C:\cache\images\abc")
        with mock.patch("ui.ocr_dialog.show_ocr_result",
                        return_value="识别结果") as show, \
                mock.patch.object(self.win.chat_area.input,
                                  "append_text") as append:
            captured["on_done"](_Result("识别结果"))
        self.assertEqual(show.call_args.args[2], "识别结果")
        append.assert_called_once_with("识别结果")
        self.assertIsNone(self.win._ocr_task)

    def test_empty_result_notifies_instead_of_dialog(self):
        captured = self._drive_to_recognize()
        with mock.patch("ui.ocr_dialog.show_ocr_result") as show, \
                mock.patch("ui.main_window.notify") as notify:
            captured["on_done"](_Result("  "))
        show.assert_not_called()
        self.assertEqual(notify.call_args.args[2], "没识别到文字")

    def test_failure_notifies(self):
        captured = self._drive_to_recognize()
        with mock.patch("ui.main_window.notify") as notify:
            captured["on_fail"]("识别失败：模型缺失")
        self.assertEqual(notify.call_args.args[2], "识别失败")
        self.assertIsNone(self.win._ocr_task)

    def test_stale_token_result_is_dropped(self):
        captured = self._drive_to_recognize()
        self.win._ocr_token += 1   # 模拟用户又发起了新一轮
        with mock.patch("ui.ocr_dialog.show_ocr_result") as show:
            captured["on_done"](_Result("识别结果"))
        show.assert_not_called()

    def test_stale_token_failure_is_dropped(self):
        captured = self._drive_to_recognize()
        self.win._ocr_token += 1
        with mock.patch("ui.main_window.notify") as notify:
            captured["on_fail"]("取图失败")
        notify.assert_not_called()

    def test_image_fetch_failure_notifies(self):
        captured = {}

        def fake_local(url, on_ready, on_fail=None):
            captured["on_fail"] = on_fail

        with mock.patch.object(self.win.chat_area.chat_view,
                               "local_image_path", new=fake_local), \
                mock.patch("core.ocr.is_available", return_value=True), \
                mock.patch("ui.main_window.notify") as notify:
            self.win.chat_area.chat_view.ocr_requested.emit("https://s/a.png")
            captured["on_fail"]("图片获取失败，请稍后再试")
        self.assertEqual(notify.call_args.args[2], "取图失败")

    def test_viewer_hint_cleared_when_result_arrives(self):
        """识别收尾后查看器不能还挂着「正在本机识别文字…」。"""
        captured = self._drive_to_recognize()
        msg = self.win._room_messages["r1"][0]
        self.win.image_loader = _FakeLoader()
        with mock.patch.object(ImageViewer, "_start_load"):
            self.win._on_image_view_requested(msg)
        viewer = self.win.findChildren(ImageViewer)[0]
        self.addCleanup(viewer.close)
        self.addCleanup(viewer.deleteLater)
        viewer.show()
        viewer._show_hint("正在本机识别文字…")
        with mock.patch("ui.ocr_dialog.show_ocr_result",
                        return_value=None):
            captured["on_done"](_Result("text"))
        self.assertEqual(viewer._hint, "")

    def test_viewer_hint_cleared_on_failure(self):
        captured = self._drive_to_recognize()
        with mock.patch.object(ImageViewer, "_start_load"):
            viewer = ImageViewer("/file-upload/x/a.png", _FakeLoader())
        self.addCleanup(viewer.close)
        self.addCleanup(viewer.deleteLater)
        viewer.show()
        viewer._show_hint("正在本机识别文字…")
        with mock.patch("ui.main_window.notify"):
            captured["on_fail"]("识别失败：模型缺失")
        self.assertEqual(viewer._hint, "")

    def test_warmup_skipped_without_client_or_dep(self):
        """预热门控：未登录 / 依赖缺失时不起线程。"""
        from core import ocr
        with mock.patch("ui.api_workers.run_async") as run:
            self.win.client = None
            self.win._warmup_ocr()
            run.assert_not_called()
            self.win.client = object()
            with mock.patch.object(ocr, "is_available", return_value=False):
                self.win._warmup_ocr()
            run.assert_not_called()
            with mock.patch.object(ocr, "is_available", return_value=True):
                self.win._warmup_ocr()
            run.assert_called_once()

    def test_viewer_button_is_wired_to_main_window(self):
        """查看器「提取文字」→ 主窗口识别：确认线真的接上。"""
        msg = self.win._room_messages["r1"][0]
        self.win.image_loader = _FakeLoader()
        with mock.patch.object(ImageViewer, "_start_load"):
            self.win._on_image_view_requested(msg)
        viewers = self.win.findChildren(ImageViewer)
        self.assertEqual(len(viewers), 1)
        # _on_image_view_requested 内部已 show，必须关掉，否则可见查看器
        # 泄漏到后续用例，_viewer_on_top 类断言被污染
        self.addCleanup(viewers[0].close)
        with mock.patch.object(ChatView, "local_image_path") as resolve, \
                mock.patch("core.ocr.is_available", return_value=True):
            viewers[0]._request_ocr()
        resolve.assert_called_once()
        self.assertEqual(resolve.call_args.args[0], "https://s/a.png")

    def test_viewer_on_top_detection(self):
        self.assertFalse(self.win._viewer_on_top())
        msg = self.win._room_messages["r1"][0]
        self.win.image_loader = _FakeLoader()
        with mock.patch.object(ImageViewer, "_start_load"):
            self.win._on_image_view_requested(msg)
        viewer = self.win.findChildren(ImageViewer)[0]
        self.addCleanup(viewer.deleteLater)
        viewer.show()
        self.assertTrue(self.win._viewer_on_top())
        viewer.close()
        self.assertFalse(self.win._viewer_on_top())

    def test_result_dialog_topmost_while_viewer_open(self):
        captured = self._drive_to_recognize()
        msg = self.win._room_messages["r1"][0]
        self.win.image_loader = _FakeLoader()
        with mock.patch.object(ImageViewer, "_start_load"):
            self.win._on_image_view_requested(msg)
        viewer = self.win.findChildren(ImageViewer)[0]
        self.addCleanup(viewer.deleteLater)
        self.addCleanup(viewer.close)
        viewer.show()
        with mock.patch("ui.ocr_dialog.show_ocr_result",
                        return_value=None) as show:
            captured["on_done"](_Result("text"))
        self.assertTrue(show.call_args.kwargs.get("stay_on_top"))
        # 查看器关掉后不再置顶（普通右键识别路径不变）
        viewer.close()
        with mock.patch("ui.ocr_dialog.show_ocr_result",
                        return_value=None) as show2:
            captured2 = self._drive_to_recognize()
            captured2["on_done"](_Result("text"))
        self.assertFalse(show2.call_args.kwargs.get("stay_on_top"))


if __name__ == "__main__":
    unittest.main()
