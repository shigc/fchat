"""core.ai_client 单元测试：URL 归一化、流式/非流式解析、取消与错误。

不联网：所有 HTTP 响应由 FakeResponse 提供。
"""
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ai_client import (  # noqa: E402
    AICancelled, AIClient, AIError, AINotConfigured, resolve_endpoint,
)


class FakeResponse:
    def __init__(self, status_code=200, data=None, lines=None, text="",
                 headers=None):
        self.status_code = status_code
        self._data = data
        self._lines = list(lines or [])
        self.text = text
        self.headers = dict(headers or {})
        self.closed = False

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data

    def iter_lines(self, chunk_size=1024):
        for line in self._lines:
            yield line.encode("utf-8") if isinstance(line, str) else line

    def close(self):
        self.closed = True


def make_client(**kwargs):
    kwargs.setdefault("base_url", "https://ai.example.com/v1")
    kwargs.setdefault("token", "secret-token")
    kwargs.setdefault("model", "qwen3")
    return AIClient(**kwargs)


class ResolveEndpointTest(unittest.TestCase):
    def test_full_endpoint_unchanged(self):
        self.assertEqual(
            resolve_endpoint("https://ai.example.com/v1/chat/completions"),
            "https://ai.example.com/v1/chat/completions")

    def test_v1_base(self):
        self.assertEqual(resolve_endpoint("https://ai.example.com/v1"),
                         "https://ai.example.com/v1/chat/completions")

    def test_bare_host(self):
        self.assertEqual(resolve_endpoint("https://ai.example.com/"),
                         "https://ai.example.com/v1/chat/completions")

    def test_empty(self):
        self.assertEqual(resolve_endpoint(""), "")
        self.assertEqual(resolve_endpoint(None), "")

    def test_http_and_path_preserved(self):
        self.assertEqual(resolve_endpoint(" http://10.0.0.1:8000/api "),
                         "http://10.0.0.1:8000/api/v1/chat/completions")


class ChatTest(unittest.TestCase):
    def test_not_configured(self):
        client = AIClient("", token="", model="")
        with self.assertRaises(AINotConfigured):
            client.chat([{"role": "user", "content": "hi"}])

    def test_non_stream_success_payload_and_headers(self):
        resp = FakeResponse(data={"choices": [
            {"message": {"role": "assistant", "content": "你好"}}]})
        client = make_client()
        client._session.post = mock.Mock(return_value=resp)
        text = client.chat([{"role": "user", "content": "hi"}],
                           temperature=0.2, max_tokens=100)
        self.assertEqual(text, "你好")
        args, kwargs = client._session.post.call_args
        self.assertEqual(args[0], "https://ai.example.com/v1/chat/completions")
        self.assertFalse(kwargs["json"]["stream"])
        self.assertEqual(kwargs["json"]["model"], "qwen3")
        self.assertEqual(kwargs["json"]["max_tokens"], 100)
        self.assertEqual(kwargs["headers"]["Authorization"],
                         "Bearer secret-token")
        self.assertTrue(resp.closed)

    def test_message_content_parts_list(self):
        resp = FakeResponse(data={"choices": [{"message": {
            "content": [{"type": "text", "text": "分"}, {"text": "段"}]}}]})
        client = make_client()
        client._session.post = mock.Mock(return_value=resp)
        self.assertEqual(client.chat([]), "分段")

    def test_reasoning_content_fallback(self):
        resp = FakeResponse(data={"choices": [{"message": {
            "content": "", "reasoning_content": "思考结果"}}]})
        client = make_client()
        client._session.post = mock.Mock(return_value=resp)
        self.assertEqual(client.chat([]), "思考结果")

    def test_http_error_contains_status_and_message(self):
        resp = FakeResponse(status_code=401,
                            data={"error": {"message": "invalid api key"}})
        client = make_client()
        client._session.post = mock.Mock(return_value=resp)
        with self.assertRaises(AIError) as ctx:
            client.chat([])
        self.assertIn("401", str(ctx.exception))
        self.assertIn("invalid api key", str(ctx.exception))
        self.assertNotIn("secret-token", str(ctx.exception))

    def test_empty_content_raises(self):
        resp = FakeResponse(data={"choices": []})
        client = make_client()
        client._session.post = mock.Mock(return_value=resp)
        with self.assertRaises(AIError):
            client.chat([])

    def test_cancel_before_request_skips_network(self):
        client = make_client()
        client._session.post = mock.Mock()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(AICancelled):
            client.chat([], cancel=cancel)
        client._session.post.assert_not_called()

    def test_connection_error_wrapped(self):
        import requests
        client = make_client()
        client._session.post = mock.Mock(
            side_effect=requests.ConnectionError("boom"))
        with self.assertRaises(AIError) as ctx:
            client.chat([])
        self.assertIn("连接失败", str(ctx.exception))


