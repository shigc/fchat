"""应用版本号单一来源。

设置页「关于」、打包脚本（build_release.py / fchat.iss）与更新检查
（core/updater.py）都从这里取版本，避免多处各写各的。
"""
from __future__ import annotations

APP_VERSION = "1.4.1"
