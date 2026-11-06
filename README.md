# Fchat — 微信风格界面的 Rocket.Chat 桌面客户端

> 中文 ｜ [English](README.en.md)

基于 **Python 3 + PyQt5 + Rocket.Chat 官方 Python API** 的桌面聊天客户端。
界面布局与交互借鉴微信的常见聊天应用设计：左侧导航栏、会话列表、聊天窗口
三栏结构，绿色/白色聊天气泡、圆形头像、时间标签等。

## 功能特性

- **微信风格三栏界面**：导航栏（会话/设置）｜会话列表｜聊天窗口
- **登录**：弹窗登录，可选记住密码（本地加密保存，见下方配置说明）；
  设置页开启「启动时自动登录」后，下次启动直接用已存凭据后台登录，
  失败（改密/断网）自动回落到预填好的登录框
- **端到端解密**：支持 Rocket.Chat `rc.v2.aes-sha2` E2E 加密房间的消息/文件解密（登录时可选填 E2E 密码）
- **会话列表**：频道 `#` / 私有群 / 私聊统一展示，未读数角标
  - 排序固定为「置顶优先 → 最后消息时间倒序」，取消置顶后自动落回时间位置
  - 会话名优先取服务器配置的中文显示名（`fname`，其次短 `topic`），
    私聊按 `username` 反查显示名；服务器未配置中文名的频道仍显示原始 slug
  - 时间列输出中文周名（周一…周日），不随系统 locale 变英文
- **聊天**：
  - 文字消息气泡（自己绿色靠右、对方白色靠左，群聊显示发送者昵称）
  - 图片消息：异步拉取缩略图（带附件认证头），加载中/失败有区分文案，
    点击可用系统浏览器打开原图；文件消息显示 `[文件]`
  - 打开会话默认定位到最新消息，向上滚到顶再加载更早历史且不跳阅读位置
  - 系统消息、消息间隔超 5 分钟自动插入时间标签
  - 无限分页：向上滚动自动加载更早历史，切换会话后自动恢复（不重复加载）
  - 点击消息头像弹出用户资料卡（头像/昵称/用户名/状态/角色/邮箱），
    点卡片外任意位置自动关闭
  - 消息与会话的右键菜单均带 QPainter 现画的矢量图标，跟随亮/暗主题
  - 附带上传：支持选择图片/文件发送（`rooms_media` 上传 + `rooms.mediaConfirm`
    确认，与官方客户端同款两步流程，消息由服务端生成）
  - 离线文字识别：图片消息与图文混排的内联图可右键「识别文字」，
    本机 OCR（RapidOCR / PP-OCRv4，不联网、不上传任何图片字节），
    结果窗可先编辑再插入输入框；图片查看器同样有「提取文字」按钮
  - AI 助手（**默认关闭**，需自行填服务地址）：对单个会话划重点、
    生成日报/周报、就聊天内容问答，把聊天**文字**发送到你配置的
    OpenAI 兼容服务；不发送图片、文件与附件，首次使用有同意确认，
    端到端加密房间另有二次确认
  - 图床上传（**默认关闭**）：可选把图片经自建图床换成可直接内联显示的
    URL，见下方「数据出网说明」
- **滚动条**：会话列表与消息流都是按需出现，静置细条、悬停加粗加深
- **实时消息**：走 **DDP** 协议实时收发，断线自动重连（指数退避）。优先直连 WebSocket；握手被反向代理挡掉时自动改用 SockJS 长轮询（与官方网页版的回落方式一致）并记住该选择
- **系统托盘**：点关闭按钮隐藏到托盘（最小化仍留在任务栏）；窗口不在前台时
  新消息以托盘图标闪烁提醒；悬停图标弹出跨会话的多条未读预览卡片，移开自动
  关闭，点击某条恢复主窗口并高亮对应消息；右键菜单可打开主程序 / 打开配置 / 关闭程序
- **头像**：异步加载 `/avatar/<username>` 头像，失败回退为首字占位

## 技术栈

| 层 | 方案 |
| --- | --- |
| UI | PyQt5 (Qt 5.15) |
| 数据/历史 | `rocketchat-API`（REST） |
| 实时 | 自写小型 DDP 客户端（`websocket-client` + SockJS 长轮询回落） |

