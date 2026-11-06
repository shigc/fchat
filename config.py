"""配置管理（YAML + 密码加密）。

配置保存于系统标准目录：
    Windows:  %APPDATA%/Fchat/config.yaml
    其他平台: ~/.config/fchat/config.yaml

密码使用 Fernet 对称加密后落盘，密钥由操作系统凭据存储（keyring）托管
（Windows=凭据管理器/DPAPI，macOS=Keychain，Linux=Secret Service），
不再落盘 secret.key 文件。keyring 不可用（无桌面会话/CI/异常环境）时
密码干脆不落盘（每次手动登录），绝不回退到明文或本地密钥文件。
remember_password=False 时不写密码。

auto_login=True 时启动即以已保存的账号密码静默登录，前提是记住过密码。
会话排序由客户端按「收藏 → 最后消息时间」即时计算，不依赖此处配置
（pinned_rooms 字段仅为旧版本兼容保留，业务已不再读写）。

兼容迁移：首次加载若发现项目根目录存在旧 config.ini，则导入并
转存为 YAML，此后不再读取 ini。
"""
import configparser
import os

import yaml
from cryptography.fernet import Fernet, InvalidToken

# 项目根目录（config.py 位于项目根，一层 dirname 即可）
ROOT = os.path.dirname(os.path.abspath(__file__))

if os.name == "nt":
    _BASE_DIR = os.environ.get("APPDATA") or os.path.expanduser("~")
    CONFIG_DIR = os.path.join(_BASE_DIR, "Fchat")
else:
    CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "fchat")

CONFIG_FILE = os.path.join(CONFIG_DIR, "config.yaml")
KEY_FILE = os.path.join(CONFIG_DIR, "secret.key")
LEGACY_INI_FILE = os.path.join(ROOT, "config.ini")

# keyring 命名：固定常量，与账号无关，跨版本稳定（密钥只存一份）
_SERVICE = "fchat"
_KEY_USERNAME = "fernet-key"

# _keyring_available 的模块级缓存（懒加载单例，避免每次探测的开销）
_keyring_available_cache = None


def _ensure_config_dir():
    if not os.path.isdir(CONFIG_DIR):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
        except OSError:
            pass


def _purge_legacy_key_file():
    """新方案不再用 secret.key，旧文件是安全隐患，首次访问密钥时删除。

    旧密钥对应的密文已无法解密（密钥在 keyring，旧密钥已弃），删掉不影响
    任何功能；删不掉（无权限等）不致命，静默跳过。
    """
    if os.path.exists(KEY_FILE):
        try:
            os.remove(KEY_FILE)
        except OSError:
            pass


def keyring_available() -> bool:
    """keyring 是否可用（供 UI 查询，用于降级提示）。"""
    return _keyring_available()


def _keyring_available() -> bool:
    """探测 keyring 是否可用（无桌面会话/CI/异常环境返回 False）。

    结果模块级缓存，避免每次探测的开销。延迟 import keyring，保证
    未安装 keyring 时 config.py 仍可导入。
    """
    global _keyring_available_cache
    if _keyring_available_cache is not None:
        return _keyring_available_cache
    try:
        import keyring  # 延迟导入
        keyring.get_keyring()
        _keyring_available_cache = True
    except Exception:  # noqa: BLE001 —— 探测失败一律按不可用处理
        _keyring_available_cache = False
    return _keyring_available_cache


def _get_key() -> str | None:
    """从 keyring 读 Fernet 密钥；读取失败返回 None。"""
    try:
        import keyring
        return keyring.get_password(_SERVICE, _KEY_USERNAME)
    except Exception:  # noqa: BLE001
        return None


def _set_key(key: str) -> bool:
    """把 Fernet 密钥写入 keyring；失败返回 False。"""
    try:
        import keyring
        keyring.set_password(_SERVICE, _KEY_USERNAME, key)
        return True
    except Exception:  # noqa: BLE001
        return False


def _load_or_create_key() -> bytes | None:
    """从 keyring 取 Fernet 密钥；没有则生成并写入；不可用返回 None。

    返回 None 表示密钥不可用，调用方应据此不落盘密码/读不到密码。
    """
    _purge_legacy_key_file()
    if not _keyring_available():
        return None
    key = _get_key()
    if key:
        return key.encode("ascii")
    key = Fernet.generate_key().decode("ascii")
    return key.encode("ascii") if _set_key(key) else None


