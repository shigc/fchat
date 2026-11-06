"""消息 JSON 解析：REST 与 Realtime 共用。

Rocket.Chat 消息 JSON -> Message 模型。处理：
- 附件类型细分：图片 / 视频 / 语音 / 文件 / 引用
- 系统消息（m.t 字段）转中文描述
- 表情回应（reactions）保留
"""
from __future__ import annotations

import re
from typing import Optional

import emoji

from .models import Message, User, json_ts_to_epoch

# 匹配 :shortcode: 模式（如 :smile:、:thumbsup:）
_SHORTCODE_RE = re.compile(r":([a-zA-Z0-9_+-]+):")

# emojione / Rocket.Chat 的肤色短码后缀（:ok_hand_tone5:）：emoji 库只收录
# CLDR 名（:OK_hand_dark_skin_tone:），不认这套方言，直接交给它会把肤色
# 回应原样留在气泡/回应 chip 里（真机截图）。这里拆出 _toneN
# 自己拼 Unicode 肤色修饰符；拼出的组合不在 emoji 库中说明该字符不支持
# 肤色（如火箭），保留原文，别拼出「emoji + 色块」乱码。
_TONE_RE = re.compile(r"^(?P<base>.+?)_tone(?P<tone>[1-5])$")
_SKIN_TONE_MODIFIERS = {
    "1": "\U0001F3FB",
    "2": "\U0001F3FC",
    "3": "\U0001F3FD",
    "4": "\U0001F3FE",
    "5": "\U0001F3FF",
}
_TONE_NUMBER_BY_MODIFIER = {v: k for k, v in _SKIN_TONE_MODIFIERS.items()}
# CLDR 肤色名后缀 -> tone 号（长后缀在前：medium-dark 也以 dark 结尾）
_CLDR_TONE_SUFFIXES = (
    ("_medium-light_skin_tone", "2"),
    ("_medium-dark_skin_tone", "4"),
    ("_light_skin_tone", "1"),
    ("_medium_skin_tone", "3"),
    ("_dark_skin_tone", "5"),
)
# 短码名 -> 字符的兜底表，首次遇到 emoji 库不认的短码时才建表
_EMOJI_NAME_MAP = None

# E2E 解密失败占位文案：主窗口改密码成功后按它定位要刷新的占位消息
E2E_PLACEHOLDER_TEXT = "无法解密的加密消息"

# 消息永久链接（引用跳转），如 https://host/direct/<rid>?msg=<mid>；
# 也兼容本应用发送的相对链 /direct/<rid>?msg=<mid>（前缀可省略）
_QUOTE_LINK_RE = re.compile(
    r"(?:https?://[^/\s]+)?/(?:direct|channel|group|c|p|d)/[\w-]+\?msg=([\w-]+)")

# ---------------------------------------------------------------------------
# 转发标记（发送端 ui/forward_dialog 与本文件解析共用，别让两边各写一套）
#
# 本应用在转发出去的消息体首行嵌入一个可读标记，接收端据此识别成
# 「合并转发聊天记录」/「逐条转发」，从而渲染成微信式卡片/标记。标记后的
# 主体仍是可读文本，Rocket.Chat 网页版能看到发送人与内容，不会白屏。
# ---------------------------------------------------------------------------
MERGE_HEAD = "⟳[聊天记录]"            # 合并转发汇总消息首行前缀
FORWARD_HEAD = "⟳[转发消息]"          # 逐条转发消息首行前缀
_MERGE_SEP = "\u001f"                 # 合并记录明细字段分隔符（内容转义后无此字符）
_MERGE_ESCAPE = "\u001e"              # 内容里转义 US 用的 RS：US->RS, RS->RS+RS


def _merge_escape(s: str) -> str:
    return s.replace(_MERGE_SEP, _MERGE_ESCAPE).replace(_MERGE_ESCAPE,
                                                        _MERGE_ESCAPE * 2)


