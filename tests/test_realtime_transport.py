"""DDP 传输层单测：SockJS 成帧、传输回落、收发接线（全部离线，不碰网络）。"""
import json
import threading
import time
import unittest
from unittest import mock

from core import realtime
from core.models import ts_to_epoch
from core.realtime import (
    DDPClient, PollingTransport, WebSocketTransport, _parse_sockjs_frames,
)


class _Resp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class _FakeTransport:
    """记录 DDPClient 发出去的帧；batches 记下每次批量调用的帧数。"""
    name = "fake"

    def __init__(self):
        self.sent = []
        self.batches = []
        self.broken = False

    def send(self, text):
        self.send_many([text])

    def send_many(self, texts):
        self.batches.append(len(texts))
        self.sent.extend(json.loads(t) for t in texts)

    def close(self):
        pass


class _RaisingTransport(_FakeTransport):
    """发送即失败：模拟服务端已把会话回收后的 xhr_send 404。"""

    def __init__(self):
        super().__init__()
        self.attempts = []

    def send_many(self, texts):
        self.attempts.append(len(texts))
        raise ConnectionError("xhr_send 失败：404")


def _wait_until(cond, timeout=2.0):
    """带超时的轮询等待（发送已改异步，断言前要等 ddp-sender 线程跑完）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.001)
    return False


def _delivered(client, transport, count: int):
    """等发送线程把 count 帧投递到 transport，且队列里不再排队着帧。"""
    def _done():
        with client._outbox_lock:
            pending = bool(client._outbox)
        return len(transport.sent) >= count and not pending
    if not _wait_until(_done):
        raise AssertionError(
            f"发送线程没投递出 {count} 帧（实际 {len(transport.sent)} 帧）")


def _settled(client):
    """等队列排空，再留一点余量证明没有迟到投递（断言「不该发」时用）。"""
    assert _wait_until(lambda: not client._outbox), "队列没有排空"
    time.sleep(0.02)


def _stop_client(client):
    """用例收尾：停掉并等发送线程退出，别在整个套件里堆一堆 daemon 线程。"""
    client.stop()
    client.join(2.0)


class TestSockjsFrames(unittest.TestCase):
    def test_keepalive_legs_produce_nothing(self):
        self.assertEqual(_parse_sockjs_frames("o\n"), [])
        self.assertEqual(_parse_sockjs_frames("h\n"), [])
        self.assertEqual(_parse_sockjs_frames("\n"), [])

    def test_data_frame_decodes_to_raw_ddp_strings(self):
        body = 'a["{\\"msg\\":\\"connected\\",\\"session\\":\\"S1\\"}"]\n'
        self.assertEqual(
            _parse_sockjs_frames(body),
            [("a", ['{"msg":"connected","session":"S1"}'])])

    def test_close_frame_reported(self):
        frames = _parse_sockjs_frames('c[3000,"Go away"]')
        self.assertEqual([op for op, _ in frames], ["c"])

    def test_malformed_line_ignored(self):
        self.assertEqual(_parse_sockjs_frames("a{not json"), [])


class TestTransportChoice(unittest.TestCase):
    def _client(self):
        return DDPClient("https://chat.example.com", "uid1", "tok1")

    def test_urls_derived_from_server(self):
        c = self._client()
        self.assertEqual(c.ws_url, "wss://chat.example.com/websocket")
        self.assertEqual(c.sockjs_url, "https://chat.example.com/sockjs")
        plain = DDPClient("http://chat.local/", "u", "t")
        self.assertEqual(plain.ws_url, "ws://chat.local/websocket")
        self.assertEqual(plain.sockjs_url, "http://chat.local/sockjs")

    def test_websocket_preferred_when_it_works(self):
        c = self._client()
        with mock.patch.object(WebSocketTransport, "connect",
                               return_value=None) as ws_connect:
            t = c._open_transport()
        self.assertIsInstance(t, WebSocketTransport)
        ws_connect.assert_called_once_with()
        self.assertFalse(c._prefer_polling)

    def test_falls_back_when_handshake_refused(self):
        """服务端反代挡掉 Upgrade 时（本项目实测 400），必须换到长轮询。"""
        c = self._client()
        with mock.patch.object(WebSocketTransport, "connect",
                               side_effect=Exception("Handshake status 400")), \
             mock.patch.object(PollingTransport, "connect",
                               return_value=None):
            t = c._open_transport()
        self.assertIsInstance(t, PollingTransport)
        self.assertTrue(c._prefer_polling)

    def test_fallback_latches_so_handshake_is_not_retried(self):
        """回落之后不再反复试 WebSocket，否则会像以前那样持续刷失败日志。"""
        c = self._client()
        c._prefer_polling = True
        with mock.patch.object(WebSocketTransport, "connect") as ws_connect, \
             mock.patch.object(PollingTransport, "connect",
                               return_value=None):
            t = c._open_transport()
        self.assertIsInstance(t, PollingTransport)
        ws_connect.assert_not_called()


class TestPollingTransport(unittest.TestCase):
    def _tp(self):
        return PollingTransport("https://chat.example.com/sockjs",
                                ssl_verify=False)

    def test_connect_needs_open_frame(self):
        tp = self._tp()
        with mock.patch.object(realtime.requests, "post",
                               return_value=_Resp(200, "o\n")):
            tp.connect()
        with mock.patch.object(realtime.requests, "post",
                               return_value=_Resp(400, "Not a valid request")):
            with self.assertRaises(ConnectionError):
                tp.connect()

    def test_session_path_shape(self):
        """SockJS 要求 /sockjs/<server>/<session>，各段随机。"""
        tp = self._tp()
        prefix, server, session = tp.base_url.split("/")[-3:]
        self.assertEqual(prefix, "sockjs")
        self.assertTrue(server.isdigit() and len(server) == 3)
        self.assertEqual(len(session), 8)

    def test_send_frames_as_json_array(self):
        tp = self._tp()
        frame = json.dumps({"msg": "connect"})
        with mock.patch.object(realtime.requests, "post",
                               return_value=_Resp(204)) as post:
            tp.send(frame)
        url = post.call_args[0][0]
        self.assertIn("/xhr_send", url)
        self.assertEqual(json.loads(post.call_args[1]["data"]), [frame])
        self.assertFalse(post.call_args[1]["verify"])

    def test_send_failure_raises_for_reconnect(self):
        tp = self._tp()
        with mock.patch.object(realtime.requests, "post",
                               return_value=_Resp(404)):
            with self.assertRaises(ConnectionError):
                tp.send("{}")

    def test_send_many_chunks_frames_into_few_posts(self):
        """一次 xhr_send 可带多帧：96 个房间订阅必须是几次 POST，不是 96 次。"""
        tp = self._tp()
        frames = [json.dumps({"i": i}) for i in range(realtime._SEND_CHUNK * 2 + 3)]
        posts = []
        urls = []

        def fake_post(url, **kw):
            urls.append(url)
            posts.append(json.loads(kw["data"]))
            return _Resp(204)

        with mock.patch.object(realtime.requests, "post", side_effect=fake_post):
            tp.send_many(frames)
        self.assertEqual([len(p) for p in posts],
                         [realtime._SEND_CHUNK, realtime._SEND_CHUNK, 3])
        self.assertEqual([json.loads(f) for p in posts for f in p],
                         [{"i": i} for i in range(len(frames))])
        self.assertEqual(len(urls), 3)
        self.assertTrue(all("/xhr_send" in u for u in urls))

    def test_run_returns_when_send_channel_broken(self):
        """发送通道失效后必须马上退出轮询：只收不发的会话是僵尸连接。"""
        tp = self._tp()
        tp.broken = True
        with mock.patch.object(realtime.requests, "post") as post:
            tp.run(lambda send: None, lambda raw: self.fail("no frame expected"),
                   threading.Event())
        post.assert_not_called()

    def test_run_dispatches_frames_then_exits(self):
        tp = self._tp()
        stop = threading.Event()
        got = []
        body = 'a["{\\"msg\\":\\"connected\\"}"]\nh\n'

        def fake_post(url, **kw):
            if "/xhr_send" in url:
                return _Resp(204)
            stop.set()          # 收到一轮即止
            return _Resp(200, body)

        with mock.patch.object(realtime.requests, "post",
                               side_effect=fake_post) as post:
            tp.run(lambda send: send("ping-frame"), lambda raw: got.append(raw),
                   stop)
        self.assertEqual(got, ['{"msg":"connected"}'])
        self.assertIn("/xhr_send", post.call_args_list[0][0][0])

    def test_run_returns_when_session_closes(self):
        tp = self._tp()
        stop = threading.Event()
        with mock.patch.object(realtime.requests, "post",
                               side_effect=[_Resp(204), _Resp(200, 'c[1001,"x"]')]):
            tp.run(lambda send: None, lambda raw: self.fail("no frame expected"),
                   stop)

    def test_run_returns_on_request_error(self):
        tp = self._tp()
        stop = threading.Event()
        with mock.patch.object(realtime.requests, "post",
                               side_effect=[_Resp(204),
                                            realtime.requests.RequestException()]):
            tp.run(lambda send: None, lambda raw: None, stop)


class TestClientSendPath(unittest.TestCase):
    """重构后 _send_raw / _subscribe_room 认的是 transport，不再认 WebSocketApp。"""

    def _client(self):
        c = DDPClient("https://chat.example.com", "uid1", "tok1")
        self.addCleanup(_stop_client, c)
        return c

    def test_connected_frame_drives_login_and_resubscribe(self):
        c = self._client()
        tp = _FakeTransport()
        c._transport = tp
        c.subscribe_room("GENERAL")           # 未连接：只登记，不发帧
        self.assertEqual(tp.sent, [])

        c._handle_frame(json.dumps({"msg": "connected", "session": "S1"}))
        _delivered(c, tp, 4)                  # login + 1 房间 + 2 notify
        self.assertEqual(tp.sent[0]["msg"], "method")
        self.assertEqual(tp.sent[0]["method"], "login")
        self.assertEqual(tp.sent[0]["params"][0]["resume"], "tok1")
        # 登录后第一批 sub：房间在前，stream-notify-user 两个事件殿后
        room_subs = [f for f in tp.sent if f.get("name") == "stream-room-messages"]
        notify_subs = [f for f in tp.sent if f.get("name") == "stream-notify-user"]
        self.assertEqual(len(room_subs), 1)
        self.assertEqual(room_subs[0]["params"][0], ["GENERAL"])
        self.assertEqual({f["params"][0][0] for f in notify_subs},
                         {"uid1/user-status", "uid1/subscriptions-changed"})

    def test_login_and_all_resubscribes_go_out_in_one_batch(self):
        """重订阅几十个房间必须合并成一次批量发送。

        长轮询下每帧都是一次阻塞 POST（实测约 1.2 秒），逐帧发会把接收循环
        占到上百秒；期间没有 xhr 轮询，服务端约 60 秒就作废会话，剩下的帧
        全部 404 —— 这正是"只收不发"僵尸连接的来源。
        """
        c = self._client()
        tp = _FakeTransport()
        c._transport = tp
        for i in range(60):
            c._subscribed_rooms.add(f"R{i}")
        c._handle_frame(json.dumps({"msg": "connected", "session": "S1"}))
        _delivered(c, tp, 63)
        self.assertEqual(tp.batches, [63])                    # login + 60 房间 + 2 notify
        self.assertEqual(len([f for f in tp.sent if f["msg"] == "sub"]), 62)

    def test_bulk_subscribe_uses_one_batch_for_unseen_rooms(self):
        c = self._client()
        tp = _FakeTransport()
        c._transport = tp
        c._ws_connected = True
        c.subscribe_room("OLD")
        _delivered(c, tp, 1)
        tp.batches.clear()
        tp.sent.clear()

        c.subscribe_rooms(["A", "B", "OLD", "B"])
        _delivered(c, tp, 2)
        self.assertEqual(tp.batches, [2])
        self.assertEqual(sorted(f["params"][0] for f in tp.sent
                                if f["msg"] == "sub"), [["A"], ["B"]])
        c.subscribe_rooms(["A", "B"])           # 全部已登记：不该再发
        _settled(c)
        self.assertEqual(tp.batches, [2])

    def test_send_failure_latches_transport_as_dead(self):
        """xhr_send 404 之后继续逐帧发只是刷日志：要立刻判死并等重连。"""
        c = self._client()
        tp = _RaisingTransport()
        c._transport = tp
        c._ws_connected = True
        c._handle_frame(json.dumps({"msg": "ping", "id": "p1"}))
        # 判死是发送线程做的，断言前要等它把这一批处理完
        self.assertTrue(_wait_until(lambda: tp.attempts == [1] and tp.broken
                                    and not c._ws_connected))

        c._send_raw({"msg": "pong"})          # 判死后的帧直接丢弃
        c.subscribe_room("GENERAL")           # 只登记，等重连后批量重发
        _settled(c)
        self.assertEqual(tp.attempts, [1])

    def test_ping_answered_on_live_transport(self):
        c = self._client()
        tp = _FakeTransport()
        c._transport = tp
        c._ws_connected = True
        c._handle_frame(json.dumps({"msg": "ping", "id": "p1"}))
        _delivered(c, tp, 1)
        self.assertEqual(tp.sent, [{"msg": "pong"}])

    def test_frames_ignored_before_connection_is_up(self):
        c = self._client()
        tp = _FakeTransport()
        c._transport = tp
        c._handle_frame(json.dumps({"msg": "ping"}))
        _settled(c)                # 入队的帧由发送线程判定"未连接"后丢弃
        self.assertEqual(tp.sent, [])

    def test_stream_message_reaches_callback(self):
        got = []
        c = DDPClient("https://chat.example.com", "uid1", "tok1",
                      on_message=got.append)
        c._handle_frame(json.dumps({
            "msg": "changed", "collection": "stream-room-messages",
            "id": "GENERAL",
            "fields": {"args": [{
                "_id": "A1", "msg": "hello", "rid": "GENERAL",
                "u": {"_id": "other", "username": "other", "name": "Other"},
                "ts": "2026-09-18T02:00:00.000Z",
            }]},
        }))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].text, "hello")
        self.assertEqual(got[0].room_id, "GENERAL")


class TestRoomSubscribeDialect(unittest.TestCase):
    """服务端方言：新版收 rid 数组，旧版只认单个 rid（本项目实测为后者）。"""

    def _client(self):
        c = DDPClient("https://chat.example.com", "uid1", "tok1")
        self.addCleanup(_stop_client, c)
        tp = _FakeTransport()
        c._transport = tp
        c._ws_connected = True
        return c, tp

    def _nosub(self, sub_id, error="not-allowed"):
        return json.dumps({"msg": "nosub", "id": sub_id,
                           "error": {"error": error}})

    def test_array_shape_by_default(self):
        c, tp = self._client()
        c.subscribe_room("GENERAL")
        _delivered(c, tp, 1)
        self.assertEqual(tp.sent[0]["params"], [["GENERAL"], False])

    def test_not_allowed_falls_back_to_single_rid(self):
        c, tp = self._client()
        c.subscribe_room("GENERAL")
        c.subscribe_room("RANDOM")
        _delivered(c, tp, 2)
        c._handle_frame(self._nosub(tp.sent[0]["id"]))
        self.assertEqual(c._rid_style, "single")
        _delivered(c, tp, 6)            # 2 首订 + 换方言重订（2 房间 + 2 notify）
        subs = [f for f in tp.sent
                if f["msg"] == "sub" and f.get("name") == "stream-room-messages"]
        pairs = {(p[0][0] if isinstance(p[0], list) else p[0], p[1])
                 for p in (s["params"] for s in subs[-2:])}
        self.assertEqual(pairs, {("GENERAL", False), ("RANDOM", False)})

    def test_switch_happens_only_once(self):
        """换方言后仍被拒说明是真没权限，不能再反复重订。"""
        c, tp = self._client()
        c.subscribe_room("GENERAL")
        _delivered(c, tp, 1)
        c._handle_frame(self._nosub(tp.sent[0]["id"]))
        _delivered(c, tp, 4)            # 1 首订 + 1 房间 + 2 notify
        after = len(tp.sent)
        c._handle_frame(self._nosub(tp.sent[-1]["id"]))
        _settled(c)
        self.assertEqual(len(tp.sent), after)
        self.assertEqual(c._rid_style, "single")

    def test_latched_dialect_reused_for_later_rooms(self):
        c, tp = self._client()
        c.subscribe_room("GENERAL")
        _delivered(c, tp, 1)
        c._handle_frame(self._nosub(tp.sent[0]["id"]))
        _delivered(c, tp, 4)
        tp.sent.clear()
        c.subscribe_room("OTHER")
        _delivered(c, tp, 1)
        self.assertEqual(tp.sent[0]["params"], ["OTHER", False])

    def test_meteor_match_error_also_switches_dialect(self):
        """新版服务端拿到单个 rid 时抛的是 Match error，同样该换方言。"""
        c, tp = self._client()
        c._rid_style = "array"
        c.subscribe_room("GENERAL")
        _delivered(c, tp, 1)
        c._handle_frame(self._nosub(tp.sent[0]["id"],
                                    "Match error: Expected an array, got a string"))
        self.assertEqual(c._rid_style, "single")
        _delivered(c, tp, 4)
        self.assertEqual([f for f in tp.sent if f["msg"] == "sub"
                          and f.get("name") == "stream-room-messages"][-1]["params"],
                         ["GENERAL", False])

    def test_unrelated_rejection_keeps_dialect(self):
        c, tp = self._client()
        c.subscribe_room("GENERAL")
        _delivered(c, tp, 1)
        c._handle_frame(self._nosub(tp.sent[0]["id"], "not-authorized"))
        _settled(c)
        self.assertEqual(c._rid_style, "array")
        self.assertEqual(len([f for f in tp.sent if f["msg"] == "sub"]), 1)

    def test_room_with_outstanding_sub_is_not_subscribed_twice(self):
        c, tp = self._client()
        c._subscribe_room("GENERAL")
        c._subscribe_room("GENERAL")
        _delivered(c, tp, 1)
        self.assertEqual([f["msg"] for f in tp.sent], ["sub"])

    def test_subscribe_during_handshake_finishes_with_one_sub(self):
        """主线程订阅与 connected 重订阅交错时，也只能留一个订阅。"""
        c, tp = self._client()
        c._ws_connected = False
        c.subscribe_room("GENERAL")           # 未连接：只登记
        c._handle_frame(json.dumps({"msg": "connected", "session": "S1"}))
        _delivered(c, tp, 4)
        c._ws_connected = True
        c._subscribe_room("GENERAL")          # 交错发出的第二路订阅
        _settled(c)
        rooms = [f["params"][0] for f in tp.sent
                 if f["msg"] == "sub"
                 and f.get("name") == "stream-room-messages"]
        self.assertEqual(rooms, [["GENERAL"]])

    def test_reconnect_resubscribes_with_fresh_ids(self):
        """旧连接的 sub id 随断线作废，重连后必须重新订阅一次。"""
        c, tp = self._client()
        c.subscribe_room("GENERAL")
        _delivered(c, tp, 1)
        c._ws_connected = False
        c._handle_frame(json.dumps({"msg": "connected", "session": "S2"}))
        _delivered(c, tp, 5)            # 首订 1 + connected 重发 4
        rooms = [f["params"][0] for f in tp.sent
                 if f["msg"] == "sub"
                 and f.get("name") == "stream-room-messages"]
        self.assertEqual(rooms, [["GENERAL"], ["GENERAL"]])
        self.assertEqual(len(c._sub_by_id), 1)


class TestRealtimeFrameShape(unittest.TestCase):
    """真机抓到的 changed 帧：frame.id 是占位值，时间是 Mongo EJSON。"""

    RID = "6aacaa92526404caf2577ec4"
    FRAME = json.dumps({
        "msg": "changed", "collection": "stream-room-messages", "id": "id",
        "fields": {
            "eventName": "6aacaa92526404caf2577ec4",
            "args": [{
                "_id": "gp6Z99LJkj9of3Tc6",
                "msg": "hello",
                "rid": "6aacaa92526404caf2577ec4",
                "u": {"_id": "uid1", "username": "alice", "name": "Alice"},
                "ts": {"$date": 1789700762842},
                "attachments": [],
            }],
        },
    })

    def _receive(self):
        got = []
        c = DDPClient("https://chat.example.com", "uid1", "tok1",
                      on_message=got.append)
        c._handle_frame(self.FRAME)
        return got

    def test_room_id_comes_from_payload_not_frame_id(self):
        msg = self._receive()[0]
        self.assertEqual(msg.room_id, self.RID)
        self.assertEqual(msg.text, "hello")
        self.assertTrue(msg.is_mine)

    def test_ejson_timestamp_parsed_to_real_time(self):
        msg = self._receive()[0]
        self.assertAlmostEqual(msg.timestamp, 1789700762.842, places=3)

    def test_server_replay_delivered_once(self):
        """实测同一条消息会被重复推两次，气泡和未读数都不能翻倍。"""
        got = []
        c = DDPClient("https://chat.example.com", "uid1", "tok1",
                      on_message=got.append)
        c._handle_frame(self.FRAME)
        c._handle_frame(self.FRAME)
        self.assertEqual(len(got), 1)

    def test_edit_is_not_mistaken_for_replay(self):
        got = []
        c = DDPClient("https://chat.example.com", "uid1", "tok1",
                      on_message=got.append)
        c._handle_frame(self.FRAME)
        edited = json.loads(self.FRAME)
        edited["fields"]["args"][0]["_updatedAt"] = {"$date": 1789700800000}
        c._handle_frame(json.dumps(edited))
        self.assertEqual(len(got), 2)

    def test_replay_cache_stays_bounded(self):
        c = DDPClient("https://chat.example.com", "uid1", "tok1")
        for i in range(realtime._SEEN_LIMIT + 64):
            c._is_replay({"_id": str(i)})
        self.assertEqual(len(c._seen_msgs), realtime._SEEN_LIMIT)


class TestNotifyUserSubscribe(unittest.TestCase):
    """stream-notify-user：user-status / subscriptions-changed 订阅与解析。"""

    def _client(self, **kw):
        c = DDPClient("https://chat.example.com", "uid1", "tok1", **kw)
        self.addCleanup(_stop_client, c)
        tp = _FakeTransport()
        c._transport = tp
        c._ws_connected = True
        return c, tp

    def _changed(self, event, args):
        return json.dumps({
            "msg": "changed", "collection": "stream-notify-user",
            "id": "id",
            "fields": {"eventName": f"uid1/{event}", "args": args},
        })

    def test_connected_subscribes_notify_events_in_array_dialect(self):
        c, tp = self._client()
        c._handle_frame(json.dumps({"msg": "connected", "session": "S1"}))
        _delivered(c, tp, 3)                      # login + 2 个 notify
        notify = [f for f in tp.sent
                  if f.get("name") == "stream-notify-user"]
        self.assertEqual(len(notify), 2)
        self.assertEqual(notify[0]["params"], [["uid1/user-status"], False])
        self.assertEqual(notify[1]["params"],
                         [["uid1/subscriptions-changed"], False])

    def test_dialect_switch_resends_notify_in_single_style(self):
        c, tp = self._client()
        c.subscribe_room("GENERAL")
        _delivered(c, tp, 1)
        c._handle_frame(json.dumps({
            "msg": "nosub", "id": tp.sent[0]["id"],
            "error": {"error": "not-allowed"}}))
        _delivered(c, tp, 4)                      # 换方言后整批重发
        notify = [f for f in tp.sent
                  if f.get("name") == "stream-notify-user"
                  and f["params"][0] == "uid1/user-status"]
        self.assertEqual(len(notify), 1)
        self.assertEqual(notify[0]["params"], ["uid1/user-status", False])

    def test_user_status_dict_payload_reaches_callback(self):
        got = []
        c, _ = self._client(on_user_status=lambda u, s: got.append((u, s)))
        c._handle_frame(self._changed(
            "user-status",
            [{"_id": "u9", "username": "alice", "status": "busy"}]))
        self.assertEqual(got, [("alice", "busy")])

    def test_user_status_list_payload_reaches_callback(self):
        """老版本把 args 给成 [username, status] 双元素列表，同样要吃下。"""
        got = []
        c, _ = self._client(on_user_status=lambda u, s: got.append((u, s)))
        c._handle_frame(self._changed("user-status", ["alice", "away"]))
        self.assertEqual(got, [("alice", "away")])

    def test_user_status_unresolvable_payload_ignored(self):
        got = []
        c, _ = self._client(on_user_status=lambda u, s: got.append((u, s)))
        c._handle_frame(self._changed("user-status", [{"status": "online"}]))
        c._handle_frame(self._changed("user-status", []))
        self.assertEqual(got, [])

    def test_subscriptions_changed_reaches_callback(self):
        got = []
        c, _ = self._client(on_subscription=lambda a, s: got.append((a, s)))
        sub = {"rid": "R1", "unread": 3, "t": "p"}
        c._handle_frame(self._changed("subscriptions-changed",
                                      ["updated", sub]))
        self.assertEqual(got, [("updated", sub)])

    def test_subscriptions_changed_malformed_ignored(self):
        got = []
        c, _ = self._client(on_subscription=lambda a, s: got.append((a, s)))
        c._handle_frame(self._changed("subscriptions-changed", ["updated"]))
        c._handle_frame(self._changed("subscriptions-changed",
                                      ["updated", "not-a-dict"]))
        self.assertEqual(got, [])

    def test_callback_exception_does_not_kill_loop(self):
        def boom(u, s):
            raise RuntimeError("x")
        c, _ = self._client(on_user_status=boom)
        c._handle_frame(self._changed("user-status", ["alice", "online"]))


class TestTimestampParsing(unittest.TestCase):
    def test_ejson_milliseconds_become_seconds(self):
        self.assertAlmostEqual(ts_to_epoch({"$date": 1789700762842}),
                               1789700762.842, places=3)

    def test_iso_and_ejson_agree_for_the_same_instant(self):
        epoch = 1789700762
        iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch))
        self.assertAlmostEqual(ts_to_epoch(iso), float(epoch), places=3)
        self.assertAlmostEqual(ts_to_epoch({"$date": epoch * 1000}),
                               float(epoch), places=3)

    def test_seconds_pass_through_unchanged(self):
        self.assertEqual(ts_to_epoch(1789700762), 1789700762.0)


if __name__ == "__main__":
    unittest.main()