def _encrypt_password(plain: str) -> str:
    """加密密码，返回密文字符串。keyring 不可用或失败时返回空（不落盘明文）。"""
    if not plain:
        return ""
    key = _load_or_create_key()
    if key is None:
        return ""
    try:
        f = Fernet(key)
        return f.encrypt(plain.encode("utf-8")).decode("ascii")
    except Exception:  # noqa: BLE001
        return ""


def _decrypt_password(token: str) -> str:
    """解密密码。keyring 不可用/密文格式不符/解密失败时按空密码处理。"""
    if not token:
        return ""
    key = _load_or_create_key()
    if key is None:
        return ""
    try:
        f = Fernet(key)
        return f.decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, Exception):  # noqa: BLE001
        return ""


def _defaults():
    return {
        "server_url": "",
        "username": "",
        "password": "",
        "remember_password": False,
        "auto_login": False,
        "e2e_password": "",
        "pinned_rooms": [],
        "theme": "light",
        "accent_color": "#07C160",
        "font_family": "Microsoft YaHei",
        "font_size": 13,
        "chat_wallpaper": "",
        "collapsed_favorites": False,
        "status": "online",
        "shortcuts": {},
        "storage_dir": "",
        "upload_service_url": "",
        "upload_service_token": "",
        "upload_service_enabled": False,
        "update_auto_check": True,
        # AI（自建的 OpenAI 兼容网关）。token 与密码同款 Fernet 加密；
        # consent 记录「首次使用内容将发送至模型」是否已确认。
        "llm_url": "",
        "llm_model": "",
        "llm_token": "",
        "llm_enabled": False,
        "llm_consent": False,
        "reminders": [],
        # 通知设置：notify_enabled 总开关；notify_mentions_only 仅 @我/私聊时提醒。
        # 两者都只影响提醒（toast/托盘闪烁/角标预览），不影响未读计数，
        # 与「消息免打扰」语义一致（免打扰也只压提醒不动未读）。
        "notify_enabled": True,
        "notify_mentions_only": False,
        # 发送方式：enter=回车发送/Shift+回车换行；ctrlenter=Ctrl+回车发送/回车换行
        "send_mode": "enter",
    }


def save_config(server_url="", username="", password="",
                remember_password=False, auto_login=False, e2e_password="",
                pinned_rooms=None, appearance=None, status="online",
                shortcuts=None, storage_dir="", upload_service_url="",
                upload_service_token="", upload_service_enabled=False,
                update_auto_check=True, llm_url="", llm_model="",
                llm_token="", llm_enabled=False, llm_consent=False,
                reminders=None, notify_enabled=True, notify_mentions_only=False,
                send_mode="enter"):
    """把配置写入 config.yaml。密码/E2E 密码/AI token 均以 Fernet 密文写入。"""
    data = {
        "server": {
            "server_url": server_url or "",
            "username": username or "",
            "password": _encrypt_password(password) if remember_password else "",
            "remember_password": bool(remember_password),
            "auto_login": bool(auto_login),
            "e2e_password": _encrypt_password(e2e_password or ""),
            "status": status or "online",
        },
        "appearance": {
            "pinned_rooms": list(pinned_rooms or []),
            "shortcuts": dict(shortcuts or {}),
            **(appearance or {}),
        },
        "storage": {
            "storage_dir": storage_dir or "",
        },
        "upload_service": {
            "url": upload_service_url or "",
            "token": _encrypt_password(upload_service_token or ""),
            "enabled": bool(upload_service_enabled),
        },
        "update": {
            "auto_check": bool(update_auto_check),
        },
        "llm": {
            "url": llm_url or "",
            "model": llm_model or "",
            "token": _encrypt_password(llm_token or ""),
            "enabled": bool(llm_enabled),
            "consent": bool(llm_consent),
        },
        "reminders": [dict(r) for r in (reminders or [])],
        "notify": {
            "enabled": bool(notify_enabled),
            "mentions_only": bool(notify_mentions_only),
        },
        "send": {
            "mode": send_mode or "enter",
        },
    }
    try:
        _ensure_config_dir()
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, default_flow_style=False)
    except OSError:
        pass