def _merge_unescape(s: str) -> str:
    return s.replace(_MERGE_ESCAPE * 2, _MERGE_ESCAPE).replace(
        _MERGE_ESCAPE, _MERGE_SEP)


def _parse_merge_body(text: str) -> list:
    """把合并转发消息体解析成 forward_list:
    [{sender, ts, ts_text, text, att}]；无法解析时返回空列表。

    ts 字段兼容两种形态：旧卡片的 epoch 秒（纯数字）→ 记入 ts；新卡片的
    可读文本（format_chat_time 输出）→ 记入 ts_text，ts 置 0。
    """
    lines = text.splitlines()
    if not lines or not lines[0].startswith(MERGE_HEAD):
        return []
    out = []
    for ln in lines[1:]:
        if not ln.startswith(_MERGE_SEP):
            continue
        parts = ln.split(_MERGE_SEP)
        if len(parts) < 5:
            continue
        # 结构: sep + attflag + sender + ts + text + attjson(可选)
        _, att_flag, sender, ts, content = parts[:5]
        entry = {"sender": _merge_unescape(sender),
                 "ts": float(ts) if ts.replace(".", "", 1).replace("-", "", 1).isdigit() else 0.0,
                 "ts_text": "",
                 "text": _merge_unescape(content), "att": None}
        if not entry["ts"]:
            entry["ts_text"] = ts
        if att_flag == "1" and len(parts) >= 6:
            import json as _json
            try:
                entry["att"] = _json.loads(_merge_unescape(parts[5]))
            except Exception:
                entry["att"] = None
        out.append(entry)
    return out


def _forward_list_from_attachments(attachments: list) -> list:
    """从方案二附件卡片数组重建 forward_list（与 _parse_merge_body 同构）。

    方案二（核心/messages.build_forward_payload_many 的 merge=True）把合并转发
    写进 attachments：正文 text 只留「⟳[聊天记录]共N条」，每条子消息一张
    Rocket.Chat 附件卡片 {color, title, text, collapsed}，其中
    title = "@发送人 · 时间文本"，text = 子消息正文。
    图片子消息的卡片额外带 image_url（图片真实地址），text 为
    "[右键打开链接查看原图](image_url)"；轮廓文件的卡片 text 为
    "[文件名](完整URL)" markdown 引用。
    返回 [{sender, ts, ts_text, text, att}]。图片卡片的 att 携带
    {"image_url": ..., "title": ...} 供渲染器识别为图片条目；其余 att 为 None
    （附件已编码进 text 的 markdown 引用）。
    """
    if not attachments:
        return []
    out = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        title = str(att.get("title") or "").strip()
        body = str(att.get("text") or "")
        sender = ""
        ts_text = ""
        # title 形如「@发送人 · 时间」，用最后一个「 · 」拆分
        if title.startswith("@"):
            rest = title[1:]
            if " · " in rest:
                sender, _, ts_text = rest.rpartition(" · ")
            else:
                sender = rest
        entry_att = None
        image_url = att.get("image_url") or att.get("imageUrl")
        if image_url:
            entry_att = {"image_url": str(image_url), "title": title}
        out.append({"sender": sender, "ts": 0.0, "ts_text": ts_text,
                    "text": body, "att": entry_att})
    return out


def _cldr_name_to_rc(name: str) -> str:
    """CLDR 肤色名（:OK_hand_dark_skin_tone:）转 emojione 的 _toneN。"""
    for cldr, tone in _CLDR_TONE_SUFFIXES:
        if name.endswith(cldr):
            return name[:-len(cldr)] + f"_tone{tone}"
    return ""


