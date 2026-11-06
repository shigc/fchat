"""会话快照缓存测试：roundtrip、损坏容错、按 server|user 隔离。"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import conv_cache, paths
from core.models import Conversation


class ConvCacheTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patchers = [
            mock.patch.object(paths, "_current_root", self._tmp.name),
            mock.patch.object(paths, "_root_loaded", True),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self._tmp.cleanup()

    def _conv(self, **kw):
        fields = dict(id="rid1", name="张三", room_type="d", unread=3,
                      avatar_url="http://x/a.png", is_favorite=True,
                      username="zhangsan", last_message_time=1700000000.0,
                      last_message_text="你好")
        fields.update(kw)
        return Conversation(**fields)

    def test_roundtrip(self):
        convs = [self._conv(), self._conv(id="rid2", name=" GENERAL",
                                          room_type="c", is_favorite=False,
                                          is_team=True, encrypted=True)]
        conv_cache.save_conversations_snapshot("http://s", "u1", convs)
        loaded = conv_cache.load_conversations_snapshot("http://s", "u1")
        self.assertEqual(len(loaded), 2)
        a, b = loaded
        self.assertEqual(a.id, "rid1")
        self.assertEqual(a.name, "张三")
        self.assertEqual(a.unread, 3)
        self.assertTrue(a.is_favorite)
        self.assertEqual(a.username, "zhangsan")
        self.assertEqual(a.last_message_time, 1700000000.0)
        self.assertEqual(a.last_message_text, "你好")
        self.assertFalse(b.is_favorite)
        self.assertTrue(b.is_team)
        self.assertTrue(b.encrypted)
        self.assertFalse(a.encrypted)

    def test_legacy_snapshot_without_encrypted_key(self):
        """旧快照缺 encrypted 键：加载得 False，不抛异常。"""
        conv = self._conv()
        conv_cache.save_conversations_snapshot("http://s", "u1", [conv])
        path = conv_cache._snapshot_path("http://s", "u1")
        import json
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for item in data:
            item.pop("encrypted", None)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        loaded = conv_cache.load_conversations_snapshot("http://s", "u1")
        self.assertEqual(len(loaded), 1)
        self.assertFalse(loaded[0].encrypted)

    def test_muted_and_mention_count_roundtrip(self):
        conv = self._conv(muted=True, mention_count=4)
        conv_cache.save_conversations_snapshot("http://s", "u1", [conv])
        loaded = conv_cache.load_conversations_snapshot("http://s", "u1")[0]
        self.assertTrue(loaded.muted)
        self.assertEqual(loaded.mention_count, 4)

    def test_legacy_snapshot_without_new_keys(self):
        """旧快照缺 muted/mention_count 键：加载成 False/0。"""
        conv_cache.save_conversations_snapshot(
            "http://s", "u1", [self._conv()])
        path = conv_cache._snapshot_path("http://s", "u1")
        import json
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for item in data:
            item.pop("muted", None)
            item.pop("mention_count", None)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        loaded = conv_cache.load_conversations_snapshot("http://s", "u1")[0]
        self.assertFalse(loaded.muted)
        self.assertEqual(loaded.mention_count, 0)

    def test_keyed_by_server_and_user(self):
        conv_cache.save_conversations_snapshot("http://s", "u1", [self._conv()])
        self.assertEqual(
            conv_cache.load_conversations_snapshot("http://s", "u2"), [])
        self.assertEqual(
            conv_cache.load_conversations_snapshot("http://t", "u1"), [])

    def test_missing_returns_empty(self):
        self.assertEqual(
            conv_cache.load_conversations_snapshot("http://s", "u9"), [])

    def test_corrupted_returns_empty(self):
        conv_cache.save_conversations_snapshot("http://s", "u1", [self._conv()])
        path = conv_cache._snapshot_path("http://s", "u1")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(
            conv_cache.load_conversations_snapshot("http://s", "u1"), [])

    def test_save_failure_is_silent(self):
        with mock.patch.object(conv_cache.os, "replace",
                               side_effect=OSError("disk full")):
            # 不应抛异常
            conv_cache.save_conversations_snapshot(
                "http://s", "u1", [self._conv()])

    def test_empty_args_noop(self):
        conv_cache.save_conversations_snapshot("", "u1", [self._conv()])
        conv_cache.save_conversations_snapshot("http://s", "", [self._conv()])
        self.assertEqual(conv_cache.load_conversations_snapshot("", "u1"), [])


if __name__ == "__main__":
    unittest.main()