def _load_yaml() -> dict:
    """读取 config.yaml，返回合并了默认值的配置 dict。"""
    data = _defaults()
    if not os.path.exists(CONFIG_FILE):
        return data
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001
        return data
    s = raw.get("server") or {}
    token = str(s.get("password", "") or "")
    e2e_token = str(s.get("e2e_password", "") or "")
    data.update({
        "server_url": str(s.get("server_url", "") or ""),
        "username": str(s.get("username", "") or ""),
        "password": _decrypt_password(token) if token else "",
        "remember_password": bool(s.get("remember_password", False)),
        "auto_login": bool(s.get("auto_login", False)),
        "e2e_password": _decrypt_password(e2e_token) if e2e_token else "",
        "status": str(s.get("status", "online") or "online"),
    })
    # 外观/置顶配置
    appearance = raw.get("appearance") or {}
    data["pinned_rooms"] = list(appearance.get("pinned_rooms", []) or [])
    data["theme"] = str(appearance.get("theme", "light") or "light")
    data["accent_color"] = str(appearance.get("accent_color", "#07C160") or "#07C160")
    data["font_family"] = str(appearance.get("font_family", "Microsoft YaHei") or "Microsoft YaHei")
    data["font_size"] = int(appearance.get("font_size", 13) or 13)
    data["chat_wallpaper"] = str(
        appearance.get("chat_wallpaper", "") or "")
    data["collapsed_favorites"] = bool(
        appearance.get("collapsed_favorites", False))
    data["shortcuts"] = {
        str(k): str(v) for k, v in (appearance.get("shortcuts") or {}).items()
    }
    # 存储路径（cache/logs 所在根目录；config/密钥本身不迁移）
    data["storage_dir"] = str(
        (raw.get("storage") or {}).get("storage_dir", "") or "")
    # 上传服务（图文混排内联图片用；未配置时图片回落为文件链接）。
    # enabled 缺省时按"已配置 url 即在用"迁移，保持旧配置行为不变
    _up = raw.get("upload_service") or {}
    data["upload_service_url"] = str(_up.get("url", "") or "")
    _up_token = str(_up.get("token", "") or "")
    data["upload_service_token"] = _decrypt_password(_up_token) if _up_token else ""
    data["upload_service_enabled"] = bool(
        _up.get("enabled", bool(data["upload_service_url"])))
    # 自动更新检查开关（仅控制启动时自动检查，关于页手动检查不受限）
    data["update_auto_check"] = bool(
        (raw.get("update") or {}).get("auto_check", True))
    # AI 配置：token 解密失败按空处理（与密码一致），不阻断启动
    _llm = raw.get("llm") or {}
    llm_token = str(_llm.get("token", "") or "")
    data["llm_url"] = str(_llm.get("url", "") or "")
    data["llm_model"] = str(_llm.get("model", "") or "")
    data["llm_token"] = _decrypt_password(llm_token) if llm_token else ""
    data["llm_enabled"] = bool(_llm.get("enabled", False))
    data["llm_consent"] = bool(_llm.get("consent", False))
    data["reminders"] = [
        dict(r) for r in (raw.get("reminders") or []) if isinstance(r, dict)]
    # 通知设置（缺省按默认值，旧配置无该键时保持开启）
    _notify = raw.get("notify") or {}
    data["notify_enabled"] = bool(_notify.get("enabled", True))
    data["notify_mentions_only"] = bool(_notify.get("mentions_only", False))
    # 发送方式（缺省回车发送）
    _send = raw.get("send") or {}
    data["send_mode"] = str(_send.get("mode", "enter") or "enter")
    return data