def _emoji_name_map() -> dict:
    """短码名（小写、不含冒号）-> 字符；含 CLDR 肤色名转成的 _toneN。

    emoji 库不认的短码才走这张表兜底。ZWJ 组合的肤色（如
    :woman_astronaut_tone5:）在库里是独立条目，不能靠基字符末尾拼
    修饰符还原，必须整名查表。
    """
    global _EMOJI_NAME_MAP
    if _EMOJI_NAME_MAP is not None:
        return _EMOJI_NAME_MAP
    index = {}
    for char, data in emoji.EMOJI_DATA.items():
        if not isinstance(data, dict):
            continue
        names = [data.get("en") or ""]
        names.extend(data.get("alias") or [])
        for name in names:
            name = str(name or "").strip(":").lower()
            if not name:
                continue
            index.setdefault(name, char)
            rc_name = _cldr_name_to_rc(name)
            if rc_name:
                index.setdefault(rc_name, char)
    _EMOJI_NAME_MAP = index
    return index


def _shortcode_char(name: str) -> str:
    """短码名（不含冒号）转 Unicode；认不出返回空串。"""
    name = str(name or "").strip(":")
    if not name:
        return ""
    try:
        char = emoji.emojize(f":{name}:", language="alias")
    except Exception:  # noqa: BLE001
        return ""
    if char != f":{name}:":
        return char
    match = _TONE_RE.match(name)
    if match is None:
        return ""
    known = _emoji_name_map().get(name.lower())
    if known:
        return known
    base = _shortcode_char(match.group("base"))
    if not base:
        return ""
    # 部分基字符自带 VS16（:heart: -> ❤️），肤色修饰符要直接跟基字符
    combined = (base.replace("\ufe0f", "")
                + _SKIN_TONE_MODIFIERS[match.group("tone")])
    return combined if combined in emoji.EMOJI_DATA else ""


def emojize(text: str) -> str:
    """把文本中的 :shortcode: 转为 Unicode emoji；无匹配时原样返回。

    逐 token 解析而不是整段交给 emoji.emojize：后者不认 emojione 的肤色
    后缀，整段转换会把 :ok_hand_tone5: 这类回应原样留下。认不出的 token
    （含纯文本里的 12:30:45）保持原文。
    """
    if not text or ":" not in text:
        return text

    def _sub(match):
        return _shortcode_char(match.group(1)) or match.group(0)

    try:
        return _SHORTCODE_RE.sub(_sub, text)
    except Exception:  # noqa: BLE001
        return text

# 系统消息类型 -> 中文描述模板（{user} 为动作对象；au/ru 另有 {operator}
# 操作者）。真机实测：au 的 u=添加人（u.name 是其真实姓名）、
# msg=被添加者 username；ru 假定同构，若真机发现方向相反再调
_SYSTEM_TEMPLATES = {
    "uj": "{user} 加入频道",
    "ul": "{user} 离开频道",
    "au": "{user} 被 {operator} 添加到频道",
    "ru": "{user} 被 {operator} 移出频道",
    "message_pinned": "{user} 置顶了一条消息",
    "message_pinned_attachment": "{user} 置顶了一条消息",
    "subscription-role-added": "{user} 的角色已更新",
    "subscription-role-removed": "{user} 的角色已更新",
    "room-archived": "频道已被归档",
    "room-unarchived": "频道已被取消归档",
    "room-name-changed": "{user} 修改了频道名",
    "room-announcement": "{user} 设置了频道公告",
    "room-topic-changed": "{user} 修改了频道主题",
    "user-muted": "{user} 已被禁言",
    "user-unmuted": "{user} 已解除禁言",
    # E2E 开关系统消息：服务端 msg 字段是操作者 username（真机实测）
    "room_e2e_enabled": "{user} 开启了端到端加密",
    "room_e2e_disabled": "{user} 关闭了端到端加密",
}

RECALL_TEMPLATE = "{user} 撤回了一条消息"


# 撤回类系统消息：msg 字段是原消息内容，不能当动作对象用
_RECALL_TYPES = ("rm", "message_removed")


def _sender_from_json(m: dict, server_url: str) -> User:
    u = m.get("u", {}) or {}
    sender = User(
        id=u.get("_id", ""),
        username=u.get("username", ""),
        name=u.get("name", ""),
    )
    if sender.username and server_url:
        sender.avatar_url = f"{server_url}/avatar/{sender.username}"
    return sender


