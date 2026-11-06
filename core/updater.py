"""自动更新核心逻辑（纯 Python，禁止 import Qt）。

更新服务端只需静态文件托管（nginx）：
    <base_url>/manifest.json   {"version": "1.1.0", "url": "Fchat-1.1.0-setup.exe",
                                "sha256": "...", "released_at": "..."}
    <base_url>/Fchat-1.1.0-setup.exe

更新地址来源：打包时 build_release.py 把项目根 .env 的
FCHAT_UPDATE_BASE_URL 编译成 _update_cfg.pyd（地址烧进机器码，用户不可改）；
开发态直接读 .env。
网络失败一律抛异常交由调用方降级（启动检查静默跳过），不中断主流程。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re

import requests

log = logging.getLogger(__name__)

_DOWNLOAD_TIMEOUT = (5, 30)   # (连接, 读取) 秒
_CHUNK = 64 * 1024


class DownloadCancelled(Exception):
    """用户取消下载（progress_cb 抛出、由 download_installer 透传）。"""


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_update_config() -> dict:
    """返回 {"base_url": ..., "manifest": ...}；任何失败都降级为禁用更新。

    打包态优先取编译进包的 _update_cfg 模块（机器码，不可篡改）；
    开发态读项目根 .env 的 FCHAT_UPDATE_BASE_URL / FCHAT_UPDATE_MANIFEST。
    """
    try:
        import _update_cfg  # 打包态：build_release.py 生成并编译
        return {"base_url": str(_update_cfg.BASE_URL),
                "manifest": str(_update_cfg.MANIFEST)}
    except ImportError:
        pass
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_project_root(), ".env"))
        base = (os.environ.get("FCHAT_UPDATE_BASE_URL") or "").strip()
        manifest = (os.environ.get("FCHAT_UPDATE_MANIFEST") or "").strip()
        return {"base_url": base, "manifest": manifest or "manifest.json"}
    except Exception as e:  # noqa: BLE001
        log.warning("读取更新配置失败，更新功能停用：%s", e)
        return {"base_url": "", "manifest": "manifest.json"}


def manifest_url(cfg: dict) -> str:
    return (cfg.get("base_url", "") or "").rstrip("/") + "/" + \
        (cfg.get("manifest", "manifest.json") or "manifest.json").lstrip("/")


def _require_https(url: str, what: str) -> str:
    """更新链路一律走 https。

    清单与安装包出自同一静态目录，sha256 只能防网络劫持、防不住目录被篡改；
    明文 http 下连这一步都不可信，所以直接拒绝，不做降级警告。
    """
    text = (url or "").strip()
    if not text.lower().startswith("https://"):
        raise ValueError(f"{what}必须使用 https（当前：{text[:80] or '空'}）")
    return text


def fetch_manifest(base_url: str, manifest: str = "manifest.json") -> dict:
    """拉取并校验更新清单；网络/格式问题抛异常，由调用方降级。"""
    url = _require_https(
        base_url.rstrip("/") + "/" +
        (manifest or "manifest.json").lstrip("/"), "更新清单地址")
    resp = requests.get(url, timeout=_DOWNLOAD_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or not data.get("version") or not data.get("url"):
        raise ValueError("更新清单格式无效")
    return data


def _version_tuple(text: str):
    """把 "1.2.3" 解析成 (1, 2, 3)；解析失败返回 None。"""
    parts = str(text or "").strip().strip("vV").split(".")
    out = []
    for p in parts:
        if not re.fullmatch(r"\d+", p):
            return None
        out.append(int(p))
    return tuple(out) if out else None


def is_new_version(remote: str, local: str) -> bool:
    """远端版本是否严格大于本地；任一版本号畸形都返回 False（不误报）。"""
    r = _version_tuple(remote)
    l = _version_tuple(local)
    if r is None or l is None:
        return False
    n = max(len(r), len(l))
    r += (0,) * (n - len(r))
    l += (0,) * (n - len(l))
    return r > l


def updates_dir() -> str:
    from core.paths import cache_root
    return os.path.join(cache_root(), "updates")


def installer_filename(url: str) -> str:
    """从清单 URL 取安装包文件名，并把字符收敛到安全集。

    这个名字会被写进「等本进程退出 → 起安装器」的 .cmd 批处理
    （ui/main_window._launch_installer）：URL 里一个裸引号就能跳出引号
    执行命令，反斜杠与 `..` 能把文件写到更新目录之外。
    """
    name = str(url or "").split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    if name.startswith(".") or not name.lower().endswith(".exe"):
        return "fchat-setup.exe"
    return name


def download_installer(url: str, dest_dir: str, progress_cb=None) -> str:
    """流式下载安装器到 dest_dir（.part 原子改名），返回最终文件路径。

    progress_cb(done_bytes, total_bytes)，total 未知时为 0。下载中断留下
    的 .part 下次重下前由调用方覆盖写，不影响（每次都是全新 .part）。
    """
    url = _require_https(url, "安装包地址")
    os.makedirs(dest_dir, exist_ok=True)
    final = os.path.join(dest_dir, installer_filename(url))
    part = final + ".part"
    resp = requests.get(url, stream=True, timeout=_DOWNLOAD_TIMEOUT)
    resp.raise_for_status()
    total = int(resp.headers.get("Content-Length") or 0)
    done = 0
    with open(part, "wb") as f:
        for chunk in resp.iter_content(_CHUNK):
            if chunk:
                f.write(chunk)
                done += len(chunk)
                if progress_cb:
                    progress_cb(done, total)
    os.replace(part, final)
    return final


def verify_sha256(path: str, expected: str) -> bool:
    """文件 sha256 是否与清单一致；expected 大小写不敏感，缺失算不一致。"""
    expected = (expected or "").strip().lower()
    if len(expected) != 64:
        return False
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(_CHUNK), b""):
                h.update(chunk)
    except OSError:
        return False
    return h.hexdigest() == expected
