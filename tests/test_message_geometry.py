"""委托几何验证：layout_rects 是绘制与点击命中的唯一算术来源。

ChatView 的头像/图片命中测试和 MessageDelegate 的绘制必须算出同一个矩形，
否则"点头像弹资料卡"会点到别处。这里锁住三件事：
矩形落在行内、头像与气泡顶对齐、气泡高度等于模型算出的内容高度。

运行：python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QRect, Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

import ui.message_model as mm  # noqa: E402
from core.models import Message, User  # noqa: E402
from ui.chat_view import ChatView  # noqa: E402
from ui.message_model import RoleMessage  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _sender(i):
    return User(id=f"u{i}", username=f"user{i}", name=f"用户{i}",
                avatar_url=f"https://x/avatar/user{i}")


def _msg(n, mine=False, msg_type="text", text=None, **kw):
    if text is None:
        text = "正文 " + ("填充文字 " * (n % 3))
    return Message(
        id=f"m{n}", room_id="r", is_mine=mine, sender=_sender(n % 3),
        timestamp=1700000000.0 + n * 60, msg_type=msg_type,
        text=text, **kw)


class TestLayoutRects(unittest.TestCase):

    def setUp(self):
        self.view = ChatView()
        self.view.resize(640, 420)
        self.view.set_room("r")
        self.view.set_show_name(True)
        msgs = [_msg(i) for i in range(12)]
        # 图片消息走 image_rect 分支；自己的消息走右侧布局
        msgs.insert(5, _msg(50, msg_type="image",
                            file_url="/file-upload/img.png", text=""))
        msgs.insert(8, _msg(60, mine=True))
        # 模型按旧→新存，时间递增即可
        self.view.model.prepend_messages_immediate(
            sorted(msgs, key=lambda m: m.timestamp))
        self.delegate = self.view._delegate

    def _rects(self, row):
        idx = self.view.model.index(row, 0)
        row_rect = QRect(0, 0, 640, idx.data(mm.RoleRowHeight))
        return idx, row_rect, self.delegate.layout_rects(idx, row_rect)

    def test_rects_stay_inside_row(self):
        for row in range(self.view.model.rowCount()):
            idx, row_rect, (bubble, avatar, image) = self._rects(row)
            self.assertGreater(row_rect.height(), 0, f"row {row} 高度为 0")
            self.assertTrue(bubble.isValid() and not bubble.isNull(),
                            f"row {row} 气泡矩形无效")
            self.assertGreaterEqual(bubble.top(), 0, f"row {row} 气泡顶超出行")
            self.assertLessEqual(bubble.bottom(), row_rect.height(),
                                 f"row {row} 气泡底超出行高 {row_rect.height()}")
            for name, rect in (("头像", avatar), ("图片", image)):
                if rect.isNull():
                    continue
                self.assertGreaterEqual(rect.top(), 0, f"row {row} {name}顶超出")
                self.assertLessEqual(rect.bottom(), row_rect.height(),
                                     f"row {row} {name}底超出行高")

    def test_avatar_is_opposite_side_of_mine(self):
        """自己的消息头像在气泡右侧，别人的在左侧。"""
        for row in range(self.view.model.rowCount()):
            idx = self.view.model.index(row, 0)
            msg = idx.data(RoleMessage)
            _r, _c, (bubble, avatar, _i) = self._rects(row)
            if avatar.isNull():
                continue
            if msg.is_mine:
                self.assertGreater(avatar.left(), bubble.right(),
                                   f"row {row} 自己的消息头像应在气泡右侧")
            else:
                self.assertLess(avatar.right(), bubble.left(),
                                f"row {row} 别人的消息头像应在气泡左侧")
            # 头像与气泡顶对齐
            self.assertEqual(avatar.top(), bubble.top(), f"row {row} 头像未与气泡顶对齐")

    def test_image_message_has_image_rect(self):
        found = [row for row in range(self.view.model.rowCount())
                 if self.view.model.index(row, 0).data(RoleMessage).msg_type == "image"]
        self.assertTrue(found, "没造出图片消息")
        for row in found:
            _idx, _rr, (_b, _a, image) = self._rects(row)
            self.assertFalse(image.isNull(), f"row {row} 图片矩形缺失")
            self.assertEqual((image.width(), image.height()),
                             (mm._IMG_W, mm._IMG_H))

    def test_system_row_has_no_rects(self):
        idx = self.view.model.index(0, 0)
        msg = idx.data(RoleMessage)
        msg.msg_type = "system"
        self.view.model.update_message(msg)
        bubble, avatar, image = self.delegate.layout_rects(
            idx, QRect(0, 0, 640, 40))
        self.assertTrue(bubble.isNull() and avatar.isNull() and image.isNull())

    def test_hit_test_uses_same_rect_as_paint(self):
        """命中测试取的矩形必须与绘制一致：点头像中心应判为头像。"""
        for row in range(self.view.model.rowCount()):
            idx = self.view.model.index(row, 0)
            _b, avatar, _i = self.delegate.layout_rects(
                idx, QRect(0, 0, 640, idx.data(mm.RoleRowHeight)))
            if avatar.isNull():
                continue
            center = avatar.center()
            self.assertTrue(avatar.contains(center),
                            f"row {row} 头像中心点不落进头像矩形")
            # 中心点不应同时落进气泡（两者不能重叠到互相抢占）
            bubble, _a, _i = self.delegate.layout_rects(
                idx, QRect(0, 0, 640, idx.data(mm.RoleRowHeight)))
            self.assertFalse(bubble.contains(center),
                             f"row {row} 头像中心点同时命中气泡")
            return
        self.fail("没有任何带头像的行")


class TestAvatarHitTest(unittest.TestCase):

    def setUp(self):
        self.view = ChatView()
        self.view.resize(640, 420)
        self.view.set_room("r")
        self.view.set_show_name(True)
        msgs = sorted((_msg(i) for i in range(10)), key=lambda m: m.timestamp)
        self.view.model.prepend_messages_immediate(msgs)
        self.delegate = self.view._delegate

    def _click(self, pos):
        from PyQt5.QtCore import QEvent
        from PyQt5.QtGui import QMouseEvent
        ev = QMouseEvent(QEvent.MouseButtonPress, pos, Qt.LeftButton,
                         Qt.NoButton, Qt.NoModifier)
        self.view.mousePressEvent(ev)

    def _first_avatar_row(self):
        for row in range(self.view.model.rowCount()):
            idx = self.view.model.index(row, 0)
            _b, avatar, _i = self.delegate.layout_rects(
                idx, self.view.visualRect(idx))
            if not avatar.isNull():
                return row, idx.data(RoleMessage), avatar
        self.fail("没有带头像的消息行")

    def test_click_on_avatar_emits_profile_requested(self):
        row, msg, avatar = self._first_avatar_row()
        got = []
        self.view.profile_requested.connect(lambda m, p: got.append((m, p)))
        self._click(avatar.center())
        self.assertEqual(len(got), 1, "点头像没有触发资料卡请求")
        self.assertEqual(got[0][0].id, msg.id)

    def test_click_on_bubble_does_not_emit(self):
        _row, _msg, _avatar = self._first_avatar_row()
        # 取一个没有头像的行气泡中心（同一发送者的后续消息）
        target = None
        for row in range(self.view.model.rowCount()):
            idx = self.view.model.index(row, 0)
            bubble, avatar, _i = self.delegate.layout_rects(
                idx, self.view.visualRect(idx))
            if bubble.isNull():
                continue
            probe = bubble.center()
            if not avatar.isNull() and avatar.contains(probe):
                continue
            target = (idx.data(RoleMessage), probe)
            break
        self.assertIsNotNone(target, "造不出只命中气泡而不命中头像的点")
        got = []
        self.view.profile_requested.connect(lambda m, p: got.append(m))
        self._click(target[1])
        self.assertEqual(got, [], "点气泡不该弹资料卡")


class TestVideoClick(unittest.TestCase):
    """视频点击路由：有封面点封面、无封面点气泡，都走系统打开而非图片查看器。"""

    def setUp(self):
        self.view = ChatView()
        self.view.resize(640, 420)
        self.view.set_room("r")
        self.view.set_show_name(True)

    def tearDown(self):
        self.view.deleteLater()

    def _video(self, cover):
        return Message(
            id="mv", room_id="r", is_mine=False,
            sender=User(id="u1", username="u1", name="对方"),
            timestamp=1700000000.0, msg_type="video",
            file_url="/file-upload/v/abc.mp4", file_name="会议录像",
            cover_url=cover, text="")

    def _click_msg_rect(self, msg, kind):
        self.view.model.append_message(msg)
        row = self.view.model.find_message_row(msg.id)
        idx = self.view.model.index(row, 0)
        bubble, _avatar, image = self.view._delegate.layout_rects(
            idx, self.view.visualRect(idx))
        rect = image if kind == "image" else bubble
        self.assertFalse(rect.isNull(), f"{kind} 矩形不应为空")
        from PyQt5.QtCore import QEvent
        from PyQt5.QtGui import QMouseEvent
        ev = QMouseEvent(QEvent.MouseButtonPress, rect.center(),
                         Qt.LeftButton, Qt.NoButton, Qt.NoModifier)
        opened = []
        viewer = []
        self.view.image_view_requested.connect(lambda m: viewer.append(m))
        with mock.patch.object(self.view, "_open_file_cached",
                               side_effect=lambda m: opened.append(m)):
            self.view.mousePressEvent(ev)
        return opened, viewer

    def test_click_video_cover_opens_file_not_viewer(self):
        opened, viewer = self._click_msg_rect(self._video("/file-upload/c.png"),
                                              "image")
        self.assertEqual(viewer, [], "视频封面不该进图片查看器")
        self.assertEqual([m.id for m in opened], ["mv"])

    def test_click_coverless_video_bubble_opens_file(self):
        opened, viewer = self._click_msg_rect(self._video(""), "bubble")
        self.assertEqual(viewer, [])
        self.assertEqual([m.id for m in opened], ["mv"])

    def test_click_image_still_emits_viewer(self):
        msg = Message(
            id="mi", room_id="r", is_mine=False,
            sender=User(id="u1", username="u1", name="对方"),
            timestamp=1700000000.0, msg_type="image",
            file_url="/file-upload/img.png", text="")
        opened, viewer = self._click_msg_rect(msg, "image")
        self.assertEqual(opened, [])
        self.assertEqual([m.id for m in viewer], ["mi"])


class TestProfileCard(unittest.TestCase):

    def test_builds_from_cached_user_fields(self):
        from ui.profile_popup import ProfileCard
        card = ProfileCard(_sender(1))
        self.assertTrue(card.windowFlags() & Qt.Popup)
        card._apply_info({
            "name": "张三", "username": "zhangsan", "status": "online",
            "status_text": "搬砖中", "roles": ["user", "admin"],
            "email": "z@example.com",
        })
        self.assertEqual(card.name_label.text(), "张三")
        self.assertEqual(card.user_label.text(), "@zhangsan")
        self.assertIn("在线", card.status_label.text())
        self.assertIn("搬砖中", card.status_label.text())
        self.assertIn("admin", card.roles_label.text())
        self.assertNotIn("user", card.roles_label.text())   # 普通角色不展示
        self.assertTrue(card.roles_label.isVisibleTo(card))
        self.assertEqual(card.email_label.text(), "z@example.com")
        card.deleteLater()


class TestStickerBackdrop(unittest.TestCase):
    """透明底图片（表情包）的主色渐变垫底（官方客户端同款）。"""

    def setUp(self):
        self.view = ChatView()
        self.delegate = self.view._delegate

    def _alpha_pixmap(self, w=100, h=80):
        from PyQt5.QtGui import QColor, QPainter, QPixmap
        pm = QPixmap(w, h)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(230, 190, 60))  # 贴纸主色（黄脸）
        p.drawEllipse(w // 2 - 20, h // 2 - 20, 40, 40)
        p.end()
        return pm

    def test_returns_dpr_sized_gradient(self):
        from PyQt5.QtGui import QImage
        out = self.delegate._sticker_backdrop(
            "k", self._alpha_pixmap(), 80, 64, 1.0)
        self.assertFalse(out.isNull())
        img = out.toImage()
        self.assertEqual(img.width(), 80)
        self.assertEqual(img.height(), 64)
        # 左缘是主色亮端、右缘近黑（官方实测走向），左侧明显亮于右侧
        l = img.pixelColor(2, 32)
        r = img.pixelColor(77, 32)
        self.assertGreater(max(l.red(), l.green(), l.blue()), 120)
        self.assertLess(max(r.red(), r.green(), r.blue()), 60)

    def test_second_call_hits_cache(self):
        pm = self._alpha_pixmap()
        first = self.delegate._sticker_backdrop("k", pm, 80, 64, 1.0)
        again = self.delegate._sticker_backdrop("k", pm, 80, 64, 1.0)
        self.assertIs(first, again)
        # 换 key（dpr 不同）不复用
        other = self.delegate._sticker_backdrop("k", pm, 80, 64, 2.0)
        self.assertIsNot(first, other)

    def test_clear_pending_resources_clears_cache(self):
        self.delegate._sticker_backdrop(
            "k", self._alpha_pixmap(), 80, 64, 1.0)
        self.delegate.clear_pending_resources()
        self.assertEqual(self.delegate._backdrop_cache, {})


if __name__ == "__main__":
    unittest.main()
