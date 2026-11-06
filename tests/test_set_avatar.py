"""set_avatar 头像上传测试（mock requests.post）。离线。"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.api_client import ApiClient, ApiError  # noqa: E402


class SetAvatarTest(unittest.TestCase):
    def _client(self):
        c = ApiClient("https://s.example.com")
        c._rc = mock.Mock()
        c.user_id = "u1"
        c.auth_token = "tok"
        return c

    def _png_file(self):
        f = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        f.write(b"\x89PNG\r\n\x1a\nfake")
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_posts_multipart_with_auth(self):
        c = self._client()
        with mock.patch("core.api_client.requests.post") as post:
            post.return_value.json.return_value = {"success": True}
            self.assertTrue(c.set_avatar(self._png_file()))
        args, kwargs = post.call_args
        self.assertIn("/api/v1/users.setAvatar", args[0])
        self.assertEqual(kwargs["headers"]["X-Auth-Token"], "tok")
        self.assertEqual(kwargs["headers"]["X-User-Id"], "u1")
        self.assertIn("image", kwargs["files"])
        self.assertEqual(kwargs["timeout"], 30)
        self.assertTrue(kwargs["verify"])

    def test_success_false_raises_with_server_error(self):
        c = self._client()
        with mock.patch("core.api_client.requests.post") as post:
            post.return_value.json.return_value = {
                "success": False, "error": "文件太大"}
            with self.assertRaises(ApiError) as ctx:
                c.set_avatar(self._png_file())
        self.assertIn("文件太大", str(ctx.exception))

    def test_missing_file_raises(self):
        c = self._client()
        missing = os.path.join(tempfile.gettempdir(),
                               "no-such-avatar-file-xyz.png")
        with self.assertRaises(ApiError):
            c.set_avatar(missing)

    def test_network_error_raises_api_error(self):
        c = self._client()
        with mock.patch("core.api_client.requests.post",
                        side_effect=OSError("boom")):
            with self.assertRaises(ApiError):
                c.set_avatar(self._png_file())

    def test_not_logged_in_raises(self):
        c = ApiClient("https://s.example.com")
        with self.assertRaises(ApiError):
            c.set_avatar(self._png_file())


if __name__ == "__main__":
    unittest.main()
