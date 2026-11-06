"""测试共享工具：隔离配置目录 + mock keyring 抽象层。

config.py 的密钥存储已改为 keyring 托管。真实环境（offscreen / CI / 无桌面
会话）下 keyring 可用性不确定，为避免测试依赖真实系统凭据存储，这里统一把
config 的 keyring 抽象层 mock 为「可用」（内存 dict），个别降级用例再切为
「不可用」。

用法：测试类继承 KeyringIsolatedTestCase，即可获得隔离的配置目录 + 可用的
keyring mock；需要测降级时调用 self.make_keyring_unavailable()。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import config


def install_keyring_mock(testcase):
    """给已有临时目录隔离的测试类补挂 keyring mock（可用，内存 dict）。

    用于那些已有自己的 setUp 隔离 CONFIG_DIR、但不方便继承
    KeyringIsolatedTestCase 的测试类。在 setUp 里调用一次即可，
    通过 addCleanup 自动恢复。返回一个 dict，可向其写入损坏密钥模拟降级。
    """
    store = {}
    state = {"available": True}
    patchers = [
        mock.patch.object(config, "_keyring_available",
                          side_effect=lambda: state["available"]),
        mock.patch.object(config, "_get_key",
                          side_effect=lambda: store.get("k")),
        mock.patch.object(config, "_set_key",
                          side_effect=lambda v: store.__setitem__("k", v) or True),
    ]
    for p in patchers:
        p.start()
        testcase.addCleanup(p.stop)
    return store, state


class KeyringIsolatedTestCase(unittest.TestCase):
    """把配置目录隔离到临时目录，并把 keyring 抽象层 mock 为可用（内存 dict）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = self._tmp.name
        self._orig = (config.CONFIG_DIR, config.CONFIG_FILE,
                      config.KEY_FILE, config.LEGACY_INI_FILE)
        config.CONFIG_DIR = os.path.join(tmp, "cfgdir")
        config.CONFIG_FILE = os.path.join(config.CONFIG_DIR, "config.yaml")
        config.KEY_FILE = os.path.join(config.CONFIG_DIR, "secret.key")
        config.LEGACY_INI_FILE = os.path.join(tmp, "config.ini")

        # keyring 抽象层 mock 为可用：_get/_set 走内存 dict
        self._kr_store = {}
        self._kr_available = True
        self._kr_patchers = [
            mock.patch.object(config, "_keyring_available",
                              side_effect=lambda: self._kr_available),
            mock.patch.object(config, "_get_key",
                              side_effect=lambda: self._kr_store.get("k")),
            mock.patch.object(config, "_set_key",
                              side_effect=lambda v: self._set_kr(v)),
        ]
        for p in self._kr_patchers:
            p.start()
            self.addCleanup(p.stop)

    def _set_kr(self, value):
        self._kr_store["k"] = value
        return True

    def make_keyring_unavailable(self):
        """把 keyring 切为不可用（用于降级用例）。"""
        self._kr_available = False

    def tearDown(self):
        (config.CONFIG_DIR, config.CONFIG_FILE,
         config.KEY_FILE, config.LEGACY_INI_FILE) = self._orig
        self._tmp.cleanup()
