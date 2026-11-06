"""用户在线状态：归一化 / tooltip / UserStatusProvider / 行组件接入。

- normalize_status_key 矩阵：department 含「停用」→ departed 优先；
  active 不参与判定；_mobile 后缀归并；未知档回退 offline
- Provider：缓存 TTL、在途去重、并发上限、失败负缓存、invalidate
- _MemberRow / _DirectoryRow：即时画点 + 补拉回填
- ProfileCard 状态行

GUI 相关 offscreen 渲染、不联网；run_async 一律 mock 成同步替身。
运行：python -m unittest discover -s tests -v
"""
import hashlib
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication  # noqa: E402

from core.models import DirectoryEntry  # noqa: E402
from ui import user_status  # noqa: E402
from ui.user_status import (  # noqa: E402
    UserStatusProvider, normalize_status_key, status_label, status_tooltip,
)

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _sync_run_async(fn, on_done=None, on_fail=None, parent=None):
    """run_async 同步替身：当前线程直接执行并回调。"""
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001
        if on_fail:
            on_fail(e)
        return
    if on_done:
        on_done(result)


# --------------------------------------------------------------------- #
# 归一化
# --------------------------------------------------------------------- #
class NormalizeTest(unittest.TestCase):

    def test_department_stop_working_wins(self):
        self.assertEqual(
            normalize_status_key("online", "烽火通信 / 停用帐户 / 2025年"),
            "departed")

    def test_active_false_does_not_mean_departed(self):
        """真机调研：active=false 里混有临时/外包/公用账户，不都对应离职。"""
        self.assertEqual(
            normalize_status_key("online", "临时用户 / 外包人员",
                                 active=False),
            "online")
        self.assertEqual(normalize_status_key("online", "公用账户",
                                              active=False), "online")

    def test_mobile_suffix_merged(self):
        self.assertEqual(normalize_status_key("online_mobile"), "online")
        self.assertEqual(normalize_status_key("away_mobile"), "away")

    def test_unknown_falls_back_offline(self):
        self.assertEqual(normalize_status_key(""), "offline")
        self.assertEqual(normalize_status_key("none"), "offline")
        self.assertEqual(normalize_status_key(None), "offline")

    def test_valid_keys_pass_through(self):
        for k in ("online", "away", "busy", "dnd"):
            self.assertEqual(normalize_status_key(k), k)

    def test_tooltip_and_label(self):
        self.assertEqual(status_tooltip("online", "会议中"), "在线 · 会议中")
        self.assertEqual(status_tooltip("online", ""), "在线")
        self.assertEqual(status_tooltip("departed", "随便写的"), "已停用")
        # 私聊标题栏：自定义状态优先；离职固定显示「已停用」
        self.assertEqual(status_label("online", "会议中"), "会议中")
        self.assertEqual(status_label("busy", ""), "忙碌")
        self.assertEqual(status_label("departed", "会议中"), "已停用")


# --------------------------------------------------------------------- #
# Provider
# --------------------------------------------------------------------- #
class _FakeClient:
    def __init__(self, infos=None, fail=False):
        self.infos = infos or {}
        self.fail = fail
        self.calls = []

    def fetch_user_info(self, username):
        self.calls.append(username)
        if self.fail:
            raise RuntimeError("boom")
        return self.infos.get(username)


