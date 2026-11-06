# 第三方组件许可清单

本项目 Copyright (C) 2026 shigc，按 **GPL-2.0-or-later** 分发，许可全文见
[LICENSE](LICENSE)。本文件与 LICENSE 一并打进安装包（`packaging/fchat.iss`
的 `[Files]`），使二进制副本自带署名与许可出处。

下表列出随项目分发或随安装包分发的第三方组件、它们各自的许可，以及选择当前
许可证路线的原因。

版本与许可字段取自 `requirements.txt` / `requirements-ocr.txt` 的固定版本在 PyPI
上的元数据（`License-Expression`，缺失时取 `License` 字段与 License 分类），
核对时间：2026-11。

## 为什么不是 MIT

PyQt5 由 Riverbank Software 以 **GPL-2.0-or-later 与商业授权双许可**发布（PyPI
元数据的 License 字段写作 "GPL v3"，官方条款为 GPL 第 2 版及后续版本，可与商业
授权二选一）。MIT 是「单向兼容 GPL」的宽松许可：可以把 MIT 代码并入 GPL 项目，
但不能反过来用 MIT 覆盖 GPL 的义务。因此使用 PyQt5 而不取商业授权时，整个项目
必须以 GPL 分发——这就是本仓库选择 GPL-2.0-or-later 的唯一原因。

## 运行期依赖（`requirements.txt`）

| 组件 | 固定版本 | 许可 | 备注 |
| --- | --- | --- | --- |
| PyQt5 | 5.15.11 | GPL-2.0-or-later / Riverbank 商业授权 | 决定本项目许可走向 |
| PyQt5-Qt5（Qt 5.15 运行库） | 5.15.x（未固定） | LGPL-3.0 | Qt 动态库，安装目录内为独立 DLL，可整体替换 |
| PyQt5-sip | 12.20.0 | BSD-2-Clause | |
| requests | 2.33.1 | Apache-2.0 | |
| websocket-client | 1.9.0 | Apache-2.0 | 手写 DDP 客户端的 WebSocket/SockJS 传输 |
| rocketchat-API | 3.7.1 | MIT | |
| PyYAML | 6.0.2 | MIT | 加密配置文件的载体 |
| cryptography | 44.0.1 | Apache-2.0 **或** BSD-3-Clause | Fernet 口令/凭据加密 |
| keyring | 25.7.0 | MIT | 系统凭据存储后端 |
| pywin32-ctypes | 0.2.3 | BSD-3-Clause | keyring 在 Windows 上的后端 |
| jaraco.classes / jaraco.functools / jaraco.context | 3.4.0 / 4.6.0 / 6.1.2 | MIT | keyring 传递依赖 |
| emoji | 2.14.1 | BSD-3-Clause | 表情选择器名称表 |
| python-dotenv | 1.0.1 | BSD-3-Clause | 读取构建期 `.env` |
| urllib3 | 2.8.0 | MIT | requests 传递依赖 |
| certifi | 2026.10.23 | MPL-2.0 | CA 根证书包 |
| idna | 3.20 | BSD-3-Clause | requests 传递依赖 |

## 可选 OCR 依赖（`requirements-ocr.txt`，只支持 Python < 3.13）

`rapidocr-onnxruntime` 自身与它的传递依赖都不在 `requirements.txt` 里，只有装了
可选依赖才会进入产物。除 `rapidocr-onnxruntime==1.4.4` 外均未固定版本，下表版本
为核对时的最新发布。

| 组件 | 核对版本 | 许可 | 备注 |
| --- | --- | --- | --- |
| rapidocr-onnxruntime | 1.4.4 | Apache-2.0 | PP-OCR 模型权重同为 Apache-2.0 |
| onnxruntime | 1.31.x | MIT | 推理引擎 |
| opencv-python | 5.0.x | Apache-2.0 | 预处理 |
| numpy | 2.5.x | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | 多许可拼接（含随附的第三方片段） |
| Shapely | 2.2.x | BSD-3-Clause | |
| pyclipper | 1.4.x | MIT | |
| six | 1.17.x | MIT | |
| Pillow | 13.x | MIT-CMU | rapidocr 传递依赖，本项目源码不直接使用 |
| tqdm | 4.70.x | MPL-2.0 AND MIT | |

OCR 推理为**纯本机**：不联网、不外传图片字节，模型文件随包本地读取。

## 构建期工具（不随产物分发）

| 工具 | 核对版本 | 许可 | 说明 |
| --- | --- | --- | --- |
| Cython | 3.3.x | Apache-2.0 | 把核心模块编成 `.pyd` |
| PyInstaller | 6.22.x | GPL-2.0-or-later + PyInstaller 例外 | 打包例外允许分发被打包程序；本项目自身已是 GPL，不构成额外约束 |
| Inno Setup | 6.x | Inno Setup License（LGPL 系） | 生成的安装器可自由分发 |

## 分发时要一并给出去的东西

GPL-2.0 第 1 条要求复制品随附本许可文本，本项目按以下方式满足：

1. 源码仓库根目录的 [LICENSE](LICENSE)（GPLv2 全文，未改动）；版权声明写在
   README「许可证」一节与本文档首段。
2. 安装包内置 `LICENSE` 与本文档（`packaging/fchat.iss` 的 `[Files]` 段），
   安装向导首屏展示许可文本（`LicenseFile`）。
3. corresponding source 即本仓库公开可得的完整源码；用自动更新升级时，
   更新页同时给出源码地址。

需要自行确认的点（因分发方式而异，本文不代替法律意见）：

- 若替换或升级 Qt 运行库，保持 LGPL-3.0 的「用户可替换」条件：Qt 相关 DLL 仍以
  独立文件形式安装在 `{app}` 目录下、不做静态合并。
- 二进制再分发（例如放入应用商店）需要一并携带 GPL 文本与源码获取方式。
- 上游各组件的许可证原文随 pip 包安装在
  `site-packages/<包名>-<版本>.dist-info/` 下，可从对应发行版直接取用。