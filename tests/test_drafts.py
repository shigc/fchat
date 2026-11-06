"""每会话草稿存储：读写、删除、账号隔离、损坏文件容错。

草稿落在 CONFIG_DIR/drafts.json（不在 cache/），这里把模块级 CONFIG_DIR
patch 到临时目录，避免碰真实 %APPDATA%。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import drafts  # noqa: E402


class DraftsTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(drafts, "CONFIG_DIR", self._tmp.name)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_roundtrip_text_and_quote(self):
        quote = {"msg_id": "m1", "sender": "张三", "text": "原文"}
        drafts.set_draft("https://s", "alice", "r1", "草稿内容", quote)
        got = drafts.get_draft("https://s", "alice", "r1")
        self.assertEqual(got["text"], "草稿内容")
        self.assertEqual(got["quote"], quote)

    def test_missing_returns_none(self):
        self.assertIsNone(drafts.get_draft("https://s", "alice", "nope"))

    def test_empty_text_and_quote_deletes_key(self):
        drafts.set_draft("https://s", "alice", "r1", "abc")
        drafts.set_draft("https://s", "alice", "r1", "   ")
        self.assertIsNone(drafts.get_draft("https://s", "alice", "r1"))
        # 键被真正删掉，而不是留一条空记录
        with open(drafts._drafts_path(), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {})

    def test_quote_only_draft_survives_empty_text(self):
        quote = {"msg_id": "m1", "sender": "b", "text": "t"}
        drafts.set_draft("https://s", "alice", "r1", "", quote)
        got = drafts.get_draft("https://s", "alice", "r1")
        self.assertEqual(got["text"], "")
        self.assertEqual(got["quote"], quote)

    def test_accounts_and_rooms_isolated(self):
        drafts.set_draft("https://s", "alice", "r1", "A")
        drafts.set_draft("https://s", "bob", "r1", "B")
        drafts.set_draft("https://s", "alice", "r2", "C")
        drafts.set_draft("https://t", "alice", "r1", "D")
        self.assertEqual(drafts.get_draft("https://s", "alice", "r1")["text"], "A")
        self.assertEqual(drafts.get_draft("https://s", "bob", "r1")["text"], "B")
        self.assertEqual(drafts.get_draft("https://s", "alice", "r2")["text"], "C")
        self.assertEqual(drafts.get_draft("https://t", "alice", "r1")["text"], "D")

    def test_overwrite_keeps_latest(self):
        drafts.set_draft("https://s", "alice", "r1", "v1")
        drafts.set_draft("https://s", "alice", "r1", "v2")
        self.assertEqual(drafts.get_draft("https://s", "alice", "r1")["text"], "v2")

    def test_corrupt_file_degrades_to_empty(self):
        path = drafts._drafts_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertIsNone(drafts.get_draft("https://s", "alice", "r1"))
        # 损坏后仍能正常写入（读取失败只降级为空表）
        drafts.set_draft("https://s", "alice", "r1", "ok")
        self.assertEqual(drafts.get_draft("https://s", "alice", "r1")["text"], "ok")

    def test_delete_draft(self):
        drafts.set_draft("https://s", "alice", "r1", "abc")
        drafts.delete_draft("https://s", "alice", "r1")
        self.assertIsNone(drafts.get_draft("https://s", "alice", "r1"))

    def test_empty_room_id_is_noop(self):
        drafts.set_draft("https://s", "alice", "", "abc")
        self.assertFalse(os.path.exists(drafts._drafts_path()))


if __name__ == "__main__":
    unittest.main()
