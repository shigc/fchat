"""AI 划重点浮层面板：摘要 / 日报 / 周报。

与成员/文件/聊天记录面板共用 RoomPanelBase（无边框 Tool 窗、失活自关、
随主题取色）。面板只负责展示与发出生成请求；真正的隐私门禁、上下文采集
与模型调用都在主窗口，便于统一复用与测试。流式增量 60ms 合并刷新，
生成中可停止；关闭面板不打断后台生成（用户要求），重开自动恢复生成态，
只有换房间/退出登录/点「停止」才取消在途任务。
"""
from __future__ import annotations

import logging

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QTextCursor
from PyQt5.QtWidgets import (
    QApplication, QHBoxLayout, QTextBrowser,
)

from core.markdown import md_to_html

from .room_panels import RoomPanelBase
from .widgets import WxComboBox, WxLabel, WxPushButton, _color, install_text_menu

log = logging.getLogger(__name__)

_STREAM_FLUSH_MS = 60
_RANGES = (100, 200, 500)


def _plain_html(text: str, color: str, size: int) -> str:
    import html as _html
    return (f"<div style=\"color:{color};font-size:{size}px;"
            f"line-height:150%;\">{_html.escape(text)}</div>")


class AIPanel(RoomPanelBase):
    """房间标题栏第 4 个面板：生成划重点/日报/周报。"""

    _W = 460
    _H = 520

    generate_requested = pyqtSignal(str, int)   # kind, limit
    stop_requested = pyqtSignal()
    save_requested = pyqtSignal(str)            # 待保存的 Markdown 正文

    def __init__(self, parent=None):
        super().__init__("ai", parent)
        self._encrypted = False
        self._shown_rid = None
        self._raw = ""
        self._final = ""
        self._generating = False
        self._task_running = False
        self._has_attempt = False
        self._last_kind = "summary"
        self._last_limit = 200

        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(_STREAM_FLUSH_MS)
        self._flush_timer.timeout.connect(self._flush)

        title = WxLabel("AI 划重点", self._theme, self)
        title.set_variant("bold")
        self._theme_widgets.append(title)
        self._root.addWidget(title)

        self.warn = WxLabel(
            "当前会话已开启端到端加密：生成时解密后的聊天明文会发送到"
            "模型服务器，端到端保密性将不再成立。", self._theme, self)
        self.warn.set_variant("danger")
        self.warn.setWordWrap(True)
        self.warn.hide()
        self._theme_widgets.append(self.warn)
        self._root.addWidget(self.warn)

        ctrl = QHBoxLayout()
        ctrl.setSpacing(6)
        self.range_combo = WxComboBox(self._theme)
        for n in _RANGES:
            self.range_combo.addItem(f"最近 {n} 条", n)
        self.range_combo.setCurrentIndex(_RANGES.index(200))
        self._theme_widgets.append(self.range_combo)
        ctrl.addWidget(self.range_combo)
        ctrl.addStretch(1)
        self._root.addLayout(ctrl)

        btns = QHBoxLayout()
        btns.setSpacing(6)
        self.summary_btn = WxPushButton("划重点", self._theme, parent=self)
        self.daily_btn = WxPushButton("日报", self._theme, parent=self)
        self.weekly_btn = WxPushButton("周报", self._theme, parent=self)
        self._theme_widgets.extend(
            [self.summary_btn, self.daily_btn, self.weekly_btn])
        self.summary_btn.clicked.connect(
            lambda _=False: self._start("summary"))
        self.daily_btn.clicked.connect(lambda _=False: self._start("daily"))
        self.weekly_btn.clicked.connect(
            lambda _=False: self._start("weekly"))
        btns.addWidget(self.summary_btn)
        btns.addWidget(self.daily_btn)
        btns.addWidget(self.weekly_btn)
        btns.addStretch(1)
        self._root.addLayout(btns)

        self.coverage = WxLabel("范围：未生成", self._theme, self)
        self.coverage.set_variant("hint")
        self.coverage.setWordWrap(True)
        self._theme_widgets.append(self.coverage)
        self._root.addWidget(self.coverage)

        self.view = QTextBrowser(self)
        install_text_menu(self.view)
        self.view.setOpenExternalLinks(True)
        self._root.addWidget(self.view, 1)
        self._apply_view_style()
        self._show_placeholder()

        self.status = WxLabel("", self._theme, self)
        self.status.set_variant("hint")
        self.status.setWordWrap(True)
        self._theme_widgets.append(self.status)
        self._root.addWidget(self.status)

        foot = QHBoxLayout()
        foot.setSpacing(6)
        self.stop_btn = WxPushButton("停止", self._theme, parent=self)
        self.retry_btn = WxPushButton("重试", self._theme, parent=self)
        self.copy_btn = WxPushButton("复制", self._theme, parent=self)
        self.save_btn = WxPushButton("另存为", self._theme, parent=self)
        for w in (self.stop_btn, self.retry_btn, self.copy_btn, self.save_btn):
            self._theme_widgets.append(w)
        self.stop_btn.hide()
        self.retry_btn.hide()
        self.copy_btn.setEnabled(False)
        self.save_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._on_stop)
        self.retry_btn.clicked.connect(self._on_retry)
        self.copy_btn.clicked.connect(self._on_copy)
        self.save_btn.clicked.connect(self._on_save)
        foot.addWidget(self.stop_btn)
        foot.addWidget(self.retry_btn)
        foot.addStretch(1)
        foot.addWidget(self.copy_btn)
        foot.addWidget(self.save_btn)
        self._root.addLayout(foot)

    # ------------------------------------------------------------------ #
    # 外部状态
    # ------------------------------------------------------------------ #
    def set_encrypted(self, encrypted: bool):
        self._encrypted = bool(encrypted)
        self.warn.setVisible(self._encrypted)

    def set_task_running(self, running: bool):
        """主窗口标记后台生成任务是否在途（关面板后任务继续跑）。"""
        self._task_running = bool(running)

    def begin_generating(self, limit: int):
        """主窗口门禁通过后调用：清空上次结果并进入生成态。"""
        self._last_limit = int(limit or 200)
        self._has_attempt = True
        self._raw = ""
        self._final = ""
        self._flush_timer.stop()
        self.view.clear()
        self.coverage.setText("范围：读取中…")
        self.status.set_variant("hint")
        self.status.setText("正在读取聊天记录…")
        self._set_generating(True)

    def append_delta(self, text: str):
        if not text:
            return
        self._raw += text
        self._update_actions()

    def append_status(self, text: str):
        if not text:
            return
        self.status.set_variant("hint")
        self.status.setText(text)
        # 采集结束时 worker 发来的是覆盖范围文案（不是「正在…」）
        if not text.startswith("正在"):
            self.coverage.setText(text)

    def set_coverage(self, text: str):
        if text:
            self.coverage.setText(text)

    def finish(self, text: str, coverage: str = ""):
        text = text or self._raw
        self._final = text
        self._raw = text
        self._set_generating(False)
        self._render(text)
        if coverage:
            self.coverage.setText(coverage)
        self.status.set_variant("hint")
        self.status.setText("完成")
        self._update_actions()

    def fail(self, err: str):
        self._set_generating(False)
        self.status.set_variant("danger")
        self.status.setText(f"生成失败：{err or '未知错误'}")
        self._update_actions()

    def cancelled(self):
        self._set_generating(False)
        self.status.set_variant("hint")
        self.status.setText("已停止")
        if self._raw:
            self.view.setPlainText(self._raw)
        self._update_actions()

    # ------------------------------------------------------------------ #
    # 子类钩子
    # ------------------------------------------------------------------ #
    def _on_open(self):
        if self._rid != self._shown_rid:
            self._shown_rid = self._rid
            self._raw = ""
            self._final = ""
            self._has_attempt = False
            self._show_placeholder()
            self.coverage.setText("范围：未生成")
            self.status.set_variant("hint")
            self.status.setText("")
        if self._task_running:
            # 后台任务仍在生成：恢复生成态（_on_closed 停掉的 flush 定时器
            # 由 _set_generating 重启，积压的增量下一跳整段刷出）；状态行
            # 文案由任务回调维护，被下面的「已停止」兜底改写过就补回
            self._set_generating(True)
            if not self.status.text().startswith("正在"):
                self.status.set_variant("hint")
                self.status.setText("正在生成…")
        else:
            self._set_generating(False)
            # 关闭面板时主窗口会取消在途任务，而 worker 自己发来的 cancelled
            # 回调已被 token 判成迟到信号丢弃，所以这里必须把残留的「正在…」
            # 落成真话，否则重开面板永远卡在生成态
            if self.status.text().startswith("正在"):
                self.status.set_variant("hint")
                self.status.setText("已停止" if self._raw else "")
        self._update_actions()

    def _on_closed(self):
        self._flush_timer.stop()

    def _apply_theme(self):
        super()._apply_theme()
        self._apply_view_style()
        if self._final:
            self._render(self._final)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _apply_view_style(self):
        t = self._theme
        bg = _color(t, "conv_bg", "#ffffff")
        fg = _color(t, "text_primary", "#191919")
        border = _color(t, "input_border", "#e0e0e0")
        self.view.setStyleSheet(
            f"QTextBrowser{{background:{bg};color:{fg};"
            f"border:1px solid {border};border-radius:6px;padding:4px;}}")

    def _html_color(self, key: str, default: str) -> str:
        return _color(self._theme, key, default)

    def _font_px(self) -> int:
        try:
            return int(getattr(self._theme, "font_size", 13) or 13)
        except (TypeError, ValueError):
            return 13

    def _show_placeholder(self):
        size = self._font_px()
        color = self._html_color("text_secondary", "#999999")
        self.view.setHtml(_plain_html(
            "点击「划重点」生成摘要；也可生成今天的日报或最近 7 天的周报。",
            color, size))

    def _render(self, text: str):
        if not text:
            self.view.clear()
            return
        fg = self._html_color("text_primary", "#191919")
        body = md_to_html(text) or "<p></p>"
        self.view.setHtml(
            f"<div style=\"color:{fg};font-size:{self._font_px()}px;"
            f"line-height:150%;\">{body}</div>")
        self.view.moveCursor(QTextCursor.End)

    def _flush(self):
        if not self._raw:
            return
        try:
            self.view.setPlainText(self._raw)
            self.view.moveCursor(QTextCursor.End)
        except RuntimeError:
            pass

    def _set_generating(self, on: bool):
        self._generating = bool(on)
        for w in (self.range_combo, self.summary_btn,
                  self.daily_btn, self.weekly_btn):
            w.setEnabled(not on)
        self.stop_btn.setVisible(on)
        self.retry_btn.setVisible(not on and self._has_attempt)
        if on:
            self._flush_timer.start()
        else:
            self._flush_timer.stop()
            # 已完成的结果以 Markdown HTML 呈现，普通 flush 会把它打回纯文本
            if self._raw and self._raw != self._final:
                self._flush()
        self._update_actions()

    def _update_actions(self):
        has_text = bool(self._raw or self._final)
        self.copy_btn.setEnabled(has_text)
        self.save_btn.setEnabled(has_text)

    def _start(self, kind: str):
        if self._generating:
            return
        limit = int(self.range_combo.currentData() or 200)
        self._last_kind = kind
        self._last_limit = limit
        self.generate_requested.emit(kind, limit)

    def _on_stop(self, *_):
        self.stop_requested.emit()

    def _on_retry(self, *_):
        if self._generating:
            return
        self.generate_requested.emit(self._last_kind, self._last_limit)

    def _on_copy(self, *_):
        text = self._final or self._raw
        if text:
            QApplication.clipboard().setText(text)

    def _on_save(self, *_):
        text = self._final or self._raw
        if text:
            self.save_requested.emit(text)
