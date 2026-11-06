"""轻量 Markdown -> Qt 富文本 HTML 转换。

Rocket.Chat 消息正文是 markdown，QLabel 原生支持渲染 Qt 富文本
（HTML 子集），无需引入第三方依赖。支持：
    **粗体** *斜体* ~~删除线~~ `行内代码` ```代码块```
    [文字](链接) 自动链接 标题 引用 无序/有序列表 换行

设计要点：先整体 HTML 转义再匹配语法标记，杜绝注入；
不识别的语法保持原样（纯文本兜底）。
"""
from __future__ import annotations

import html
import re

# 行内语法（在已转义文本上执行）
_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*|__([^_\n]+)__")
_ITALIC_RE = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])|(?<![\w_])_([^_\n]+)_(?![\w_])")
_STRIKE_RE = re.compile(r"~~([^~\n]+)~~")
# 内联图片 ![alt](任意非空白 url)。必须先于 _LINK_RE，避免 "!" 漏进链接显示名。
# 括号内不允许空白，Rocket.Chat 相对附件路径（/file-upload/…）也会命中。
_IMG_RE = re.compile(r"!\[([^\]\n]*)\]\(([^)\s]+)\)")
# 链接：文本可以是已转成 <img> 的图片标签（链接式图片 [![a](i)](url)）。
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
# 自动链接：前面不能是引号/等号/大于号（避免命中 href 属性）。
# URL 末尾常见的中文/英文闭合标点不属于链接本身，剥掉避免整段吞进 href。
_AUTOLINK_RE = re.compile(r"(?<![\w\"'=&;])(https?://[^\s<>\"'，。、；：！？]+)")
_URL_TRAIL_PUNCT_RE = re.compile(r"[.,;:!?…)）\]\}\"]+$")
# @提及：匹配 @username（不在标签内部）
_MENTION_RE = re.compile(r"(?<![\w])@([a-zA-Z0-9._\-]+)")

# 链接颜色
_LINK_COLOR = "#576b95"
# 提及颜色
_MENTION_COLOR = "#12b5f5"

# 内联图片占位尺寸（真实尺寸加载后由模型在 build_text_doc 里重写）
_INLINE_IMG_W = 240
_INLINE_IMG_H = 180


def _inline(s: str) -> str:
    s = _IMG_RE.sub(
        lambda m: '<img src="{0}" alt="{1}" width="{2}" height="{3}">'.format(
            m.group(2), m.group(1), _INLINE_IMG_W, _INLINE_IMG_H), s)
    s = _CODE_SPAN_RE.sub(
        r'<code style="background-color:rgba(0,0,0,0.06);padding:1px 3px;'
        r'border-radius:3px;">\1</code>', s)
    s = _BOLD_RE.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", s)
    s = _ITALIC_RE.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", s)
    s = _STRIKE_RE.sub(r"<s>\1</s>", s)
    s = _LINK_RE.sub(
        lambda m: f'<a href="{m.group(2)}" style="color:{_LINK_COLOR};'
                  f'text-decoration:underline;">{m.group(1)}</a>', s)
    s = _AUTOLINK_RE.sub(
        lambda m: '<a href="{0}" style="color:{1};'
                  'text-decoration:underline;">{0}</a>'.format(
                      _URL_TRAIL_PUNCT_RE.sub("", m.group(1)), _LINK_COLOR), s)
    return s


# 块级语法
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")
# 缩进保留在分组里：列表项的相对缩进决定是否嵌套。
_UL_ITEM_RE = re.compile(r"^([ \t]*)[-*+]\s+(.*)$")
_OL_ITEM_RE = re.compile(r"^([ \t]*)\d+[.)]\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s*&gt;\s?(.*)$")   # 转义后的 ">"
_FENCE_RE = re.compile(r"^\s*```\s*(\S*)\s*$")
_HR_RE = re.compile(r"^[ \t]{0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")

# 块间距全部写成内联 margin：Qt 富文本只认内联样式，默认样式表会给
# h1–h6/ul/ol 塞 12–16px 的上下边距，再叠上空行 <br> 就是大片空白。
_HEADING_MARGINS = {1: "14px 0 6px 0", 2: "12px 0 5px 0",
                    3: "10px 0 4px 0", 4: "8px 0 4px 0"}
# <font size> 用相对字号（+n），跟随气泡/面板设置的基础字号
_HEADING_SIZES = {1: 2, 2: 1, 3: 1, 4: 0}
_P_STYLE = "margin:0 0 8px 0;"
_PRE_STYLE = ("background-color:rgba(0,0,0,0.06);padding:6px;"
              "border-radius:4px;word-wrap:break-word;white-space:pre-wrap;"
              "max-width:100%;margin:4px 0 8px 0;")