class RetryTest(unittest.TestCase):
    """429/502/503 瞬时过载自动重试：退避、Retry-After、取消打断。"""

    def _busy(self, status=503):
        return FakeResponse(
            status_code=status, data={"error": {"message": "忙"}})

    def test_503_retried_then_success(self):
        ok = FakeResponse(data={"choices": [{"message": {"content": "好了"}}]})
        client = make_client()
        client._session.post = mock.Mock(side_effect=[self._busy(), ok])
        with mock.patch("core.ai_client.time.sleep") as m_sleep:
            self.assertEqual(client.chat([]), "好了")
        self.assertEqual(client._session.post.call_count, 2)
        m_sleep.assert_called_once_with(2.0)

    def test_retries_exhausted_raises_last_error(self):
        client = make_client()
        client._session.post = mock.Mock(return_value=self._busy())
        with mock.patch("core.ai_client.time.sleep"):
            with self.assertRaises(AIError) as ctx:
                client.chat([])
        self.assertIn("503", str(ctx.exception))
        self.assertEqual(client._session.post.call_count, 3)

    def test_429_uses_retry_after_header(self):
        busy = self._busy(429)
        busy.headers = {"Retry-After": "7"}
        ok = FakeResponse(data={"choices": [{"message": {"content": "好"}}]})
        client = make_client()
        client._session.post = mock.Mock(side_effect=[busy, ok])
        with mock.patch("core.ai_client.time.sleep") as m_sleep:
            self.assertEqual(client.chat([]), "好")
        m_sleep.assert_called_once_with(7.0)

    def test_502_also_retried(self):
        ok = FakeResponse(data={"choices": [{"message": {"content": "好"}}]})
        client = make_client()
        client._session.post = mock.Mock(
            side_effect=[self._busy(502), self._busy(503), ok])
        with mock.patch("core.ai_client.time.sleep"):
            self.assertEqual(client.chat([]), "好")
        self.assertEqual(client._session.post.call_count, 3)

    def test_non_retry_status_not_retried(self):
        resp = FakeResponse(status_code=401,
                            data={"error": {"message": "bad key"}})
        client = make_client()
        client._session.post = mock.Mock(return_value=resp)
        with self.assertRaises(AIError):
            client.chat([])
        self.assertEqual(client._session.post.call_count, 1)

    def test_cancel_during_backoff_aborts(self):
        client = make_client()
        client._session.post = mock.Mock(return_value=self._busy())
        cancel = threading.Event()
        with mock.patch.object(cancel, "wait", return_value=True):
            with self.assertRaises(AICancelled):
                client.chat([], cancel=cancel)
        self.assertEqual(client._session.post.call_count, 1)


class StreamTest(unittest.TestCase):
    def test_stream_deltas_and_done(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"你"}}]}',
            "",
            'data: {"choices":[{"delta":{"content":"好"}}]}',
            "data: [DONE]",
        ]
        resp = FakeResponse(lines=lines)
        client = make_client()
        client._session.post = mock.Mock(return_value=resp)
        chunks = []
        text = client.chat([], on_delta=chunks.append)
        self.assertEqual(text, "你好")
        self.assertEqual(chunks, ["你", "好"])
        _, kwargs = client._session.post.call_args
        self.assertTrue(kwargs["json"]["stream"])
        self.assertEqual(kwargs["headers"]["Accept"], "text/event-stream")

    def test_stream_ignores_comment_and_bad_json(self):
        lines = [
            ": ping",
            "data: not-json",
            'data: {"choices":[{"delta":{"content":"ok"}}]}',
        ]
        resp = FakeResponse(lines=lines)
        client = make_client()
        client._session.post = mock.Mock(return_value=resp)
        self.assertEqual(client.chat([], on_delta=lambda s: None), "ok")

    def test_stream_error_object(self):
        lines = ['data: {"error":{"message":"rate limited"}}']
        resp = FakeResponse(lines=lines)
        client = make_client()
        client._session.post = mock.Mock(return_value=resp)
        with self.assertRaises(AIError) as ctx:
            client.chat([], on_delta=lambda s: None)
        self.assertIn("rate limited", str(ctx.exception))

    def test_stream_gateway_returns_plain_json(self):
        import json
        body = json.dumps({"choices": [{"message": {"content": "整体返回"}}]})
        resp = FakeResponse(lines=[body])
        client = make_client()
        client._session.post = mock.Mock(return_value=resp)
        self.assertEqual(client.chat([], on_delta=lambda s: None), "整体返回")

    def test_cancel_mid_stream(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"a"}}]}',
            'data: {"choices":[{"delta":{"content":"b"}}]}',
        ]
        resp = FakeResponse(lines=lines)
        client = make_client()
        client._session.post = mock.Mock(return_value=resp)
        cancel = threading.Event()

        def _on_delta(_chunk):
            cancel.set()

        with self.assertRaises(AICancelled):
            client.chat([], cancel=cancel, on_delta=_on_delta)

    def test_delta_message_fallback(self):
        lines = ['data: {"choices":[{"message":{"content":"兜底"}}]}']
        resp = FakeResponse(lines=lines)
        client = make_client()
        client._session.post = mock.Mock(return_value=resp)
        self.assertEqual(client.chat([], on_delta=lambda s: None), "兜底")

    def test_stream_empty_raises(self):
        resp = FakeResponse(lines=["data: [DONE]"])
        client = make_client()
        client._session.post = mock.Mock(return_value=resp)
        with self.assertRaises(AIError):
            client.chat([], on_delta=lambda s: None)


class FromConfigTest(unittest.TestCase):
    def test_builds_client_and_normalizes_endpoint(self):
        cfg = {"llm_url": " https://ai.example.com/v1 ",
               "llm_model": " qwen3 ", "llm_token": " tk "}
        client = AIClient.from_config(cfg)
        self.assertEqual(client.endpoint,
                         "https://ai.example.com/v1/chat/completions")
        self.assertEqual(client.model, "qwen3")
        self.assertEqual(client.token, "tk")

    def test_missing_url_or_model_raises(self):
        for cfg in ({}, {"llm_url": "https://x"}, {"llm_model": "m"},
                    {"llm_url": "  ", "llm_model": "m"}):
            with self.assertRaises(AINotConfigured):
                AIClient.from_config(cfg)

    def test_empty_token_allowed(self):
        client = AIClient.from_config(
            {"llm_url": "https://x", "llm_model": "m"})
        self.assertEqual(client.token, "")


if __name__ == "__main__":
    unittest.main()
