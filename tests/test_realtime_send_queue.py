"""DDP 发送队列单测：xhr_send 绝不能占用调用方线程（离线、不碰网络）。

背景（同机实测）：这个部署的反向代理不转发 Upgrade 头，实时通道只能走 SockJS
xhr-polling，于是每一次 xhr_send 都是一整趟 TCP+TLS 握手（约 1 秒，代理一慢顶到
20 秒超时）。点私聊订阅单个房间、登录时几十个房间整批订阅都发生在 Qt 主线程，
同步发送就是「窗口未响应」的根因。这里用 fake transport 注入 DDPClient，验证：
入队立即返回、投递发生在 ddp-sender 线程、短时间内的多帧合并成一次 send_many、
发送失败判死、断线时陈旧帧被丢弃、stop 能把两个线程收干净。
运行：python -m unittest discover -s tests -v
"""
import json
import threading
import time
import unittest
from unittest import mock

from core import realtime
from core.realtime import DDPClient


def _wait_until(cond, timeout=2.0):
    """带超时的轮询等待（不用固定 sleep 蒙）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.001)
    return False


def _delivered(client, transport, count: int):
    """等发送线程把 count 帧投递到 transport，且队列里不再排着帧。"""
    def _done():
        with client._outbox_lock:
            pending = bool(client._outbox)
        return len(transport.sent) >= count and not pending
    if not _wait_until(_done):
        with client._outbox_lock:
            queued = len(client._outbox)
        raise AssertionError(
            f"发送线程没投递出 {count} 帧（实际 {len(transport.sent)} 帧、"
            f"队列剩余 {queued} 帧）")


def _settled(client):
    """等队列排空，再留一点余量证明没有迟到投递（断言「不该发」时用）。"""
    assert _wait_until(lambda: not client._outbox), "队列没有排空"
    time.sleep(0.02)


def _stop_client(client):
    """收尾：停掉并等两个后台线程退出，别在套件里堆一堆 daemon 线程。"""
    client.stop()
    client.join(2.0)


class _BaseTransport:
    """记录投递结果，并记下投递发生在哪条线程（用于证明不占用调用方）。"""
    name = "fake"

    def __init__(self):
        self.broken = False
        self.sent = []
        self.batches = []
        self.calls = 0
        self.thread_names = []

    def _record(self, texts):
        self.calls += 1
        self.batches.append(len(texts))
        self.thread_names.append(threading.current_thread().name)
        self.sent.extend(json.loads(t) for t in texts)

    def send_many(self, texts):
        self._record(texts)

    def send(self, text):
        self.send_many([text])

    def close(self):
        pass


class _ParkedTransport(_BaseTransport):
    """send_many 停在 entered/release 之间：把发送线程「钉」在一次 POST 里。"""

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def send_many(self, texts):
        self.entered.set()
        self.release.wait(2.0)
        self._record(texts)


class _SlowTransport(_BaseTransport):
    """一次 send_many 耗时 sleep_sec 秒，模拟经代理的真实阻塞 POST。"""

    def __init__(self, sleep_sec=0.5):
        super().__init__()
        self.sleep_sec = sleep_sec

    def send_many(self, texts):
        time.sleep(self.sleep_sec)
        self._record(texts)


class _RaisingTransport(_BaseTransport):
    """发送即抛：模拟会话被服务端回收后的 xhr_send 404。"""

    def __init__(self):
        super().__init__()
        self.attempts = []

    def send_many(self, texts):
        self.attempts.append(len(texts))
        raise ConnectionError("xhr_send 失败：404")


class _QuietTransport(_BaseTransport):
    """给 _run_loop 用的假传输：run 立刻返回（等于一次断线），不碰网络。"""

    def run(self, on_open, on_frame, stop):
        return


class _StopOnRunTransport(_QuietTransport):
    """run 里把客户端停掉：让 _run_loop 走完一轮断线收尾后退出。

    不能在测试里 patch time.sleep 来收尾——_run_loop 的退避已是
    Event.wait（支持手动重连），patch sleep 不再拦截，循环会永不返回。
    """

    def __init__(self, client):
        super().__init__()
        self.client = client

    def run(self, on_open, on_frame, stop):
        self.client.stop()


class _QueueTestBase(unittest.TestCase):
    """公共骨架：用例里建出来的客户端统一在 tearDown 停线程，别堆一堆 daemon。"""

    def setUp(self):
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            _stop_client(client)

    def _client(self, transport=None, connected=True):
        client = DDPClient("https://chat.example.com", "uid1", "tok1")
        self.clients.append(client)
        if transport is not None:
            client._transport = transport
            client._ws_connected = connected
        return client


class SendQueueThreadingTest(_QueueTestBase):
    """调用方只入队：投递发生在 ddp-sender 线程。"""

    def test_subscribe_room_returns_while_post_is_parked(self):
        """发送线程卡在一次 xhr_send 里时，主线程的订阅调用必须立即返回。"""
        tp = _ParkedTransport()
        c = self._client(tp)
        c.subscribe_room("R1")
        self.assertTrue(_wait_until(tp.entered.is_set), "发送线程没接上队列")

        started = time.monotonic()
        c.subscribe_room("R2")                   # 此刻一次 POST 还悬着
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.05,
                        f"调用方被阻塞 POST 拖住了（{elapsed:.3f}s）")

        tp.release.set()
        _delivered(c, tp, 2)
        self.assertEqual(tp.batches, [1, 1])

    def test_bulk_subscribe_returns_immediately_though_post_is_slow(self):
        """登录时 95 个房间整批订阅：调用方不等 HTTP，帧稍后照常投递。"""
        rooms = [f"R{i}" for i in range(95)]
        tp = _SlowTransport(sleep_sec=0.5)
        c = self._client(tp)
        started = time.monotonic()
        c.subscribe_rooms(rooms)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.05,
                        f"整批订阅把调用方冻住了（{elapsed:.3f}s）")
        _delivered(c, tp, 95)
        self.assertEqual(len(tp.sent), 95)

    def test_delivery_happens_on_sender_thread(self):
        """帧由 ddp-sender 线程送出，不是由调用方线程顺手送。"""
        tp = _BaseTransport()
        c = self._client(tp)
        c.subscribe_rooms(["A", "B"])
        _delivered(c, tp, 2)
        self.assertEqual(set(tp.thread_names), {"ddp-sender"})
        self.assertNotIn(threading.current_thread().name, tp.thread_names)

    def test_sender_thread_is_daemon_and_named(self):
        """发送线程是 daemon 且叫 ddp-sender：不能挡住退出。"""
        tp = _BaseTransport()
        c = self._client(tp)
        c.subscribe_room("A")
        self.assertTrue(_wait_until(lambda: c._sender is not None))
        self.assertEqual(c._sender.name, "ddp-sender")
        self.assertTrue(c._sender.daemon)

    def test_queue_drains_without_start(self):
        """没走 start() 的用法（单测/脚本直连）也得有人消费队列，否则帧永久滞留。"""
        tp = _BaseTransport()
        c = self._client(tp)
        self.assertIsNone(c._sender)
        c.subscribe_room("A")
        _delivered(c, tp, 1)


class SendQueueBatchingTest(_QueueTestBase):
    """一批帧合并成一次 send_many：这是 AGENTS 里的硬约束，不能在队列层拆散。"""

    def test_frames_queued_while_sender_is_busy_merge_into_one_call(self):
        """发送线程正忙时排队的多帧，回头一次带走（1 次 POST 装多帧）。"""
        tp = _ParkedTransport()
        c = self._client(tp)
        c.subscribe_room("R1")
        self.assertTrue(_wait_until(tp.entered.is_set))
        # 发送线程被钉在 R1 的 POST 上，这几帧只能排在 outbox 里
        for rid in ("R2", "R3", "R4"):
            c.subscribe_room(rid)
        tp.release.set()

        _delivered(c, tp, 4)
        self.assertEqual(tp.calls, 2)                     # 不是 4 次
        self.assertEqual(tp.batches, [1, 3])

    def test_bulk_subscribe_is_not_split_into_per_frame_posts(self):
        """整批订阅在队列层不得逐帧发送：分块由 PollingTransport._SEND_CHUNK 负责。"""
        tp = _BaseTransport()
        c = self._client(tp)
        c.subscribe_rooms([f"R{i}" for i in range(realtime._SEND_CHUNK * 3 + 5)])
        _delivered(c, tp, realtime._SEND_CHUNK * 3 + 5)
        self.assertEqual(tp.batches, [realtime._SEND_CHUNK * 3 + 5])

    def test_connected_reconnect_frames_go_out_as_one_batch(self):
        """重连后的 login + 全量重订阅仍是一批。"""
        tp = _BaseTransport()
        c = self._client(tp)
        c._ws_connected = False
        for i in range(30):
            c._subscribed_rooms.add(f"R{i}")
        c._handle_frame(json.dumps({"msg": "connected", "session": "S1"}))
        _delivered(c, tp, 33)                             # login + 30 房间 + 2 notify
        self.assertEqual(tp.batches, [33])


class SendQueueFailureTest(_QueueTestBase):
    """发送失败要立刻把这条传输判死，措辞与级别保持原样。"""

    def test_send_failure_latches_transport_and_connection_dead(self):
        tp = _RaisingTransport()
        c = self._client(tp)
        c.subscribe_room("A")
        self.assertTrue(_wait_until(
            lambda: tp.attempts == [1] and tp.broken and not c._ws_connected))
        self.assertTrue(tp.broken)
        self.assertFalse(c._ws_connected)

    def test_send_failure_is_logged_as_warning(self):
        """日志级别必须是 warning：默认 INFO 环境下要看得见（真机就靠它定位）。"""
        tp = _RaisingTransport()
        c = self._client(tp)
        with self.assertLogs("core.realtime", level="WARNING") as captured:
            c.subscribe_room("A")
            # broken 是 log.warning 之后才置位的，等它就不会抢在日志落锤前退出
            self.assertTrue(_wait_until(lambda: tp.attempts == [1] and tp.broken))
        self.assertTrue(any("DDP 发送通道失效" in line for line in captured.output))
        self.assertIn("WARNING", {record.levelname for record in captured.records})

    def test_sender_survives_a_failing_batch(self):
        """一批发送失败不能把发送线程弄死：后面还要靠它，且不能重复投递。"""
        tp = _RaisingTransport()
        c = self._client(tp)
        c.subscribe_rooms(["A", "B"])
        self.assertTrue(_wait_until(lambda: tp.attempts == [2]))
        _settled(c)
        self.assertTrue(_wait_until(lambda: c._sender.is_alive()))
        self.assertEqual(tp.attempts, [2])                # 判死后不再重试同一批

    def test_dropped_frames_when_disconnected_before_send(self):
        """断线后队列里的帧直接丢弃：connected 分支会按房间重新生帧。"""
        tp = _ParkedTransport()
        c = self._client(tp)
        c.subscribe_room("R1")
        self.assertTrue(_wait_until(tp.entered.is_set))
        c.subscribe_room("R2")                            # 排在 outbox 里
        c._ws_connected = False                           # 断线
        tp.release.set()
        _settled(c)
        self.assertEqual([f["params"][0] for f in tp.sent], [["R1"]])

    def test_dropped_frames_when_transport_gone(self):
        tp = _ParkedTransport()
        c = self._client(tp)
        c.subscribe_room("R1")
        self.assertTrue(_wait_until(tp.entered.is_set))
        c.subscribe_room("R2")
        c._transport = None                               # _run_loop 清理后的状态
        tp.release.set()
        _settled(c)
        self.assertEqual([f["params"][0] for f in tp.sent], [["R1"]])

    def test_stale_frames_never_reach_the_new_transport(self):
        """重连换上的新 transport 不能收到上一条连接残留的帧（否则同房间重复订阅）。"""
        old = _ParkedTransport()
        c = self._client(old)
        c.subscribe_room("R1")
        self.assertTrue(_wait_until(old.entered.is_set))
        old.release.set()
        _delivered(c, old, 1)

        # 断线清理：置空 transport + 判未连接 + 清掉陈旧帧（_run_loop 的 finally）
        c._ws_connected = False
        c._transport = None
        c._clear_outbox()
        fresh = _BaseTransport()
        c._transport = fresh
        c._ws_connected = True
        _settled(c)
        self.assertEqual(fresh.sent, [])

    def test_run_loop_clears_outbox_on_disconnect(self):
        """走真正的 _run_loop 收尾：队列里的陈旧帧在 finally 被清空。"""
        parked = _ParkedTransport()
        c = self._client(parked)
        c.subscribe_room("R1")
        self.assertTrue(_wait_until(parked.entered.is_set))
        # 发送线程被钉住，这一帧只能留在队列里等清理
        c._send_many([{"msg": "pong"}])
        self.assertEqual(len(c._outbox), 1)

        idle = _StopOnRunTransport(c)
        with mock.patch.object(DDPClient, "_open_transport", return_value=idle):
            c._run_loop()                                 # 一轮「连接→断线→退出」

        parked.release.set()
        self.assertEqual(c._outbox, [])                   # 陈旧帧已被清掉
        _settled(c)
        self.assertEqual([f for f in parked.sent if f["msg"] == "pong"], [])
        self.assertEqual(idle.sent, [])


class SendQueueLifecycleTest(unittest.TestCase):
    """start/stop 的线程收尾：别让发送线程留活。"""

    def test_start_launches_sender_together_with_loop(self):
        c = DDPClient("https://chat.example.com", "uid1", "tok1")
        self.addCleanup(_stop_client, c)
        idle = _QuietTransport()
        # patch 生效期间就得把线程收干净：否则循环线程会在 patch 摘掉后
        # 调用真正的 _open_transport（离线单测不许碰网络）
        with mock.patch.object(DDPClient, "_open_transport", return_value=idle):
            c.start()
            self.assertTrue(_wait_until(
                lambda: c._sender is not None and c._sender.is_alive()))
            self.assertTrue(c._thread.is_alive())
            self.assertEqual(c._sender.name, "ddp-sender")
            c.stop()
            c.join(2.0)
            self.assertFalse(c._thread.is_alive())
            self.assertFalse(c._sender.is_alive())
        # _QuietTransport.run 立刻返回 → 走断线清理，不产生任何帧
        self.assertEqual(idle.sent, [])

    def test_stop_wakes_and_joins_sender_thread(self):
        """stop() 要唤醒可能在 wait() 里睡着的发送线程，join 后线程真的结束。"""
        tp = _BaseTransport()
        c = DDPClient("https://chat.example.com", "uid1", "tok1")
        self.addCleanup(_stop_client, c)
        c._transport = tp
        c._ws_connected = True
        c.subscribe_room("A")
        self.assertTrue(_wait_until(lambda: c._sender is not None
                                    and c._sender.is_alive()))
        # 此刻发送线程正睡在 event.wait(0.5) 里；stop 必须把它叫醒，
        # 否则 join 至少要等满一个轮询周期
        started = time.monotonic()
        c.stop()
        c.join(2.0)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.45, f"stop() 没唤醒发送线程（{elapsed:.3f}s）")
        self.assertFalse(c._sender.is_alive())
        self.assertIsNone(c._thread)                 # 没 start() 过：只有发送线程
        alive = [t for t in threading.enumerate() if t.name == "ddp-sender"]
        self.assertEqual(alive, [])

    def test_stop_is_idempotent_without_sender(self):
        """没发过帧的客户端 stop() 不该炸（登出/关窗口都会调它）。"""
        c = DDPClient("https://chat.example.com", "uid1", "tok1")
        c.stop()
        c.join(1.0)
        self.assertIsNone(c._sender)


if __name__ == "__main__":
    unittest.main()
