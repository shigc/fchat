# 贡献指南

感谢你对 Fchat 的关注！欢迎提交 Issue、Pull Request 或参与讨论。

## 开发环境

- Python 3.10–3.12（`requests` 的固定版本要求 ≥ 3.10，CI 跑 3.10 与 3.12）
- 安装依赖：`pip install -r requirements.txt`
- 运行：`python main.py`

> 提示：图片「识别文字」的 `rapidocr-onnxruntime` 是**可选**依赖，单独放在
> `requirements-ocr.txt`（该包官方只支持到 Python 3.12）。开发时不装它，
> 测试同样全绿——OCR 测试用假引擎注入。

## 运行测试

```bash
python -m unittest discover -s tests -v
```

所有单元测试均为**离线运行**，无需真实 Rocket.Chat 服务器。
改动代码后请确保测试全部通过。

> 注意：`tests/test_main_window_wiring.py` 因每个用例都实例化完整主窗口，
> 运行较慢（约 15 分钟），请耐心等待或单独运行其他测试模块。

## 代码风格

- 遵循 PEP 8，4 空格缩进，行宽约 88 字符
- 类型标注：公开接口建议加类型注解
- 注释使用中文，解释「为什么」而非「是什么」
- 纯逻辑（无 Qt 依赖）放在 `core/`，界面相关放在 `ui/`

## 提交 PR

1. 从 `master` 新建分支，命名建议 `feature/xxx` 或 `fix/xxx`
2. 保持改动聚焦，一个 PR 解决一个问题
3. 提交信息简洁清晰，说明改动目的
4. 确保测试通过后再提交

## 贡献授权

提交 PR 即表示你同意把你的改动以本项目的许可证（见 [LICENSE](LICENSE)，
GPL-2.0-or-later）授权给所有使用者（inbound = outbound），并确认你有权做出该授权
（你写的是自己的代码，或所在机构允许你开源）。无需单独签署 CLA。

GPL 是 copyleft：你的改动被合入后，任何再分发（含修改版）都必须继续按 GPL 公开
对应源码。如果你不能接受这一点，请不要提交 PR。

## 项目结构

```
fchat/
├── main.py              # 入口
├── config.py            # 配置读写（YAML + Fernet 密码加密 + keyring 密钥托管）
├── core/                # 纯逻辑（不 import Qt）
│   ├── api_client.py    # REST 客户端
│   ├── realtime.py      # DDP 客户端（WebSocket + SockJS 回落）
│   ├── e2e.py           # E2E 端到端解密
│   └── ...
├── ui/                  # PyQt5 界面
└── tests/               # 单元测试（unittest，离线可跑）
```

## 行为准则

请保持友善、尊重。任何形式的骚扰、歧视、人身攻击都不被容忍。