_LIST_TOP_STYLE = "margin:2px 0 8px 0;"
_LIST_NESTED_STYLE = "margin:1px 0 3px 0;"
_LI_STYLE = "margin:1px 0;"


def _indent_width(prefix: str) -> int:
    return len(prefix.replace("\t", "    "))


def _match_list_item(line: str):
    """返回 (缩进宽度, 是否有序, 正文)；不是列表项返回 None。"""
    m = _UL_ITEM_RE.match(line)
    if m:
        return _indent_width(m.group(1)), False, m.group(2)
    m = _OL_ITEM_RE.match(line)
    if m:
        return _indent_width(m.group(1)), True, m.group(2)
    return None


def _is_block_start(line: str) -> bool:
    return bool(_FENCE_RE.match(line) or _HEADING_RE.match(line)
                or _QUOTE_RE.match(line) or _HR_RE.match(line)
                or _match_list_item(line))


def _heading_html(level: int, inner: str) -> str:
    size = _HEADING_SIZES[level]
    if size:
        inner = f'<font size="+{size}">{inner}</font>'
    return (f'<p style="margin:{_HEADING_MARGINS[level]};'
            f'font-weight:bold;">{inner}</p>')


def _parse_list(lines, start):
    """从 start 起收集一个列表，返回 (items, 下一行下标)。

    items 里每项是 [深度, 是否有序, 正文行]；空行后若仍是列表项就并进
    同一个列表（模型常在列表项之间空行），紧跟在列表项后的普通行按
    markdown 懒续行处理、并入上一项（否则会掉出缩进，像孤立段落）。
    """
    first = _match_list_item(lines[start])
    base_indent = first[0]
    items = []
    i = start
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            if j < n and _match_list_item(lines[j]):
                i = j
                continue
            break
        item = _match_list_item(line)
        if item:
            indent, ordered, content = item
            depth = 1 if indent <= base_indent + 1 else 2
            items.append([depth, ordered, [content]])
            i += 1
            continue
        if _is_block_start(line):
            break
        items[-1][2].append(line.strip())
        i += 1
    return items, i


def _render_list_items(items) -> str:
    parts = []
    stack = []  # [(深度, 标签)]
    for depth, ordered, body_lines in items:
        tag = "ol" if ordered else "ul"
        while stack and stack[-1][0] > depth:
            parts.append(f"</li></{stack.pop()[1]}>")
        if not stack or stack[-1][0] < depth:
            style = _LIST_TOP_STYLE if not stack else _LIST_NESTED_STYLE
            parts.append(f'<{tag} style="{style}">')
            stack.append((depth, tag))
        elif stack[-1][1] != tag:
            parts.append(f"</li></{stack.pop()[1]}>")
            style = _LIST_TOP_STYLE if not stack else _LIST_NESTED_STYLE
            parts.append(f'<{tag} style="{style}">')
            stack.append((depth, tag))
        else:
            parts.append("</li>")
        body = "<br>".join(_inline(x) for x in body_lines)
        parts.append(f'<li style="{_LI_STYLE}">{body}')
    while stack:
        parts.append(f"</li></{stack.pop()[1]}>")
    return "".join(parts)


def md_to_html(text: str) -> str:
    """markdown 文本转 HTML（Qt 富文本子集）。空文本返回空串。

    块之间用紧凑的内联 margin 表达间距，空行只作分块、不输出 <br>；
    列表项之间/内部的空行与懒续行都合并进同一个列表。
    """
    if not text or not text.strip():
        return ""
    text = html.escape(text, quote=False)
    out = []
    i = 0
    lines = text.split("\n")
    n = len(lines)
    while i < n:
        line = lines[i]

        if not line.strip():
            i += 1
            continue

        fence = _FENCE_RE.match(line)
        if fence:
            # 代码块：收集到闭合 ``` 或结尾
            i += 1
            code = []
            while i < n and not _FENCE_RE.match(lines[i]):
                code.append(lines[i])
                i += 1
            i += 1  # 跳过闭合 ```
            out.append(f'<pre style="{_PRE_STYLE}">{chr(10).join(code)}</pre>')
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            level = min(len(heading.group(1)), 4)
            out.append(_heading_html(level, _inline(heading.group(2))))
            i += 1
            continue

        if _HR_RE.match(line):
            out.append('<hr style="margin:10px 0;">')
            i += 1
            continue

        if _QUOTE_RE.match(line):
            quoted = []
            while i < n and _QUOTE_RE.match(lines[i]):
                quoted.append(_QUOTE_RE.match(lines[i]).group(1))
                i += 1
            out.append('<blockquote style="margin:4px 0 8px 0;">%s</blockquote>'
                       % "<br>".join(_inline(q) for q in quoted))
            continue

        if _match_list_item(line):
            items, i = _parse_list(lines, i)
            out.append(_render_list_items(items))
            continue

        # 段落：连续非空行用 <br> 保留手动换行，空行/块标记处截断
        para = [line.strip()]
        i += 1
        while i < n and lines[i].strip() and not _is_block_start(lines[i]):
            para.append(lines[i].strip())
            i += 1
        out.append(f'<p style="{_P_STYLE}">'
                   f'{"<br>".join(_inline(x) for x in para)}</p>')

    return "".join(out)


