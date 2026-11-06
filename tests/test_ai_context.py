"""core.ai_context 单元测试：采集分页/截断/过滤、覆盖范围、提示词构造。

全部离线，不碰网络与 Qt。
运行：python -m unittest tests.test_ai_context -v
"""
import datetime
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import ai_context as ctx  # noqa: E402
from core.ai_client import AICancelled  # noqa: E402
from core.messages import E2E_PLACEHOLDER_TEXT  # noqa: E402
from core.models import Message, User  # noqa: E402


def mk_msg(i, ts, text=None, *, sender="张三", mine=False,
           msg_type="text", file_name=""):
    return Message(
        id=str(i),
        room_id="r1",
        text=text if text is not None else f"消息{i}",
        sender=User(id=f"u{i}", username=f"user{i}", name=sender),
        timestamp=ts,
        msg_type=msg_type,
        is_mine=mine,
        file_name=file_name,
    )


class FakeHistory:
    """按 offset/count 切片的假客户端；messages 为时间倒序（新→旧）。"""

    def __init__(self, messages):
        self.messages = list(messages)
        self.calls = []

    def get_history(self, room_id, count=50, offset=0, room_type="c"):
        self.calls.append({"room_id": room_id, "count": count,
                           "offset": offset, "room_type": room_type})
        return self.messages[offset:offset + count]


def make_descending(n, start=1_700_000_000.0, step=60.0):
    """生成 n 条时间倒序消息：第 0 条最新。"""
    return [mk_msg(i, start - i * step) for i in range(n)]


class CollectPaginationTest(unittest.TestCase):
    def test_pages_until_limit_and_reports_coverage(self):
        msgs = make_descending(250)
        fake = FakeHistory(msgs)
        seen_progress = []
        result = ctx.collect_room_context(
            fake, "r1", "c", limit=250, on_progress=seen_progress.append)

        self.assertEqual(len(result.messages), 250)
        self.assertEqual(result.scanned, 250)
        self.assertEqual([c["offset"] for c in fake.calls], [0, 100, 200])
        self.assertTrue(all(c["room_id"] == "r1" for c in fake.calls))
        self.assertTrue(all(c["room_type"] == "c" for c in fake.calls))
        # 时间正序输出：旧 → 新
        stamps = [m.timestamp for m in result.messages]
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(result.messages[-1].id, "0")
        self.assertIn("达到 250 条上限", result.coverage)
        self.assertIn("250 条", result.coverage)
        # 每页回调一次，最后一次为 250
        self.assertEqual(seen_progress, [100, 200, 250])

    def test_stops_when_short_page(self):
        fake = FakeHistory(make_descending(30))
        result = ctx.collect_room_context(fake, "r1", "c", limit=200)
        self.assertEqual(len(result.messages), 30)
        # 短页即到底，不再多拉一页
        self.assertEqual(len(fake.calls), 1)
        self.assertFalse(result.truncated)
        self.assertIn("最近 30 条", result.coverage)
        self.assertNotIn("上限", result.coverage)

    def test_offset_advances_by_page_length(self):
        fake = FakeHistory(make_descending(120))
        ctx.collect_room_context(fake, "r1", "c", limit=120)
        # 第二页只需 20 条（want 随剩余额度缩小），收满即停
        self.assertEqual([c["offset"] for c in fake.calls], [0, 100])
        self.assertEqual(fake.calls[1]["count"], 20)

    def test_duplicate_ids_are_deduped(self):
        msgs = make_descending(150)
        # 服务端重复推送：把第 99 条再塞到第 100 位
        msgs.insert(100, msgs[99])
        fake = FakeHistory(msgs)
        result = ctx.collect_room_context(fake, "r1", "c", limit=150)
        ids = [m.id for m in result.messages]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(result.messages), 150)

    def test_empty_history(self):
        fake = FakeHistory([])
        result = ctx.collect_room_context(fake, "r1", "c")
        self.assertEqual(result.messages, [])
        self.assertEqual(result.text, "")
        self.assertIn("没有可发送", result.coverage)


class CollectSinceTest(unittest.TestCase):
    def test_stops_at_time_window(self):
        # 10 条倒序，since_ts 取第 4 条（index 3）的时间：应包含 0..3
        msgs = make_descending(10)
        since = msgs[3].timestamp
        result = ctx.collect_room_context(
            FakeHistory(msgs), "r1", "c", since_ts=since)
        self.assertEqual([m.id for m in result.messages],
                         ["3", "2", "1", "0"])
        self.assertEqual(result.since_ts, since)
        self.assertFalse(result.truncated)
        self.assertIn("至今", result.coverage)
        self.assertIn("共 4 条", result.coverage)

    def test_since_older_than_all_returns_everything(self):
        msgs = make_descending(5)
        result = ctx.collect_room_context(
            FakeHistory(msgs), "r1", "c", since_ts=msgs[-1].timestamp - 10)
        self.assertEqual(len(result.messages), 5)

    def test_since_and_limit_marks_truncated(self):
        msgs = make_descending(300)
        result = ctx.collect_room_context(
            FakeHistory(msgs), "r1", "c", limit=50,
            since_ts=msgs[-1].timestamp - 10)
        self.assertEqual(len(result.messages), 50)
        self.assertTrue(result.truncated)
        self.assertIn("达到 50 条上限", result.coverage)