## 安装

```bash
# Python 3.10 及以上（建议 3.10–3.12，见下方说明），建议使用虚拟环境
pip install -r requirements.txt
```

> 版本下限来自固定版本本身：`requests==2.33.1` 要求 Python ≥ 3.10，
> `keyring`/`websocket-client` 要求 ≥ 3.9。3.13/3.14 也能装主依赖
> （PyQt5 5.15.11 是 cp38-abi3 轮子，`pyqt5-sip` 有 cp313/cp314），
> 但可选的 OCR 包官方限制 `<3.13`，装不上就没有「识别文字」。CI 只跑
> 3.10 / 3.12，3.13 及以上按「能装能用但未进 CI」对待。

## 运行

```bash
python main.py
```

## 可选依赖：离线文字识别

图片「识别文字」用 `rapidocr-onnxruntime`（模型随 wheel 自带，
纯本机推理、不联网）：

```bash
pip install -r requirements-ocr.txt
```

没装时该菜单项会提示补装，其余功能不受影响。它被单独拆成一个文件，
正是因为该包只支持到 Python 3.12——写进主依赖会让 3.13+ 用户装不起来。

## 数据出网说明

除了你登录的 Rocket.Chat 服务器，程序只可能再连下面几类地址。除服务器本身外，
**全部默认关闭或只在你配置了地址时才生效**，源码里没有任何内置遥测/统计上报。

| 出网目标 | 默认状态 | 发出去的内容 |
| --- | --- | --- |
| 你登录的 Rocket.Chat 服务器 | 必需 | 登录凭据、消息、附件、头像 |
| AI 服务（你自填的 OpenAI 兼容地址） | 关闭 | 选中会话的聊天**文字**（默认最近 200 条，上限 1000 条 / 约 3 万字符）与你的提问；请求带 `Authorization: Bearer <token>`。**不发送**图片、文件、附件 |
| 图床上传服务（你自填的地址） | 关闭 | 待上传的图片/文件字节 + `Bearer <token>` |
| 自动更新服务器（构建时写入的地址） | 开启，可关 | 无凭据；只 GET `manifest.json` 与安装包，且强制 https |
| OCR | — | 不外网，图片字节只做本机推理 |

需要知道的边界：

- AI 与图床的地址都由你自己填写。**首次使用 AI 会弹同意确认**（改了地址后重新
  确认），对端到端加密房间另有「解密明文将离开本机」的二次确认。
- 消息里指向第三方域名的图片/头像链接，加载时**不会**带上你的 Rocket.Chat
  会话凭据（同源判定见 `core/url_guard.py`）。附件下载重定向时同样：跳到别的
  主机就不再携带凭据，跳转次数有上限。
- 会话缓存（历史消息、草稿、图片）为排障与离线查看而**明文**存在本机缓存目录，
  详见 [SECURITY.md](SECURITY.md)。

## 打包（可选）

仓库提供 `build_release.py` 一键发布脚本（Cython 编译 → PyInstaller → Inno Setup 生成安装包）。
构建配置从项目根 `.env` 读取（模板见 [`.env.example`](.env.example)），也可用同名环境变量覆盖：

| 键 | 说明 |
| --- | --- |
| `FCHAT_UPDATE_BASE_URL` | 自动更新服务地址（留空则更新功能停用） |
| `FCHAT_UPDATE_MANIFEST` | 更新清单文件名（默认 `manifest.json`） |
| `FCHAT_GCC` | gcc 编译器路径（Cython 编译用） |
| `FCHAT_UPLOAD_DIR` | 安装包上传目录 |

```bash
# 复制模板并填入实际值
cp .env.example .env
python build_release.py
```

> 依赖 gcc、Cython、Inno Setup，仅用于发布 Windows 安装包；普通使用无需打包。

## 配置（config.yaml）

配置保存于系统标准目录（首次在登录界面输入后自动生成）：

- Windows：`%APPDATA%/Fchat/config.yaml`
- 其他平台：`~/.config/fchat/config.yaml`

密码与 E2E 密码均以 **Fernet 对称加密**后落盘，密钥由**操作系统凭据存储（keyring）**托管——
Windows 凭据管理器（DPAPI）/ macOS Keychain / Linux Secret Service，不再落盘 `secret.key`
文件。勾选「记住密码」时才写入登录密码，`e2e_password` 填写过则始终加密保存。
**keyring 不可用**（无桌面会话/CI/异常环境）时密码干脆不落盘，每次需手动登录。

