"""AI 结果对话框：流式展示、停止/重试/复制/插入。

- 流式期间正文以纯文本增量刷新（60ms 合并一次，避免逐 token 重排），
  完成后用 core.markdown 渲染富文本；停止后保留已生成的部分，可复制。
- 关闭（右上角 X / Esc / 取消键）即取消后台任务；任务随对话框销毁会
  自动解绑线程（见 ui/ai_worker.AITask），不会出现 QThread 析构报错。
- 本对话框只负责展示与回传文本，「插入输入框」由调用方决定是追加还是
  替换；绝不自动发送。
"""
from __future__ import annotations

import html
import logging
from typing import Callable, Optional

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QTextCursor
from PyQt5.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QTextBrowser,
)

from core.markdown import md_to_html

from .ai_worker import AITask
from .dialog_base import FramelessCardDialog
from .widgets import WxLabel, WxPushButton, _color, install_text_menu

log = logging.getLogger(__name__)

_FLUSH_MS = 60


class AIDialog(FramelessCardDialog):
    """一次 AI 任务的结果窗。

    job_factory() 每次调用返回 `job(worker)`；重试会重新调用它，从而
    为每次请求创建全新的 AIClient / 采集流程。
    """

    def __init__(self, parent=None, theme=None, title: str = "AI 助手",
                 job_factory: Optional[Callable] = None,
                 context_note: str = "", allow_insert: bool = True,
                 width: int = 640):
        super().__init__(title, theme, parent, width=width)
        self.setFixedHeight(560)
        self.setWindowTitle(title)
        self.inserted_text: Optional[str] = None
        self._job_factory = job_factory
        self._allow_insert = allow_insert
        self._context_note = context_note or ""
        self._task: Optional[AITask] = None
        self._raw = ""
        self._final_text = ""
        self._running = False
        self._dirty = False
        self._closing = False

        self._note = WxLabel(context_note or "", theme, self)
        self._note.set_variant("hint")
        self._note.setWordWrap(True)
        self._layout.addWidget(self._note)

        self._view = QTextBrowser(self)
        self._view.setOpenExternalLinks(True)
        self._view.setMinimumHeight(360)
        install_text_menu(self._view)
        self._layout.addWidget(self._view, 1)
        self._apply_view_style()

        self._status = WxLabel("准备中…", theme, self)
        self._status.set_variant("hint")
        self._layout.addWidget(self._status)

        foot = QHBoxLayout()
        foot.setSpacing(10)
        self._stop_btn = WxPushButton("停止", theme, parent=self)
        self._stop_btn.clicked.connect(self._on_stop)
        foot.addWidget(self._stop_btn)
        foot.addStretch(1)
        self._retry_btn = WxPushButton("重试", theme, parent=self)
        self._retry_btn.clicked.connect(self._on_retry)
        foot.addWidget(self._retry_btn)
        self._copy_btn = WxPushButton("复制", theme, parent=self)
        self._copy_btn.clicked.connect(self._on_copy)
        foot.addWidget(self._copy_btn)
        self._insert_btn = WxPushButton("插入输入框", theme, parent=self)
        self._insert_btn.clicked.connect(self._on_insert)
        self._insert_btn.setVisible(allow_insert)
        foot.addWidget(self._insert_btn)
        self._close_btn_bottom = WxPushButton("关闭", theme, parent=self)
        self._close_btn_bottom.clicked.connect(self.reject)
        foot.addWidget(self._close_btn_bottom)
        self._layout.addLayout(foot)

        self._theme_widgets.extend([
            self._note, self._status, self._stop_btn, self._retry_btn,
            self._copy_btn, self._insert_btn, self._close_btn_bottom])

        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(_FLUSH_MS)
        self._flush_timer.timeout.connect(self._flush)

        self._set_running(False)
        self._update_action_buttons()

    # ------------------------------------------------------------------ #
    # 外观
    # ------------------------------------------------------------------ #
    def _apply_view_style(self):
        t = self._theme
        bg = _color(t, "conv_bg", "#ffffff")
        fg = _color(t, "text_primary", "#191919")
        border = _color(t, "input_border", "#e0e0e0")
        self._view.setStyleSheet(
            f"QTextBrowser{{background:{bg};color:{fg};"
            f"border:1px solid {border};border-radius:6px;padding:2px;}}")

    def set_theme(self, theme):
        super().set_theme(theme)
        self._apply_view_style()
        # 已完成的消息以 HTML 渲染，换主题需要重绘
        if self._final_text:
            self._render_markdown(self._final_text)

    def _render_markdown(self, text: str):
        t = self._theme
        fg = _color(t, "text_primary", "#191919")
        family = html.escape(
            getattr(t, "font_family", "") or "Microsoft YaHei", quote=True)
        try:
            size = int(getattr(t, "font_size", 13) or 13)
        except (TypeError, ValueError):
            size = 13
        body = md_to_html(text) or "<p></p>"
        self._view.setHtml(
            f"<div style=\"color:{fg};font-family:'{family}';"
            f"font-size:{size}px;line-height:150%;\">{body}</div>")
        self._view.moveCursor(QTextCursor.End)

    # ------------------------------------------------------------------ #
    # 任务生命周期
    # ------------------------------------------------------------------ #
    def start(self):
        """开始首次请求（模态 exec_ 之前调用）。"""
        self._start_task()

    def _start_task(self):
        if self._running:
            return
        self._raw = ""
        self._final_text = ""
        self._dirty = False
        self._view.clear()
        self._note.setText(self._context_note)
        self._status.set_variant("hint")
        self._status.setText("正在生成…")
        self._stop_btn.setEnabled(True)
        try:
            job = self._job_factory() if self._job_factory else None
            if job is None:
                raise RuntimeError("AI 任务未配置")
        except Exception as e:  # noqa: BLE001
            log.warning("创建 AI 任务失败：%s", e)
            self._show_error(str(e))
            return

        self._set_running(True)
        self._update_action_buttons()
        task = AITask(job, parent=self)
        self._task = task
        task.delta.connect(self._on_delta)
        task.status.connect(self._on_status)
        task.done.connect(self._on_done)
        task.failed.connect(self._on_failed)
        task.cancelled.connect(self._on_cancelled)

    def _set_running(self, running: bool):
        self._running = running
        self._stop_btn.setVisible(running)
        self._retry_btn.setVisible(not running)
        if running:
            self._flush_timer.start()
        else:
            self._flush_timer.stop()
            self._flush()

    def _update_action_buttons(self):
        has_text = bool(self._final_text or self._raw)
        self._copy_btn.setEnabled(has_text)
        self._insert_btn.setEnabled(has_text and self._allow_insert)
        self._retry_btn.setEnabled(not self._running)

    # ------------------------------------------------------------------ #
    # 任务信号（全部在主线程）
    # ------------------------------------------------------------------ #
    def _on_delta(self, text):
        try:
            if not text:
                return
            self._raw += text
            self._dirty = True
            self._update_action_buttons()
        except Exception:  # noqa: BLE001
            log.exception("AI 增量回调异常")

    def _on_status(self, text):
        try:
            if text:
                # 状态只走底部状态行；顶部 _note 是来源/覆盖范围说明，
                # 写进去后完成时无人清空，会一直停在「正在请求模型…」
                self._status.set_variant("hint")
                self._status.setText(text)
        except Exception:  # noqa: BLE001
            log.exception("AI 状态回调异常")

    def _on_done(self, result):
        try:
            text = ""
            coverage = ""
            if isinstance(result, dict):
                text = str(result.get("text") or "")
                coverage = str(result.get("coverage") or "")
            elif result is not None:
                text = str(result)
            if not text:
                text = self._raw
            if coverage:
                self._note.setText(coverage)
            self._final_text = text
            self._raw = text
            self._set_running(False)
            self._status.set_variant("hint")
            self._status.setText("完成")
            self._render_markdown(text)
            self._update_action_buttons()
        except Exception:  # noqa: BLE001
            log.exception("AI 完成回调异常")

    def _on_failed(self, err):
        try:
            self._set_running(False)
            self._show_error(err)
        except Exception:  # noqa: BLE001
            log.exception("AI 失败回调异常")

    def _show_error(self, err):
        self._status.set_variant("danger")
        self._status.setText(f"生成失败：{err or '未知错误'}")
        self._update_action_buttons()

    def _on_cancelled(self):
        try:
            self._set_running(False)
            self._status.set_variant("hint")
            self._status.setText(
                "已停止，保留已生成的内容" if self._raw else "已停止")
            self._update_action_buttons()
        except Exception:  # noqa: BLE001
            log.exception("AI 取消回调异常")

    # ------------------------------------------------------------------ #
    # 增量刷新
    # ------------------------------------------------------------------ #
    def _flush(self):
        if not self._dirty:
            return
        self._dirty = False
        try:
            self._view.setPlainText(self._raw)
            bar = self._view.verticalScrollBar()
            bar.setValue(bar.maximum())
        except RuntimeError:
            pass

    # ------------------------------------------------------------------ #
    # 按键
    # ------------------------------------------------------------------ #
    def _on_stop(self, *_):
        if not self._running:
            return
        self._stop_btn.setEnabled(False)
        self._status.set_variant("hint")
        self._status.setText("正在停止…")
        if self._task is not None:
            self._task.cancel()

    def _on_retry(self, *_):
        if self._running:
            return
        if self._task is not None:
            self._detach_task_signals()
            self._task.cancel()
            self._task = None
        self._start_task()

    def _detach_task_signals(self):
        """重试前断开旧任务信号，避免迟到的 cancelled 覆盖新任务状态。"""
        task = self._task
        if task is None:
            return
        for sig, slot in ((task.delta, self._on_delta),
                          (task.status, self._on_status),
                          (task.done, self._on_done),
                          (task.failed, self._on_failed),
                          (task.cancelled, self._on_cancelled)):
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    def _on_copy(self, *_):
        text = self._final_text or self._raw
        if not text:
            return
        try:
            QApplication.clipboard().setText(text)
            self._status.set_variant("hint")
            self._status.setText("已复制到剪贴板")
        except Exception:  # noqa: BLE001
            log.exception("复制 AI 结果失败")

    def _on_insert(self, *_):
        if not self._allow_insert:
            return
        text = self._final_text or self._raw
        if not text:
            return
        self.inserted_text = text
        self.accept()

    # ------------------------------------------------------------------ #
    # 关闭即取消
    # ------------------------------------------------------------------ #
    def reject(self, *_):
        self._closing = True
        if self._task is not None and self._running:
            self._task.cancel()
        super().reject()

    def accept(self, *_):
        if self._task is not None and self._running:
            self._task.cancel()
        super().accept()

    def closeEvent(self, event):
        self.reject()
        event.accept()

    @property
    def raw_text(self) -> str:
        return self._final_text or self._raw


def run_ai_dialog(parent, theme, title: str, job_factory: Callable,
                  context_note: str = "", allow_insert: bool = True
                  ) -> Optional[str]:
    """模态执行一次 AI 任务；用户点「插入输入框」时返回文本，否则 None。"""
    dlg = AIDialog(parent, theme, title, job_factory,
                   context_note=context_note, allow_insert=allow_insert)
    try:
        dlg.start()
        dlg.exec_()
        return dlg.inserted_text
    finally:
        dlg.deleteLater()