def _system_display(template: str, sender: User, actor: str):
    """计算系统消息的展示文本与待解析占位符。

    返回 (text, pending)。动作对象是 u 本人时（actor 为空或等于
    u.username）本地就能用 u.name 渲染；第三方目标（au/ru 的 msg）
    本地没有其资料，先显示 username 并记入 pending（占位符 ->
    username），由 UI 层异步解析真实姓名后按模板重排。
    """
    pending = {}

    def _disp(ph: str, username: str, known_name: str) -> str:
        if known_name:
            return known_name
        if username:
            pending[ph] = username
            return username
        return "?"

    fmt = {}
    if "{operator}" in template:
        # 操作者就是 u（au/ru 同构，见 _SYSTEM_TEMPLATES 注释）
        fmt["operator"] = _disp("operator", sender.username, sender.name)
    own_action = not actor or actor == sender.username
    fmt["user"] = _disp("user", actor or sender.username,
                        sender.name if own_action else "")
    return template.format(**fmt), pending


def _parse_system(m: dict, sender: User) -> Optional[Message]:
    t = m.get("t", "")
    if not t:
        return None
    template = _SYSTEM_TEMPLATES.get(t)
    if template is None:
        return None
    # uj/ul 等：msg 字段是动作对象的 username（见 _system_display）
    actor = (m.get("msg") or "").strip()
    text, pending = _system_display(template, sender, actor)
    return Message(
        id=m.get("_id", ""),
        text=text,
        sender=sender,
        timestamp=json_ts_to_epoch(m.get("ts")),
        msg_type="system",
        system_template=template,
        system_names=pending,
    )


def format_system_text(msg, resolved: dict = None) -> str:
    """姓名解析后按模板重排系统消息文本（UI 层回调用）。

    resolved: 占位符 -> 真实姓名，与 msg.system_resolved 累积合并。
    msg.system_names 之外的占位符都是 u 本人的展示（解析层已用 u.name
    渲染过），回排时用 sender 兜底。模板丢失或占位符异常时返回原文本。
    """
    if not msg.system_template:
        return msg.text
    known = dict(msg.system_resolved or {})
    if resolved:
        known.update(resolved)
    names = {}
    for ph, uname in (msg.system_names or {}).items():
        names[ph] = known.get(ph) or uname
    sender_disp = ""
    if msg.sender is not None:
        sender_disp = msg.sender.name or msg.sender.username
    for ph in re.findall(r"\{(\w+)\}", msg.system_template):
        names.setdefault(ph, sender_disp)
    try:
        return msg.system_template.format(**names)
    except (KeyError, IndexError, ValueError):
        return msg.text


