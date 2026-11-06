"""表情包收藏：stickers/ 存储、页签渲染、右键收藏入口、发送接线。

offscreen；不联网。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QEvent, QPoint  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from core.models import Message, User  # noqa: E402
from core import stickers  # noqa: E402
from ui.chat_input import ChatInput  # noqa: E402
from ui.chat_view import ChatView  # noqa: E402
from ui.emoji_picker import EmojiPicker  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class StickerStoreTest(unittest.TestCase):
    """storage_root/stickers 的写入、去重、嗅探与列表排序。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch("core.paths.storage_root",
                             return_value=self._tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def _png(self, name, size=3):
        from PyQt5.QtGui import QPixmap
        p = os.path.join(self._tmp.name, name)
        QPixmap(size, size).save(p, "PNG")
        return p

    def test_save_and_list(self):
        src = self._png("a.png")
        dst = stickers.save_sticker(src)
        self.assertTrue(os.path.isfile(dst))
        self.assertIn("stickers", dst)
        self.assertEqual(stickers.list_stickers(), [dst])

    def test_dedupe_by_content(self):
        src = self._png("a.png")
        again = os.path.join(self._tmp.name, "copy_of_a.png")
        with open(src, "rb") as f:
            data = f.read()
        with open(again, "wb") as f:
            f.write(data)
        d1 = stickers.save_sticker(src)
        d2 = stickers.save_sticker(again)
        self.assertEqual(d1, d2)
        self.assertEqual(len(stickers.list_stickers()), 1)

    def test_sniff_ext_for_weird_name(self):
        src = self._png("noext")
        os.rename(src, os.path.join(self._tmp.name, "weird"))
        weird = os.path.join(self._tmp.name, "weird")
        dst = stickers.save_sticker(weird)
        self.assertTrue(dst.endswith(".png"))

    def test_list_newest_first(self):
        import time
        a = stickers.save_sticker(self._png("a.png", size=3))
        time.sleep(0.05)
        b = stickers.save_sticker(self._png("b.png", size=4))
        self.assertEqual(stickers.list_stickers(), [b, a])

    def test_usage_sort_and_persistence(self):
        """用过次数多的排前面；计数落盘，进程视角之外仍生效。"""
        import time
        a = stickers.save_sticker(self._png("a.png", size=3))
        time.sleep(0.05)
        b = stickers.save_sticker(self._png("b.png", size=4))
        # b 更新收藏，但 a 用得多 → a 在前
        stickers.record_sticker_use(a)
        stickers.record_sticker_use(a)
        stickers.record_sticker_use(b)
        self.assertEqual(stickers.list_stickers(), [a, b])
        self.assertEqual(stickers.usage_count(a), 2)
        # 再读一次（模拟下次打开）顺序稳定
        self.assertEqual(stickers.list_stickers(), [a, b])

    def test_delete_sticker_removes_file_and_usage(self):
        src = self._png("a.png")
        dst = stickers.save_sticker(src)
        stickers.record_sticker_use(dst)
        self.assertTrue(stickers.delete_sticker(dst))
        self.assertFalse(os.path.isfile(dst))
        self.assertEqual(stickers.usage_count(dst), 0)
        self.assertEqual(stickers.list_stickers(), [])
        # 再删一次：文件已不存在，返回 False 不抛
        self.assertFalse(stickers.delete_sticker(dst))


class EmojiPickerStickerTabTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch("core.paths.storage_root",
                             return_value=self._tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        self.picker = EmojiPicker(close_on_select=False)
        self.addCleanup(self.picker.deleteLater)

    def test_mode_toggle_switches_ui(self):
        # 未 show() 的父窗里 isVisible 恒 False，用 isVisibleTo/isHidden 判断显隐
        self.assertTrue(self.picker.emoji_tab_btn.isChecked())
        self.picker.set_mode("sticker")
        self.assertFalse(self.picker._emoji_header.isVisibleTo(self.picker))
        self.assertTrue(self.picker._emoji_header.isHidden())
        self.assertTrue(self.picker.import_btn.isVisibleTo(self.picker))
        self.picker.set_mode("emoji")
        self.assertTrue(self.picker._emoji_header.isVisibleTo(self.picker))
        self.assertFalse(self.picker.import_btn.isVisibleTo(self.picker))

    def test_empty_state_hint(self):
        self.picker.set_mode("sticker")
        labels = [self.picker._grid.itemAt(i).widget()
                  for i in range(self.picker._grid.count())]
        self.assertTrue(any(isinstance(w, __import__(
            "PyQt5.QtWidgets", fromlist=["QLabel"]).QLabel)
            and "右键" in w.text() for w in labels))

    def test_sticker_click_emits_path(self):
        from PyQt5.QtGui import QPixmap
        src = os.path.join(self._tmp.name, "s.png")
        QPixmap(4, 4).save(src, "PNG")
        stickers.save_sticker(src)
        got = []
        self.picker.sticker_selected.connect(got.append)
        self.picker.set_mode("sticker")
        # 找到第一个表情按钮并点击
        for i in range(self.picker._grid.count()):
            w = self.picker._grid.itemAt(i).widget()
            if isinstance(w, __import__("PyQt5.QtWidgets",
                                        fromlist=["QToolButton"]).QToolButton):
                w.click()
                break
        self.assertEqual(len(got), 1)
        self.assertTrue(os.path.isfile(got[0]))

    def test_sticker_click_records_usage(self):
        from PyQt5.QtGui import QPixmap
        src = os.path.join(self._tmp.name, "s.png")
        QPixmap(4, 4).save(src, "PNG")
        path = stickers.save_sticker(src)
        self.picker.set_mode("sticker")
        self.picker._on_sticker_clicked(path)
        self.picker._on_sticker_clicked(path)
        self.assertEqual(stickers.usage_count(path), 2)

    def _sticker_btn(self):
        from PyQt5.QtWidgets import QToolButton
        for i in range(self.picker._grid.count()):
            w = self.picker._grid.itemAt(i).widget()
            if isinstance(w, QToolButton):
                return w
        return None

    def _save_one(self):
        from PyQt5.QtGui import QPixmap
        src = os.path.join(self._tmp.name, "s.png")
        QPixmap(6, 6).save(src, "PNG")
        return stickers.save_sticker(src)

    def test_context_menu_only_delete(self):
        # 预览改为悬停自动弹出，右键只留删除
        path = self._save_one()
        self.picker.set_mode("sticker")
        with mock.patch("ui.emoji_picker.QMenu") as MQ:
            self.picker._show_sticker_menu(path, QPoint(0, 0))
        texts = [c.args[0] for c in MQ.return_value.addAction.call_args_list
                 if c.args]
        self.assertEqual(texts, ["删除"])

    def test_delete_with_confirm_removes_and_rebuilds(self):
        path = self._save_one()
        self.picker.set_mode("sticker")
        self.assertIsNotNone(self._sticker_btn())
        with mock.patch("ui.confirm_dialog.confirm", return_value=True) as cf:
            self.picker._delete_sticker(path)
        cf.assert_called_once()
        self.assertFalse(os.path.isfile(path))
        # 网格已重建为空态提示
        from PyQt5.QtWidgets import QLabel
        labels = [self.picker._grid.itemAt(i).widget()
                  for i in range(self.picker._grid.count())]
        self.assertTrue(any(isinstance(w, QLabel) for w in labels))

    def test_delete_cancelled_keeps_sticker(self):
        path = self._save_one()
        self.picker.set_mode("sticker")
        with mock.patch("ui.confirm_dialog.confirm", return_value=False):
            self.picker._delete_sticker(path)
        self.assertTrue(os.path.isfile(path))

    def test_import_menu_offers_files_and_folder(self):
        from ui.emoji_picker import EmojiPicker
        self.picker.set_mode("sticker")
        with mock.patch("ui.emoji_picker.QMenu") as MQ:
            acts = [mock.Mock(), mock.Mock()]
            # 两次打开菜单各调两次 addAction
            MQ.return_value.addAction.side_effect = acts + acts
            MQ.return_value.exec_.return_value = acts[0]
            with mock.patch.object(EmojiPicker,
                                   "_import_files_dialog") as mf, \
                    mock.patch.object(EmojiPicker,
                                      "_import_folder_dialog") as mfo:
                self.picker._on_import_clicked()
                mf.assert_called_once()
                mfo.assert_not_called()
                MQ.return_value.exec_.return_value = acts[1]
                self.picker._on_import_clicked()
            mf.assert_called_once()
            mfo.assert_called_once()

    def test_import_failure_shows_reason(self):
        from PyQt5.QtGui import QPixmap
        src = os.path.join(self._tmp.name, "s.png")
        QPixmap(6, 6).save(src, "PNG")
        self.picker.set_mode("sticker")
        with mock.patch("ui.emoji_picker.QFileDialog") as MF, \
                mock.patch("core.stickers.save_sticker",
                           side_effect=OSError("磁盘空间不足")):
            MF.getOpenFileNames.return_value = ([src], "")
            self.picker._import_files_dialog()
        self.assertIn("磁盘空间不足", self.picker.hint.text())
        self.assertIn("导入失败", self.picker.hint.text())

    def test_import_invalid_image_shows_reason(self):
        bad = os.path.join(self._tmp.name, "bad.png")
        with open(bad, "wb") as f:
            f.write(b"this is not an image")
        self.picker.set_mode("sticker")
        with mock.patch("core.stickers.save_sticker") as save:
            self.picker._import_many([bad])
        self.assertIn("不是有效图片", self.picker.hint.text())
        save.assert_not_called()

    def test_import_success_repopulates(self):
        from PyQt5.QtGui import QPixmap
        src = os.path.join(self._tmp.name, "s.png")
        QPixmap(6, 6).save(src, "PNG")
        self.picker.set_mode("sticker")
        self.picker._import_many([src])
        self.assertIsNotNone(self._sticker_btn())

    def test_import_multi_files(self):
        from PyQt5.QtGui import QPixmap
        paths = []
        for i in range(3):
            p = os.path.join(self._tmp.name, f"m{i}.png")
            QPixmap(i + 2, i + 2).save(p, "PNG")
            paths.append(p)
        self.picker.set_mode("sticker")
        with mock.patch("ui.emoji_picker.QFileDialog") as MF:
            MF.getOpenFileNames.return_value = (paths, "")
            self.picker._import_files_dialog()
        self.assertIn("已导入 3 张", self.picker.hint.text())
        self.assertEqual(len(stickers.list_stickers()), 3)

    def test_import_folder_scans_images(self):
        from PyQt5.QtGui import QPixmap
        folder = os.path.join(self._tmp.name, "batch")
        os.makedirs(folder)
        for i in range(2):
            QPixmap(i + 3, i + 3).save(
                os.path.join(folder, f"f{i}.png"), "PNG")
        with open(os.path.join(folder, "说明.txt"), "w") as f:
            f.write("not an image")
        os.makedirs(os.path.join(folder, "sub"))
        QPixmap(5, 5).save(os.path.join(folder, "sub", "nested.png"), "PNG")
        self.picker.set_mode("sticker")
        with mock.patch("ui.emoji_picker.QFileDialog") as MF:
            MF.getExistingDirectory.return_value = folder
            self.picker._import_folder_dialog()
        # 只收顶层图片文件，文本与子目录不进
        self.assertIn("已导入 2 张", self.picker.hint.text())
        self.assertEqual(len(stickers.list_stickers()), 2)

    def test_import_dedupes_same_content(self):
        # 完全相同的内容不同文件名：core 按内容 md5 去重，只算新收一张
        from PyQt5.QtGui import QPixmap
        src = os.path.join(self._tmp.name, "a.png")
        QPixmap(7, 7).save(src, "PNG")
        copy = os.path.join(self._tmp.name, "a_copy.png")
        with open(src, "rb") as f:
            data = f.read()
        with open(copy, "wb") as f:
            f.write(data)
        other = os.path.join(self._tmp.name, "b.png")
        QPixmap(8, 8).save(other, "PNG")
        self.picker.set_mode("sticker")
        self.picker._import_many([src, copy, other])
        self.assertIn("已导入 2 张", self.picker.hint.text())
        self.assertIn("1 张重复已跳过", self.picker.hint.text())
        self.assertEqual(len(stickers.list_stickers()), 2)

    def test_import_all_duplicates_no_new(self):
        from PyQt5.QtGui import QPixmap
        src = os.path.join(self._tmp.name, "a.png")
        QPixmap(9, 9).save(src, "PNG")
        self.picker.set_mode("sticker")
        self.picker._import_many([src])  # 第一次是新增
        self.picker._import_many([src])  # 第二次应报全部重复
        self.assertIn("重复，未新增", self.picker.hint.text())
        self.assertEqual(len(stickers.list_stickers()), 1)

    def test_save_report_new_flag(self):
        from PyQt5.QtGui import QPixmap
        src = os.path.join(self._tmp.name, "a.png")
        QPixmap(10, 10).save(src, "PNG")
        dst, created = stickers.save_sticker(src, report_new=True)
        self.assertTrue(created)
        again, created2 = stickers.save_sticker(src, report_new=True)
        self.assertEqual(dst, again)
        self.assertFalse(created2)

    def test_hover_preview_shows_and_hides(self):
        path = self._save_one()
        self.picker.set_mode("sticker")
        btn = self._sticker_btn()
        self.assertIsNotNone(btn)
        QApplication.sendEvent(btn, QEvent(QEvent.Enter))
        preview = self.picker._hover_preview
        self.assertIsNotNone(preview)
        self.assertTrue(preview.isVisible())
        self.assertIn(os.path.basename(path), preview._tip.text())
        QApplication.sendEvent(btn, QEvent(QEvent.Leave))
        self.assertFalse(preview.isVisible())

    def test_hover_preview_hidden_with_picker(self):
        # 面板失活自关/关闭时预览是独立 Tool 窗，必须跟着收
        path = self._save_one()
        self.picker.set_mode("sticker")
        self.picker.show()  # 未 show 过的窗 hide() 不触发 hideEvent
        btn = self._sticker_btn()
        QApplication.sendEvent(btn, QEvent(QEvent.Enter))
        preview = self.picker._hover_preview
        self.assertTrue(preview.isVisible())
        self.picker.hide()
        self.assertFalse(preview.isVisible())


class EmojiPickerToggleTest(unittest.TestCase):
    """输入框表情按钮的开关语义（Tool 窗失活自关 + 按钮点击回放的竞态）。"""

    def setUp(self):
        self.inp = ChatInput()
        self.addCleanup(self.inp.deleteLater)

    def test_toggle_opens_then_closes(self):
        self.inp._toggle_emoji_picker()
        picker = self.inp._emoji_picker
        self.assertTrue(picker.isVisible())
        self.inp._toggle_emoji_picker()
        self.assertFalse(picker.isVisible())

    def test_insert_emoji_keeps_picker_open(self):
        # 插入后不能把焦点抢回输入框：Tool 窗失活会立即自关，连选就断了
        self.inp._toggle_emoji_picker()
        picker = self.inp._emoji_picker
        picker.emoji_selected.emit("👍")
        self.assertEqual(self.inp.input.toPlainText(), "👍")
        self.assertTrue(picker.isVisible())


class StickerSendWiringTest(unittest.TestCase):
    """聊天区收藏表情与发送接线。"""

    def test_chat_input_forwards_picker_signal(self):
        inp = ChatInput()
        self.addCleanup(inp.deleteLater)
        got = []
        inp.sticker_selected.connect(got.append)
        inp._toggle_emoji_picker()
        inp._emoji_picker.sticker_selected.emit("/tmp/x.png")
        self.assertEqual(got, ["/tmp/x.png"])

    def test_chat_view_save_sticker_uses_local_path(self):
        view = ChatView()
        self.addCleanup(view.deleteLater)
        with mock.patch.object(view, "local_image_path") as lip, \
                mock.patch("core.stickers.save_sticker") as save, \
                mock.patch.object(view, "status_message") as sm:
            view._sticker_saved("/cache/img.png")
            save.assert_called_once_with("/cache/img.png")
            sm.emit.assert_called_once()

    def test_context_menu_has_sticker_entry_for_image(self):
        view = ChatView()
        self.addCleanup(view.deleteLater)
        sender = User(id="u1", username="a", name="A")
        msg = Message(id="m1", room_id="r", sender=sender,
                      msg_type="image", file_url="/file-upload/x.png")
        with mock.patch("ui.chat_view.QMenu") as MQ:
            view._show_context_menu(msg, QPoint(0, 0))
        texts = [c.args[0] for c in MQ.return_value.addAction.call_args_list
                 if c.args]
        self.assertIn("收藏为表情", texts)

    def test_context_menu_no_sticker_entry_for_text(self):
        view = ChatView()
        self.addCleanup(view.deleteLater)
        sender = User(id="u1", username="a", name="A")
        msg = Message(id="m1", room_id="r", sender=sender,
                      msg_type="text", text="hi")
        with mock.patch("ui.chat_view.QMenu") as MQ:
            view._show_context_menu(msg, QPoint(0, 0))
        texts = [c.args[0] for c in MQ.return_value.addAction.call_args_list
                 if c.args]
        self.assertNotIn("收藏为表情", texts)


if __name__ == "__main__":
    unittest.main()