YAML 结构（手动查看参考，密文字段以 `gAAAA...` 代指 Fernet 密文）。
`save_config` 一次写出的顶层段共 9 个，`server` 只是其中之一：

```yaml
server:
  server_url: https://chat.example.com
  username: myname
  password: gAAAA...        # 未勾选「记住密码」时为空
  remember_password: true
  auto_login: false
  e2e_password: gAAAA...
  status: online
appearance:
  pinned_rooms: []
  shortcuts: {}
  theme: light
  accent_color: '#07C160'
  font_family: Microsoft YaHei
  font_size: 13
  chat_wallpaper: ''
  collapsed_favorites: false
storage:
  storage_dir: ''
upload_service:
  url: ''
  token: gAAAA...
  enabled: false
update:
  auto_check: true
llm:
  url: ''
  model: ''
  token: gAAAA...
  enabled: false
  consent: false
reminders: []
notify:
  enabled: true
  mentions_only: false
send:
  mode: enter
```

| 键 | 说明 |
| --- | --- |
| `server.server_url` | Rocket.Chat 服务器地址，如 `https://chat.example.com` 或 `http://ip:3000` |
| `server.username` | 登录用户名（不是邮箱） |
| `server.password` | 登录密码；勾选记住后以 Fernet 密文保存（密钥在系统 keyring），否则留空每次输入 |
| `server.remember_password` | `true`/`false`。自动登录的前提 |
| `server.auto_login` | `true`/`false`。启动时用已存凭据后台登录，失败回落到登录框。在设置页「通用」里开关 |
| `server.e2e_password` | E2E 解密密码；填写后可解密加密房间的消息 |
| `server.status` | 上次选择的在线状态 |
| `appearance.*` | 主题/主色/字体/聊天背景/置顶列表/快捷键，由设置页与右键维护 |
| `storage.storage_dir` | 缓存与日志的存储根，留空用系统标准目录 |
| `upload_service.*` | 自建图床地址、Bearer token（密文）与开关 |
| `update.auto_check` | 启动时是否检查新版本 |
| `llm.*` | AI 服务地址/模型/token（密文）/开关/首次使用同意标记 |
| `reminders` | 提醒事项列表 |
| `notify.*` | 托盘提醒总开关与「仅 @我/私聊」 |
| `send.mode` | `enter` 或 `ctrlenter` |

> 兼容迁移：首次加载若发现项目根目录存在旧 `config.ini`，会自动导入并
> 转存为加密 YAML，此后不再读取 ini（原文件不删除）。
> 登录后配置会自动写入。服务器地址支持 http/https。

## 使用说明

1. 启动程序，弹出登录框，填写服务器地址、用户名、密码后点击登录
   - 勾选「记住密码」才会把密码加密落盘，这也是自动登录的前提
   - 登录后在 设置 → 通用 打开「启动时自动登录」，下次启动直接进主界面
2. 登录成功进入主界面，左侧为会话列表，点击会话在右侧聊天
   - 会话打开即停在最新消息，向上滚到顶加载更早的历史
   - 右键会话可置顶/取消置顶、退出；置顶的会话始终排在最前
   - 点击消息头像查看对方资料，点卡片外任意位置关闭
   - 右键消息可复制、引用回复、撤回，附件消息另有保存项
3. 底部输入框输入文字回车发送；点输入框右侧的「+」可选择图片或文件上传
4. 实时消息会自动推送；会话列表顺序随最新消息自动变化

## 验证

内置单元测试覆盖配置加密/迁移与局部保存器互相覆盖、会话排序与置顶回退、
消息流滚动定位、图片异步到货重绘、委托几何与头像命中、矢量图标可辨识性与
主题跟随、E2E 解密链、DDP 传输（SockJS 成帧、握手回落、出站帧批量发送与发送
失效重连、订阅方言切换、重复推送去重）（离线运行，无需真实服务器）：

```bash
python -m unittest discover -s tests -v
```

## 目录结构

`core/` 是纯逻辑层（约定不 import Qt，可直接单测），`ui/` 是 PyQt5 界面层。

