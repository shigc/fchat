"""core.msg_cache 消息快照模块：序列化 roundtrip 与损坏文件兜底。

运行：python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from dataclasses import asdict
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.models import Message, User  # noqa: E402
from core import msg_cache  # noqa: E402


class MsgCacheTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch("core.msg_cache.cache_root",
                                 lambda: self._tmp.name)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _msg(self, mid="m1", ts=100.0, text="hello"):
        return Message(
            id=mid, room_id="R1", text=text, timestamp=ts,
            sender=User(id="u1", username="bob", name="Bob",
                        avatar_url="/avatar/u1", department="研发"),
            msg_type="quote", attachments=[{"type": "file", "size": 12}],
            is_mine=True, file_url="/file-upload/a.png", file_name="a.png",
            file_size=12, description="原文本", reactions={"👍": ["bob"]},
            quote_msg_id="m0", quote_author="alice",
            quote_link="/direct/R9?msg=m0", mentions=[{"username": "bob"}],
            is_forwarded=True, forward_author="carol",
            edited_at=1700000005.0, starred=True, pinned=True,
            mentions_me=True, mention_all=False,
        )

    def test_roundtrip_keeps_fields_and_sender(self):
        msg = self._msg()
        msg_cache.save_messages_snapshot("https://s", "alice", "R1", [msg])
        loaded = msg_cache.load_messages_snapshot("https://s", "alice", "R1")
        self.assertEqual(len(loaded), 1)
        self.assertIsInstance(loaded[0].sender, User)
        self.assertEqual(asdict(loaded[0]), asdict(msg))

    def test_roundtrip_keeps_interaction_flags(self):
        msg = self._msg()
        msg_cache.save_messages_snapshot("https://s", "alice", "R1", [msg])
        loaded = msg_cache.load_messages_snapshot("https://s", "alice", "R1")[0]
        self.assertEqual(loaded.edited_at, 1700000005.0)
        self.assertTrue(loaded.starred)
        self.assertTrue(loaded.pinned)
        self.assertTrue(loaded.mentions_me)
        self.assertFalse(loaded.mention_all)

    def test_legacy_snapshot_without_interaction_flags(self):
        """旧快照缺新字段：加载走 dataclass 默认值，不误标已编辑。"""
        path = msg_cache._snapshot_path("https://s", "alice", "R1")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        import json
        data = asdict(self._msg("m4"))
        for key in ("edited_at", "starred", "pinned",
                    "mentions_me", "mention_all"):
            data.pop(key, None)
        with open(path, "w", encoding="utf-8") as f:
            json.dump([data], f, ensure_ascii=False)
        loaded = msg_cache.load_messages_snapshot("https://s", "alice", "R1")[0]
        self.assertEqual(loaded.edited_at, 0.0)
        self.assertFalse(loaded.starred)
        self.assertFalse(loaded.pinned)
        self.assertFalse(loaded.mentions_me)
        self.assertFalse(loaded.mention_all)

    def test_load_missing_or_empty_args_returns_empty(self):
        self.assertEqual(msg_cache.load_messages_snapshot(
            "https://s", "alice", "Rnone"), [])
        self.assertEqual(msg_cache.load_messages_snapshot("", "alice", "R1"),
                         [])

    def test_save_empty_creates_nothing(self):
        msg_cache.save_messages_snapshot("https://s", "alice", "R1", [])
        self.assertFalse(os.path.exists(
            msg_cache._snapshot_path("https://s", "alice", "R1")))

    def test_load_corrupt_returns_empty(self):
        msg_cache.save_messages_snapshot("https://s", "alice", "R1",
                                         [self._msg()])
        path = msg_cache._snapshot_path("https://s", "alice", "R1")
        with open(path, "w", encoding="utf-8") as f:
            f.write("not json")
        self.assertEqual(msg_cache.load_messages_snapshot(
            "https://s", "alice", "R1"), [])

    def test_broken_items_skipped(self):
        path = msg_cache._snapshot_path("https://s", "alice", "R1")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        import json
        with open(path, "w", encoding="utf-8") as f:
            json.dump(["garbage", {"no_id": True}, asdict(self._msg("m2"))],
                      f, ensure_ascii=False)
        loaded = msg_cache.load_messages_snapshot("https://s", "alice", "R1")
        self.assertEqual([m.id for m in loaded], ["m2"])

    def test_future_fields_ignored(self):
        """快照里比当前 Message 多出的键（降级/旧版混跑）不能让加载崩掉。"""
        path = msg_cache._snapshot_path("https://s", "alice", "R1")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        import json
        data = asdict(self._msg("m3"))
        data["future_field"] = "x"
        with open(path, "w", encoding="utf-8") as f:
            json.dump([data], f, ensure_ascii=False)
        loaded = msg_cache.load_messages_snapshot("https://s", "alice", "R1")
        self.assertEqual([m.id for m in loaded], ["m3"])


if __name__ == "__main__":
    unittest.main()
