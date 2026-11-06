"""无边框卡片对话框拖动：标题带可拖动，内容区与右键不触发。

无边框窗口没有系统标题栏，基类自己处理标题带的按下/移动。
"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PyQt5.QtGui import QMouseEvent  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from ui.dialog_base import FramelessCardDialog  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _mouse(widget, type_, pos, button=Qt.LeftButton,
           buttons=Qt.NoButton):
    # 5 参构造不会更新 globalPos（拖动计算依赖它），必须显式给屏幕坐标
    ev = QMouseEvent(type_, QPointF(pos), QPointF(pos),
                     QPointF(widget.mapToGlobal(pos)),
                     button, buttons, Qt.NoModifier)
    QApplication.sendEvent(widget, ev)


class DialogDragTest(unittest.TestCase):

    def setUp(self):
        self.dlg = FramelessCardDialog("标题")
        self.dlg.resize(440, 240)
        self.dlg.show()
        QApplication.processEvents()

    def tearDown(self):
        self.dlg.close()
        self.dlg.deleteLater()

    def test_title_is_mouse_transparent(self):
        """标题必须让鼠标事件穿透到对话框，否则点标题无法起拖。"""
        self.assertTrue(
            self.dlg._title.testAttribute(Qt.WA_TransparentForMouseEvents))

    def test_drag_from_title_moves_dialog(self):
        start = self.dlg._title.geometry().center()
        before = self.dlg.pos()
        _mouse(self.dlg, QEvent.MouseButtonPress, start,
               buttons=Qt.LeftButton)
        _mouse(self.dlg, QEvent.MouseMove, start + QPoint(40, 30),
               buttons=Qt.LeftButton)
        self.assertEqual(self.dlg.pos(), before + QPoint(40, 30))
        _mouse(self.dlg, QEvent.MouseButtonRelease, start + QPoint(40, 30))
        # 松开后继续移动不再跟随
        _mouse(self.dlg, QEvent.MouseMove, start + QPoint(80, 60),
               buttons=Qt.LeftButton)
        self.assertEqual(self.dlg.pos(), before + QPoint(40, 30))

    def test_content_area_press_does_not_drag(self):
        start = QPoint(60, self.dlg._title.geometry().bottom() + 40)
        before = self.dlg.pos()
        _mouse(self.dlg, QEvent.MouseButtonPress, start,
               buttons=Qt.LeftButton)
        _mouse(self.dlg, QEvent.MouseMove, start + QPoint(40, 30),
               buttons=Qt.LeftButton)
        self.assertEqual(self.dlg.pos(), before)

    def test_right_button_does_not_drag(self):
        start = self.dlg._title.geometry().center()
        before = self.dlg.pos()
        _mouse(self.dlg, QEvent.MouseButtonPress, start,
               button=Qt.RightButton, buttons=Qt.RightButton)
        _mouse(self.dlg, QEvent.MouseMove, start + QPoint(40, 30),
               buttons=Qt.LeftButton)
        self.assertEqual(self.dlg.pos(), before)

    def test_edge_drag_does_not_resize_fixed_dialog(self):
        """非可伸缩弹窗（确认/新建类）不能被边沿拖动改尺寸。"""
        before = self.dlg.geometry()
        corner = QPoint(self.dlg.width() - 2, self.dlg.height() - 2)
        _mouse(self.dlg, QEvent.MouseButtonPress, corner,
               buttons=Qt.LeftButton)
        _mouse(self.dlg, QEvent.MouseMove, corner + QPoint(40, 30),
               buttons=Qt.LeftButton)
        self.assertEqual(self.dlg.geometry(), before)


class ResizableDialogTest(unittest.TestCase):
    """可伸缩弹窗：无系统边框，四条边/四角自己拖。"""

    def setUp(self):
        self.dlg = FramelessCardDialog("标题", resizable=True)
        self.dlg.resize(560, 420)
        self.dlg.show()
        QApplication.processEvents()

    def tearDown(self):
        self.dlg.close()
        self.dlg.deleteLater()

    def _press_move(self, start, delta):
        _mouse(self.dlg, QEvent.MouseButtonPress, start,
               buttons=Qt.LeftButton)
        _mouse(self.dlg, QEvent.MouseMove, start + delta,
               buttons=Qt.LeftButton)
        _mouse(self.dlg, QEvent.MouseButtonRelease, start + delta)

    def test_bottom_right_corner_drag_grows(self):
        before = self.dlg.geometry()
        corner = QPoint(self.dlg.width() - 2, self.dlg.height() - 2)
        self._press_move(corner, QPoint(40, 30))
        self.assertEqual(self.dlg.width(), before.width() + 40)
        self.assertEqual(self.dlg.height(), before.height() + 30)
        self.assertEqual(self.dlg.pos(), before.topLeft())

    def test_left_edge_drag_keeps_right_edge(self):
        before = self.dlg.geometry()
        point = QPoint(2, self.dlg.height() // 2)
        self._press_move(point, QPoint(-40, 0))
        self.assertEqual(self.dlg.width(), before.width() + 40)
        self.assertEqual(self.dlg.geometry().right(), before.right())
        self.assertEqual(self.dlg.height(), before.height())

    def test_top_edge_drag_wins_over_title_drag(self):
        """上边沿与标题拖动带重叠：应先按缩放处理，下缘钉住。"""
        before = self.dlg.geometry()
        point = QPoint(self.dlg.width() // 2, 2)
        self._press_move(point, QPoint(0, 30))
        self.assertEqual(self.dlg.height(), before.height() - 30)
        self.assertEqual(self.dlg.geometry().bottom(), before.bottom())
        self.assertEqual(self.dlg.y(), before.y() + 30)

    def test_shrink_is_clamped_to_minimum(self):
        corner = QPoint(self.dlg.width() - 2, self.dlg.height() - 2)
        self._press_move(corner, QPoint(-2000, -2000))
        self.assertEqual(self.dlg.width(), self.dlg.minimumWidth())
        self.assertEqual(self.dlg.height(), self.dlg.minimumHeight())

    def test_resize_cursor_follows_edges(self):
        _mouse(self.dlg, QEvent.MouseMove,
               QPoint(2, self.dlg.height() // 2), button=Qt.NoButton)
        self.assertEqual(self.dlg.cursor().shape(), Qt.SizeHorCursor)
        _mouse(self.dlg, QEvent.MouseMove,
               QPoint(self.dlg.width() // 2, self.dlg.height() // 2),
               button=Qt.NoButton)
        self.assertEqual(self.dlg.cursor().shape(), Qt.ArrowCursor)

    def test_title_band_still_moves_dialog(self):
        start = self.dlg._title.geometry().center()
        before = self.dlg.pos()
        self._press_move(start, QPoint(40, 30))
        self.assertEqual(self.dlg.pos(), before + QPoint(40, 30))
        self.assertEqual(self.dlg.width(), 560)


class MainWindowCornerResizeTest(unittest.TestCase):
    """主窗口四角斜向缩放：圆角 mask 会裁掉四角收不到鼠标事件，
    缩放热区必须大于圆角半径，角落才能同时命中两条边并斜向缩放。
    """

    def setUp(self):
        from ui import main_window as mw
        self.mw_mod = mw
        # 自动登录会起线程，构造测试窗口时拦掉
        with mock.patch.object(mw.MainWindow, "_try_auto_login"):
            self.win = mw.MainWindow()
        self.win.resize(800, 600)
        self.win.show()
        QApplication.processEvents()

    def tearDown(self):
        self.win.close()
        self.win.deleteLater()
        QApplication.processEvents()

    def test_resize_margin_exceeds_corner_radius(self):
        """热区必须大于圆角半径，否则角落 x/y 热区全被 mask 裁掉。"""
        self.assertGreater(self.mw_mod._WIN_RESIZE_MARGIN,
                           self.mw_mod._WIN_RADIUS)

    def test_corners_hit_both_edges_under_mask(self):
        """四角在 mask 可见区域内应同时命中两条边（斜向缩放可达）。"""
        self.assertFalse(self.win.mask().isEmpty())
        w, h = self.win.width(), self.win.height()
        m = self.mw_mod._WIN_RESIZE_MARGIN
        corners = {
            "TL": (m // 2, m), "TR": (w - m // 2, m),
            "BL": (m // 2, h - m), "BR": (w - m // 2, h - m),
        }
        expected = {
            "TL": int(Qt.LeftEdge | Qt.TopEdge),
            "TR": int(Qt.RightEdge | Qt.TopEdge),
            "BL": int(Qt.LeftEdge | Qt.BottomEdge),
            "BR": int(Qt.RightEdge | Qt.BottomEdge),
        }
        for name, pt in corners.items():
            p = QPoint(*pt)
            self.assertTrue(self.win.mask().contains(p),
                            f"{name} 被 mask 裁掉")
            self.assertEqual(self.win._edge_at(p), expected[name],
                             f"{name} 未同时命中两条边")

    def test_corner_resize_changes_width_and_height(self):
        """左上角向左上拖，宽高应同时缩小（斜向缩放）。"""
        geo = self.win.geometry()
        m = self.mw_mod._WIN_RESIZE_MARGIN
        edges = self.win._edge_at(QPoint(m, m))
        self.assertEqual(edges, int(Qt.LeftEdge | Qt.TopEdge))
        self.win._begin_resize(edges, geo.topLeft() + QPoint(m, m))
        self.win._resize_to(geo.topLeft() + QPoint(m + 40, m + 40))
        geo2 = self.win.geometry()
        self.assertLess(geo2.width(), geo.width())
        self.assertLess(geo2.height(), geo.height())


class MainWindowEdgeResizeTest(unittest.TestCase):
    """主窗口边沿缩放：不透明子部件吞掉靠近边沿的鼠标事件，必须靠
    eventFilter + mouse tracking 才能在子部件/滚动条上刷新缩放光标、
    并拦截按下与拖动。
    """

    def setUp(self):
        from ui import main_window as mw
        self.mw_mod = mw
        with mock.patch.object(mw.MainWindow, "_try_auto_login"):
            self.win = mw.MainWindow()
        self.win.resize(900, 600)
        self.win.show()
        QApplication.processEvents()
        # offscreen 下 splitter 布局不准，聊天区只占一部分宽度；把聊天
        # 视口拉伸到窗口宽度模拟真实场景（右边缘 = 窗口右边缘）
        self.win.chat_area.setGeometry(0, 0, self.win.width(), self.win.height())
        self.win.chat_area.chat_view.setGeometry(
            0, 52, self.win.width(), self.win.height() - 52)
        QApplication.processEvents()

    def tearDown(self):
        self.win.close()
        self.win.deleteLater()
        QApplication.processEvents()

    def test_subwidgets_have_mouse_tracking(self):
        """子部件必须开 tracking，未按下移动才持续产生 MouseMove 事件，
        否则缩放光标只在 Enter 瞬间刷一次（顶部/底部图标不及时的根因）。"""
        cv = self.win.chat_area.chat_view
        self.assertTrue(cv.viewport().hasMouseTracking())
        self.assertTrue(self.win.conversation_list.viewport().hasMouseTracking())
        self.assertTrue(self.win.sidebar.hasMouseTracking())

    def test_scrollbars_tracked_and_filtered(self):
        """滚动条盖住窗口右/左边缘，必须纳入过滤器并开 tracking，
        否则鼠标搁在滚动条上既不刷新光标也无法缩放。"""
        for sb in (self.win.chat_area.chat_view.verticalScrollBar(),
                   self.win.conversation_list.verticalScrollBar()):
            self.assertTrue(sb.hasMouseTracking())
            self.assertIn(sb, self.win._resize_tracked)

    def test_right_edge_drag_resizes_via_eventfilter(self):
        """在聊天视口右边缘按下+向右拖，宽度应增大：按下/移动都发生在
        子部件上，靠 eventFilter 拦截缩放（MainWindow.mouseMoveEvent
        收不到子部件上的移动事件）。"""
        cv = self.win.chat_area.chat_view
        vp = cv.viewport()
        m = self.mw_mod._WIN_RESIZE_MARGIN
        x = self.win.width() - 3
        y = vp.height() // 2
        before = self.win.width()
        _mouse(vp, QEvent.MouseButtonPress, QPoint(x, y),
               buttons=Qt.LeftButton)
        self.assertEqual(self.win._resize_edges, int(Qt.RightEdge))
        _mouse(vp, QEvent.MouseMove, QPoint(x + 50, y),
               buttons=Qt.LeftButton)
        self.assertEqual(self.win.width(), before + 50)
        _mouse(vp, QEvent.MouseButtonRelease, QPoint(x + 50, y))

    def test_top_edge_move_updates_cursor(self):
        """鼠标在聊天视口顶部边缘移动应刷新为垂直缩放光标
        （tracking 让移动持续触发事件过滤器刷新光标）。"""
        vp = self.win.chat_area.chat_view.viewport()
        # 顶部边缘在窗口热区内，_edge_at 应命中 TopEdge -> SizeVerCursor
        pos = QPoint(vp.width() // 2, 3)
        self.assertEqual(self.win._edge_at(pos), int(Qt.TopEdge))
        # 光标形状由 _sync_resize_cursor 按窗口坐标位置设置
        self.win._sync_resize_cursor(pos)
        self.assertEqual(self.win.cursor().shape(), Qt.SizeVerCursor)

    def test_right_edge_move_updates_cursor_from_event_pos(self):
        """é¼ æ å¨çªå£å³è¾¹ç¼ç§»å¨ï¼åæ åºæäºä»¶æºå¸¦çä½ç½®å·æ°ä¸ºæ¨ªåç¼©æ¾ç®­å¤´ï¼èé QCursor.pos()ï¼å®å¨å¿«éç§»å¨æ¶æ»åäºäºä»¶ä½ç½®ï¼"""
        # éè¿è£äºè¿æ»¤å¨çä¾§æ åé MouseMoveï¼å¶ globalPos æå¨è®¾ä¸ºçªå£å³è¾¹ç¼ï¼
        # offscreen ä¸ QCursor.pos() æ¯ (10,10)ï¼è¥ eventFilter è¯¯ç¨å®ä¼å¾å°éè¯¯åæ ï¼å¿é¡»ç¨äºä»¶ä½ç½®æè½å½ä¸­å³è¾¹ç¼ç­åº"""
        sb = self.win.sidebar
        x, y = self.win.width() - 2, 300
        gpos = self.win.mapToGlobal(QPoint(x, y))
        self.assertEqual(self.win._edge_at(QPoint(x, y)), int(Qt.RightEdge))
        ev = QMouseEvent(QEvent.MouseMove, QPointF(sb.mapFromGlobal(gpos)),
                         QPointF(sb.mapFromGlobal(gpos)), QPointF(gpos),
                         Qt.NoButton, Qt.NoButton, Qt.NoModifier)
        QApplication.sendEvent(sb, ev)
        self.assertEqual(self.win.cursor().shape(), Qt.SizeHorCursor)

    def test_chat_view_move_does_not_override_resize_cursor(self):
        """èå¤©è§å£ä¸ç§»å¨ä¸å¾è¦çä¸»çªå£çç¼©æ¾åæ ï¼
        ChatView.mouseMoveEvent æ¾æ¯å¸§ setCursor(ArrowCursor)ï¼è§å£ç»§æ¿å®èéä¸»çªå£åæ ï¼
        å¯¼è´èå¤©è®°å½è¾¹ç¼ç¼©æ¾ç®­å¤´ä¸æ¾ç¤ºã"""
        cv = self.win.chat_area.chat_view
        vp = cv.viewport()
        # ä¸»çªå£è®¾ç½®ç¼©æ¾åæ 
        self.win.setCursor(Qt.SizeHorCursor)
        pos = QPoint(vp.width() // 2, vp.height() // 2)
        _mouse(vp, QEvent.MouseMove, pos)
        # ChatView ä¸å¾æåæ åè·æç®­å¤´ï¼å¦åè§å£ä¸ç¼©æ¾ç®­å¤´ä¸æ¾ç¤º
        self.assertEqual(cv.cursor().shape(), Qt.SizeHorCursor)


if __name__ == "__main__":
    unittest.main()