```
fchat/
├── main.py                  # 入口
├── config.py                # 配置读写（YAML + Fernet 密码加密 + keyring 密钥托管 + ini 迁移）
├── core/
│   ├── models.py            # Conversation / Message / User
│   ├── api_client.py        # REST 数据层（rocketchat_API 封装 + E2E 接入 + 图床上传）
│   ├── realtime.py          # DDP 客户端（WebSocket + SockJS 长轮询两种传输）
│   ├── e2e.py               # E2E 端到端解密（rc.v2.aes-sha2）
│   ├── messages.py          # REST/Realtime 消息统一解析
│   ├── markdown.py          # 轻量 Markdown → Qt 富文本
│   ├── url_guard.py         # 出网凭据与「用系统程序打开」的 URL 守卫
│   ├── ai_client.py         # OpenAI 兼容 AI 客户端（SSE 流式 + 取消）
│   ├── ai_context.py        # AI 上下文采集与提示词构造（条数/时间窗/字符三层截断）
│   ├── ocr.py               # 离线文字识别（RapidOCR / PP-OCRv4，纯本机）
│   ├── updater.py           # 自动更新（清单 / 下载 / sha256，禁止 import Qt）
│   ├── version.py           # 应用版本号单一来源
│   ├── paths.py             # 数据目录（缓存 / 日志）唯一来源
│   ├── log_setup.py         # 日志初始化（文件轮转 + 崩溃捕获）
│   ├── ssl_prewarm.py       # SSL 预热与 REST 并发控制
│   ├── msg_cache.py         # 消息历史快照缓存
│   ├── conv_cache.py        # 会话列表快照缓存
│   ├── status_cache.py      # 用户在线状态缓存
│   ├── read_state.py        # 每会话本地已读位置
│   ├── drafts.py            # 未发送草稿
│   └── stickers.py          # 自定义表情包存储
├── ui/
│   ├── main_window.py       # 主窗口（三栏 + 登录/自动登录 + 实时整合 + 分页）
│   ├── sidebar.py           # 最左侧图标导航栏
│   ├── conversation_list.py # 会话列表（置顶 + 最新消息时间排序）
│   ├── chat_view.py         # 聊天消息流（滚动定位 / 命中测试 / 右键菜单）
│   ├── message_model.py     # 消息模型与委托（虚拟化绘制）
│   ├── chat_input.py        # 输入框 / 附件按钮 / 引用预览条
│   ├── chat_background.py   # 聊天背景预设（纯色/渐变/纹理，亮暗两套）
│   ├── window_chrome.py     # 自绘窗口按钮（最小化 / 最大化 / 关闭）
│   ├── widgets.py           # 头像 / 气泡等通用控件
│   ├── icons.py             # QPainter 现画的矢量菜单图标
│   ├── theme.py             # 亮/暗主题 + 主色调 + 字体 → QSS
│   ├── login_dialog.py      # 登录对话框 + LoginRunner（自动登录复用）
│   ├── dialog_base.py       # 无边框圆角卡片对话框基类
│   ├── confirm_dialog.py    # 微信式确认弹窗
│   ├── settings_page.py     # 设置页（通用/外观/字体/上传服务/AI/关于）
│   ├── profile_popup.py     # 点头像弹出的用户资料卡
│   ├── room_panels.py       # 房间成员 / 文件 / 消息记录浮层
│   ├── room_info_popup.py   # 频道/团队信息浮卡
│   ├── directory_panel.py   # 目录面板（频道 / 用户 / 团队）
│   ├── create_room_dialogs.py  # 新建私聊 / 频道 / 团队
│   ├── forward_dialog.py    # 转发目标选择
│   ├── quick_switcher.py    # Ctrl+K 快速切换会话
│   ├── global_search_panel.py  # 跨房间全局搜索
│   ├── message_list_dialog.py  # 置顶 / 星标消息列表
│   ├── read_receipts_popup.py  # 已读 / 未读名单浮窗
│   ├── reminders_dialog.py  # 消息提醒列表
│   ├── emoji_picker.py      # Emoji 弹板
│   ├── image_viewer.py      # 原图查看（翻页 / 缩放 / 旋转 / 另存）
│   ├── ocr_dialog.py        # 识别结果窗（可编辑后插入）
│   ├── screenshot_capture.py   # 框选截图遮罩
│   ├── upload_dialog.py     # 上传进度弹窗
│   ├── download_dialog.py   # 下载进度弹窗
│   ├── ai_panel.py          # AI 划重点浮层（摘要 / 日报 / 周报）
│   ├── ai_dialog.py         # AI 结果对话窗（流式 / 停止 / 插入）
│   ├── ai_worker.py         # 可流式、可取消的 AI 后台任务
│   ├── api_workers.py       # 异步任务线程（run_async）
│   ├── realtime_bridge.py   # DDP → Qt 信号桥接（QThread）
│   ├── avatar_loader.py     # 头像 / 图片异步加载与缓存
│   ├── user_status.py       # 在线状态归一化与按需拉取
│   ├── tray.py              # 系统托盘（关闭到托盘 / 未读闪烁 / 悬停预览卡片）
│   ├── updater_dialog.py    # 更新下载进度弹窗
│   └── single_instance.py   # 单实例守卫
├── tests/                   # 单元测试（unittest，离线可跑）
└── resources/style.qss      # 微信色系样式
```

