"""core 流式上传测试：_MultipartBody 线格式/进度/取消 + send_file 分流。离线。

session.post 全 mock（假 post 增量 read body 模拟 urllib3），不联网。
线格式与 requests files= 逐字节比对（boundary 归一化后），守住「手动流式
multipart 必须和库发出的完全一致」这条不变量——尤其中文文件名按原始 UTF-8。
"""
import io
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from core.api_client import (  # noqa: E402
    ApiClient, ApiError, UploadCancelled, _MultipartBody,
)

_OK_RESP = {"success": True, "file": {"_id": "f1", "url": "/files/f1"}}


def _rc_stub(post_side_effect=None):
    """撑起 _rooms_media_stream 的假 RocketChat（真字符串 url + Mock session）。"""
    rc = mock.Mock()
    rc.server_url = "https://s.example.com"
    rc.api_path = "/api/v1/"
    rc.headers = {"X-Auth-Token": "tok", "X-User-Id": "u1"}
    rc.ssl_verify = True
    rc.cert = None
    rc.proxies = None
    rc.timeout = 30
    if post_side_effect is not None:
        rc.session.post.side_effect = post_side_effect
    return rc


def _fake_post(captured, resp=None, status=200):
    """假 session.post：增量 read body（模拟 urllib3），记录到 captured。"""
    def post(url, data=None, headers=None, **kw):
        captured["url"] = url
        captured["headers"] = headers
        captured["len"] = getattr(data, "len", None)
        if data is not None:
            buf = bytearray()
            while True:
                chunk = data.read(8192)   # 取消时这里会抛 UploadCancelled
                if not chunk:
                    break
                buf += chunk
            captured["body"] = bytes(buf)
        r = mock.Mock()
        r.status_code = status
        r.json.return_value = resp if resp is not None else _OK_RESP
        return r
    return post


class MultipartBodyTest(unittest.TestCase):
    def _parts(self, name, payload, mime="application/pdf"):
        boundary = "deadbeefdeadbeefdeadbeefdeadbeef"
        safe = name.replace('"', "").replace("\r", "").replace("\n", "")
        prefix = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{safe}"\r\n'
            f"Content-Type: {mime}\r\n\r\n").encode("utf-8")
        suffix = f"\r\n--{boundary}--\r\n".encode("utf-8")
        return boundary, prefix, suffix

    def test_streamed_body_byte_identical_to_requests(self):
        """手动拼的 multipart 与 requests files= 发出的逐字节一致（中文文件名）。"""
        name, mime, payload = "测试报告.pdf", "application/pdf", b"Z" * 5000
        boundary, prefix, suffix = self._parts(name, payload, mime)
        ref = requests.Request(
            "POST", "http://x/",
            files={"file": (name, io.BytesIO(payload), mime)}).prepare()
        rc_boundary = ref.headers["Content-Type"].split("boundary=")[1]
        ref_body = ref.body.replace(rc_boundary.encode(), boundary.encode())

        body = _MultipartBody(io.BytesIO(payload), prefix, suffix, len(payload))
        out = bytearray()
        while True:
            chunk = body.read(8192)
            if not chunk:
                break
            out += chunk
        self.assertEqual(bytes(out), ref_body)
        self.assertEqual(body.len, len(ref_body))

    def test_read_all_returns_full_body(self):
        _, prefix, suffix = self._parts("a.bin", b"abc", "application/octet-stream")
        body = _MultipartBody(io.BytesIO(b"abc"), prefix, suffix, 3)
        self.assertEqual(body.read(-1), prefix + b"abc" + suffix)

    def test_progress_monotonic_to_total(self):
        payload = b"Q" * (200 * 1024)
        _, prefix, suffix = self._parts("a.bin", payload, "application/octet-stream")
        prog = []
        body = _MultipartBody(io.BytesIO(payload), prefix, suffix, len(payload),
                              progress_cb=lambda d, t: prog.append((d, t)))
        while body.read(16384):
            pass
        self.assertEqual(prog[-1], (len(payload), len(payload)))
        self.assertEqual([d for d, _ in prog], sorted(d for d, _ in prog))

    def test_cancel_raises_during_file_stage(self):
        payload = b"Q" * 5000
        _, prefix, suffix = self._parts("a.bin", payload, "application/octet-stream")
        ev = threading.Event()
        ev.set()
        body = _MultipartBody(io.BytesIO(payload), prefix, suffix, len(payload),
                              cancel=ev)
        body.read(len(prefix))          # 吃掉前缀进入文件段
        with self.assertRaises(UploadCancelled):
            body.read(100)


class RoomsMediaStreamTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "报告.txt")
        with open(self.path, "wb") as f:
            f.write(b"hello-world" * 100)
        self.size = os.path.getsize(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def _client(self, post):
        c = ApiClient("https://s.example.com")
        c._rc = _rc_stub(post)
        return c

    def test_stream_posts_to_rooms_media_and_parses(self):
        cap = {}
        c = self._client(_fake_post(cap))
        fid, url = c._rooms_media_stream("r1", self.path)
        self.assertEqual((fid, url), ("f1", "/files/f1"))
        self.assertTrue(cap["url"].endswith("/api/v1/rooms.media/r1"))
        self.assertIn("multipart/form-data; boundary=", cap["headers"]["Content-Type"])
        self.assertEqual(cap["headers"]["X-Auth-Token"], "tok")   # 复用库鉴权头
        self.assertEqual(cap["len"], len(cap["body"]))             # .len 与实际一致
        # 中文文件名按原始 UTF-8 进 Content-Disposition
        self.assertIn("报告.txt".encode("utf-8"), cap["body"])

    def test_stream_reports_progress(self):
        cap = {}
        c = self._client(_fake_post(cap))
        prog = []
        c._rooms_media_stream("r1", self.path,
                              progress_cb=lambda d, t: prog.append((d, t)))
        self.assertEqual(prog[-1], (self.size, self.size))

    def test_stream_cancel_propagates(self):
        cap = {}
        c = self._client(_fake_post(cap))
        ev = threading.Event()
        ev.set()
        with self.assertRaises(UploadCancelled):
            c._rooms_media_stream("r1", self.path, cancel=ev)

    def test_stream_http_error_raises_apierror(self):
        cap = {}
        c = self._client(_fake_post(cap, resp=None, status=500))
        with self.assertRaises(ApiError):
            c._rooms_media_stream("r1", self.path)

    def test_stream_success_false_raises_apierror(self):
        cap = {}
        c = self._client(_fake_post(cap, resp={"success": False}))
        with self.assertRaises(ApiError):
            c._rooms_media_stream("r1", self.path)


class SendFileDispatchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "a.txt")
        with open(self.path, "wb") as f:
            f.write(b"data" * 500)

    def tearDown(self):
        self._tmp.cleanup()

    def _client(self, post):
        c = ApiClient("https://s.example.com")
        c._rc = _rc_stub(post)
        c._rc.rooms_media_confirm.return_value = {"message": {"_id": "m1"}}
        return c

    def test_no_progress_uses_rooms_media_then_confirm(self):
        """不给 progress_cb/cancel：走库 rooms_media 上传 + mediaConfirm 确认。"""
        cap = {}
        c = self._client(_fake_post(cap))
        c._rc.rooms_media.return_value = _OK_RESP
        self.assertTrue(c.send_file("r1", self.path))
        c._rc.rooms_media.assert_called_once_with("r1", self.path)
        c._rc.rooms_media_confirm.assert_called_once_with("r1", "f1")
        self.assertNotIn("body", cap)            # session.post 未被调用
        c._rc.call_api_post.assert_not_called()  # 不再自组 chat.sendMessage

    def test_with_progress_uses_stream_then_confirm(self):
        """给了 progress_cb：走流式上传 + confirm，不碰库 rooms_media。"""
        cap = {}
        c = self._client(_fake_post(cap))
        prog = []
        self.assertTrue(c.send_file("r1", self.path,
                                    progress_cb=lambda d, t: prog.append((d, t))))
        c._rc.rooms_media.assert_not_called()
        self.assertIn("body", cap)               # session.post 被调用
        self.assertTrue(prog)
        c._rc.rooms_media_confirm.assert_called_once_with("r1", "f1")
        c._rc.call_api_post.assert_not_called()

    def test_with_cancel_only_uses_stream(self):
        cap = {}
        c = self._client(_fake_post(cap))
        ev = threading.Event()
        self.assertTrue(c.send_file("r1", self.path, cancel=ev))
        c._rc.rooms_media.assert_not_called()
        self.assertIn("body", cap)
        c._rc.rooms_media_confirm.assert_called_once_with("r1", "f1")

    def test_cancel_propagates_as_uploadcancelled(self):
        cap = {}
        c = self._client(_fake_post(cap))
        ev = threading.Event()
        ev.set()
        with self.assertRaises(UploadCancelled):   # 不被包成 ApiError
            c.send_file("r1", self.path, cancel=ev)
        c._rc.rooms_media_confirm.assert_not_called()

    def test_stream_failure_wrapped_as_apierror(self):
        cap = {}
        c = self._client(_fake_post(cap, resp=None, status=500))
        with self.assertRaises(ApiError):
            c.send_file("r1", self.path, progress_cb=lambda d, t: None)
        c._rc.rooms_media_confirm.assert_not_called()

    def test_confirm_failure_wrapped_as_apierror(self):
        """上传成功但确认失败：如实报错（不静默发成未确认上传）。"""
        cap = {}
        c = self._client(_fake_post(cap))
        c._rc.rooms_media_confirm.side_effect = RuntimeError("boom")
        with self.assertRaises(ApiError):
            c.send_file("r1", self.path, progress_cb=lambda d, t: None)


if __name__ == "__main__":
    unittest.main()