def _parse_attachment(m: dict, attachments: list) -> dict:
    """从第一个附件提取类型与字段，返回供 Message 使用的 kwargs 片段。"""
    first = attachments[0] or {}
    out = {
        "file_url": "",
        "cover_url": "",
        "file_name": "",
        "file_size": 0,
        "description": str(first.get("description", "") or ""),
        "msg_type": "text",
        "quote_msg_id": "",
        "quote_author": "",
        "quote_link": "",
        "quote_image_url": "",
        "quote_file_name": "",
    }
    image_url = first.get("image_url") or first.get("imageUrl") or ""
    video_url = first.get("video_url") or ""
    audio_url = first.get("audio_url") or ""
    message_link = str(first.get("message_link", "") or "")
    title_link = first.get("title_link") or ""
    title = str(first.get("title", "") or "")
    author = str(first.get("author_name", "") or "")
    att_text = str(first.get("text", "") or "")

    if video_url:
        # 注意：视频附件同时带 image_url（封面缩略图），必须先判断 video
        out["msg_type"] = "video"
        out["file_url"] = video_url
        out["cover_url"] = image_url  # 视频封面
        out["file_name"] = title
    elif audio_url:
        out["msg_type"] = "audio"
        out["file_url"] = audio_url
        out["file_name"] = title
    elif image_url:
        out["msg_type"] = "image"
        out["file_url"] = image_url
    elif message_link and (att_text or author):
        # 引用回复（网页版带 message_link，本应用自带 permalink）。
        # 网页版引用附件的 text 常为空、只有 author_name，不能要求 text 非空
        out["msg_type"] = "quote"
        out["description"] = att_text
        out["quote_author"] = author
        out["quote_link"] = message_link
        link = _QUOTE_LINK_RE.search(message_link)
        if link:
            out["quote_msg_id"] = link.group(1)
        # 网页版引用的原始消息附件嵌在子 attachments 里：图片/视频封面
        # 带 image_url，文件带 title/title_link，供引用块画缩略图/文件行
        for sub in first.get("attachments") or []:
            if not isinstance(sub, dict):
                continue
            sub_img = sub.get("image_url") or sub.get("imageUrl") or ""
            if sub_img and not out["quote_image_url"]:
                out["quote_image_url"] = str(sub_img)
                # 图文混排被引用时父附件 text 恒为空、说明在子附件
                # description——不取这段，引用块就只剩一张缩略图
                sub_desc = str(sub.get("description", "") or "").strip()
                if not out["description"] and sub_desc:
                    out["description"] = sub_desc
            elif not sub_img and sub.get("title_link") \
                    and not out["quote_file_name"]:
                out["quote_file_name"] = str(sub.get("title", "") or "")
    elif title_link:
        out["msg_type"] = "file"
        out["file_url"] = title_link
        out["file_name"] = title
    elif att_text and (title or author):
        # 无链接的 text+title/author：引用回复/自定义内容
        out["msg_type"] = "quote"
        out["description"] = att_text
        out["quote_author"] = author
    else:
        # 无法识别的附件，保留文本兜底
        out["description"] = att_text
    return out


def _file_meta(m: dict, att: dict, file_name: str, file_size: int):
    """补充文件名与大小（优先消息顶层 m.file，其次附件内的 file）。"""
    f = m.get("file") or (att.get("file") if isinstance(att, dict) else None) or {}
    if isinstance(f, dict):
        if not file_name:
            file_name = f.get("name", "") or ""
        if not file_size:
            try:
                file_size = int(f.get("size", 0) or 0)
            except (TypeError, ValueError):
                file_size = 0
    return file_name, file_size


def _format_reactions(reactions: dict) -> str:
    """把 reactions dict 格式化为 '[👍 x2] [🎉 x1]' 风格的简短文本。"""
    if not isinstance(reactions, dict) or not reactions:
        return ""
    parts = []
    for emoji, info in reactions.items():
        try:
            names = info.get("usernames", []) if isinstance(info, dict) else []
            parts.append(f"{emoji} x{len(names)}" if names else str(emoji))
        except AttributeError:
            continue
    return "  ".join(parts)


def _is_inline_image(raw: str, url: str) -> bool:
    """raw markdown 是否以 ![..](url) 内联图片形式引用了该 url。

    Rocket.Chat 的 urls 字段对 [..](url) 文件链接与 ![..](url) 内联图片都会生成，
    二者 URL 相同，只能靠正文里的 "!" 区分——文件链接不是内联图片，不应作为图片气泡渲染。
    """
    for ref in re.findall(r"!\[[^\]\n]*\]\(([^)\s]+)\)", raw):
        if ref.strip().rstrip("/") == url.strip().rstrip("/"):
            return True
    return False


def mention_scope(mentions: list) -> str:
    """判定消息的 @ 范围：'' 无提及 / 'user' @具体人 / 'all' @所有人或@在线。

    服务端把 @all / @here 也放进 mentions 数组（_id/username 为 "all"/"here"），
    与 @具体人混在一起，需要单独区分。"""
    for m in mentions or []:
        ident = str(m.get("id", "")).lower()
        uname = str(m.get("username", "")).lower()
        if ident in ("all", "here") or uname in ("all", "here"):
            return "all"
    return "user" if mentions else ""