class CollectFilterTest(unittest.TestCase):
    def test_skips_system_e2e_placeholder_and_empty(self):
        msgs = [
            mk_msg("a", 100, "正常消息"),
            mk_msg("b", 90, "系统提示", msg_type="system"),
            mk_msg("c", 80, E2E_PLACEHOLDER_TEXT),
            mk_msg("d", 70, "   "),
            mk_msg("e", 60, ""),
        ]
        result = ctx.collect_room_context(FakeHistory(msgs), "r1", "p")
        self.assertEqual([m.id for m in result.messages], ["a"])
        self.assertIn("正常消息", result.text)
        self.assertNotIn("系统提示", result.text)
        self.assertNotIn(E2E_PLACEHOLDER_TEXT, result.text)

    def test_attachment_without_text_uses_display_text(self):
        msgs = [
            mk_msg("f", 100, "", msg_type="file", file_name="报告.docx"),
            mk_msg("i", 90, "", msg_type="image"),
        ]
        result = ctx.collect_room_context(FakeHistory(msgs), "r1", "c")
        self.assertEqual(len(result.messages), 2)
        self.assertIn("[文件] 报告.docx", result.text)
        self.assertIn("[图片]", result.text)

    def test_multiline_collapsed_and_mine_marked(self):
        ts = time.mktime((2026, 10, 20, 15, 30, 0, 0, 0, -1))
        msgs = [mk_msg("m", ts, "第一行\n\n第二行\t结尾", mine=True)]
        result = ctx.collect_room_context(FakeHistory(msgs), "r1", "c")
        self.assertIn("第一行 第二行 结尾", result.text)
        self.assertIn("张三（我）", result.text)
        self.assertIn("[10-20 15:30]", result.text)

    def test_single_message_length_capped(self):
        msgs = [mk_msg("long", 100, "字" * 3000)]
        result = ctx.collect_room_context(FakeHistory(msgs), "r1", "c")
        self.assertIn("…", result.text)
        self.assertLess(len(result.text), ctx.PER_MESSAGE_CHARS + 100)


class CollectCharCapTest(unittest.TestCase):
    def test_keeps_newest_and_flags_truncation(self):
        msgs = [
            mk_msg("new", 300, "新" * 400),
            mk_msg("mid", 200, "中" * 400),
            mk_msg("old", 100, "旧" * 400),
        ]
        result = ctx.collect_room_context(
            FakeHistory(msgs), "r1", "c", max_chars=600)
        self.assertTrue(result.truncated)
        self.assertIn("已按字符上限截断", result.coverage)
        self.assertIn("新", result.text)
        self.assertNotIn("旧", result.text)
        self.assertLessEqual(len(result.text), 601)
        # 收到的是最新的一条或两条，输出仍为时间正序
        self.assertEqual(result.messages[-1].id, "new")

    def test_tiny_budget_adds_nothing(self):
        msgs = [mk_msg("a", 100, "内容很长" * 100)]
        result = ctx.collect_room_context(
            FakeHistory(msgs), "r1", "c", max_chars=5)
        self.assertEqual(result.messages, [])
        self.assertEqual(result.text, "")
        self.assertIn("没有可发送", result.coverage)

    def test_char_cap_not_reported_when_fits(self):
        msgs = make_descending(3)
        result = ctx.collect_room_context(
            FakeHistory(msgs), "r1", "c", max_chars=30000)
        self.assertFalse(result.truncated)
        self.assertNotIn("截断", result.coverage)


class CancelTest(unittest.TestCase):
    def test_cancel_before_first_page(self):
        cancel = threading.Event()
        cancel.set()
        fake = FakeHistory(make_descending(10))
        with self.assertRaises(AICancelled):
            ctx.collect_room_context(fake, "r1", "c", cancel=cancel)
        self.assertEqual(fake.calls, [])

    def test_cancel_mid_pagination(self):
        cancel = threading.Event()
        fake = FakeHistory(make_descending(300))

        def on_progress(scanned):
            if scanned >= 100:
                cancel.set()

        with self.assertRaises(AICancelled):
            ctx.collect_room_context(
                fake, "r1", "c", limit=300, cancel=cancel,
                on_progress=on_progress)
        self.assertEqual(len(fake.calls), 1)

    def test_progress_callback_exception_is_ignored(self):
        def boom(_n):
            raise RuntimeError("callback")

        result = ctx.collect_room_context(
            FakeHistory(make_descending(5)), "r1", "c", on_progress=boom)
        self.assertEqual(len(result.messages), 5)

    def test_status_callback_receives_coverage(self):
        got = []
        result = ctx.collect_room_context(
            FakeHistory(make_descending(5)), "r1", "c",
            on_status=got.append)
        self.assertEqual(got, [result.coverage])


