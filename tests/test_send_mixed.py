"""send_mixed 图文混排发送测试（mock rooms_media / rooms_media_confirm）。离线。

新语义（2026-10-16）：媒体逐个走上传 + mediaConfirm——服务端为每个媒体生成
原生文件/图片消息、上传记录永久保留（未确认的约 24 小时后被服务端清理，
历史消息里的媒体全部 404，这就是旧「拼 markdown 正文」实现的根因）。
媒体之前最近的一段文本作该条消息的 caption；纯文本退回一条 chat.postMessage。
例外：无文件且配置了上传服务时，纯图片继续走图床内联 markdown（旧行为）。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.api_client import ApiClient, ApiError  # noqa: E402


class SendMixedTest(unittest.TestCase):
    def _client(self):
        c = ApiClient("https://s.example.com")
        c._rc = mock.Mock()
        c.user_id = "u1"
        c.auth_token = "tok"
        # 跳过 parse_message，直接返回底层消息 dict，聚焦组装与上传逻辑
        c._message_from_json = mock.Mock(side_effect=lambda m, rid: m)
        c._rc.rooms_media_confirm.return_value = {
            "message": {"_id": "m1", "msg": ""}}
        return c

    def _ok_media(self, c, fid="f1"):
        c._rc.rooms_media.return_value = {
            "success": True, "file": {"_id": fid, "url": "/uf/sys.png"}}

    def _png_file(self):
        f = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        f.write(b"\x89PNG\r\n\x1a\nfake")
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def _txt_file(self):
        f = tempfile.NamedTemporaryFile(suffix=".txt", delete=False)
        f.write(b"hello")
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    # ---- 纯文本 / 无媒体 ---- #
    def test_text_only_single_post(self):
        c = self._client()
        c._rc.chat_post_message.return_value = {
            "message": {"_id": "m1", "msg": "hi"}}
        ret = c.send_mixed("r1", [("text", "hi")])
        c._rc.chat_post_message.assert_called_once_with("hi", room_id="r1")
        c._rc.rooms_media.assert_not_called()
        c._rc.rooms_media_confirm.assert_not_called()
        self.assertEqual(ret["_id"], "m1")

    def test_empty_text_parts_post_nothing(self):
        c = self._client()
        ret = c.send_mixed("r1", [("text", "   ")])
        c._rc.chat_post_message.assert_not_called()
        self.assertIsNone(ret)

    # ---- 原生路径：上传 + confirm ---- #
    def test_image_uploaded_then_confirmed_with_caption(self):
        c = self._client()
        self._ok_media(c)
        c._rc.rooms_media_confirm.return_value = {
            "message": {"_id": "m9", "msg": "图文"}}
        path = self._png_file()
        ret = c.send_mixed("r1", [("text", "图文"), ("image", path)])
        c._rc.rooms_media.assert_called_once_with("r1", path)
        c._rc.rooms_media_confirm.assert_called_once_with("r1", "f1", msg="图文")
        # 媒体走 confirm 后不再有自组 markdown 的 chat.postMessage
        c._rc.chat_post_message.assert_not_called()
        self.assertEqual(ret["_id"], "m9")

    def test_caption_is_text_since_previous_media(self):
        # [图1, "中", 图2]：图1 无前置文本（不传 msg），图2 带 "中"
        c = self._client()
        c._rc.rooms_media.side_effect = [
            {"success": True, "file": {"_id": "f1", "url": "/a1.png"}},
            {"success": True, "file": {"_id": "f2", "url": "/a2.png"}},
        ]
        p1 = self._png_file()
        p2 = self._png_file()
        c.send_mixed("r1", [("image", p1), ("text", "中"), ("image", p2)])
        self.assertEqual(c._rc.rooms_media_confirm.call_args_list, [
            mock.call("r1", "f1"),
            mock.call("r1", "f2", msg="中"),
        ])

    def test_trailing_text_merges_into_media_caption(self):
        # 尾随文本并入最后一条媒体的 caption：图文混排合并为一条消息
        # （官方客户端「图片 + 说明」格式），不再拆成图片 + 纯文本两条
        c = self._client()
        self._ok_media(c)
        path = self._png_file()
        ret = c.send_mixed("r1", [("image", path), ("text", "尾部说明")])
        c._rc.rooms_media_confirm.assert_called_once_with(
            "r1", "f1", msg="尾部说明")
        c._rc.chat_post_message.assert_not_called()
        self.assertEqual(ret["_id"], "m1")

    def test_trailing_multiline_text_merges_with_caption(self):
        # 图片前后的文本都进同一条 caption（换行连接）
        c = self._client()
        self._ok_media(c)
        path = self._png_file()
        c.send_mixed("r1", [("text", "看这个"), ("image", path),
                            ("text", "第一行"), ("text", "第二行")])
        c._rc.rooms_media_confirm.assert_called_once_with(
            "r1", "f1", msg="看这个\n第一行\n第二行")
        c._rc.chat_post_message.assert_not_called()

    def test_trailing_text_between_medias_merges_into_last(self):
        # 多媒体时尾随文本只并入最后一条媒体的 caption
        c = self._client()
        c._rc.rooms_media.side_effect = [
            {"success": True, "file": {"_id": "f1", "url": "/u/1.png"}},
            {"success": True, "file": {"_id": "f2", "url": "/u/2.png"}},
        ]
        c._rc.rooms_media_confirm.side_effect = [
            {"message": {"_id": "m1", "msg": ""}},
            {"message": {"_id": "m2", "msg": ""}},
        ]
        p1, p2 = self._png_file(), self._png_file()
        c.send_mixed("r1", [("image", p1), ("text", "第一张"),
                            ("image", p2), ("text", "第二张说明")])
        self.assertEqual(
            c._rc.rooms_media_confirm.call_args_list,
            [mock.call("r1", "f1"),
             mock.call("r1", "f2", msg="第一张\n第二张说明")])
        c._rc.chat_post_message.assert_not_called()

    def test_whitespace_only_text_is_not_caption(self):
        c = self._client()
        self._ok_media(c)
        path = self._png_file()
        c.send_mixed("r1", [("text", "  \n "), ("image", path)])
        c._rc.rooms_media_confirm.assert_called_once_with("r1", "f1")

    def test_file_part_confirmed_with_caption(self):
        c = self._client()
        self._ok_media(c)
        path = self._txt_file()
        c.send_mixed("r1", [("text", "附件如下"), ("file", path)])
        c._rc.rooms_media.assert_called_once_with("r1", path)
        c._rc.rooms_media_confirm.assert_called_once_with(
            "r1", "f1", msg="附件如下")
        c._rc.chat_post_message.assert_not_called()

    # ---- 失败路径 ---- #
    def test_missing_file_raises_no_upload(self):
        c = self._client()
        missing = os.path.join(tempfile.gettempdir(), "no-such-mixed-file.png")
        with self.assertRaises(ApiError):
            c.send_mixed("r1", [("image", missing)])
        c._rc.rooms_media.assert_not_called()
        c._rc.rooms_media_confirm.assert_not_called()

    def test_upload_failure_raises_no_confirm(self):
        c = self._client()
        c._rc.rooms_media.return_value = {"success": False}
        path = self._png_file()
        with self.assertRaises(ApiError):
            c.send_mixed("r1", [("text", "hi"), ("image", path)])
        c._rc.rooms_media_confirm.assert_not_called()
        c._rc.chat_post_message.assert_not_called()

    def test_upload_missing_fid_raises(self):
        c = self._client()
        c._rc.rooms_media.return_value = {"success": True, "file": {"url": "/u"}}
        path = self._png_file()
        with self.assertRaises(ApiError):
            c.send_mixed("r1", [("image", path)])
        c._rc.rooms_media_confirm.assert_not_called()

    def test_confirm_failure_raises(self):
        c = self._client()
        self._ok_media(c)
        c._rc.rooms_media_confirm.side_effect = RuntimeError("boom")
        path = self._png_file()
        with self.assertRaises(ApiError):
            c.send_mixed("r1", [("image", path)])

    def test_stops_after_first_upload_failure(self):
        c = self._client()
        c._rc.rooms_media.side_effect = [
            {"success": False},
            {"success": True, "file": {"_id": "f2", "url": "/a2.png"}},
        ]
        p1 = self._png_file()
        p2 = self._png_file()
        with self.assertRaises(ApiError):
            c.send_mixed("r1", [("image", p1), ("image", p2)])
        self.assertEqual(c._rc.rooms_media.call_count, 1)

    # ---- 上传服务（图床）分支：仅纯「文本 + 图片」保持旧内联行为 ---- #
    def _svc_client(self, token="tk123"):
        # 配置了上传服务：纯图片场景走 _upload_via_service
        c = ApiClient(
            "https://s.example.com",
            upload_service_url="https://img.example.com/up",
            upload_service_token=token)
        c._rc = mock.Mock()
        c.user_id = "u1"
        c.auth_token = "tok"
        c._message_from_json = mock.Mock(side_effect=lambda m, rid: m)
        return c

    def test_service_images_only_still_inline(self):
        # 无文件：保持旧行为——图床 URL 内联进一条 markdown 消息
        c = self._svc_client()
        c._rc.chat_post_message.return_value = {"message": {}}
        path = self._png_file()
        with mock.patch("core.api_client.requests.post") as post:
            post.return_value.status_code = 200
            post.return_value.raise_for_status = mock.Mock()
            post.return_value.text = "https://s.example.com/svc/a.png"
            c.send_mixed("r1", [("text", "hi"), ("image", path)])
        post.assert_called_once()
        c._rc.rooms_media.assert_not_called()
        c._rc.rooms_media_confirm.assert_not_called()
        expected = "hi\n\n![picture](https://s.example.com/svc/a.png)\n"
        c._rc.chat_post_message.assert_called_once_with(expected, room_id="r1")

    def test_service_sends_bearer_token(self):
        c = self._svc_client(token="secret-token")
        c._rc.chat_post_message.return_value = {"message": {}}
        path = self._png_file()
        with mock.patch("core.api_client.requests.post") as post:
            post.return_value.status_code = 200
            post.return_value.raise_for_status = mock.Mock()
            post.return_value.text = "https://s.example.com/svc/c.png"
            c.send_mixed("r1", [("image", path)])
        _, kwargs = post.call_args
        self.assertEqual(kwargs["headers"]["Authorization"],
                         "Bearer secret-token")
        c._rc.chat_post_message.assert_called_once()

    def test_service_plain_text_url_tolerates_whitespace(self):
        # 响应体为纯文本 URL（非 JSON）：能容忍首尾空白
        c = self._svc_client()
        c._rc.chat_post_message.return_value = {"message": {}}
        path = self._png_file()
        with mock.patch("core.api_client.requests.post") as post:
            post.return_value.status_code = 200
            post.return_value.raise_for_status = mock.Mock()
            post.return_value.text = "  https://s.example.com/svc/b.png\n"
            c.send_mixed("r1", [("image", path)])
        expected = "\n![picture](https://s.example.com/svc/b.png)\n"
        c._rc.chat_post_message.assert_called_once_with(expected, room_id="r1")

    def test_service_failure_raises_no_post(self):
        c = self._svc_client()
        path = self._png_file()
        with mock.patch("core.api_client.requests.post",
                        side_effect=OSError("net")):
            with self.assertRaises(ApiError):
                c.send_mixed("r1", [("image", path)])
        c._rc.chat_post_message.assert_not_called()

    def test_service_empty_url_raises(self):
        # 上传服务返回空文本视为失败
        c = self._svc_client()
        path = self._png_file()
        with mock.patch("core.api_client.requests.post") as post:
            post.return_value.status_code = 200
            post.return_value.raise_for_status = mock.Mock()
            post.return_value.text = ""
            with self.assertRaises(ApiError):
                c.send_mixed("r1", [("image", path)])
        c._rc.chat_post_message.assert_not_called()

    def test_service_with_file_goes_native(self):
        # 混排里有文件：整体走原生消息（图片也不再经图床，
        # 保证同一批媒体形式一致、且都能被 confirm 永久保留）
        c = self._svc_client()
        self._ok_media(c)
        img = self._png_file()
        doc = self._txt_file()
        with mock.patch("core.api_client.requests.post") as post:
            c.send_mixed("r1", [("text", "看"), ("image", img), ("file", doc)])
        post.assert_not_called()
        self.assertEqual(c._rc.rooms_media.call_count, 2)
        self.assertEqual(c._rc.rooms_media_confirm.call_args_list, [
            mock.call("r1", "f1", msg="看"),
            mock.call("r1", "f1"),
        ])


if __name__ == "__main__":
    unittest.main()
