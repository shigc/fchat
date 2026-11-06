"""发送输入框新功能测试：粘贴图片、拖拽文件、Markdown 预览、截图按钮。

GUI 测试，offscreen 渲染，不联网。
运行：python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import Qt, QUrl, QMimeData  # noqa: E402
from PyQt5.QtGui import (  # noqa: E402
    QPixmap, QImage, QColor, QImageReader, QTextCursor, QTextDocument,
)
from PyQt5.QtWidgets import QApplication  # noqa: E402

from ui.chat_input import ChatInput  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _png() -> QPixmap:
    img = QImage(10, 10, QImage.Format_ARGB32)
    img.fill(QColor("red"))
    return QPixmap.fromImage(img)


class FakeMime:
    """只暴露 ChatInput 用到的 mime 接口。"""

    def __init__(self, urls=None):
        self._urls = urls or []

    def urls(self):
        return self._urls


class FakeDropEvent:
    def __init__(self, mime):
        self._mime = mime
        self._accepted = False

    def mimeData(self):
        return self._mime

    def acceptProposedAction(self):
        self._accepted = True

    @property
    def accepted(self):
        return self._accepted


class TestPasteImage(unittest.TestCase):
    def test_paste_image_inserts_inline(self):
        # 粘贴剪贴板图片不再立即发送，而是内联插入输入框光标处（与截图一致），
        # 发送时由 _collect_inline_parts 取回随 send 一起发出。
        inp = ChatInput()
        sent = []
        inp.send_file.connect(sent.append)
        mime = QMimeData()
        mime.setImageData(_png().toImage())
        inp.input.insertFromMimeData(mime)
        self.assertEqual(sent, [])
        # 输入框文档里应含内联图片片段
        self.assertTrue(self._has_image_fragment(inp.input.document()))

    @staticmethod
    def _has_image_fragment(doc):
        block = doc.begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                if it.fragment().charFormat().isImageFormat():
                    return True
                it += 1
            block = block.next()
        return False

    def test_paste_pixmap_clipboard_image_inserts_inline(self):
        # 「复制为图片」用 clipboard().setPixmap 写入，读回时 imageData() 是
        # QPixmap 而非 QImage——粘贴必须也能识别，否则这类图贴不进去。
        from PyQt5.QtGui import QPixmap, QColor
        pm = QPixmap(100, 50)
        pm.fill(QColor("red"))
        inp = ChatInput()
        sent = []
        inp.send_file.connect(sent.append)
        mime = QMimeData()
        mime.setImageData(pm)  # setImageData 收到 QPixmap，读回即 QPixmap
        inp.input.insertFromMimeData(mime)
        self.assertEqual(sent, [])
        self.assertTrue(self._has_image_fragment(inp.input.document()))

    def test_paste_then_send_emits_mixed(self):
        # 粘贴的图片内联插入后，按发送键应合并成一次 send_mixed（不再拆成 send_file）
        inp = ChatInput()
        mixed = []
        inp.send_mixed.connect(mixed.append)
        mime = QMimeData()
        mime.setImageData(_png().toImage())
        inp.input.insertFromMimeData(mime)
        inp._send()
        self.assertEqual(len(mixed), 1)
        parts = mixed[0]
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0][0], "image")
        path = parts[0][1]
        self.assertTrue(path.endswith(".png"))
        self.assertTrue(os.path.exists(path))
        os.remove(path)
        self.assertEqual(inp.input.toPlainText(), "")

    def test_paste_nonimage_text_goes_to_qtextedit(self):
        inp = ChatInput()
        sent = []
        inp.send_file.connect(sent.append)
        mime = QMimeData()
        mime.setText("plain text")
        # 非图片、非本地文件：应落在 QTextEdit 默认分支（塞进文本），不发文件
        inp.input.insertFromMimeData(mime)
        self.assertEqual(sent, [])
        self.assertEqual(inp.input.toPlainText(), "plain text")

    def test_drop_local_image_file_embeds_inline(self):
        # 拖入本地图片文件不再立即 send_file，改为在输入框内联，发送时走 send_mixed
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.close()
        img = _png().toImage()
        img.save(tmp.name, "PNG")
        try:
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(tmp.name)])
            inp = ChatInput()
            sent = []
            mixed = []
            inp.send_file.connect(sent.append)
            inp.send_mixed.connect(mixed.append)
            inp.input.insertFromMimeData(mime)
            self.assertEqual(sent, [])
            self.assertTrue(self._has_image_fragment(inp.input.document()))
            inp._send()
            # 图片内嵌后发送为一次 send_mixed，含 ("image", 路径)
            self.assertEqual(len(mixed), 1)
            parts = mixed[0]
            self.assertEqual(len(parts), 1)
            self.assertEqual(parts[0][0], "image")
            self.assertTrue(os.path.exists(parts[0][1]))
            os.remove(parts[0][1])
        finally:
            os.remove(tmp.name)

    def test_drop_local_text_file_embeds_as_file(self):
        # 拖入本地文本文件内嵌为文件占位；无文字无截图时发送走 send_file
        # 正规附件通道（上传 + mediaConfirm，上传永久保留），不再拼进混排
        tmp = tempfile.NamedTemporaryFile(suffix=".txt", delete=False)
        tmp.close()
        try:
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(tmp.name)])
            inp = ChatInput()
            sent = []
            mixed = []
            inp.send_file.connect(sent.append)
            inp.send_mixed.connect(mixed.append)
            inp.input.insertFromMimeData(mime)
            self.assertEqual(sent, [])
            parts = inp._collect_inline_parts()
            norm = tmp.name.replace(os.sep, "/")
            self.assertEqual(parts, [("file", norm)])
            inp._send()
            # 纯文件逐个走 send_file；路径透传不落盘、不进混排通道
            self.assertEqual(sent, [norm])
            self.assertEqual(mixed, [])
        finally:
            os.remove(tmp.name)

    def test_save_qimage_returns_temp_png(self):
        inp = ChatInput()
        img = _png().toImage()
        path = inp._save_qimage(img)
        self.assertTrue(path.endswith(".png"))
        self.assertTrue(os.path.exists(path))
        os.remove(path)

    def test_save_qimage_null_returns_empty(self):
        inp = ChatInput()
        self.assertEqual(inp._save_qimage(None), "")


class TestDragDrop(unittest.TestCase):
    def test_has_local_urls_true_for_local_file(self):
        mime = FakeMime([QUrl.fromLocalFile(r"C:\tmp\a.png")])
        self.assertTrue(ChatInput._has_local_urls(mime))

    def test_has_local_urls_false_for_remote(self):
        mime = FakeMime([QUrl("https://example.com/a.png")])
        self.assertFalse(ChatInput._has_local_urls(mime))

    def test_drop_local_file_embeds_in_input(self):
        # ChatInput 输入区 dropEvent：拖入本地文件内嵌进输入框，而非 send_file
        inp = ChatInput()
        sent = []
        inp.send_file.connect(sent.append)
        ev = FakeDropEvent(FakeMime([QUrl.fromLocalFile(r"C:\tmp\b.txt")]))
        inp.dropEvent(ev)
        self.assertEqual(sent, [])
        self.assertTrue(ev.accepted)
        # 内嵌后文档含文件占位片段，_collect_inline_parts 可还原路径
        self.assertEqual(inp._collect_inline_parts(), [("file", "C:/tmp/b.txt")])

    def test_drag_enter_accepts_local_file(self):
        inp = ChatInput()
        ev = FakeDropEvent(FakeMime([QUrl.fromLocalFile(r"C:\tmp\c.txt")]))
        with mock.patch("ui.chat_input.QWidget.dragEnterEvent") as super_ev:
            inp.dragEnterEvent(ev)
        super_ev.assert_not_called()
        self.assertTrue(ev.accepted)


class TestMarkdownPreview(unittest.TestCase):
    def test_toggle_preview_shows_html(self):
        inp = ChatInput()
        inp.preview_btn.setChecked(True)
        inp.input.setPlainText("**加粗**")
        # QTextEdit 会把 <b> 规范化为 font-weight:600 的 span，说明已按富文本渲染
        self.assertIn("font-weight:600", inp.preview_label.toHtml())
        self.assertFalse(inp.preview_bar.isHidden())

    def test_preview_hidden_when_toggle_off(self):
        inp = ChatInput()
        inp.preview_btn.setChecked(True)
        inp.preview_btn.setChecked(False)
        self.assertTrue(inp.preview_bar.isHidden())

    def test_preview_renders_compact_blocks(self):
        """空行不再生成空块、列表项合并：预览里只有标题和两个列表项。"""
        inp = ChatInput()
        inp.preview_btn.setChecked(True)
        inp.input.setPlainText("## 标题\n\n- 甲\n\n- 乙\n续行")
        doc = inp.preview_label.document()
        blocks = []
        block = doc.begin()
        while block.isValid():
            blocks.append(block.text())
            block = block.next()
        self.assertEqual([t for t in blocks if not t.strip()], [])
        self.assertEqual(len(blocks), 3)
        self.assertEqual(blocks[0], "标题")
        self.assertIn("续行", blocks[2])

    def test_preview_injects_inline_image_resource(self):
        from PyQt5.QtCore import QObject, pyqtSignal
        from PyQt5.QtGui import QTextCursor, QTextDocument

        class StubLoader(QObject):
            image_ready = pyqtSignal(str)

            def __init__(self):
                super().__init__()
                self.pm = _png()

            def resolve(self, url):
                return url

            def pixmap_for(self, url, size):
                return self.pm

        loader = StubLoader()
        inp = ChatInput()
        inp.set_image_loader(loader)
        inp.preview_btn.setChecked(True)
        inp.input.setPlainText("图 ![t](https://s.example.com/a.png) 结束")

        doc = inp.preview_label.document()
        cur = QTextCursor(doc)
        cur.movePosition(cur.Start)
        found = False
        while True:
            fmt = cur.charFormat()
            if fmt.isImageFormat():
                name = fmt.toImageFormat().name()
                self.assertEqual(name, "https://s.example.com/a.png")
                res = doc.resource(QTextDocument.ImageResource, QUrl(name))
                self.assertIsNotNone(res)
                found = True
                break
            if cur.atEnd():
                break
            cur.movePosition(cur.NextCharacter)
        self.assertTrue(found)


class TestScreenshotButton(unittest.TestCase):
    def test_click_emits_screenshot_requested(self):
        inp = ChatInput()
        fired = []
        inp.screenshot_requested.connect(lambda: fired.append(True))
        inp.screenshot_btn.click()
        self.assertEqual(fired, [True])


class TestInlineScreenshot(unittest.TestCase):
    """截图改为内联插入输入框：insert_screenshot_image 把图写进 QTextEdit
    文档，_send 按文档顺序取回文本与截图分别发送。"""

    def _img(self):
        return _png().toImage()

    def test_insert_places_image_in_document(self):
        inp = ChatInput()
        inp.input.setPlainText("前")
        cur = inp.input.textCursor()
        cur.movePosition(cur.End)
        inp.input.setTextCursor(cur)
        img = self._img()
        inp.insert_screenshot_image(img)
        # 文档里应出现一个图片片段（isImageFormat 片段）
        doc = inp.input.document()
        found = False
        block = doc.begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                if it.fragment().charFormat().isImageFormat():
                    found = True
                it += 1
            block = block.next()
        self.assertTrue(found)

    def test_send_emits_text_and_inline_image(self):
        import os as _os
        inp = ChatInput()
        texts = []
        files = []
        mixed = []
        inp.send_text.connect(texts.append)
        inp.send_file.connect(files.append)
        inp.send_mixed.connect(mixed.append)
        inp.input.setPlainText("hello")
        cur = inp.input.textCursor()
        cur.movePosition(cur.End)
        inp.input.setTextCursor(cur)
        inp.insert_screenshot_image(self._img())
        inp._send()
        # 图文混排应合并成**一次** send_mixed，不再拆成 send_text + send_file
        self.assertEqual(texts, [])
        self.assertEqual(files, [])
        self.assertEqual(len(mixed), 1)
        parts = mixed[0]
        self.assertEqual(parts[0], ("text", "hello"))
        self.assertEqual(parts[1][0], "image")
        path = parts[1][1]
        self.assertTrue(path.endswith(".png"))
        self.assertTrue(_os.path.exists(path))
        _os.remove(path)
        self.assertEqual(inp.input.toPlainText(), "")

    def test_wide_screenshot_keeps_full_resolution_for_send(self):
        """过宽截图只在输入框显示尺寸上缩到 480，发送取回的仍是原图分辨率，
        否则上传/查看原图会被永久下采样变模糊。"""
        import os as _os
        from PyQt5.QtGui import QImageReader
        from PyQt5.QtGui import QTextFormat
        inp = ChatInput()
        mixed = []
        inp.send_mixed.connect(mixed.append)
        wide = QImage(1000, 500, QImage.Format_ARGB32)
        wide.fill(QColor("blue"))
        cur = inp.input.textCursor()
        cur.movePosition(cur.End)
        inp.input.setTextCursor(cur)
        inp.insert_screenshot_image(wide)
        # document resource 保存的应是 1000 宽原图（非 480 缩小版），
        # 这样发送/查看原图不会因下采样而变模糊
        doc = inp.input.document()
        found_w = None
        block = doc.begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                cfmt = it.fragment().charFormat()
                if cfmt.isImageFormat():
                    found_w = cfmt.property(QTextFormat.ImageWidth)
                    res = doc.resource(
                        QTextDocument.ImageResource,
                        QUrl(cfmt.property(QTextFormat.ImageName)))
                    self.assertEqual(res.width(), 1000)  # 存原图，不减分辨率
                it += 1
            block = block.next()
        self.assertEqual(found_w, 480)  # 显示宽度被压到 480
        # 发送落盘的 PNG 应是 1000 宽，而非 480
        inp._send()
        self.assertEqual(len(mixed), 1)
        path = mixed[0][0][1]
        reader = QImageReader(path)
        self.assertEqual(reader.size().width(), 1000)
        del reader
        _os.remove(path)

    def test_send_nothing_when_empty(self):
        inp = ChatInput()
        texts = []
        files = []
        inp.send_text.connect(texts.append)
        inp.send_file.connect(files.append)
        inp._send()
        self.assertEqual(texts, [])
        self.assertEqual(files, [])


class TestMultiParagraphText(unittest.TestCase):
    """多段落/换行的纯文本必须合并成**一条** send_text，不能逐段落各发一条
    （否则回车换行或空行会把一条消息拆成多条）。同样覆盖手敲 markdown 图片/文件
    链接等文本场景。"""

    def _make(self):
        inp = ChatInput()
        texts = []
        files = []
        mixed = []
        inp.send_text.connect(texts.append)
        inp.send_file.connect(files.append)
        inp.send_mixed.connect(mixed.append)
        return inp, texts, files, mixed

    def test_multi_paragraph_sends_once(self):
        inp, texts, files, mixed = self._make()
        inp.input.setPlainText("第一段\n\n第二段\n第三段")
        inp._send()
        self.assertEqual(len(texts), 1)
        self.assertIn("第一段", texts[0])
        self.assertIn("第二段", texts[0])
        self.assertIn("第三段", texts[0])
        self.assertEqual(files, [])
        self.assertEqual(mixed, [])

    def test_typed_markdown_image_sends_once(self):
        # 手敲 ![..](url) 是纯文本，也应作为一条消息发，不因空行拆成两条
        inp, texts, files, mixed = self._make()
        inp.input.setPlainText(
            "图文混排测试\n\n"
            "![picture](https://host/file-upload/x/pic.png)")
        inp._send()
        self.assertEqual(len(texts), 1)
        self.assertIn("![picture]", texts[0])
        self.assertEqual(mixed, [])

    def test_typed_markdown_file_link_sends_once(self):
        # 手敲文件链接 [..](url) 同样是纯文本，应作为一条文本消息，不拆条
        inp, texts, files, mixed = self._make()
        inp.input.setPlainText("看这个\n\n[报告.pdf](https://host/file-upload/x/r.pdf)")
        inp._send()
        self.assertEqual(len(texts), 1)
        self.assertIn("[报告.pdf]", texts[0])
        self.assertEqual(mixed, [])

    def test_text_and_dropped_file_mix_as_one_mixed(self):
        # 拖入真实文件 + 多段文本：一次 send_mixed，文本按序保留、文件透传
        import tempfile as _tf
        tmp = _tf.NamedTemporaryFile(suffix=".txt", delete=False)
        tmp.close()
        try:
            norm = tmp.name.replace(os.sep, "/")
            inp, texts, files, mixed = self._make()
            inp.input.setPlainText("第一段\n\n第二段")
            cur = inp.input.textCursor()
            cur.movePosition(cur.End)
            inp.input.setTextCursor(cur)
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(tmp.name)])
            inp.input.insertFromMimeData(mime)
            inp._send()
            self.assertEqual(len(mixed), 1)
            parts = mixed[0]
            self.assertIn(("text", "第一段"), parts)
            self.assertIn(("text", "第二段"), parts)
            self.assertIn(("file", norm), parts)
            self.assertEqual(texts, [])
        finally:
            os.remove(tmp.name)


class TestToolbarIcons(unittest.TestCase):
    """工具栏改纯图标后：每个按钮都有图标和鼠标悬停工具提示，且信号仍接上。"""

    def test_all_buttons_have_icon_and_tooltip(self):
        inp = ChatInput()
        for attr in ("attach_btn", "emoji_btn", "screenshot_btn",
                     "preview_btn", "send_btn"):
            btn = getattr(inp, attr)
            self.assertFalse(btn.icon().isNull(), f"{attr} 没有图标")
            self.assertTrue(btn.toolTip().strip(),
                            f"{attr} 没有悬停提示文本")

    def test_send_button_still_emits_text(self):
        inp = ChatInput()
        texts = []
        inp.send_text.connect(texts.append)
        inp.input.setPlainText("你好")
        inp.send_btn.click()
        self.assertEqual(texts, ["你好"])

    def test_screenshot_button_emits_signal(self):
        inp = ChatInput()
        fired = []
        inp.screenshot_requested.connect(lambda: fired.append(True))
        inp.screenshot_btn.click()
        self.assertEqual(fired, [True])


class TestChooseMultipleFiles(unittest.TestCase):
    """附件按钮多选：选 N 个文件逐个发 send_file 信号（微信式批量发送）。"""

    def test_multi_paths_emitted_one_by_one(self):
        inp = ChatInput()
        fired = []
        inp.send_file.connect(fired.append)
        paths = ["/tmp/a.png", "/tmp/b.docx", "/tmp/c.mp3"]
        with mock.patch("ui.chat_input.QFileDialog.getOpenFileNames",
                        return_value=(paths, "筛选")):
            inp._choose_file()
        self.assertEqual(fired, paths)

    def test_cancel_emits_nothing(self):
        inp = ChatInput()
        fired = []
        inp.send_file.connect(fired.append)
        with mock.patch("ui.chat_input.QFileDialog.getOpenFileNames",
                        return_value=([], "")):
            inp._choose_file()
        self.assertEqual(fired, [])


class TestMention(unittest.TestCase):
    """@ 提及（微信式）：触发、过滤、插入、@all 项与成员补拉请求。"""

    def _members(self):
        return [
            {"_id": "1", "username": "alice", "name": "Alice"},
            {"_id": "2", "username": "bob", "name": "Bob"},
            {"_id": "3", "username": "charlie", "name": "Charlie"},
        ]

    def _type(self, inp, text):
        """模拟在聚焦输入框里打字到行尾。
        setPlainText 在控件无焦点时会把光标重置到位置 0，导致按 textCursor
        位置找 @ 的触发逻辑测不到；真实场景用户是在聚焦框里敲字，文本变化时
        光标在末尾。这里在设文本后把光标移到行尾，等价于真实输入态。"""
        inp.input.setPlainText(text)
        cur = inp.input.textCursor()
        cur.movePosition(cur.End)
        inp.input.setTextCursor(cur)

    def test_all_item_only_for_channel_and_group(self):
        for rt in ("c", "p"):
            inp = ChatInput()
            inp.set_mention_candidates(rt, self._members())
            self._type(inp, "hi @")
            picker = inp._mention_picker
            self.assertIsNotNone(picker)
            roles = [picker.list.item(i).data(Qt.UserRole)
                     for i in range(picker.list.count())]
            self.assertIn("@all", roles)
        # 私聊 d 不提供 @all
        inp = ChatInput()
        inp.set_mention_candidates("d", self._members())
        self._type(inp, "hi @")
        roles = [inp._mention_picker.list.item(i).data(Qt.UserRole)
                 for i in range(inp._mention_picker.list.count())]
        self.assertNotIn("@all", roles)

    def test_trigger_needs_word_boundary(self):
        inp = ChatInput()
        inp.set_mention_candidates("c", self._members())
        # @ 前空白 → 触发下拉
        self._type(inp, "hi @")
        self.assertTrue(inp._mention_active)
        self.assertTrue(inp._mention_picker.isVisible())
        # @ 前非空白（如 email）→ 不触发
        inp2 = ChatInput()
        inp2.set_mention_candidates("c", self._members())
        self._type(inp2, "abc@")
        self.assertFalse(inp2._mention_active)

    def test_selection_inserts_mention_and_closes(self):
        inp = ChatInput()
        inp.set_mention_candidates("c", self._members())
        self._type(inp, "hi @")
        inp._insert_mention("@alice")
        self.assertEqual(inp.input.toPlainText(), "hi @alice ")
        self.assertFalse(inp._mention_active)
        # 光标在插入内容末尾
        cur = inp.input.textCursor().position()
        self.assertEqual(cur, len("hi @alice "))

    def test_filter_by_username_prefix(self):
        inp = ChatInput()
        inp.set_mention_candidates("c", self._members())
        self._type(inp, "hi @")
        picker = inp._mention_picker
        picker.filter_to("bo")
        roles = [picker.list.item(i).data(Qt.UserRole)
                 for i in range(picker.list.count())]
        # 输入具体过滤词后 @all 不再保留（只有空关键词或 "all" 前缀才显示 @all）
        self.assertNotIn("@all", roles)
        self.assertIn("@bob", roles)
        self.assertNotIn("@alice", roles)
        # 过滤 "al" 前缀应重新出现 @all + alice
        picker.filter_to("al")
        roles = [picker.list.item(i).data(Qt.UserRole)
                 for i in range(picker.list.count())]
        self.assertIn("@all", roles)
        self.assertIn("@alice", roles)
        self.assertNotIn("@bob", roles)

    def test_select_all_option(self):
        inp = ChatInput()
        inp.set_mention_candidates("c", self._members())
        self._type(inp, "hi @")
        inp._insert_mention("@all")
        self.assertEqual(inp.input.toPlainText(), "hi @all ")

    # -- 已插入的 @提及 是整体：退格/删除只能整段删，不能逐个删字符 -- #

    def test_backspace_at_end_deletes_whole_mention(self):
        inp = ChatInput()
        inp.set_mention_candidates("c", self._members())
        self._type(inp, "hi @")
        inp._insert_mention("@alice")          # -> "hi @alice "，光标在末尾
        self.assertEqual(inp.input.toPlainText(), "hi @alice ")
        # Backspace（删尾随空格）应一次性删除整个 @alice，光标回到 @ 前
        self.assertTrue(inp._undo_mention_on_delete(Qt.Key_Backspace))
        self.assertEqual(inp.input.toPlainText(), "hi ")
        self.assertEqual(inp.input.textCursor().position(), len("hi "))
        self.assertIsNone(inp._mention_commit_span)

    def test_backspace_deleting_middle_char_removes_whole_mention(self):
        # 把光标移到 @alice 中间删一个字符，也不能只删那一个字符，而要整段删
        inp = ChatInput()
        inp.set_mention_candidates("c", self._members())
        self._type(inp, "hi @")
        inp._insert_mention("@alice")
        cur = inp.input.textCursor()
        cur.setPosition(6)                     # alice 中间
        inp.input.setTextCursor(cur)
        self.assertTrue(inp._undo_mention_on_delete(Qt.Key_Backspace))
        self.assertEqual(inp.input.toPlainText(), "hi ")

    def test_delete_inside_mention_removes_whole_mention(self):
        inp = ChatInput()
        inp.set_mention_candidates("c", self._members())
        self._type(inp, "hi @")
        inp._insert_mention("@alice")
        cur = inp.input.textCursor()
        cur.setPosition(4)                     # 提及开头附近
        inp.input.setTextCursor(cur)
        # Delete 命中提及内字符 → 整段删
        self.assertTrue(inp._undo_mention_on_delete(Qt.Key_Delete))
        self.assertEqual(inp.input.toPlainText(), "hi ")

    def test_backspace_before_mention_not_intercepted(self):
        # 光标在 @ 前（"hi " 之后）按 Backspace，删的是前一个字符，不拦截
        inp = ChatInput()
        inp.set_mention_candidates("c", self._members())
        self._type(inp, "hi @")
        inp._insert_mention("@alice")
        cur = inp.input.textCursor()
        cur.setPosition(3)                     # "hi " 与 @ 之间
        inp.input.setTextCursor(cur)
        self.assertFalse(inp._undo_mention_on_delete(Qt.Key_Backspace))

    def test_delete_selection_overlapping_mention_removes_whole_mention(self):
        # 选中跨越 @ 提及 的文本再删除，也应整段删掉提及，而不是只删选中部分
        inp = ChatInput()
        inp.set_mention_candidates("c", self._members())
        self._type(inp, "hi @")
        inp._insert_mention("@bob")            # -> "hi @bob "
        cur = inp.input.textCursor()
        cur.setPosition(4)
        cur.setPosition(7, QTextCursor.KeepAnchor)   # 选中 "bob"
        inp.input.setTextCursor(cur)
        self.assertTrue(inp._undo_mention_on_delete(Qt.Key_Backspace))
        self.assertEqual(inp.input.toPlainText(), "hi ")

    def test_mention_wanted_emitted_without_candidates(self):
        inp = ChatInput()
        inp._mention_candidates = []
        inp.set_mention_candidates("c", [])
        fired = []
        inp.mention_wanted.connect(lambda: fired.append(True))
        self._type(inp, "hi @")
        self.assertTrue(fired)


class TestDraftAndEdit(unittest.TestCase):
    """草稿快照/恢复与编辑条：编辑中的输入不是本会话草稿。"""

    def test_draft_snapshot_keeps_text_and_quote(self):
        inp = ChatInput()
        inp.input.setPlainText("草稿内容")
        inp.set_quote("小明", "原消息", "m1", "r1", "c", "general")
        draft = inp.draft_snapshot()
        self.assertEqual(draft["text"], "草稿内容")
        self.assertEqual(draft["quote"]["msg_id"], "m1")
        self.assertEqual(draft["quote"]["room_slug"], "general")

    def test_restore_draft_sets_text_and_quote_without_signal(self):
        inp = ChatInput()
        fired = []
        inp.input.textChanged.connect(lambda: fired.append(True))
        inp.restore_draft({"text": "恢复我", "quote": {
            "msg_id": "m2", "sender": "Alice", "text": "原文"}})
        self.assertEqual(inp.input.toPlainText(), "恢复我")
        self.assertIsNotNone(inp.quote_context)
        self.assertEqual(inp.quote_context["sender"], "Alice")
        self.assertFalse(inp.quote_bar.isHidden())
        # blockSignals：草稿定时器不会被恢复动作触发
        self.assertEqual(fired, [])
        self.assertFalse(inp.is_editing())

    def test_restore_empty_draft_clears_previous(self):
        inp = ChatInput()
        inp.input.setPlainText("旧")
        inp.set_quote("小明", "原消息", "m1")
        inp.restore_draft(None)
        self.assertEqual(inp.input.toPlainText(), "")
        self.assertIsNone(inp.quote_context)
        self.assertTrue(inp.quote_bar.isHidden())

    def test_begin_edit_saves_pre_edit_and_shows_bar(self):
        inp = ChatInput()
        inp.input.setPlainText("打了一半")
        inp.set_quote("小明", "引用", "m9")
        inp.begin_edit("m1", "待编辑正文")
        self.assertTrue(inp.is_editing())
        self.assertEqual(inp.input.toPlainText(), "待编辑正文")
        self.assertFalse(inp.edit_bar.isHidden())
        self.assertEqual(inp._pre_edit["text"], "打了一半")
        self.assertEqual(inp._pre_edit["quote"]["msg_id"], "m9")

    def test_cancel_edit_restores_input_and_quote(self):
        inp = ChatInput()
        inp.input.setPlainText("打了一半")
        inp.set_quote("小明", "引用", "m9")
        fired = []
        inp.edit_cancelled.connect(lambda: fired.append(True))
        inp.begin_edit("m1", "待编辑正文")
        inp.cancel_edit()
        self.assertFalse(inp.is_editing())
        self.assertEqual(inp.input.toPlainText(), "打了一半")
        self.assertEqual(inp.quote_context["msg_id"], "m9")
        self.assertTrue(inp.edit_bar.isHidden())
        self.assertEqual(fired, [True])

    def test_cancel_without_edit_is_noop(self):
        inp = ChatInput()
        inp.input.setPlainText("正文")
        fired = []
        inp.edit_cancelled.connect(lambda: fired.append(True))
        inp.cancel_edit()
        self.assertEqual(inp.input.toPlainText(), "正文")
        self.assertEqual(fired, [])

    def test_finish_edit_clears_input_without_cancel_signal(self):
        inp = ChatInput()
        inp.set_quote("小明", "引用", "m9")
        fired = []
        inp.edit_cancelled.connect(lambda: fired.append(True))
        inp.begin_edit("m1", "待编辑正文")
        inp.finish_edit()
        self.assertFalse(inp.is_editing())
        self.assertEqual(inp.input.toPlainText(), "")
        self.assertIsNone(inp.quote_context)
        self.assertTrue(inp.edit_bar.isHidden())
        self.assertEqual(fired, [])


class TestSendMediaDuringEdit(unittest.TestCase):
    """编辑态下发媒体：必须先退出编辑再发送，否则 _edit_msg_id 残留使
    输入框不清空，文件占位常驻导致每次回车重发同一批文件（2026-11-04）。"""

    def test_send_file_in_edit_mode_abandons_edit_and_clears(self):
        inp = ChatInput()
        fired = []
        inp.edit_cancelled.connect(lambda: fired.append(True))
        inp.begin_edit("m1", "待编辑正文")
        inp.input.setPlainText("")
        inp.insert_file(r"D:\tmp\big.iso")
        emitted = []
        inp.send_file.connect(lambda p: emitted.append(p))
        inp._send()
        self.assertEqual(emitted, [r"D:\tmp\big.iso"])
        self.assertFalse(inp.is_editing())
        self.assertEqual(fired, [True])
        self.assertTrue(inp.edit_bar.isHidden())
        self.assertEqual(inp.input.toPlainText(), "")

    def test_send_mixed_in_edit_mode_abandons_edit_and_clears(self):
        inp = ChatInput()
        fired = []
        inp.edit_cancelled.connect(lambda: fired.append(True))
        inp.begin_edit("m1", "待编辑正文")
        inp.insert_file(r"D:\tmp\big.iso")
        emitted = []
        inp.send_mixed.connect(lambda parts: emitted.append(parts))
        inp._send()
        self.assertEqual(
            emitted, [[("text", "待编辑正文"), ("file", r"D:\tmp\big.iso")]])
        self.assertFalse(inp.is_editing())
        self.assertEqual(fired, [True])
        self.assertEqual(inp.input.toPlainText(), "")

    def test_send_text_in_edit_mode_keeps_edit(self):
        # 纯文本回车仍走编辑提交：保留编辑态与内容（失败可重试），不清空
        inp = ChatInput()
        fired = []
        inp.edit_cancelled.connect(lambda: fired.append(True))
        inp.begin_edit("m1", "待编辑正文")
        emitted = []
        inp.send_text.connect(lambda t: emitted.append(t))
        inp._send()
        self.assertEqual(emitted, ["待编辑正文"])
        self.assertTrue(inp.is_editing())
        self.assertEqual(fired, [])
        self.assertEqual(inp.input.toPlainText(), "待编辑正文")

    def test_send_file_without_edit_clears_as_before(self):
        inp = ChatInput()
        inp.insert_file(r"D:\tmp\big.iso")
        emitted = []
        inp.send_file.connect(lambda p: emitted.append(p))
        inp._send()
        self.assertEqual(emitted, [r"D:\tmp\big.iso"])
        self.assertFalse(inp.is_editing())
        self.assertEqual(inp.input.toPlainText(), "")


if __name__ == "__main__":
    unittest.main()
