"""config.py 配置层验证：YAML 回环、Fernet 加密落盘、失败路径与 ini 迁移。

运行：python -m unittest discover -s tests -v
不依赖真实 Rocket.Chat 服务器，不触碰用户真实配置目录（全部隔离到临时目录）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from tests.helpers import KeyringIsolatedTestCase


class ConfigTestBase(KeyringIsolatedTestCase):
    """把配置目录/密钥/旧 ini 全部隔离到临时目录，keyring mock 为可用。"""


class TestConfigRoundtrip(ConfigTestBase):

    def test_save_load_roundtrip_password_encrypted_on_disk(self):
        config.save_config(server_url="https://chat.example.com",
                           username="alice", password="s3cret!",
                           remember_password=True, e2e_password="e2e-pass")
        cfg = config.load_config()
        self.assertEqual(cfg["server_url"], "https://chat.example.com")
        self.assertEqual(cfg["username"], "alice")
        self.assertEqual(cfg["password"], "s3cret!")
        self.assertEqual(cfg["e2e_password"], "e2e-pass")
        self.assertTrue(cfg["remember_password"])

        with open(config.CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = f.read()
        # 明文绝不落盘
        self.assertNotIn("s3cret!", raw)
        self.assertNotIn("e2e-pass", raw)
        # 落盘的是 Fernet 密文（gAAAA 开头的 token）
        compact = raw.replace(" ", "")
        self.assertIn("password:gAAAA", compact)
        self.assertIn("e2e_password:gAAAA", compact)

    def test_remember_password_false_keeps_password_empty(self):
        config.save_config(server_url="https://x", username="bob",
                           password="pw", remember_password=False,
                           e2e_password="e2e")
        cfg = config.load_config()
        self.assertEqual(cfg["password"], "")
        self.assertFalse(cfg["remember_password"])
        # e2e 密码不受 remember_password 影响，仍加密保存
        self.assertEqual(cfg["e2e_password"], "e2e")


class TestConfigFailurePaths(ConfigTestBase):
    """负面用例：损坏的密文 / 密钥 / YAML 必须静默降级为默认值，不得抛异常。"""

    def test_corrupt_ciphertext_returns_empty_password(self):
        config.save_config(password="pw", remember_password=True)
        with open(config.CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write("server:\n  password: not-a-valid-fernet-token\n")
        cfg = config.load_config()
        self.assertEqual(cfg["password"], "")

    def test_keyring_unavailable_password_not_persisted(self):
        """keyring 不可用时密码不落盘（每次手动登录），对应旧「密钥损坏/缺失」降级。"""
        self.make_keyring_unavailable()
        config.save_config(password="s3cret!", remember_password=True,
                           e2e_password="e2e-pass", llm_token="tk-llm",
                           upload_service_token="up-tk")
        cfg = config.load_config()
        self.assertEqual(cfg["password"], "")
        self.assertEqual(cfg["e2e_password"], "")
        self.assertEqual(cfg["llm_token"], "")
        self.assertEqual(cfg["upload_service_token"], "")
        # 明文绝不落盘
        with open(config.CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("s3cret!", raw)
        self.assertNotIn("e2e-pass", raw)

    def test_corrupt_key_in_keyring_returns_empty_password(self):
        """keyring 里存了损坏密钥：解密失败按空密码处理（对应旧「损坏密钥文件」）。"""
        config.save_config(password="pw", remember_password=True)
        self._kr_store["k"] = "corrupted-key-material"
        cfg = config.load_config()
        self.assertEqual(cfg["password"], "")

    def test_corrupt_yaml_returns_defaults(self):
        config.save_config(password="pw", remember_password=True)
        with open(config.CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write(":::: not yaml ::::")
        cfg = config.load_config()
        self.assertEqual(cfg, config._defaults())

    def test_missing_config_returns_defaults(self):
        self.assertEqual(config.load_config(), config._defaults())


class TestKeyringKey(ConfigTestBase):
    """密钥由 keyring 托管：生成、幂等、旧 secret.key 清理。"""

    def test_key_generated_and_stored_in_keyring(self):
        config._encrypt_password("pw")
        # keyring 抽象层（内存 dict）里应有密钥
        self.assertIn("k", self._kr_store)
        self.assertTrue(self._kr_store["k"])

    def test_key_persists_across_sessions(self):
        k1 = config._load_or_create_key()
        k2 = config._load_or_create_key()
        self.assertEqual(k1, k2)

    def test_legacy_key_file_purged(self):
        # 模拟旧版本残留的 secret.key，首次访问密钥时应被删除
        os.makedirs(config.CONFIG_DIR, exist_ok=True)
        with open(config.KEY_FILE, "wb") as f:
            f.write(b"legacy-key")
        self.assertTrue(os.path.exists(config.KEY_FILE))
        config._load_or_create_key()
        self.assertFalse(os.path.exists(config.KEY_FILE))

    def test_upload_service_token_encrypted_on_disk(self):
        config.save_config(server_url="https://s", username="u",
                           upload_service_url="https://img.example.com/up",
                           upload_service_token="tk-secret")
        with open(config.CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("tk-secret", raw)
        compact = raw.replace(" ", "")
        self.assertIn("token:gAAAA", compact)


class TestLegacyIniMigration(ConfigTestBase):

    def _write_ini(self):
        with open(config.LEGACY_INI_FILE, "w", encoding="utf-8") as f:
            f.write("[server]\n"
                    "server_url = http://192.168.1.100:3000\n"
                    "username = legacy_user\n"
                    "password = legacy_pw\n"
                    "remember_password = true\n")

    def test_ini_imported_to_yaml_and_password_encrypted(self):
        self._write_ini()
        cfg = config.load_config()
        self.assertEqual(cfg["server_url"], "http://192.168.1.100:3000")
        self.assertEqual(cfg["username"], "legacy_user")
        self.assertEqual(cfg["password"], "legacy_pw")
        self.assertTrue(cfg["remember_password"])
        # 已转存 YAML，且明文不落盘
        self.assertTrue(os.path.exists(config.CONFIG_FILE))
        with open(config.CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("legacy_pw", raw)

    def test_ini_not_read_again_after_yaml_exists(self):
        self._write_ini()
        self.assertIsNotNone(config.load_config())
        os.remove(config.LEGACY_INI_FILE)
        cfg = config.load_config()  # YAML 已存在，不依赖 ini
        self.assertEqual(cfg["username"], "legacy_user")

    def test_malformed_ini_returns_defaults(self):
        with open(config.LEGACY_INI_FILE, "w", encoding="utf-8") as f:
            f.write("this is not an ini file\x00\x01")
        cfg = config.load_config()
        self.assertEqual(cfg, config._defaults())


class TestSaveE2EPassword(ConfigTestBase):

    def test_update_e2e_password_preserves_other_fields(self):
        config.save_config(server_url="https://s", username="u",
                           password="pw", remember_password=True)
        config.save_e2e_password("new-e2e")
        cfg = config.load_config()
        self.assertEqual(cfg["server_url"], "https://s")
        self.assertEqual(cfg["username"], "u")
        self.assertEqual(cfg["password"], "pw")
        self.assertTrue(cfg["remember_password"])
        self.assertEqual(cfg["e2e_password"], "new-e2e")

    def test_empty_e2e_password_clears_field(self):
        config.save_config(e2e_password="old")
        config.save_e2e_password("")
        self.assertEqual(config.load_config()["e2e_password"], "")


class TestSaveServerURL(ConfigTestBase):

    def test_save_server_url_roundtrip(self):
        config.save_config(server_url="https://old.example.com",
                           username="u", password="pw",
                           remember_password=True, auto_login=True)
        config.save_server_url("https://new.example.com")
        cfg = config.load_config()
        self.assertEqual(cfg["server_url"], "https://new.example.com")
        # 其余字段保持不变
        self.assertEqual(cfg["username"], "u")
        self.assertEqual(cfg["password"], "pw")
        self.assertTrue(cfg["auto_login"])

    def test_save_server_url_trims_and_preserves_fields(self):
        config.save_config(server_url="https://s", username="u",
                           remember_password=True)
        config.save_appearance(theme="dark", font_size=16)
        config.save_server_url("  https://t.example.com  ")
        cfg = config.load_config()
        self.assertEqual(cfg["server_url"], "https://t.example.com")
        self.assertEqual(cfg["theme"], "dark")
        self.assertEqual(cfg["font_size"], 16)


class TestAutoLogin(ConfigTestBase):

    def test_auto_login_roundtrip(self):
        config.save_config(server_url="https://s", username="u",
                           password="pw", remember_password=True,
                           auto_login=True)
        cfg = config.load_config()
        self.assertTrue(cfg["auto_login"])
        self.assertTrue(cfg["remember_password"])

        # 关闭开关不应牵连凭据
        config.save_auto_login(False)
        cfg = config.load_config()
        self.assertFalse(cfg["auto_login"])
        self.assertEqual(cfg["username"], "u")
        self.assertEqual(cfg["password"], "pw")

    def test_auto_login_defaults_off(self):
        config.save_config(server_url="https://s", username="u",
                           password="pw", remember_password=True)
        self.assertFalse(config.load_config()["auto_login"])

    def test_save_login_keeps_auto_login_and_appearance(self):
        config.save_login("https://s", "u", "pw", remember_password=True)
        config.save_auto_login(True)
        config.save_appearance(theme="dark", font_size=16)
        config.save_login("https://t", "carol", "pw2", remember_password=True,
                          e2e_password="e2e")
        cfg = config.load_config()
        self.assertTrue(cfg["auto_login"])
        self.assertEqual(cfg["server_url"], "https://t")
        self.assertEqual(cfg["password"], "pw2")
        self.assertEqual(cfg["e2e_password"], "e2e")
        self.assertEqual(cfg["theme"], "dark")
        self.assertEqual(cfg["font_size"], 16)


class TestPartialSaversPreserveEverything(ConfigTestBase):
    """回归：局部保存器曾因漏传 appearance 把主题/字体静默抹掉。"""

    def test_each_partial_saver_keeps_unrelated_fields(self):
        config.save_login("https://s", "u", "pw", remember_password=True,
                          e2e_password="e2e-old")
        config.save_auto_login(True)
        config.save_pinned_rooms(["room-a", "room-b"])
        config.save_appearance(theme="dark", accent_color="#10AEFF",
                               font_family="SimHei", font_size=16)

        savers = [
            ("save_e2e_password", lambda: config.save_e2e_password("e2e-new")),
            ("save_pinned_rooms", lambda: config.save_pinned_rooms(["room-c"])),
            ("save_auto_login", lambda: config.save_auto_login(False)),
            ("save_appearance", lambda: config.save_appearance(font_size=20)),
        ]
        for label, mutate in savers:
            mutate()
            cfg = config.load_config()
            self.assertEqual(cfg["server_url"], "https://s", label)
            self.assertEqual(cfg["username"], "u", label)
            self.assertEqual(cfg["password"], "pw", label)
            self.assertEqual(cfg["theme"], "dark", label)
            self.assertEqual(cfg["accent_color"], "#10AEFF", label)
            self.assertEqual(cfg["font_family"], "SimHei", label)

        cfg = config.load_config()
        self.assertEqual(cfg["e2e_password"], "e2e-new")
        self.assertEqual(cfg["pinned_rooms"], ["room-c"])
        self.assertFalse(cfg["auto_login"])
        self.assertEqual(cfg["font_size"], 20)


class TestUploadService(ConfigTestBase):
    """上传服务配置：默认未配置，保存/加载回环，局部保存器不抹掉它。"""

    def test_defaults_empty(self):
        cfg = config.load_config()
        self.assertEqual(cfg["upload_service_url"], "")
        self.assertEqual(cfg["upload_service_token"], "")

    def test_save_load_roundtrip(self):
        config.save_config(server_url="https://s", username="u",
                           password="pw", remember_password=True,
                           upload_service_url="https://img.example.com/up",
                           upload_service_token="tk-secret")
        cfg = config.load_config()
        self.assertEqual(cfg["upload_service_url"], "https://img.example.com/up")
        self.assertEqual(cfg["upload_service_token"], "tk-secret")
        self.assertEqual(cfg["server_url"], "https://s")

    def test_save_upload_service(self):
        config.save_config(server_url="https://s", username="u")
        config.save_upload_service("https://img.example.com/up", "tk-secret")
        cfg = config.load_config()
        self.assertEqual(cfg["upload_service_url"], "https://img.example.com/up")
        self.assertEqual(cfg["upload_service_token"], "tk-secret")
        # 空串清空（恢复未配置）
        config.save_upload_service("  ")
        cfg = config.load_config()
        self.assertEqual(cfg["upload_service_url"], "")
        self.assertEqual(cfg["upload_service_token"], "")

    def test_partial_savers_keep_upload_service(self):
        config.save_config(server_url="https://s", username="u",
                           password="pw", remember_password=True,
                           upload_service_url="https://img.example.com/up",
                           upload_service_token="tk-secret",
                           auto_login=True)
        config.save_e2e_password("e2e")
        config.save_appearance(theme="dark")
        config.save_storage_dir("/tmp/store")
        cfg = config.load_config()
        self.assertEqual(cfg["upload_service_url"], "https://img.example.com/up")
        self.assertEqual(cfg["upload_service_token"], "tk-secret")
        self.assertEqual(cfg["auto_login"], True)

    def test_enabled_default_false(self):
        cfg = config.load_config()
        self.assertFalse(cfg["upload_service_enabled"])

    def test_enabled_roundtrip(self):
        config.save_config(server_url="https://s", username="u",
                           upload_service_url="https://img.example.com/up",
                           upload_service_enabled=True)
        self.assertTrue(config.load_config()["upload_service_enabled"])
        config.save_upload_service("https://img.example.com/up",
                                   enabled=False)
        self.assertFalse(config.load_config()["upload_service_enabled"])

    def test_enabled_migrates_from_configured_url(self):
        """旧配置没有 enabled 键：已配置 url 视为在用（保持旧行为）。"""
        config.save_config(server_url="https://s", username="u",
                           upload_service_url="https://img.example.com/up")
        with open(config.CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = f.read()
        # 手工删掉 enabled 行，模拟旧版本写入的配置
        lines = [ln for ln in raw.splitlines() if "enabled" not in ln]
        with open(config.CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.assertTrue(config.load_config()["upload_service_enabled"])
        # 从未配置过的全新配置 → 默认关
        config.save_config(server_url="https://s", username="u")
        self.assertFalse(config.load_config()["upload_service_enabled"])


class TestLLMConfig(ConfigTestBase):
    """AI 配置：默认关闭、token 密文落盘、局部保存不丢其他字段。"""

    def test_defaults_disabled_and_empty(self):
        cfg = config.load_config()
        self.assertEqual(cfg["llm_url"], "")
        self.assertEqual(cfg["llm_model"], "")
        self.assertEqual(cfg["llm_token"], "")
        self.assertFalse(cfg["llm_enabled"])
        self.assertFalse(cfg["llm_consent"])

    def test_save_llm_roundtrip_token_encrypted(self):
        config.save_llm(url=" https://ai.example.com/v1 ",
                        model=" qwen3 ", token="tk-llm-secret",
                        enabled=True, consent=True)
        cfg = config.load_config()
        self.assertEqual(cfg["llm_url"], "https://ai.example.com/v1")
        self.assertEqual(cfg["llm_model"], "qwen3")
        self.assertEqual(cfg["llm_token"], "tk-llm-secret")
        self.assertTrue(cfg["llm_enabled"])
        self.assertTrue(cfg["llm_consent"])

        with open(config.CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("tk-llm-secret", raw)
        compact = raw.replace(" ", "")
        self.assertIn("token:gAAAA", compact)

    def test_partial_save_preserves_other_llm_fields(self):
        config.save_llm(url="https://u", model="m", token="tk",
                        enabled=True)
        config.save_llm(enabled=False)
        cfg = config.load_config()
        self.assertEqual(cfg["llm_url"], "https://u")
        self.assertEqual(cfg["llm_model"], "m")
        self.assertEqual(cfg["llm_token"], "tk")
        self.assertFalse(cfg["llm_enabled"])

    def test_save_llm_preserves_login_and_appearance(self):
        config.save_config(server_url="https://s", username="u",
                           password="pw", remember_password=True)
        config.save_appearance(theme="dark", font_size=15)
        config.save_llm(url="https://u", model="m", token="tk")
        cfg = config.load_config()
        self.assertEqual(cfg["server_url"], "https://s")
        self.assertEqual(cfg["password"], "pw")
        self.assertEqual(cfg["theme"], "dark")
        self.assertEqual(cfg["font_size"], 15)
        self.assertEqual(cfg["llm_model"], "m")

    def test_legacy_config_without_llm_section_uses_defaults(self):
        config.save_config(server_url="https://s", username="u")
        with open(config.CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = f.read()
        # 手工删掉整个 llm 段（含缩进子项），模拟旧版本配置
        out, skipping = [], False
        for ln in raw.splitlines():
            if ln.startswith("llm:"):
                skipping = True
                continue
            if skipping and (ln.startswith(" ") or not ln.strip()):
                continue
            skipping = False
            out.append(ln)
        with open(config.CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        cfg = config.load_config()
        self.assertEqual(cfg["llm_url"], "")
        self.assertFalse(cfg["llm_enabled"])

    def test_corrupt_llm_token_returns_empty(self):
        config.save_llm(token="tk")
        with open(config.CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = f.read()
        raw = raw.replace("token: gAAAA", "token: bad-")
        with open(config.CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write(raw)
        self.assertEqual(config.load_config()["llm_token"], "")


if __name__ == "__main__":
    unittest.main()