def parse_message(m: dict, room_id: str,
                  my_user_id: str = "",
                  server_url: str = "") -> Message:
    """把 Rocket.Chat 消息 JSON 解析为 Message。"""
    sender = _sender_from_json(m, server_url)
    text = emojize((m.get("msg") or "").strip())
    attachments = m.get("attachments") or []
    urls = m.get("urls") or []
    is_recall = m.get("t", "") in _RECALL_TYPES

    # 系统消息
    system = None if is_recall else _parse_system(m, sender)
    if system:
        system.room_id = room_id
        system.is_mine = bool(my_user_id and sender.id == my_user_id)
        return system

    kwargs = {
        "file_url": "",
        "cover_url": "",
        "file_name": "",
        "file_size": 0,
        "description": "",
        "msg_type": "text",
        "quote_msg_id": "",
        "quote_author": "",
        "quote_link": "",
        "quote_image_url": "",
        "quote_file_name": "",
    }
    if attachments:
        kwargs.update(_parse_attachment(m, attachments))
        kwargs["file_name"], kwargs["file_size"] = _file_meta(
            m, attachments[0] or {}, kwargs["file_name"], kwargs["file_size"])
    elif urls:
        # 旧版图片（urls 字段）：仅当正文以 ![..](url) 内联图片形式引用该 URL
        # 时才归类为图片。若只是 [..](url) 文件链接（rooms.media 上传的下载项，
        # 官方客户端按链接展示），保持文本链接，否则链接文字会被当图片吞掉。
        u = urls[0].get("url", "")
        if u.lower().endswith(
                (".png", ".jpg", ".jpeg", ".gif", ".webp")) \
                and _is_inline_image(m.get("msg") or "", u):
            kwargs["msg_type"] = "image"
            kwargs["file_url"] = u

    if not text and kwargs["msg_type"] == "image":
        text = "图片"

    # 提取 @提及列表
    mentions_raw = m.get("mentions") or []
    mentions = [
        {"id": x.get("_id", ""), "username": x.get("username", ""),
         "name": x.get("name", "")}
        for x in mentions_raw if isinstance(x, dict)
    ]

    # 转发标记识别：合并转发（首行 MERGE_HEAD）→ msg_type="merge" +
    # forward_list；逐条转发（首行 FORWARD_HEAD）→ is_forwarded +
    # forward_author。标记行仅用于识别，不进入正文展示。
    is_forwarded = False
    forward_author = ""
    forward_list = []
    head_line = text.splitlines()[0] if text else ""
    if head_line.startswith(MERGE_HEAD):
        kwargs["msg_type"] = "merge"
        # 方案二：合并转发走 attachments 卡片数组；旧格式（纯文本卡片，无附件）
        # 仍走 _parse_merge_body 向后兼容。
        if attachments:
            forward_list = _forward_list_from_attachments(attachments)
        else:
            forward_list = _parse_merge_body(text)
    elif head_line.startswith(FORWARD_HEAD):
        is_forwarded = True
        forward_author = (head_line[len(FORWARD_HEAD):].lstrip(" ·-")
                          .split()[0] if len(head_line) > len(FORWARD_HEAD) else "")
        # 逐条转发标记行不出现在气泡正文
        text = "\n".join(text.splitlines()[1:]).strip()

    is_mine = bool(my_user_id and sender.id == my_user_id)

    # E2E 消息解密失败/密钥未就绪：t 与 content 原样保留、msg 为空，解密
    # 成功路径会把 t 剥掉，所以 t 还在就说明没解开。不能落入下面的空文本
    # 分支——那会把"解不开的密文"误显示成"撤回了一条消息"（真机踩过）
    if m.get("t") == "e2e":
        return Message(
            id=m.get("_id", ""),
            room_id=room_id,
            text=E2E_PLACEHOLDER_TEXT,
            sender=sender,
            timestamp=json_ts_to_epoch(m.get("ts")),
            msg_type="system",
            is_mine=is_mine,
        )

    # 撤回消息（t=rm）msg 字段是原文，不能当正常文本展示；完全空的消息
    # 多为撤回的残留，统一转成系统消息，避免渲染出空白小气泡
    if is_recall or (kwargs["msg_type"] == "text" and not text
                     and not attachments and not kwargs["file_url"]):
        if is_mine:
            return Message(
                id=m.get("_id", ""),
                room_id=room_id,
                text="你撤回了一条消息",
                sender=sender,
                timestamp=json_ts_to_epoch(m.get("ts")),
                msg_type="system",
                is_mine=True,
            )
        # t=rm 的 u 是执行撤回的人（真机实测 u.name 是其真实姓名）；
        # u 无 username 时连异步解析都没法做，只能显示"对方"
        if not sender.username:
            return Message(
                id=m.get("_id", ""),
                room_id=room_id,
                text="对方 撤回了一条消息",
                sender=sender,
                timestamp=json_ts_to_epoch(m.get("ts")),
                msg_type="system",
                is_mine=False,
            )
        text, pending = _system_display(RECALL_TEMPLATE, sender,
                                        sender.username)
        return Message(
            id=m.get("_id", ""),
            room_id=room_id,
            text=text,
            sender=sender,
            timestamp=json_ts_to_epoch(m.get("ts")),
            msg_type="system",
            is_mine=False,
            system_template=RECALL_TEMPLATE,
            system_names=pending,
        )

    # permalink 处理：纯链接文本本身是引用消息；引用附件场景 msg 字段里
    # 也可能带 permalink（网页版），必须剥掉，否则原始 URL 出现在回复正文。
    # 网页版把 permalink 包在空 label 的 markdown 链接里（[ ](permalink)），
    # 只剥裸链接会留下 "[ ]()" 残壳，要连壳一起剥
    link = _QUOTE_LINK_RE.search(text)
    if link:
        if kwargs["msg_type"] == "text":
            kwargs["msg_type"] = "quote"
        if not kwargs["quote_msg_id"]:
            kwargs["quote_msg_id"] = link.group(1)
        if not kwargs["quote_link"]:
            kwargs["quote_link"] = link.group(0)
        wrapped = re.search(
            r"\[[^\]\n]*\]\(\s*" + re.escape(link.group(0)) + r"\s*\)", text)
        text = text.replace(
            wrapped.group(0) if wrapped else link.group(0), "").strip()

    return Message(
        id=m.get("_id", ""),
        room_id=room_id,
        text=text,
        sender=sender,
        timestamp=json_ts_to_epoch(m.get("ts")),
        msg_type=kwargs["msg_type"],
        attachments=attachments,
        is_mine=is_mine,
        file_url=kwargs["file_url"],
        cover_url=kwargs["cover_url"],
        file_name=kwargs["file_name"],
        file_size=kwargs["file_size"],
        description=kwargs["description"],
        quote_msg_id=kwargs["quote_msg_id"],
        quote_author=kwargs["quote_author"],
        quote_link=kwargs["quote_link"],
        quote_image_url=kwargs["quote_image_url"],
        quote_file_name=kwargs["quote_file_name"],
        is_forwarded=is_forwarded,
        forward_author=forward_author,
        forward_list=forward_list,
        reactions=m.get("reactions") or {},
        mentions=mentions,
        # 缺省 editedAt 时不能走 json_ts_to_epoch——None 会退化成
        # time.time()，未编辑消息会被误标成「已编辑」
        edited_at=(json_ts_to_epoch(m.get("editedAt"))
                   if m.get("editedAt") else 0.0),
        starred=bool(m.get("starred")),
        pinned=bool(m.get("pinned")),
        mentions_me=bool(my_user_id) and any(
            str(x.get("id", "")) == str(my_user_id) for x in mentions),
        mention_all=mention_scope(mentions) == "all",
    )


