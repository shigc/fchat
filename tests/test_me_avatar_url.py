"""client.me 必须带 avatar_url 的回归测试。

发送路径的乐观回显直接拿 self.me 当消息 sender；登录/恢复会话构造 me 时
漏掉 avatar_url 的话，自己消息的头像会画成首字占位且永不重试（空 URL
既不发请求也不登记等待）。mock RocketChat，离线。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.api_client import ApiClient  # noqa: E402


def _login_client():
    c = ApiClient("https://s.example.com")
    c._rc = mock.Mock()
    c._rc.headers = {"X-Auth-Token": "tok", "X-User-Id": "u1"}
    c._rc.me.return_value = {
        "data": {"_id": "u1", "username": "alice", "name": "郭某某"}}
    return c


class MeAvatarUrlTest(unittest.TestCase):
    def test_login_sets_avatar_url(self):
        c = _login_client()
        with mock.patch("core.api_client.RocketChat", return_value=c._rc):
            me = c.login("alice", "pwd-not-printed")
        self.assertEqual(me.avatar_url, "https://s.example.com/avatar/alice")

    def test_login_without_username_keeps_avatar_empty(self):
        c = _login_client()
        c._rc.me.return_value = {"data": {"_id": "u1", "username": ""}}
        with mock.patch("core.api_client.RocketChat", return_value=c._rc):
            me = c.login("alice", "pwd-not-printed")
        self.assertEqual(me.avatar_url, "")

    def test_login_with_token_sets_avatar_url(self):
        c = _login_client()
        with mock.patch("core.api_client.RocketChat", return_value=c._rc):
            me = c.login_with_token("u1", "tok")
        self.assertEqual(me.avatar_url, "https://s.example.com/avatar/alice")


if __name__ == "__main__":
    unittest.main()