def _import_legacy_ini() -> dict:
    """导入旧 config.ini 并转存 YAML；成功后不再读取 ini（不删除原文件）。"""
    legacy = _defaults()
    if not os.path.exists(LEGACY_INI_FILE):
        return legacy
    cfg = configparser.ConfigParser()
    try:
        cfg.read(LEGACY_INI_FILE, encoding="utf-8")
    except Exception:  # noqa: BLE001
        return legacy
    if cfg.has_section("server"):
        s = cfg["server"]
        legacy.update({
            "server_url": s.get("server_url", ""),
            "username": s.get("username", ""),
            "password": s.get("password", ""),
            "remember_password": s.get("remember_password", "false").lower() == "true",
        })
    # 转存 YAML（密码密文化后写入）
    save_config(
        server_url=legacy["server_url"],
        username=legacy["username"],
        password=legacy["password"],
        remember_password=legacy["remember_password"],
    )
    return legacy


def _save_all(cfg: dict):
    """以 cfg 为基础整份回写。

    save_config 是全量覆盖写，所以每个局部保存器都必须带齐其余字段，
    否则会把无关配置静默抹掉。
    """
    save_config(
        server_url=cfg["server_url"],
        username=cfg["username"],
        password=cfg["password"] if cfg["remember_password"] else "",
        remember_password=cfg["remember_password"],
        auto_login=cfg["auto_login"],
        e2e_password=cfg["e2e_password"],
        pinned_rooms=cfg["pinned_rooms"],
        status=cfg.get("status", "online"),
        shortcuts=cfg.get("shortcuts", {}),
        storage_dir=cfg.get("storage_dir", ""),
        upload_service_url=cfg.get("upload_service_url", ""),
        upload_service_token=cfg.get("upload_service_token", ""),
        upload_service_enabled=bool(cfg.get("upload_service_enabled", False)),
        update_auto_check=bool(cfg.get("update_auto_check", True)),
        llm_url=cfg.get("llm_url", ""),
        llm_model=cfg.get("llm_model", ""),
        llm_token=cfg.get("llm_token", ""),
        llm_enabled=bool(cfg.get("llm_enabled", False)),
        llm_consent=bool(cfg.get("llm_consent", False)),
        reminders=cfg.get("reminders", []),
        notify_enabled=bool(cfg.get("notify_enabled", True)),
        notify_mentions_only=bool(cfg.get("notify_mentions_only", False)),
        send_mode=cfg.get("send_mode", "enter"),
        appearance=_appearance_from_cfg(cfg),
    )


def save_login(server_url: str, username: str, password: str,
               remember_password: bool, e2e_password: str = ""):
    """登录成功后保存凭据；auto_login 与外观设置保持原值。"""
    cfg = _load_yaml()
    cfg.update({
        "server_url": server_url,
        "username": username,
        "password": password,
        "remember_password": bool(remember_password),
    })
    if e2e_password:
        cfg["e2e_password"] = e2e_password
    _save_all(cfg)


def save_auto_login(auto_login: bool):
    """单独更新自动登录开关。"""
    cfg = _load_yaml()
    cfg["auto_login"] = bool(auto_login)
    _save_all(cfg)


def save_server_url(server_url: str):
    """单独更新服务器地址（其余字段保持不变）。"""
    cfg = _load_yaml()
    cfg["server_url"] = (server_url or "").strip()
    _save_all(cfg)


def save_e2e_password(e2e_password: str):
    """单独更新已保存配置中的 E2E 密码（其余字段保持不变）。"""
    cfg = _load_yaml()
    cfg["e2e_password"] = e2e_password or ""
    _save_all(cfg)


def save_pinned_rooms(pinned_rooms: list):
    """单独更新已保存配置中的置顶房间列表。"""
    cfg = _load_yaml()
    cfg["pinned_rooms"] = list(pinned_rooms or [])
    _save_all(cfg)


def save_appearance(theme="", accent_color="", font_family="", font_size=0,
                    chat_wallpaper=None):
    """单独更新外观设置。chat_wallpaper 传 None 表示不动，空串表示清除。"""
    cfg = _load_yaml()
    if theme:
        cfg["theme"] = theme
    if accent_color:
        cfg["accent_color"] = accent_color
    if font_family:
        cfg["font_family"] = font_family
    if font_size:
        cfg["font_size"] = font_size
    if chat_wallpaper is not None:
        cfg["chat_wallpaper"] = chat_wallpaper
    _save_all(cfg)