def looks_like_markdown(text: str) -> bool:
    """粗判是否含 markdown 语法，供调用方决定是否走富文本渲染。

    门槛过严会导致「只含 URL」的消息被当纯文本渲染，链接不高亮不可点；
    此处除了常见 md 语法，还识别裸 URL 与 [text](url) 链接。
    """
    if not text:
        return False
    return bool(re.search(
        r"(```|`[^`]+`|\*\*|~~|^\s*[-*+]\s|^\s*\d+[.)]\s|^\s*#{1,4}\s|"
        r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$|"
        r"^\s*&gt;|!\[[^\]]*\]\([^)\s]+\)|\[[^\]]+\]\(https?://[^)\s]+\)|https?://[^\s<>\"']+)",
        html.escape(text, quote=False), re.M))


def inline_image_urls(text: str) -> list:
    """解析 markdown 文本里的内联图片，返回 [dict, ...]（保持出现顺序）。

    每条 dict：
      url  : src 的未转义原文（用于 ImageLoader.resolve 补全相对路径）
      src  : 已转义、会出现在 <img src="…"> 属性里的形式（用于在 HTML 中定位）
      w, h : 当前显示尺寸占位（真实尺寸加载后由模型重写）

    与 _inline() 中 _IMG_RE 的处理保持一致：先转义再匹配。只负责收集，
    不参与 HTML 生成——图像标签由 md_to_html 输出，此函数供模型定位/加资源。
    链接式图片 [![a](i)](url) 会被 md_to_html 包成 <a href>，点击由现有
    anchor 命中处理，故这里无需解析外层链接目标。
    """
    if not text:
        return []
    escaped = html.escape(text, quote=False)
    return [
        {
            "url": html.unescape(m.group(2)),
            "src": m.group(2),
            "w": _INLINE_IMG_W,
            "h": _INLINE_IMG_H,
        }
        for m in _IMG_RE.finditer(escaped)
    ]


def highlight_mentions(html_text: str, mentions: list) -> str:
    """将 HTML 中的 @username 替换为可点击的提及标签。

    mentions: [{id, username, name}, ...]
    """
    if not mentions:
        return html_text
    # 构建 username -> 显示名 映射
    name_map = {}
    for m in mentions:
        uname = m.get("username", "")
        if uname:
            display = m.get("name") or uname
            name_map[uname] = display
    if not name_map:
        return html_text

    def _replace(m):
        uname = m.group(1)
        if uname in name_map:
            display = html.escape(name_map[uname])
            return (f'<a href="mention:{html.escape(uname)}" '
                    f'style="color:{_MENTION_COLOR};font-weight:bold;">'
                    f'@{display}</a>')
        return m.group(0)

    # 只在标签外的文本部分替换（避免破坏已有 HTML 标签）
    return _replace_outside_tags(html_text, _MENTION_RE, _replace)


def apply_mentions_plain(text: str, mentions: list) -> str:
    """将纯文本中的 @username 替换为提及 HTML 标签（非 markdown 消息用）。"""
    if not mentions:
        return ""
    name_map = {}
    for m in mentions:
        uname = m.get("username", "")
        if uname:
            display = m.get("name") or uname
            name_map[uname] = display
    if not name_map:
        return ""

    escaped = html.escape(text)

    def _replace(m):
        uname = m.group(1)
        if uname in name_map:
            display = html.escape(name_map[uname])
            return (f'<a href="mention:{html.escape(uname)}" '
                    f'style="color:{_MENTION_COLOR};font-weight:bold;">'
                    f'@{display}</a>')
        return m.group(0)

    return _MENTION_RE.sub(_replace, escaped)


def _replace_outside_tags(s: str, pattern: re.Pattern, repl) -> str:
    """在 HTML 字符串中仅对标签外的文本部分执行正则替换。"""
    parts = []
    pos = 0
    tag_re = re.compile(r"<[^>]+>")
    for tag_m in tag_re.finditer(s):
        # 标签前的文本部分
        before = s[pos:tag_m.start()]
        parts.append(pattern.sub(repl, before))
        # 标签本身保持不变
        parts.append(tag_m.group(0))
        pos = tag_m.end()
    # 最后一段
    parts.append(pattern.sub(repl, s[pos:]))
    return "".join(parts)
