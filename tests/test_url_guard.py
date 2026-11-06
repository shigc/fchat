"""core/url_guard.py：会话凭据外发与「用系统程序打开」的 URL 判定。

纯逻辑、离线、不碰 Qt（头像/附件加载器与 chat_view 都复用这两个判定）。

运行：python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.url_guard import (  # noqa: E402
    can_carry_credentials, is_openable_url, is_redirect, redirect_target,
    same_origin,
)

SERVER = "https://chat.example.com"


class SameOriginTest(unittest.TestCase):
    def test_same_host_is_origin(self):
        self.assertTrue(same_origin(f"{SERVER}/file-upload/a/b.png", SERVER))
        self.assertTrue(same_origin("https://chat.example.com:443/x", SERVER))

    def test_case_insensitive_host_and_scheme(self):
        self.assertTrue(same_origin("HTTPS://CHAT.Example.com/x", SERVER))

    def test_other_host_or_scheme_is_not_origin(self):
        for url in ("https://evil.example/x.png",
                    "http://chat.example.com/x",        # 协议降级
                    "https://chat.example.com.evil/x",  # 后缀伪装
                    "https://other:2379/x"):
            self.assertFalse(same_origin(url, SERVER), url)

    def test_relative_path_counts_as_origin(self):
        # 附件常是 /file-upload/…；没补全的这次也不能丢认证头
        self.assertTrue(same_origin("/file-upload/a.png", SERVER))

    def test_subdomain_is_not_origin(self):
        self.assertFalse(same_origin("https://cdn.example.com/a.png", SERVER))

    def test_empty_inputs(self):
        self.assertFalse(same_origin("", SERVER))
        self.assertFalse(same_origin("https://chat.example.com/x", ""))


class CarryCredentialsTest(unittest.TestCase):
    def test_external_url_gets_no_credentials(self):
        self.assertFalse(
            can_carry_credentials("https://evil.example/1.png", SERVER))

    def test_non_web_scheme_never_gets_credentials(self):
        for url in ("file:///c:/windows/system32/evil.exe",
                    "javascript:alert(1)", "customapp://do",
                    "ftp://chat.example.com/x"):
            self.assertFalse(can_carry_credentials(url, SERVER), url)

    def test_unknown_server_falls_back_to_scheme_check(self):
        """登录前/裸加载器没有 server 地址可比：只排除非 Web 协议。"""
        self.assertTrue(
            can_carry_credentials("https://any.example/avatar/u", ""))
        self.assertTrue(can_carry_credentials("/file-upload/a", ""))
        self.assertFalse(can_carry_credentials("file://c/x.exe", ""))

    def test_empty_url(self):
        self.assertFalse(can_carry_credentials("", SERVER))
        self.assertFalse(can_carry_credentials("   ", ""))


class OpenableUrlTest(unittest.TestCase):
    def test_web_schemes_allowed(self):
        for url in ("https://host/f.pdf", "http://10.0.0.1:3000/f.pdf"):
            self.assertTrue(is_openable_url(url), url)

    def test_dangerous_schemes_blocked(self):
        """Windows 上 openUrl 即 ShellExecute：这些会本地执行或外泄认证。"""
        for url in ("file://unc/attacker/share/x",
                    "file:///C:/Users/me/Downloads/evil.exe",
                    "customapp://run",
                    r"\\attacker\share\x.exe",
                    "javascript:alert(1)", "", "   "):
            self.assertFalse(is_openable_url(url), url)

    def test_whitespace_tolerated(self):
        self.assertTrue(is_openable_url("  https://host/a  "))


class _Resp:
    def __init__(self, status, location=None):
        self.status_code = status
        self.headers = {} if location is None else {"Location": location}


class RedirectTest(unittest.TestCase):
    """附件下载的跳转目标解析：每一跳都要重新判定能不能带会话凭据。"""

    def test_relative_location_resolved_against_current_url(self):
        got = redirect_target(_Resp(302, "/file-upload/a.png"),
                              f"{SERVER}/rooms/1")
        self.assertEqual(got, f"{SERVER}/file-upload/a.png")

    def test_cross_host_hop_loses_credentials(self):
        """跳转是凭据外泄的主通道：换了主机就不能再算同源。"""
        nxt = redirect_target(_Resp(302, "https://cdn.evil/x"), SERVER)
        self.assertEqual(nxt, "https://cdn.evil/x")
        self.assertFalse(can_carry_credentials(nxt, SERVER))

    def test_same_origin_hop_keeps_credentials(self):
        nxt = redirect_target(_Resp(308, f"{SERVER}:443/f"), SERVER)
        self.assertTrue(can_carry_credentials(nxt, SERVER))

    def test_not_a_usable_redirect(self):
        for resp in (_Resp(200, "/x"), _Resp(302), _Resp(304, "/x"),
                     _Resp(302, "   ")):
            self.assertEqual(redirect_target(resp, SERVER), "", resp.status_code)

    def test_non_web_location_rejected(self):
        for loc in ("file:///c:/x.exe", "javascript:alert(1)"):
            self.assertEqual(redirect_target(_Resp(302, loc), SERVER), "", loc)

    def test_backslash_location_stays_on_base_origin(self):
        """Location 里的反斜杠不是分隔符：只能当路径字符，不能变成 UNC。"""
        loc = r"\\attacker\share\x.exe"
        got = redirect_target(_Resp(302, loc), SERVER)
        self.assertTrue(got.startswith(SERVER + "/"), got)
        self.assertTrue(can_carry_credentials(got, SERVER))

    def test_headers_that_is_not_a_dict_is_ignored(self):
        class Loose:
            status_code = 302
            headers = object()
        self.assertEqual(redirect_target(Loose(), SERVER), "")

    def test_is_redirect_ignores_non_int_status(self):
        self.assertTrue(is_redirect(302))
        for bad in (None, "302", 200, 404):
            self.assertFalse(is_redirect(bad), bad)


class NonStringInputTest(unittest.TestCase):
    """加载器上的字段可能被换成假对象：判定只能失败关闭，不能抛异常。"""

    def test_garbage_urls_rejected(self):
        for bad in (None, 3, object()):
            self.assertFalse(is_openable_url(bad), bad)
            self.assertFalse(can_carry_credentials(bad, SERVER), bad)
            self.assertFalse(same_origin(bad, SERVER), bad)

    def test_garbage_server_falls_back_to_scheme_check(self):
        """服务器地址用不上时，退回「只认 Web 协议」的既有降级路径。"""
        for bad in (None, 3, object(), ""):
            self.assertTrue(
                can_carry_credentials(f"{SERVER}/file-upload/a.png", bad), bad)
            self.assertFalse(
                can_carry_credentials("file:///c:/x.exe", bad), bad)

    def test_relative_path_rule_survives_missing_server(self):
        self.assertTrue(same_origin("/file-upload/a.png", None))


if __name__ == "__main__":
    unittest.main()