def save_ui_prefs(collapsed_favorites=None):
    """界面局部偏好（会话列表折叠等）。None 表示保持不动。"""
    cfg = _load_yaml()
    if collapsed_favorites is not None:
        cfg["collapsed_favorites"] = bool(collapsed_favorites)
    _save_all(cfg)


def save_reminders(reminders: list):
    """整表替换消息定时提醒。"""
    cfg = _load_yaml()
    cfg["reminders"] = [dict(r) for r in (reminders or [])]
    _save_all(cfg)


def save_notify(enabled=None, mentions_only=None):
    """单独更新通知设置；参数为 None 表示该字段保持不动。"""
    cfg = _load_yaml()
    if enabled is not None:
        cfg["notify_enabled"] = bool(enabled)
    if mentions_only is not None:
        cfg["notify_mentions_only"] = bool(mentions_only)
    _save_all(cfg)


def save_send_mode(mode: str):
    """单独更新发送方式（enter / ctrlenter）。"""
    cfg = _load_yaml()
    cfg["send_mode"] = mode or "enter"
    _save_all(cfg)


def _appearance_from_cfg(cfg: dict) -> dict:
    """从已加载的配置 dict 中提取外观设置。"""
    return {
        "theme": cfg.get("theme", "light"),
        "accent_color": cfg.get("accent_color", "#07C160"),
        "font_family": cfg.get("font_family", "Microsoft YaHei"),
        "font_size": cfg.get("font_size", 13),
        "shortcuts": cfg.get("shortcuts", {}),
        "chat_wallpaper": cfg.get("chat_wallpaper", ""),
        "collapsed_favorites": cfg.get("collapsed_favorites", False),
    }


def save_status(status: str):
    """单独更新登录状态（在线/离开/忙碌/离线）。"""
    cfg = _load_yaml()
    cfg["status"] = status or "online"
    _save_all(cfg)


def save_shortcuts(shortcuts: dict):
    """单独更新快捷键映射。"""
    cfg = _load_yaml()
    cfg["shortcuts"] = dict(shortcuts or {})
    _save_all(cfg)


def save_storage_dir(path: str):
    """单独更新存储根目录（cache/logs 迁移后调用）。空串恢复默认。"""
    cfg = _load_yaml()
    cfg["storage_dir"] = os.path.abspath(path) if path else ""
    _save_all(cfg)


def save_upload_service(url: str, token: str = "", enabled=None):
    """单独更新上传服务配置（地址 + 图床 token + 自定义服务器开关）。
    空串表示未配置；enabled 为 None 表示保持原开关不动。"""
    cfg = _load_yaml()
    cfg["upload_service_url"] = (url or "").strip()
    cfg["upload_service_token"] = (token or "").strip()
    if enabled is not None:
        cfg["upload_service_enabled"] = bool(enabled)
    _save_all(cfg)


def save_update_auto_check(enabled: bool):
    """单独更新「启动时自动检查更新」开关（其余字段保持不变）。"""
    cfg = _load_yaml()
    cfg["update_auto_check"] = bool(enabled)
    _save_all(cfg)


def save_llm(url: str = None, model: str = None, token: str = None,
             enabled=None, consent=None):
    """单独更新 AI 配置；参数为 None 表示该字段保持不动。

    token 走 _save_all → save_config 的 Fernet 加密落盘，密文不会回写到
    调用方拿到的 cfg（_load_yaml 已解密），因此重复保存不会二次加密。
    """
    cfg = _load_yaml()
    if url is not None:
        cfg["llm_url"] = (url or "").strip()
    if model is not None:
        cfg["llm_model"] = (model or "").strip()
    if token is not None:
        cfg["llm_token"] = (token or "").strip()
    if enabled is not None:
        cfg["llm_enabled"] = bool(enabled)
    if consent is not None:
        cfg["llm_consent"] = bool(consent)
    _save_all(cfg)


def load_config():
    """读取配置，返回 dict；优先 YAML，首次发现旧 config.ini 时自动迁移。"""
    if os.path.exists(CONFIG_FILE):
        return _load_yaml()
    if os.path.exists(LEGACY_INI_FILE):
        return _import_legacy_ini()
    return _defaults()