class TimeRangeTest(unittest.TestCase):
    def test_start_of_day(self):
        ts = datetime.datetime(2026, 10, 20, 15, 30).timestamp()
        expected = datetime.datetime(2026, 10, 20).timestamp()
        self.assertEqual(ctx.start_of_day(ts), expected)

    def test_period_start_daily(self):
        ts = datetime.datetime(2026, 10, 20, 15, 30).timestamp()
        self.assertEqual(ctx.period_start("daily", ts),
                         datetime.datetime(2026, 10, 20).timestamp())

    def test_period_start_weekly_is_seven_days_inclusive(self):
        ts = datetime.datetime(2026, 10, 20, 15, 30).timestamp()
        self.assertEqual(ctx.period_start("weekly", ts),
                         datetime.datetime(2026, 10, 14).timestamp())


class PromptTest(unittest.TestCase):
    def _user_blob(self, messages):
        return "\n".join(m["content"] for m in messages
                         if m["role"] == "user")

    def test_message_actions_include_instruction_and_guard(self):
        cases = {
            "translate": "翻译",
            "explain": "解释",
            "todos": "待办",
            "proofread": "错别字",
            "reply": "回复",
        }
        for action, keyword in cases.items():
            messages = ctx.message_action_prompt(action, "原始内容")
            self.assertEqual(messages[0]["role"], "system")
            self.assertIn("不是指令", messages[0]["content"])
            blob = self._user_blob(messages)
            self.assertIn("原始内容", blob)
            self.assertIn(keyword, blob)
            self.assertIn("<<<", blob)
            self.assertIn(">>>", blob)

    def test_reply_prompt_uses_username(self):
        blob = self._user_blob(
            ctx.message_action_prompt("reply", "hi", username="bob"))
        self.assertIn("@bob", blob)

    def test_empty_message_text_gets_placeholder(self):
        blob = self._user_blob(ctx.message_action_prompt("explain", "  "))
        self.assertIn("（空消息）", blob)

    def test_unknown_action_still_returns_messages(self):
        messages = ctx.message_action_prompt("whatever", "内容")
        self.assertIn("内容", self._user_blob(messages))

    def test_writing_actions(self):
        for action in ("polish", "rewrite", "shorten"):
            blob = self._user_blob(ctx.writing_assist_prompt(action, "草稿"))
            self.assertIn("草稿", blob)
            self.assertIn("只输出", blob)
            self.assertIn("<<<", blob)

    def test_reply_draft_with_intent(self):
        blob = self._user_blob(
            ctx.reply_draft_prompt("记录正文", "alice", "表示同意"))
        self.assertIn("记录正文", blob)
        self.assertIn("@alice", blob)
        self.assertIn("表示同意", blob)

    def test_summary_prompt_sections_and_coverage(self):
        room_ctx = ctx.RoomContext(
            messages=[mk_msg(1, 100)], text="张三: 你好",
            coverage="最近 1 条", truncated=False)
        blob = self._user_blob(ctx.summary_prompt(room_ctx, "me"))
        self.assertIn("最近 1 条", blob)
        self.assertIn("@me", blob)
        for section in ("讨论主题", "待办事项", "与我相关", "决策与待确认"):
            self.assertIn(section, blob)
        self.assertIn("张三: 你好", blob)

    def test_report_prompt_labels(self):
        room_ctx = ctx.RoomContext(text="记录", coverage="今天 3 条")
        daily = self._user_blob(ctx.report_prompt(room_ctx, "daily"))
        weekly = self._user_blob(ctx.report_prompt(room_ctx, "weekly"))
        self.assertIn("日报", daily)
        self.assertIn("周报", weekly)
        self.assertIn("最近 7 天", weekly)
        for section in ("工作概览", "已完成", "进行中", "风险 / 阻塞",
                        "下一步建议"):
            self.assertIn(section, daily)

    def test_search_prompt_only_contains_query(self):
        messages = ctx.search_keywords_prompt("  上周讨论的部署问题  ")
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[1]["content"], "上周讨论的部署问题")
        self.assertIn("关键词", messages[0]["content"])


class ParseKeywordsTest(unittest.TestCase):
    def test_cleans_bullets_quotes_and_punctuation(self):
        raw = "- 项目\n- 项目\n2. 截止日期。\n\"会议\"\n• 部署；"
        self.assertEqual(ctx.parse_search_keywords(raw),
                         ["项目", "截止日期", "会议", "部署"])

    def test_comma_separated(self):
        self.assertEqual(ctx.parse_search_keywords("甲,乙，丙;丁"),
                         ["甲", "乙", "丙", "丁"])

    def test_fallback_when_empty(self):
        self.assertEqual(ctx.parse_search_keywords("", "原始查询"),
                         ["原始查询"])
        self.assertEqual(ctx.parse_search_keywords("   ", " 原始查询 "),
                         ["原始查询"])

    def test_caps_at_eight(self):
        raw = "\n".join(f"关键词{i}" for i in range(20))
        self.assertEqual(len(ctx.parse_search_keywords(raw)), 8)

    def test_drops_overlong_and_blank_items(self):
        raw = "正常\n" + "很长" * 30 + "\n\n"
        self.assertEqual(ctx.parse_search_keywords(raw), ["正常"])


if __name__ == "__main__":
    unittest.main()