class ProviderTest(unittest.TestCase):

    def setUp(self):
        self.provider = UserStatusProvider()

    def test_fetch_and_cache_hit(self):
        client = _FakeClient({"u1": {"username": "u1", "status": "online",
                                     "status_text": "会议中",
                                     "department": "研发"}})
        self.provider.set_session(client, None)
        with mock.patch("ui.user_status.run_async", _sync_run_async):
            got = []
            self.provider.get("u1", lambda info: got.append(info))
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(got[0]["status_key"], "online")
            self.assertEqual(got[0]["status_text"], "会议中")
            # 缓存命中不再发请求，且同样拿到归一化结果
            got2 = []
            self.provider.get("u1", lambda info: got2.append(info))
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(got2[0]["status_key"], "online")

    def test_negative_cache_on_failure(self):
        client = _FakeClient(fail=True)
        self.provider.set_session(client, None)
        with mock.patch("ui.user_status.run_async", _sync_run_async):
            got = []
            self.provider.get("u1", lambda info: got.append(info))
            self.assertIsNone(got[0])
            self.provider.get("u1", lambda info: got.append(info))
            # 负缓存期内不再重试
            self.assertEqual(len(client.calls), 1)
            self.assertIsNone(got[1])

    def test_pending_dedup(self):
        """同一用户在途请求合并：挂起时第二次 get 不发新请求。"""
        client = _FakeClient({"u1": {"username": "u1", "status": "online"}})
        self.provider.set_session(client, None)
        tasks = []

        def hanging_async(fn, on_done=None, on_fail=None, parent=None):
            tasks.append((fn, on_done, on_fail))

        with mock.patch("ui.user_status.run_async", hanging_async):
            p = self.provider
            p._client = client
            # pending 表须非空：_pump 会跳过没有回调的在途项（无谓请求）
            p._pending["u1"] = [lambda info: None]
            p._queue.append("u1")
            p._pump()
            self.assertEqual(len(tasks), 1)
            got = []
            p.get("u1", lambda info: got.append(info))
            self.assertEqual(len(tasks), 1)
            # 完成后挂起回调一起收到结果
            _fn, on_done, _on_fail = tasks[0]
            on_done({"username": "u1", "status": "online"})
            self.assertEqual(len(got), 1)

    def test_concurrency_limit(self):
        client = _FakeClient()
        self.provider.set_session(client, None)
        tasks = []

        def hanging_async(fn, on_done=None, on_fail=None, parent=None):
            tasks.append((fn, on_done, on_fail))

        with mock.patch("ui.user_status.run_async", hanging_async):
            for i in range(6):
                self.provider.get(f"u{i}", lambda info: None)
            self.assertEqual(len(tasks), user_status._MAX_INFLIGHT)
            # 完成一个 → 队列补位一个
            _fn, on_done, _on_fail = tasks[0]
            on_done({"username": "u0", "status": "online"})
            self.assertEqual(len(tasks), user_status._MAX_INFLIGHT + 1)

    def test_invalidate_forces_refetch(self):
        client = _FakeClient({"u1": {"username": "u1", "status": "online"}})
        self.provider.set_session(client, None)
        with mock.patch("ui.user_status.run_async", _sync_run_async):
            self.provider.get("u1", lambda info: None)
            self.provider.invalidate("u1")
            self.provider.get("u1", lambda info: None)
            self.assertEqual(len(client.calls), 2)

    def test_no_client_calls_back_none(self):
        got = []
        self.provider.get("u1", lambda info: got.append(info))
        self.assertEqual(got, [None])


class ApplyPushTest(unittest.TestCase):
    """实时推送落缓存：合并旧资料、清负缓存、发信号。"""

    def setUp(self):
        self.provider = UserStatusProvider()

    def test_push_without_prior_info_makes_minimal_entry(self):
        got = []
        self.provider.status_pushed.connect(lambda u, k: got.append((u, k)))
        info = self.provider.apply_push("u1", "online")
        self.assertEqual(info["status_key"], "online")
        self.assertEqual(info["username"], "u1")
        self.assertEqual(got, [("u1", "online")])

    def test_push_keeps_department_for_departed(self):
        """推送只有 status：department 必须从旧资料保留，离职判定不丢。"""
        client = _FakeClient({"u1": {"username": "u1", "status": "online",
                                     "department": "停用帐户"}})
        self.provider.set_session(client, None)
        with mock.patch("ui.user_status.run_async", _sync_run_async):
            self.provider.get("u1", lambda info: None)
        info = self.provider.apply_push("u1", "online")
        self.assertEqual(info["status_key"], "departed")

    def test_push_clears_negative_cache(self):
        client = _FakeClient({"u1": {"username": "u1"}}, fail=True)
        self.provider.set_session(client, None)
        with mock.patch("ui.user_status.run_async", _sync_run_async):
            self.provider.get("u1", lambda info: None)
        self.assertIn("u1", self.provider._failed_until)
        self.provider.apply_push("u1", "away")
        self.assertNotIn("u1", self.provider._failed_until)
        # 推送后的缓存可直接 peek 到新状态
        self.assertEqual(self.provider.peek("u1")["status_key"], "away")

    def test_push_of_unknown_status_normalizes_offline(self):
        info = self.provider.apply_push("u1", "whatever")
        self.assertEqual(info["status_key"], "offline")


