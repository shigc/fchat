"""P1 优化：QTextDocument 缓存（_doc_cache）的正确性测试。

验证：
- 纯函数 doc（quote/merge/caption/small）同内容+同宽度命中同一实例
- build_text_doc 同消息命中同一实例
- update_message / remove_message / clear / 内联图就绪 后缓存正确失效
GUI 测试，offscreen 渲染，不联网。
运行：python -m unittest tests.test_doc_cache -v
"""
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication  # noqa: E402

from core.models import Message  # noqa: E402
from ui.message_model import MessageListModel  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _msg(mid, text, mentions=None):
    m = Message(id=mid, room_id="r", timestamp=1.0, msg_type="text",
                text=text)
    if mentions:
        m.mentions = mentions
    return m


class TestDocCache(unittest.TestCase):
    def setUp(self):
        self.model = MessageListModel(None)

    def test_quote_doc_same_instance(self):
        d1 = self.model._quote_doc("hello world", 200)
        d2 = self.model._quote_doc("hello world", 200)
        self.assertIs(d1, d2)
        # 宽度不同则不命中（各自正确）
        d3 = self.model._quote_doc("hello world", 300)
        self.assertIsNot(d1, d3)

    def test_merge_doc_same_instance(self):
        d1 = self.model._merge_content_doc("**bold** text", 200)
        d2 = self.model._merge_content_doc("**bold** text", 200)
        self.assertIs(d1, d2)
        d3 = self.model._merge_content_doc("**bold** text", 300)
        self.assertIsNot(d1, d3)

    def test_caption_doc_same_instance(self):
        d1 = self.model._caption_doc("caption", 150)
        d2 = self.model._caption_doc("caption", 150)
        self.assertIs(d1, d2)

    def test_small_doc_same_instance(self):
        d1 = self.model._small_doc("small text", 180)
        d2 = self.model._small_doc("small text", 180)
        self.assertIs(d1, d2)

    def test_build_text_doc_cached(self):
        m = _msg("m1", "hello **world**")
        d1 = self.model.build_text_doc(m)
        d2 = self.model.build_text_doc(m)
        self.assertIs(d1, d2)

    def test_build_text_doc_differs_by_content(self):
        m1 = _msg("m1", "hello")
        m2 = _msg("m1", "world")  # 同 id 不同内容
        d1 = self.model.build_text_doc(m1)
        d2 = self.model.build_text_doc(m2)
        self.assertIsNot(d1, d2)

    def test_build_text_doc_differs_by_mentions(self):
        m1 = _msg("m1", "@alice hi", mentions=[{"username": "alice", "name": "爱丽丝"}])
        m2 = _msg("m1", "@alice hi", mentions=[{"username": "alice", "name": "不同名"}])
        self.assertIsNot(self.model.build_text_doc(m1),
                         self.model.build_text_doc(m2))

    def test_update_message_invalidates_doc(self):
        m = _msg("m1", "version one")
        d1 = self.model.build_text_doc(m)
        self.model.append_message(m)
        # 内容变化后 update_message 应使原 doc 失效
        m2 = _msg("m1", "version two")
        self.model.update_message(m2)
        d2 = self.model.build_text_doc(m2)
        self.assertIsNot(d1, d2)
        # 更新后同一新内容命中缓存
        self.assertIs(self.model.build_text_doc(m2), d2)

    def test_remove_message_invalidates_doc(self):
        m = _msg("m1", "to remove")
        self.model.append_message(m)
        d1 = self.model.build_text_doc(m)
        self.model.remove_message("m1")
        d2 = self.model.build_text_doc(m)
        self.assertIsNot(d1, d2)

    def test_clear_invalidates_doc(self):
        m = _msg("m1", "hello")
        self.model.append_message(m)
        d1 = self.model.build_text_doc(m)
        self.model.clear()
        d2 = self.model.build_text_doc(m)
        self.assertIsNot(d1, d2)

    def test_invalidate_height_cache_clears_doc(self):
        m = _msg("m1", "hello")
        d1 = self.model.build_text_doc(m)
        self.model.invalidate_height_cache()
        d2 = self.model.build_text_doc(m)
        self.assertIsNot(d1, d2)

    def test_inline_image_ready_invalidates_doc(self):
        # 模拟一条含内联图的 markdown 消息
        m = _msg("m1", "![图](http://s/x.png)")
        self.model.append_message(m)
        # 手动登记反向索引，模拟 _prepare_inline_images 已登记
        self.model._inline_url_msgs.setdefault("http://s/x.png", set()).add("m1")
        d1 = self.model.build_text_doc(m)
        self.model._on_inline_image_ready("http://s/x.png")
        d2 = self.model.build_text_doc(m)
        self.assertIsNot(d1, d2)

    def test_doc_cache_has_lru_limit(self):
        # 写入超过上限的 doc，最旧的应被淘汰（不抛错，容量受控）
        for i in range(self.model._DOC_CACHE_LIMIT + 20):
            self.model._quote_doc("text %d" % i, 100)
        self.assertLessEqual(len(self.model._doc_cache),
                             self.model._DOC_CACHE_LIMIT)


if __name__ == "__main__":
    unittest.main()
