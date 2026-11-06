"""markdown 渲染验证：裸 URL / [text](url) 高亮识别、自动链接标点剥离。

核心关注点：looks_like_markdown 是消息是否走富文本渲染的门槛，门槛过严会让
「只含 URL」的消息退化成纯文本（链接不高亮不可点）。本测试离线断言 HTML 结构，
可点观感需真实窗口确认。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import markdown  # noqa: E402


class TestLooksLikeMarkdown(unittest.TestCase):

    def test_bare_url_detected(self):
        self.assertTrue(markdown.looks_like_markdown("https://example.com/plain"))
        self.assertTrue(
            markdown.looks_like_markdown("看看 https://example.com/a?b=c"))

    def test_markdown_link_detected(self):
        self.assertTrue(markdown.looks_like_markdown("[t](https://example.com)"))

    def test_plain_text_not_detected(self):
        self.assertFalse(markdown.looks_like_markdown("普通文字没有链接"))
        self.assertFalse(markdown.looks_like_markdown(""))
        self.assertFalse(markdown.looks_like_markdown(None))


class TestAutolink(unittest.TestCase):

    def test_bare_url_linkified(self):
        html = markdown.md_to_html("去 https://example.com/a 看看")
        self.assertIn('<a href="https://example.com/a"', html)
        self.assertIn("text-decoration:underline", html)

    def test_markdown_link_uses_label(self):
        html = markdown.md_to_html("[点这里](https://example.com)")
        self.assertIn('href="https://example.com"', html)
        self.assertIn(">点这里</a>", html)

    def test_trailing_punct_stripped(self):
        # 中文句号 / 括号等闭合标点不该被吞进 href
        html = markdown.md_to_html("看 https://example.com/a。")
        self.assertIn('href="https://example.com/a"', html)

    def test_trailing_ascii_punct_stripped(self):
        html = markdown.md_to_html("看 https://example.com/a).")
        self.assertIn('href="https://example.com/a"', html)


class TestInlineImage(unittest.TestCase):
    """markdown 内联图 ![alt](url) 的解析与 HTML 结构。"""

    def test_inline_image_renders_img(self):
        html = markdown.md_to_html("看 ![图](https://x/a.png) 吧")
        self.assertIn('<img src="https://x/a.png"', html)
        self.assertIn('width="240"', html)
        self.assertIn('height="180"', html)

    def test_inline_image_triggers_markdown(self):
        self.assertTrue(
            markdown.looks_like_markdown("![图](https://x/a.png)"))

    def test_plain_left_bracket_not_markdown(self):
        self.assertFalse(
            markdown.looks_like_markdown("这里有个 [ 括号"))

    def test_linked_inline_image_keeps_anchor(self):
        # 链接式图 [![a](i)](https://d) 应保留外层 <a>，点击仍走现有 anchor 逻辑
        html = markdown.md_to_html("[![a](https://x/i.png)](https://d)")
        self.assertIn('<a', html)
        self.assertIn('<img src="https://x/i.png"', html)

    def test_inline_image_urls_extract(self):
        items = markdown.inline_image_urls("![一](https://x/1.png) 和 ![二](/file-upload/2.png)")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["url"], "https://x/1.png")
        self.assertEqual(items[1]["url"], "/file-upload/2.png")
        self.assertIn('1.png', items[0]["src"])

    def test_inline_image_no_false_positive_on_link(self):
        # 普通链接不应被误判成图
        self.assertEqual(markdown.inline_image_urls("[点这里](https://e.com)"), [])



class TestBlockLayout(unittest.TestCase):
    """块级排版：空行不再变 <br>、列表合并/嵌套/懒续行。"""

    def test_blank_lines_do_not_become_br(self):
        html = markdown.md_to_html("第一段\n\n第二段")
        self.assertNotIn("<br>", html)
        self.assertEqual(html.count("<p "), 2)

    def test_heading_is_compact_paragraph(self):
        html = markdown.md_to_html("## 标题\n\n正文")
        self.assertIn('<font size="+1">标题</font>', html)
        self.assertNotIn("<h2>", html)
        self.assertNotIn("<br>", html)

    def test_list_items_survive_blank_lines(self):
        html = markdown.md_to_html("- 甲\n\n- 乙\n\n- 丙")
        self.assertEqual(html.count("<ul "), 1)
        self.assertEqual(html.count("<li "), 3)
        self.assertNotIn("<br>", html)

    def test_wrapped_line_stays_inside_list_item(self):
        html = markdown.md_to_html("- 确认分支为\nbrn_for_x，并指定节点 841e。")
        self.assertEqual(html.count("<ul "), 1)
        self.assertEqual(html.count("<li "), 1)
        self.assertIn("确认分支为<br>brn_for_x", html)
        self.assertLess(html.index("brn_for_x"), html.index("</li>"))

    def test_nested_list_uses_nested_markup(self):
        html = markdown.md_to_html("- 甲\n  - 乙\n  - 丙\n- 丁")
        self.assertEqual(html.count("<ul "), 2)
        self.assertEqual(html.count("<li "), 4)
        self.assertLess(html.index("乙"), html.index("</li></ul>"))
        self.assertGreater(html.index("丁"), html.index("</li></ul>"))

    def test_ordered_list_kept(self):
        html = markdown.md_to_html("1. 第一\n2. 第二")
        self.assertEqual(html.count("<ol "), 1)
        self.assertEqual(html.count("<li "), 2)

    def test_single_newline_is_br_inside_paragraph(self):
        html = markdown.md_to_html("甲\n乙")
        self.assertEqual(html.count("<p "), 1)
        self.assertIn("甲<br>乙", html)

    def test_horizontal_rule(self):
        html = markdown.md_to_html("上\n\n---\n\n下")
        self.assertIn("<hr", html)
        self.assertTrue(markdown.looks_like_markdown("---"))


if __name__ == "__main__":
    unittest.main()