def format_reactions(reactions: dict) -> str:
    """供 UI 层格式化表情回应标签。"""
    return _format_reactions(reactions)


def reaction_display(key: str) -> str:
    """回应短码转 Unicode（:thumbsup: -> 👍）；已是 Unicode 原样返回。"""
    key = str(key or "")
    if not key or ":" not in key:
        return key
    return emojize(key)


def rc_shortcode(char: str) -> str:
    """Unicode emoji 转 Rocket.Chat 短码；带肤色时用 _toneN 后缀。

    发 chat.react 前统一走这里：服务端按 emojione 短码表校验，而
    emoji.demojize 对肤色给的是 CLDR 名（:OK_hand_dark_skin_tone:），
    服务端不认，:ok_hand_tone5: 才是 Rocket.Chat 的方言。
    已是短码的原样返回；认不出的字符也原样返回，交服务端判断。
    """
    char = str(char or "")
    if not char or char.startswith(":"):
        return char
    tones = [_TONE_NUMBER_BY_MODIFIER[c] for c in char
             if c in _TONE_NUMBER_BY_MODIFIER]
    if len(tones) > 1:
        # 多人组合 emoji 的肤色要按人拆成多个后缀，没有可靠映射，
        # 宁可原样发字符也不发错短码
        return char
    base = "".join(c for c in char if c not in _TONE_NUMBER_BY_MODIFIER)
    try:
        name = emoji.demojize(base, language="alias")
    except Exception:  # noqa: BLE001
        return char
    if not (name.startswith(":") and name.endswith(":")):
        return char
    name = name[1:-1].lower()
    # ZWJ / 多人 emoji 会被拆成多个短码（:woman::rocket:），
    # 服务端的单码表对不上
    if not name or ":" in name:
        return char
    if tones:
        name += f"_tone{tones[0]}"
    return f":{name}:"


