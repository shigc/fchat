"""OpenAI 兼容协议的 AI 客户端。

设计约束：
- 独立 requests.Session：ApiClient 的 `_rc` 共享 Session 非线程安全，
  AI 调用全部在后台线程，不能复用。
- 流式逐块回调 + 取消 Event：`run_async` 只在结束时回调，打字机效果和
  真正的「停止」需要自己读 SSE。
- 日志只记模型名/耗时/字符数，绝不记正文、回复和 token。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Optional

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger(__name__)

# 摘要/日报这类请求输入长、输出也长，读超时不能按交互请求的 20s 算；
# 流式下该超时是「两块数据之间的最大间隔」，不会误杀整体长输出。
DEFAULT_TIMEOUT = (10, 180)
_POOL_SIZE = 4
_ERROR_TEXT_LIMIT = 300

# 网关瞬时过载（429/502/503）自动重试：只在拿到响应头、尚未读任何流式
# 内容时发生，重试不会造成正文重复；等待期间 cancel 置位立即放弃。
_RETRY_STATUSES = frozenset((429, 502, 503))
_RETRY_MAX = 2
_RETRY_BACKOFF = (2.0, 4.0)


class AIError(Exception):
    """AI 调用失败（配置/网络/协议）。"""


class AINotConfigured(AIError):
    """地址或模型未配置。"""


class AICancelled(AIError):
    """用户取消。不是失败，UI 据此静默收尾。"""


def resolve_endpoint(base_url: str) -> str:
    """把用户填写的地址归一化成 /chat/completions 完整地址。

    兼容三种写法：完整端点、以 /v1 结尾的基地址、裸域名。
    """
    url = (base_url or "").strip().rstrip("/")
    if not url:
        return ""
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return url + "/chat/completions"
    return url + "/v1/chat/completions"


def _short_text(text, limit: int = _ERROR_TEXT_LIMIT) -> str:
    s = str(text or "").replace("\n", " ").strip()
    return s[:limit]


def _retry_delay(resp, attempt: int) -> float:
    """重试等待秒数：Retry-After 头优先（钳到 1..30s），否则固定退避。"""
    try:
        return min(30.0, max(1.0, float(resp.headers.get("Retry-After"))))
    except (TypeError, ValueError):
        return _RETRY_BACKOFF[attempt]


def _error_message(data) -> str:
    """从 OpenAI 风格错误体里取可读信息；取不到就回退截断后的原串。"""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return _short_text(err.get("message") or err)
        if err:
            return _short_text(err)
        for key in ("message", "detail", "msg"):
            if data.get(key):
                return _short_text(data.get(key))
    return _short_text(data)


def _content_from_obj(obj) -> str:
    """从非流式响应里取正文，兼容 content 为分段数组与 reasoning 字段。"""
    choices = obj.get("choices") if isinstance(obj, dict) else None
    if not choices:
        return ""
    choice = choices[0] or {}
    msg = choice.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        content = "".join(
            str(part.get("text") or "")
            for part in content if isinstance(part, dict))
    if not content:
        content = msg.get("reasoning_content") or choice.get("text") or ""
    return str(content or "")


def _delta_from_obj(obj) -> str:
    """从流式 chunk 里取增量正文；兼容 message 形态。"""
    choices = obj.get("choices") if isinstance(obj, dict) else None
    if not choices:
        return ""
    choice = choices[0] or {}
    delta = choice.get("delta") or {}
    content = delta.get("content")
    if isinstance(content, list):
        content = "".join(
            str(part.get("text") or "")
            for part in content if isinstance(part, dict))
    if not content:
        content = delta.get("reasoning_content") or ""
    if not content and choice.get("message"):
        content = _content_from_obj(obj)
    return str(content or "")


class AIClient:
    """一次 AI 任务一个实例，内部 Session 不跨线程共享。"""

    @classmethod
    def from_config(cls, cfg: dict, **kwargs) -> "AIClient":
        """按 config.load_config() 的字段构造；只校验地址与模型。

        是否启用、是否已同意隐私提示由 UI 层判断（配置不完整时也要能
        在设置页里填完再保存）。
        """
        cfg = cfg or {}
        url = (cfg.get("llm_url") or "").strip()
        model = (cfg.get("llm_model") or "").strip()
        if not url or not model:
            raise AINotConfigured("AI 服务未配置（缺少地址或模型）")
        return cls(url, token=cfg.get("llm_token") or "", model=model,
                   **kwargs)

    def __init__(self, base_url: str, token: str = "", model: str = "",
                 *, timeout=DEFAULT_TIMEOUT, ssl_verify: bool = True):
        self.base_url = (base_url or "").strip()
        self.endpoint = resolve_endpoint(self.base_url)
        self.token = (token or "").strip()
        self.model = (model or "").strip()
        self.timeout = timeout
        self._session = requests.Session()
        adapter = HTTPAdapter(pool_connections=_POOL_SIZE,
                              pool_maxsize=_POOL_SIZE)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)
        self._session.verify = bool(ssl_verify)

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # noqa: BLE001
            pass

    def _headers(self, stream: bool) -> dict:
        headers = {"Content-Type": "application/json"}
        if stream:
            headers["Accept"] = "text/event-stream"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def chat(self, messages: list, *, temperature: float = 0.3,
             max_tokens: int = 2048,
             cancel: Optional[threading.Event] = None,
             on_delta: Optional[Callable[[str], None]] = None) -> str:
        """发送对话请求，返回完整正文。

        on_delta 非空时走 SSE 流式；cancel 置位后抛 AICancelled。
        """
        if not self.endpoint or not self.model:
            raise AINotConfigured("AI 服务未配置（缺少地址或模型）")
        if cancel is not None and cancel.is_set():
            raise AICancelled("已取消")
        stream = on_delta is not None
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        t0 = time.perf_counter()
        attempt = 0
        while True:
            try:
                resp = self._session.post(
                    self.endpoint, json=payload, headers=self._headers(stream),
                    timeout=self.timeout, stream=stream)
            except requests.RequestException as e:
                raise AIError(f"AI 服务连接失败：{_short_text(e)}") from e
            if resp.status_code in _RETRY_STATUSES and attempt < _RETRY_MAX:
                delay = _retry_delay(resp, attempt)
                resp.close()
                attempt += 1
                log.warning("AI 服务返回 HTTP %s，%.1fs 后第 %d/%d 次重试",
                            resp.status_code, delay, attempt, _RETRY_MAX)
                if cancel is None:
                    time.sleep(delay)
                elif cancel.wait(delay):
                    raise AICancelled("已取消")
                continue
            break
        try:
            if resp.status_code >= 400:
                raise AIError(
                    f"AI 服务返回 HTTP {resp.status_code}：{self._server_error(resp)}")
            if stream:
                text = self._read_stream(resp, on_delta, cancel)
            else:
                text = self._read_json(resp)
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
        if cancel is not None and cancel.is_set():
            raise AICancelled("已取消")
        if not text:
            raise AIError("AI 返回内容为空")
        log.info("AI 请求完成 model=%s chars=%d elapsed=%.1fs",
                 self.model, len(text), time.perf_counter() - t0)
        return text

    def _server_error(self, resp) -> str:
        try:
            data = resp.json()
        except ValueError:
            return _short_text(getattr(resp, "text", ""))
        return _error_message(data)

    def _read_json(self, resp) -> str:
        try:
            data = resp.json()
        except ValueError as e:
            raise AIError("AI 返回的不是合法 JSON") from e
        if "error" in (data or {}):
            raise AIError(f"AI 服务错误：{_error_message(data)}")
        return _content_from_obj(data).strip()

    def _read_stream(self, resp, on_delta, cancel) -> str:
        parts = []
        plain_lines = []
        for raw in resp.iter_lines(chunk_size=1024):
            if cancel is not None and cancel.is_set():
                raise AICancelled("已取消")
            if not raw:
                continue
            line = raw.decode("utf-8", "replace").strip()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                # 网关未按 SSE 返回时（整个 JSON 一行）留到最后兜底解析
                plain_lines.append(line)
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("error"):
                raise AIError(f"AI 服务错误：{_error_message(obj)}")
            delta = _delta_from_obj(obj)
            if delta:
                parts.append(delta)
                try:
                    on_delta(delta)
                except Exception:  # noqa: BLE001
                    # 回调异常不能中断流，否则网络已收的内容全丢
                    log.exception("AI 流式回调异常")
        if parts:
            return "".join(parts).strip()
        if plain_lines:
            # 有些网关忽略 stream 参数直接回普通 JSON
            try:
                obj = json.loads("\n".join(plain_lines))
            except ValueError:
                raise AIError("AI 流式响应无法解析")
            if isinstance(obj, dict) and obj.get("error"):
                raise AIError(f"AI 服务错误：{_error_message(obj)}")
            return _content_from_obj(obj).strip()
        return ""

    def test(self) -> str:
        """连通性测试：极短输出、较短超时，返回模型回复。"""
        return self.chat(
            [{"role": "user", "content": "请只回复四个字：连接正常"}],
            temperature=0, max_tokens=32)