# --------------------------------------------------------------------- #
# 磁盘缓存（先旧后新）
# --------------------------------------------------------------------- #
class _SessionClient:
    """带 server_url / me 的客户端替身：水合与落盘按账号隔离。"""

    def __init__(self, infos=None, fail=False):
        self.server_url = "http://srv"
        self.me = type("Me", (), {"username": "me"})()
        self.infos = infos or {}
        self.fail = fail
        self.calls = []

    def fetch_user_info(self, username):
        self.calls.append(username)
        if self.fail:
            raise RuntimeError("boom")
        return self.infos.get(username)


class StatusDiskCacheTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch("core.status_cache.cache_root",
                             return_value=self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.provider = UserStatusProvider()

    def test_roundtrip(self):
        from core import status_cache
        entries = {"u1": {"username": "u1", "status": "online",
                          "status_key": "online", "department": "研发",
                          "_ts": 100}}
        status_cache.save_status_cache("http://srv", "me", entries)
        loaded = status_cache.load_status_cache("http://srv", "me")
        self.assertEqual(loaded["u1"][0]["status_key"], "online")
        self.assertEqual(loaded["u1"][1], 100)
        # 按账号隔离 + 损坏文件容错
        self.assertEqual(status_cache.load_status_cache("http://srv",
                                                        "other"), {})
        with open(os.path.join(self.tmp.name, "status_cache",
                               hashlib.md5(b"http://srv|me").hexdigest()
                               + ".json"), "w", encoding="utf-8") as f:
            f.write("{bad")
        self.assertEqual(status_cache.load_status_cache("http://srv", "me"),
                         {})

    def test_stale_cache_delivered_then_refreshed(self):
        """过期缓存先同步回调（先旧后新），随后拉新再回调一次。"""
        client = _SessionClient({"u1": {"username": "u1",
                                        "status": "online"}})
        self.provider.set_session(client, None)
        stale = {"username": "u1", "status_key": "offline"}
        self.provider._cache["u1"] = (
            stale, time.monotonic() - user_status._TTL - 1)
        with mock.patch("ui.user_status.run_async", _sync_run_async):
            got = []
            self.provider.get("u1", lambda info: got.append(info))
        self.assertEqual(got[0]["status_key"], "offline")
        self.assertEqual(got[1]["status_key"], "online")
        self.assertEqual(len(client.calls), 1)

    def test_stale_cache_within_negative_window_not_refetched(self):
        """负缓存窗口内：给旧值、不再发请求（上层重试机制兜底）。"""
        client = _SessionClient(fail=True)
        self.provider.set_session(client, None)
        stale = {"username": "u1", "status_key": "offline"}
        self.provider._cache["u1"] = (
            stale, time.monotonic() - user_status._TTL - 1)
        with mock.patch("ui.user_status.run_async", _sync_run_async):
            got = []
            # 第一次 get：先回调旧值，随后拉取失败回调 None
            self.provider.get("u1", lambda info: got.append(info))
            self.assertEqual(got[0]["status_key"], "offline")
            self.assertIsNone(got[1])
            # 负缓存窗口内第二次 get：只给旧值、不再发请求
            self.provider.get("u1", lambda info: got.append(info))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(got[2]["status_key"], "offline")

    def test_set_session_hydrates_from_disk(self):
        """登录注入 session 时载入磁盘缓存：条目已过期但仍先显示。"""
        from core import status_cache
        status_cache.save_status_cache("http://srv", "me", {
            "u1": {"username": "u1", "status": "busy",
                   "status_key": "busy", "_ts": int(time.time()) - 3600},
        })
        client = _SessionClient({"u1": {"username": "u1",
                                        "status": "online"}})
        self.provider.set_session(client, None)
        with mock.patch("ui.user_status.run_async", _sync_run_async):
            got = []
            self.provider.get("u1", lambda info: got.append(info))
        self.assertEqual(got[0]["status_key"], "busy")
        self.assertEqual(got[1]["status_key"], "online")

    def test_preload_without_client_delivers_cached(self):
        """登录前预载（无 client）：缓存条目照样同步回调显示旧状态；
        缓存没有的回调 None 且不发请求。"""
        from core import status_cache
        status_cache.save_status_cache("http://srv", "me", {
            "u1": {"username": "u1", "status": "online",
                   "status_key": "online", "_ts": int(time.time()) - 3600},
        })
        self.provider.preload("http://srv", "me")
        got = []
        self.provider.get("u1", lambda info: got.append(info))
        self.assertEqual(got[0]["status_key"], "online")
        got2 = []
        self.provider.get("u2", lambda info: got2.append(info))
        self.assertEqual(got2, [None])

    def test_successful_fetch_persists(self):
        from core import status_cache
        client = _SessionClient({"u1": {"username": "u1", "name": "U1",
                                        "status": "away"}})
        self.provider.set_session(client, None)
        with mock.patch("ui.user_status.run_async", _sync_run_async):
            self.provider.get("u1", lambda info: None)
        self.provider.flush()
        loaded = status_cache.load_status_cache("http://srv", "me")
        self.assertEqual(loaded["u1"][0]["status_key"], "away")
        self.assertGreater(loaded["u1"][1], 0)
        # 换号不串数据
        self.assertNotIn("u1", status_cache.load_status_cache(
            "http://srv", "other"))


# --------------------------------------------------------------------- #
# 行组件
# --------------------------------------------------------------------- #
class MemberRowTest(unittest.TestCase):

    def test_immediate_status_from_members_endpoint(self):
        from ui.room_panels import _MemberRow
        row = _MemberRow(
            {"_id": "1", "username": "alice", "name": "Alice",
             "status": "busy"},
            lambda key, default="": default or "#000000")
        self.assertEqual(row._status_key, "busy")

    def test_enrich_updates_to_departed(self):
        from ui.room_panels import _MemberRow
        row = _MemberRow(
            {"_id": "1", "username": "alice", "name": "Alice",
             "status": "offline"},
            lambda key, default="": default or "#000000")
        row.apply_status_info({"status_key": "departed", "status_text": ""})
        self.assertEqual(row._status_key, "departed")


class DirectoryRowTest(unittest.TestCase):

    def test_search_row_departed_immediate(self):
        """搜索结果带 department：离职即时可见，不等补拉。"""
        from ui.directory_panel import _DirectoryRow
        entry = DirectoryEntry(kind="user", rid="", name="陈一",
                               username="chenyi",
                               department="烽火通信 / 停用帐户")
        row = _DirectoryRow(entry, lambda key, default="": "#000000",
                            joined=False)
        self.assertEqual(row._status_key, "departed")

    def test_browse_row_status_immediate(self):
        """users.list 浏览模式带 status：在线态即时可见。"""
        from ui.directory_panel import _DirectoryRow
        entry = DirectoryEntry(kind="user", rid="", name="陈一",
                               username="chenyi", status="busy")
        row = _DirectoryRow(entry, lambda key, default="": "#000000",
                            joined=False)
        self.assertEqual(row._status_key, "busy")
        row.apply_status_info({"status_key": "departed", "status_text": ""})
        self.assertEqual(row._status_key, "departed")

    def test_channel_row_has_no_avatar(self):
        from ui.directory_panel import _DirectoryRow
        entry = DirectoryEntry(kind="channel", rid="r1", name="general")
        row = _DirectoryRow(entry, lambda key, default="": "#000000",
                            joined=False)
        self.assertIsNone(row.avatar)


# --------------------------------------------------------------------- #
# ProfileCard
# --------------------------------------------------------------------- #
class ProfileCardStatusTest(unittest.TestCase):

    def _card(self):
        from core.models import User
        from ui.profile_popup import ProfileCard
        return ProfileCard(User(id="1", username="alice", name="Alice"))

    def test_departed_shown(self):
        card = self._card()
        card._apply_info({"username": "alice", "name": "Alice",
                          "status": "offline",
                          "department": "停用帐户 / 2025年"})
        self.assertEqual(card.status_label.text(), "已停用")

    def test_custom_status_shown(self):
        card = self._card()
        card._apply_info({"username": "alice", "name": "Alice",
                          "status": "online", "status_text": "会议中",
                          "department": "研发部"})
        self.assertEqual(card.status_label.text(), "在线 · 会议中")

    def test_missing_status_not_shown(self):
        """enrich 前的首次渲染没有 status 字段，不应误显示「离线」。"""
        card = self._card()
        card._apply_info({"username": "alice", "name": "Alice"})
        self.assertFalse(card.status_label.isVisibleTo(card))


if __name__ == "__main__":
    unittest.main()