## 说明

- 实时收发不再要求服务器放行 WebSocket：若反向代理没有转发 `Upgrade` 头，握手会返回 400，
  客户端会自动改用 SockJS 长轮询（官方网页版同一回落方式）。运维若希望恢复直连 WebSocket，
  在代理上转发 `Connection`/`Upgrade` 头即可。
- 订阅 `stream-room-messages` 存在两种服务端方言（rid 数组 / 单个 rid），客户端先按新版发起，
  被拒后整体切换；部分部署会把同一条消息重复推送两次，已在接收侧按消息指纹去重。
- 一些部署的长轮询会话在最后一次 `xhr` 轮询后约 60 秒即被服务端销毁（此后 `xhr_send` 一律返回 404，
  而 `xhr` 仍能读到帧，表现为"只收不发"的僵尸连接）。因此出站帧按批合并成少量 `xhr_send`
  请求，且一旦发送失败就立刻断开重连，不会继续往废会话里灌帧。
- 首次启动若网络不通或服务器未就绪，会提示连接失败，不影响界面使用。
- 托盘悬停预览依赖系统提供图标位置；Windows 11 把图标收进任务栏「^」溢出区时
  拿不到位置，卡片不可用，此时改用 tooltip 与点击/右键菜单（图标拖回可见区即恢复）。
- 本项目为中文本地化桌面客户端，适合对接自有 Rocket.Chat 服务器（配置在登录界面填写或参考上方 config.yaml 说明）。

## 许可证

Copyright (C) 2026 shigc

本项目基于 **GPL-2.0-or-later** 开源，全文见 [LICENSE](LICENSE)。

选 GPL 而不是 MIT 的原因很具体：界面层用的 PyQt5 是 Riverbank 的
「GPL-2.0-or-later **或** 商业授权」双许可组件，而 MIT 属于单向兼容的宽松许可——
可以把 MIT 的代码并进 GPL 项目，但不能用 MIT 去覆盖 GPL 的义务。不取商业授权，
整个项目就必须按 GPL 分发。

对你的实际含义：

- 你可以自由使用、修改、再分发，包括商用；再分发时必须一并附上 GPL 文本与
  对应源码（或书面报价），衍生作品同样按 GPL 发布。
- 闭源商用需要另行购买 Riverbank 的 PyQt 商业授权，或把界面层迁到 LGPL 的
  PySide6。
- 各第三方组件的许可清单与分发注意事项见
  [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)。
- 如果你需要 MIT 或双许可路线，请在提 PR 之前先开 issue 讨论——本项目一旦
  按 GPL 发布，回收该许可对既有贡献者不可行。

## 免责声明

- Fchat 是独立开发的第三方 Rocket.Chat 客户端，与腾讯「微信」**无任何关联**；
  文中「微信风格」仅用于描述界面交互风格，不构成对相关商标的侵权或背书。
- Rocket.Chat 为其各自持有者的商标，本项目的使用不表示与 Rocket.Chat 官方存在隶属关系。
- 本项目按「现状」提供，不提供任何明示或默示的担保，详见 [LICENSE](LICENSE)。

