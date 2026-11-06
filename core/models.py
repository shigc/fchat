"""核心数据模型：Conversation / Message / User。

这些模型是纯数据容器，不绑定任何 UI 或网络实现，方便在各个层之间传递。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class RoomType(str, Enum):
    CHANNEL = "c"       # 公开频道 #
    GROUP = "p"         # 私有频道（官方 private channel）
    DM = "d"            # 私聊
    LIVE = "l"          # livechat（预留）


# 官方房间类型 → 中文显示名。顶栏与详情用；私聊（d）在 UI 上通常不贴类型标签。
# 注意：团队主房间的 t 仍是 c/p，UI 标签要结合 is_team 区分（ui.widgets.room_type_label）
ROOM_TYPE_CN = {
    RoomType.CHANNEL: "公开频道",
    RoomType.GROUP: "私有频道",
    RoomType.DM: "私聊",
    RoomType.LIVE: "客服",
}


def room_type_cn(room_type: str) -> str:
    """把房间类型代码转成官方中文名；未知类型回退为原值。"""
    try:
        return ROOM_TYPE_CN.get(RoomType(room_type), str(room_type))
    except ValueError:
        # 未知类型（例如讨论串的 t / 服务器私有扩展）不在枚举里，原样回退
        return str(room_type)


@dataclass
class User:
    """Rocket.Chat 用户。在多对多会话中，一次只关心"联系人"这一侧。"""
    id: str = ""
    username: str = ""
    name: str = ""          # 显示名，可能为空
    avatar_url: str = ""    # 头像 URL（不一定有）
    department: str = ""    # 部门/组织信息（来自 customFields.department，可能为空）

    @property
    def display_name(self) -> str:
        return self.name or self.username or self.id

    def to_dict(self):
        return {
            "_id": self.id,
            "username": self.username,
            "name": self.name,
            "department": self.department,
        }


@dataclass
class Message:
    id: str = ""
    room_id: str = ""
    text: str = ""
    sender: Optional[User] = None
    timestamp: float = 0.0          # epoch 秒
    # "text"/"image"/"file"/"audio"/"video"/"quote"/"system"
    msg_type: str = "text"
    attachments: List[dict] = field(default_factory=list)
    is_mine: bool = False
    file_url: str = ""              # 附件/图片 URL（若有）
    cover_url: str = ""            # 视频封面图 URL（若有）
    file_name: str = ""            # 附件文件名（若有）
    file_size: int = 0             # 附件字节数（若有）
    description: str = ""         # 附件描述/引用原文本
    reactions: dict = field(default_factory=dict)   # emoji -> {usernames: [...]}
    status: str = "sent"          # "sending" / "sent" / "failed"
    mentions: List[dict] = field(default_factory=list)  # [{id, username, name}]
    quote_msg_id: str = ""        # 被引用消息 id（可能跨房间），用于跳转/补抓
    quote_author: str = ""        # 引用块显示的作者名
    quote_link: str = ""          # 原始 permalink，转发引用时带上供收方跳转
    # 引用块媒体展示：被引用的是图片/视频时画缩略图（微信式），文件时画
    # 图标 + 文件名；文本引用仍走纯文本块
    quote_image_url: str = ""     # 被引用图片/视频封面的 URL（可为相对路径）
    quote_file_name: str = ""     # 被引用文件的文件名
    # 转发语义：由 core.messages 从消息标记解析。msg_type 仍保持原类型，
    # is_forwarded 表示这是逐条转发，收到端显示「转发的消息」标记；
    # msg_type=="merge" 时 forward_list 是合并转发的聊天记录明细。
    is_forwarded: bool = False
    forward_author: str = ""      # 逐条转发：原始发送人展示名
    forward_list: List[dict] = field(default_factory=list)
    # merge 明细每项: {"sender": str, "ts": float, "ts_text": str, "text": str,
    #                  "att": Optional[dict]（首个附件）}
    # 系统消息姓名解析：text 是 system_template 用本地已知姓名渲染的结果；
    # system_names 是仍显示 username 的占位符 -> username（如 au 的被添加人，
    # 本地拿不到姓名），UI 层异步解析后按模板重排文本。解析结果累积在
    # system_resolved（占位符 -> 真实姓名），system_names 保持不变，
    # 后续占位符再解析时不会丢掉先解析出的名字
    system_template: str = ""     # 含 {user}/{operator} 占位符的原始模板
    system_names: dict = field(default_factory=dict)
    system_resolved: dict = field(default_factory=dict)
    # 编辑/置顶/星标：realtime 同 id 重推时靠这些字段原位刷新
    edited_at: float = 0.0
    starred: bool = False
    pinned: bool = False
    # @ 提及：解析时按当前登录用户判定一次，UI 不再各自比对 username
    mentions_me: bool = False
    mention_all: bool = False

    @property
    def display_text(self) -> str:
        if self.msg_type == "merge":
            return ("[聊天记录] " + (self.text or "")
                    if self.text else "[聊天记录]")
        if self.msg_type == "file":
            return "[文件] " + (self.file_name or self.text or "文件")
        if self.msg_type == "image":
            # 官方客户端的图文混排 msg 为空、说明在附件 description 里；
            # text 的「图片」是本应用的无说明占位，不当真说明
            cap = self.text if self.text and self.text != "图片" else ""
            return "[图片] " + (cap or self.description or "图片")
        if self.msg_type == "video":
            return "[视频] " + (self.file_name or self.text or "视频")
        if self.msg_type == "audio":
            return "[语音] " + (self.file_name or self.text or "语音")
        if self.msg_type == "quote":
            return self.text or "[引用]"
        return self.text


@dataclass
class DirectoryEntry:
    """目录面板条目（频道/用户/团队）。

    各 list 端点字段名不一致（尤其 teams.list），归一化在 api_client 解析层
    完成，UI 只看这里的稳定字段。计数类字段 -1 / 时间 0.0 表示服务端没给，
    UI 显示"—"。
    """
    kind: str            # "channel" / "user" / "team"
    rid: str             # 房间 id（team=主房间 roomId；user 为空）
    name: str = ""
    username: str = ""
    department: str = ""     # 用户条目专用（customFields.department）
    status: str = ""         # 用户条目专用（users.list 原样透传；directory 端点无此字段）
    users_count: int = -1
    rooms_count: int = -1
    created_ts: float = 0.0
    last_ts: float = 0.0     # 频道上次消息时间（lm）
    room_type: str = ""      # "c"/"p"（团队主房间类型）
    is_private: bool = False
    team_id: str = ""        # 团队条目专用（teams.join 需要 teamId）


@dataclass
class Conversation:
    id: str = ""
    name: str = ""
    room_type: str = "c"
    last_message: Optional[Message] = None
    unread: int = 0
    avatar_url: str = ""
    is_favorite: bool = False    # 服务器同步的收藏状态（订阅 f 字段）
    # 私聊对方的 username（name 可能被解析成中文显示名，不能混用）
    username: str = ""
    # 多人私聊：Rocket.Chat 里 t 仍是 d，只是房间成员超过 2 人。一对一
    # 隐藏发送人前缀，多人私聊要显示是谁说的
    is_multi_dm: bool = False
    # 房间 slug（订阅的 name 字段）：频道/群是 ASCII 房间名，私聊是对方
    # username。网页版 permalink 用它拼 /channel/<slug>、/direct/<username>，
    # name 是中文显示名（fname）不能拿来拼链接
    slug: str = ""
    # 会话列表展示用
    last_message_time: float = 0.0
    last_message_text: str = ""
    # 当前登录用户是否该房间的创建者（来自房间元数据 u._id 与本人比对）。
    # 自己创建的房间允许「删除」；别人创建的只能「退出/离开」。
    i_created: bool = False
    # 是否讨论串（Rocket.Chat discussion，通过父房间 id `prid` 判定）。
    # 讨论串本身的类型仍是 c/p，但 UI 需要额外标注「讨论串」。
    is_discussion: bool = False
    # 是否团队（Rocket.Chat team，房间记录带 teamMain/teamId）。团队主房间
    # 的 t 仍是 c/p（分别对应公开/私有团队），UI 标签要区分「频道/团队」。
    is_team: bool = False
    # 房间是否开启端到端加密（rooms.get/rooms.info 的 encrypted 字段；
    # 订阅记录没有该字段）。菜单文案与开关动作都看它。
    encrypted: bool = False
    # 订阅 disableNotifications：免打扰只抑制提醒，未读角标照旧
    muted: bool = False
    # 订阅 userMentions：左侧列表的 @ 徽标
    mention_count: int = 0

    def to_dict(self):
        return {
            "_id": self.id,
            "name": self.name,
            "t": self.room_type,
            "is_discussion": self.is_discussion,
            "is_team": self.is_team,
        }


def ts_to_epoch(ts) -> float:
    """把 Rocket.Chat 的时间字段转成 epoch 秒。

    REST 返回 ISO 字符串，DDP 帧返回 Mongo EJSON 的 {"$date": 毫秒}。
    """
    if isinstance(ts, dict):
        ts = ts.get("$date")
    if isinstance(ts, (int, float)):
        # EJSON 用毫秒；1e11 秒已是 5138 年，超过就按毫秒算
        return float(ts) / 1000.0 if ts > 1e11 else float(ts)
    s = str(ts)
    if not s:
        return 0.0
    try:
        iso = s.replace("Z", "+00:00")
        import datetime
        dt = datetime.datetime.fromisoformat(iso)
        return dt.timestamp()
    except Exception:
        # 直接尝试解析
        try:
            import datetime
            return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
        except Exception:
            return time.time()


def json_ts_to_epoch(ts) -> float:
    """兼容不同格式的时间字段。"""
    return ts_to_epoch(ts)
