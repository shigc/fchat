"""已读位置持久化：未读分隔线与「跳到首条未读」的本地依据。

位置用服务端消息时间戳记录（不依赖客户端时钟），只前进不后退，
按 (server, username, rid) 隔离账号与会话。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import read_state  # noqa: E402


class ReadStateTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "read_state.json")
        patcher = mock.patch.object(read_state, "_path", return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_default_zero(self):
        self.assertEqual(
            read_state.get_last_read("https://s", "alice", "r1"), 0.0)

    def test_set_then_get(self):
        read_state.set_last_read("https://s", "alice", "r1", 1700000000.0)
        self.assertEqual(
            read_state.get_last_read("https://s", "alice", "r1"), 1700000000.0)

    def test_only_forward(self):
        read_state.set_last_read("https://s", "alice", "r1", 200.0)
        read_state.set_last_read("https://s", "alice", "r1", 100.0)
        self.assertEqual(
            read_state.get_last_read("https://s", "alice", "r1"), 200.0)

    def test_account_and_room_isolated(self):
        read_state.set_last_read("https://s", "alice", "r1", 100.0)
        self.assertEqual(
            read_state.get_last_read("https://s", "bob", "r1"), 0.0)
        self.assertEqual(
            read_state.get_last_read("https://s", "alice", "r2"), 0.0)
        self.assertEqual(
            read_state.get_last_read("https://other", "alice", "r1"), 0.0)

    def test_corrupt_file_degrades_to_empty(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(
            read_state.get_last_read("https://s", "alice", "r1"), 0.0)
        read_state.set_last_read("https://s", "alice", "r1", 42.0)
        self.assertEqual(
            read_state.get_last_read("https://s", "alice", "r1"), 42.0)

    def test_written_file_is_valid_json(self):
        read_state.set_last_read("https://s", "alice", "r1", 42.0)
        with open(self.path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(len(data), 1)
        self.assertEqual(list(data.values())[0], 42.0)


if __name__ == "__main__":
    unittest.main()