def preview_text(msg: Message) -> str:
    """会话列表预览正文（不含发送人）。

    媒体只给类型占位或文件名：列表只有一行，图片说明/合并记录明细
    塞进来会被截成半句。系统消息原样返回（文本自带动作人）。
    """
    if msg.msg_type == "image":
        return "[图片]"
    if msg.msg_type == "video":
        return ("[视频] " + (msg.file_name or "")).rstrip()
    if msg.msg_type == "audio":
        return ("[语音] " + (msg.file_name or "")).rstrip()
    if msg.msg_type == "file":
        return ("[文件] " + (msg.file_name or msg.text or "")).rstrip()
    if msg.msg_type == "quote":
        return ("[引用] " + msg.text) if msg.text else "[引用]"
    return msg.text


def preview_with_sender(msg: Message, is_dm: bool = False) -> str:
    """会话列表预览：频道/群加「发送人: 」前缀，一对一私聊不加。

    REST 摘要（core.api_client）与实时/历史（ui.main_window）必须共用
    这一条规则：此前两条路径各写一份，同一房间的显示格式取决于最后
    更新走的是哪条路径，列表里出现有的带发送人、有的不带。
    一对一私聊对方固定、前缀是噪音；多人私聊（Conversation.is_multi_dm）
    与频道一样要带前缀，调用方按 is_dm 传「是否一对一」。
    系统消息文本已带动作人（「张三 离开频道」），重复前缀会变成
    「张三: 张三 离开频道」。发送人显示真实姓名（u.name），缺失才退回
    username；自己发的显示「我」。
    """
    text = preview_text(msg)
    if is_dm or msg.msg_type == "system" or not text.strip():
        return text
    if msg.is_mine:
        return f"我: {text}"
    sender = msg.sender
    name = (sender.name or sender.username) if sender is not None else ""
    return f"{name}: {text}" if name else text
