"""音频消息内联播放：卡片绘制/按钮命中 + 播放状态机接线。

offscreen；QMediaPlayer 用 mock（不真解码音频）。
运行：python -m unittest tests.test_audio_playback -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QRect  # noqa: E402
from PyQt5.QtGui import QFontMetrics, QPainter, QPixmap  # noqa: E402
from PyQt5.QtWidgets import QApplication, QStyleOptionViewItem  # noqa: E402

from ui import message_model as mm  # noqa: E402
from ui.chat_view import ChatView, file_cache_dest  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


class _FakeStates:
    PlayingState = 2
    PausedState = 1
    StoppedState = 0
    InvalidMedia = 99


def _fake_player_cls():
    cls = mock.MagicMock(name="QMediaPlayer")
    cls.PlayingState = _FakeStates.PlayingState
    cls.PausedState = _FakeStates.PausedState
    cls.StoppedState = _FakeStates.StoppedState
    cls.InvalidMedia = _FakeStates.InvalidMedia
    inst = cls.return_value
    inst.state.return_value = _FakeStates.StoppedState
    inst.position.return_value = 0
    inst.duration.return_value = 0
    return cls


def _mk_player():
    """返回 (类, 实例)，实例方法均为可断言 mock。"""
    cls = _fake_player_cls()
    return cls, cls.return_value


class DelegateAudioTest(unittest.TestCase):
    """音频卡按钮命中矩形与绘制冒烟。"""

    def setUp(self):
        self.model = mm.MessageListModel()
        self.delegate = mm.MessageDelegate(self.model)
        self.model.set_fonts(self.model._font)

    def _add_msg(self, **kw):
        from core.models import Message, User
        sender = User(id="u1", username="a", name="A", avatar_url="")
        msg = Message(id=kw.pop("id", "m1"), room_id="r", sender=sender,
                      timestamp=1700000000.0, **kw)
        self.model.append_message(msg)
        return msg

    def _opt(self, msg_id):
        idx = self.model.index(self.model.find_message_row(msg_id), 0)
        opt = QStyleOptionViewItem()
        opt.rect = QRect(0, 0, 800, self.model._get_height(idx.row()))
        return idx, opt

    def test_audio_btn_rect_non_null_and_file_null(self):
        self._add_msg(msg_type="audio", file_name="voice.mp3", text="")
        idx, opt = self._opt("m1")
        r = self.delegate.audio_btn_rect(idx, opt.rect)
        self.assertFalse(r.isNull())
        self.assertEqual(r.width(), mm._FILE_BADGE)
        self._add_msg(msg_type="file", file_name="a.pdf", text="", id="m2")
        idx2, opt2 = self._opt("m2")
        self.assertTrue(self.delegate.audio_btn_rect(idx2, opt2.rect).isNull())

    def test_paint_audio_smoke_all_states(self):
        msg = self._add_msg(msg_type="audio", file_name="voice.mp3", text="")
        h = mm._file_card_h(self.model._font, self.model._small_font)
        pm = QPixmap(400, 120)
        pm.fill()
        painter = QPainter(pm)
        rect = QRect(8, 8, mm._FILE_CARD_W, h)
        # 未播放（无时长）
        self.delegate._paint_audio_content(painter, rect, msg)
        # 播放中带进度
        self.delegate.set_audio_state(msg.id, True, 3000, 62000)
        self.delegate._paint_audio_content(painter, rect, msg)
        # 暂停复位（保留时长）
        self.delegate.clear_audio_state(msg.id, 62000)
        self.delegate._paint_audio_content(painter, rect, msg)
        painter.end()

    def test_fmt_mmss(self):
        self.assertEqual(mm._fmt_mmss(0), "0:00")
        self.assertEqual(mm._fmt_mmss(62000), "1:02")
        self.assertEqual(mm._fmt_mmss(-5), "0:00")


class ChatViewAudioTest(unittest.TestCase):
    """播放按钮 → 下载 → 播放/暂停/切换/回退 的接线。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        p1 = mock.patch("core.paths.files_dir",
                        return_value=self._tmp.name)
        p1.start()
        self.addCleanup(p1.stop)
        self.view = ChatView()
        self.addCleanup(self.view.deleteLater)
        self.view.set_room("r")
        from core.models import Message, User
        sender = User(id="u1", username="a", name="A", avatar_url="")

        def mk(mid):
            m = Message(id=mid, room_id="r", sender=sender,
                        timestamp=1700000000.0, msg_type="audio",
                        file_name=f"{mid}.mp3",
                        file_url=f"http://srv/files/{mid}.mp3")
            self.view.model.prepend_messages_immediate([m])
            return m

        self.msg1 = mk("a1")
        self.msg2 = mk("a2")

    def _cached(self, msg):
        """预置磁盘缓存命中：_ensure_cached_file 会同步回调 on_ready。"""
        dest = file_cache_dest(msg.file_url, msg.file_name)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(b"fakeaudio")
        return dest

    def test_play_creates_player_and_plays(self):
        self._cached(self.msg1)
        cls, inst = _mk_player()
        with mock.patch("ui.chat_view._AUDIO_PLAYBACK_OK", True), \
                mock.patch("ui.chat_view.QMediaPlayer", cls), \
                mock.patch("ui.chat_view.QMediaContent"):
            self.view._toggle_audio_play(self.msg1)
            self.assertIs(self.view._audio_player, inst)
            inst.setMedia.assert_called_once()
            inst.play.assert_called_once()
            self.assertTrue(self.view._audio_timer.isActive())

    def test_second_click_pauses(self):
        self._cached(self.msg1)
        cls, inst = _mk_player()
        inst.state.return_value = _FakeStates.PlayingState
        with mock.patch("ui.chat_view._AUDIO_PLAYBACK_OK", True), \
                mock.patch("ui.chat_view.QMediaPlayer", cls), \
                mock.patch("ui.chat_view.QMediaContent"):
            self.view._toggle_audio_play(self.msg1)
            self.view._toggle_audio_play(self.msg1)
            inst.pause.assert_called_once()

    def test_switch_stops_previous(self):
        self._cached(self.msg1)
        self._cached(self.msg2)
        cls, inst = _mk_player()
        with mock.patch("ui.chat_view._AUDIO_PLAYBACK_OK", True), \
                mock.patch("ui.chat_view.QMediaPlayer", cls), \
                mock.patch("ui.chat_view.QMediaContent"):
            self.view._toggle_audio_play(self.msg1)
            self.view._toggle_audio_play(self.msg2)
            inst.stop.assert_called_once()
            # 换曲后计时器继续为 a2 服务，a1 状态已复位
            self.assertEqual(self.view._audio_msg_id, "a2")
            self.assertTrue(self.view._audio_timer.isActive())
            playing, pos, dur = self.view._delegate._audio_states["a1"]
            self.assertFalse(playing)
            self.assertEqual(pos, 0)

    def test_invalid_media_falls_back_to_system_player(self):
        self._cached(self.msg1)
        cls, inst = _mk_player()
        with mock.patch("ui.chat_view._AUDIO_PLAYBACK_OK", True), \
                mock.patch("ui.chat_view.QMediaPlayer", cls), \
                mock.patch("ui.chat_view.QMediaContent"):
            self.view._toggle_audio_play(self.msg1)
            opened = []
            with mock.patch.object(self.view, "open_room_file",
                                   side_effect=lambda u, n: opened.append(u)):
                self.view._on_audio_status(_FakeStates.InvalidMedia)
            self.assertEqual(opened, [self.msg1.file_url])
            self.assertIsNone(self.view._audio_player)

    def test_playback_unavailable_opens_system_player(self):
        opened = []
        with mock.patch("ui.chat_view._AUDIO_PLAYBACK_OK", False), \
                mock.patch.object(self.view, "open_room_file",
                                  side_effect=lambda u, n: opened.append(u)):
            self.view._toggle_audio_play(self.msg1)
        self.assertEqual(opened, [self.msg1.file_url])
        self.assertIsNone(self.view._audio_player)

    def test_tick_updates_delegate_state_and_row(self):
        self._cached(self.msg1)
        cls, inst = _mk_player()
        inst.state.return_value = _FakeStates.PlayingState
        inst.position.return_value = 1500
        inst.duration.return_value = 62000
        rows = []
        self.view.model.dataChanged.connect(
            lambda tl, br, *a: rows.append(tl.row()))
        with mock.patch("ui.chat_view._AUDIO_PLAYBACK_OK", True), \
                mock.patch("ui.chat_view.QMediaPlayer", cls), \
                mock.patch("ui.chat_view.QMediaContent"):
            self.view._toggle_audio_play(self.msg1)
            self.view._on_audio_duration(62000)   # mock 不发信号，手动喂
            self.view._on_audio_tick()
        row = self.view.model.find_message_row("a1")
        self.assertEqual(rows, [row, row, row])  # 起播+时长+tick 各刷一次
        self.assertEqual(
            self.view._delegate._audio_states["a1"], (True, 1500, 62000))
        self.assertEqual(self.view._audio_dur.get("a1"), 62000)


if __name__ == "__main__":
    unittest.main()
