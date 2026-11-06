"""storage_dir 配置字段的持久化测试。离线，配置指向临时目录。"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from tests.helpers import install_keyring_mock  # noqa: E402
from core import paths  # noqa: E402


class StorageConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (config.CONFIG_DIR, config.CONFIG_FILE,
                      config.KEY_FILE, config.LEGACY_INI_FILE)
        config.CONFIG_DIR = self._tmp.name
        config.CONFIG_FILE = os.path.join(self._tmp.name, "config.yaml")
        config.KEY_FILE = os.path.join(self._tmp.name, "secret.key")
        config.LEGACY_INI_FILE = os.path.join(self._tmp.name, "config.ini")
        install_keyring_mock(self)

    def tearDown(self):
        (config.CONFIG_DIR, config.CONFIG_FILE,
         config.KEY_FILE, config.LEGACY_INI_FILE) = self._orig
        self._tmp.cleanup()

    def test_default_empty(self):
        self.assertEqual(config.load_config()["storage_dir"], "")

    def test_save_and_load_roundtrip(self):
        config.save_storage_dir(os.path.join(self._tmp.name, "data"))
        self.assertEqual(
            config.load_config()["storage_dir"],
            os.path.abspath(os.path.join(self._tmp.name, "data")))

    def test_save_keeps_other_fields(self):
        config.save_login("https://s", "u", "pw", remember_password=True)
        config.save_storage_dir(os.path.join(self._tmp.name, "data"))
        cfg = config.load_config()
        self.assertEqual(cfg["server_url"], "https://s")
        self.assertEqual(cfg["storage_dir"],
                         os.path.abspath(os.path.join(self._tmp.name, "data")))


class PathsTest(unittest.TestCase):
    """存储根目录、迁移、清空缓存、大小统计。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = self._tmp.name
        self.old_root = os.path.join(tmp, "old")
        self.new_root = os.path.join(tmp, "new")
        os.makedirs(os.path.join(self.old_root, "cache", "avatars"))
        os.makedirs(os.path.join(self.old_root, "logs"))
        with open(os.path.join(self.old_root, "cache", "avatars", "a.png"), "wb") as f:
            f.write(b"avatar")
        with open(os.path.join(self.old_root, "logs", "fchat.log"), "w",
                  encoding="utf-8") as f:
            f.write("hello log")
        # 存储根指向临时旧目录；配置文件指向另一个临时目录
        self._patchers = [
            mock.patch.object(paths, "_current_root", self.old_root),
            mock.patch.object(paths, "_root_loaded", True),
        ]
        for p in self._patchers:
            p.start()
        self._cfg = tempfile.TemporaryDirectory()
        self._cfg_orig = (config.CONFIG_DIR, config.CONFIG_FILE,
                          config.KEY_FILE, config.LEGACY_INI_FILE)
        config.CONFIG_DIR = self._cfg.name
        config.CONFIG_FILE = os.path.join(self._cfg.name, "config.yaml")
        config.KEY_FILE = os.path.join(self._cfg.name, "secret.key")
        # load_config 在 YAML 缺失时会探测 ini 触发旧配置导入，必须一并重定向
        config.LEGACY_INI_FILE = os.path.join(self._cfg.name, "config.ini")
        install_keyring_mock(self)

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        (config.CONFIG_DIR, config.CONFIG_FILE,
         config.KEY_FILE, config.LEGACY_INI_FILE) = self._cfg_orig
        self._tmp.cleanup()
        self._cfg.cleanup()

    def test_subdirs_follow_root(self):
        self.assertEqual(paths.cache_root(), os.path.join(self.old_root, "cache"))
        self.assertEqual(paths.avatars_dir(),
                         os.path.join(self.old_root, "cache", "avatars"))
        self.assertEqual(paths.images_dir(),
                         os.path.join(self.old_root, "cache", "images"))
        self.assertEqual(paths.files_dir(),
                         os.path.join(self.old_root, "cache", "files"))
        self.assertEqual(paths.logs_dir(), os.path.join(self.old_root, "logs"))

    def test_migrate_copies_and_switches(self):
        moved = paths.migrate_storage(self.new_root)
        self.assertEqual(moved, "cache、logs")
        self.assertTrue(os.path.exists(
            os.path.join(self.new_root, "cache", "avatars", "a.png")))
        self.assertTrue(os.path.exists(
            os.path.join(self.new_root, "logs", "fchat.log")))
        # 热切换生效 + 配置持久化
        self.assertEqual(os.path.normcase(paths.storage_root()),
                         os.path.normcase(self.new_root))
        self.assertEqual(
            os.path.normcase(config.load_config()["storage_dir"]),
            os.path.normcase(os.path.abspath(self.new_root)))
        # 旧目录保留（手动兜底，不自动删）
        self.assertTrue(os.path.exists(
            os.path.join(self.old_root, "cache", "avatars", "a.png")))

    def test_migrate_rejects_same_path(self):
        with self.assertRaises(ValueError):
            paths.migrate_storage(self.old_root)

    def test_migrate_rejects_empty_path(self):
        with self.assertRaises(ValueError):
            paths.migrate_storage("")
        self.assertEqual(config.load_config()["storage_dir"], "")

    def test_migrate_failure_keeps_config(self):
        # 目标是一个已存在的文件 → makedirs 抛错 → 配置必须不动
        blocker = os.path.join(self._tmp.name, "blocker")
        with open(blocker, "w") as f:
            f.write("x")
        with self.assertRaises(OSError):
            paths.migrate_storage(blocker)
        self.assertEqual(config.load_config()["storage_dir"], "")
        self.assertEqual(os.path.normcase(paths.storage_root()),
                         os.path.normcase(self.old_root))

    def test_migrate_aborts_when_verify_finds_missing_file(self):
        """已复制文件在目标侧缺失且补拷不回（源被占用）→ 校验必须中止。

        目标文件丢了会被 _verify_tree 的补拷自愈，所以还要模拟补拷阶段
        源文件被其他进程占用（日志/杀软在迁移中途锁文件的真实场景），
        这样校验才能真的失败并中止。
        """
        src_file = os.path.join(self.old_root, "cache", "avatars", "a.png")
        dst_file = os.path.join(self.new_root, "cache", "avatars", "a.png")
        real_copy2 = shutil.copy2
        real_copy_tree = paths._copy_tree
        copied = []

        def flaky_copy2(src, dst, **kw):
            if os.path.normcase(src) == os.path.normcase(src_file):
                # 首次（复制阶段）成功，之后（补拷阶段）源被占用
                if copied:
                    raise PermissionError(13, "源被占用")
                copied.append(src)
            return real_copy2(src, dst, **kw)

        def copy_then_vanish(src, dst):
            skipped = real_copy_tree(src, dst)
            # cache 与 logs 各跑一轮，第二轮里文件已不存在
            if os.path.exists(dst_file):
                os.remove(dst_file)   # 目标侧文件丢失
            return skipped

        with mock.patch.object(paths, "_copy_tree",
                               side_effect=copy_then_vanish), \
                mock.patch.object(paths.shutil, "copy2",
                                  side_effect=flaky_copy2):
            with self.assertRaises(OSError) as ctx:
                paths.migrate_storage(self.new_root)
        self.assertIn("1 个文件不一致", str(ctx.exception))
        # 配置未写入、根目录未切换：旧目录数据原样可用
        self.assertEqual(config.load_config()["storage_dir"], "")
        self.assertEqual(os.path.normcase(paths.storage_root()),
                         os.path.normcase(self.old_root))

    def test_migrate_tolerates_source_locked_files(self):
        """源文件被占用（复制阶段就失败）是文档允许的容忍情形：
        跳过该文件后其余照常迁移成功，不能被校验判死。"""
        src_file = os.path.join(self.old_root, "cache", "avatars", "a.png")
        real_copy2 = shutil.copy2

        def locked_copy2(src, dst, **kw):
            if os.path.normcase(src) == os.path.normcase(src_file):
                raise PermissionError(13, "源被占用")
            return real_copy2(src, dst, **kw)

        with mock.patch.object(paths.shutil, "copy2", side_effect=locked_copy2):
            moved = paths.migrate_storage(self.new_root)
        self.assertEqual(moved, "cache、logs")
        self.assertFalse(os.path.exists(
            os.path.join(self.new_root, "cache", "avatars", "a.png")))
        # 跳过的文件旧目录仍保留（兜底不删）
        self.assertTrue(os.path.exists(src_file))
        self.assertEqual(os.path.normcase(paths.storage_root()),
                         os.path.normcase(self.new_root))

    def test_migrate_merges_existing_target(self):
        os.makedirs(os.path.join(self.new_root, "cache"))
        with open(os.path.join(self.new_root, "cache", "keep.txt"), "w") as f:
            f.write("keep")
        paths.migrate_storage(self.new_root)
        self.assertTrue(os.path.exists(os.path.join(self.new_root, "cache", "keep.txt")))
        self.assertTrue(os.path.exists(
            os.path.join(self.new_root, "cache", "avatars", "a.png")))

    def test_clear_cache_keeps_logs(self):
        paths.clear_cache()
        self.assertFalse(os.path.exists(
            os.path.join(self.old_root, "cache", "avatars", "a.png")))
        self.assertTrue(os.path.exists(
            os.path.join(self.old_root, "logs", "fchat.log")))

    def test_dir_size(self):
        self.assertEqual(paths.dir_size(self.old_root), 6 + len("hello log"))

    def test_migrate_rejects_path_inside_cache_tree(self):
        with self.assertRaises(ValueError):
            paths.migrate_storage(os.path.join(self.old_root, "cache", "sub"))
        with self.assertRaises(ValueError):
            paths.migrate_storage(os.path.join(self.old_root, "cache"))
        self.assertEqual(config.load_config()["storage_dir"], "")

    def test_migrate_aborts_when_config_write_fails(self):
        with mock.patch("config.save_config"):
            with self.assertRaises(OSError):
                paths.migrate_storage(self.new_root)
        # 配置未写入、根目录未切换
        self.assertEqual(config.load_config()["storage_dir"], "")
        self.assertEqual(os.path.normcase(paths.storage_root()),
                         os.path.normcase(self.old_root))


class StorageRootLazyInitTest(unittest.TestCase):
    """storage_root() 懒加载：读配置 → 默认/回退分支。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cfg_orig = (config.CONFIG_DIR, config.CONFIG_FILE,
                          config.KEY_FILE, config.LEGACY_INI_FILE)
        config.CONFIG_DIR = self._tmp.name
        config.CONFIG_FILE = os.path.join(self._tmp.name, "config.yaml")
        config.KEY_FILE = os.path.join(self._tmp.name, "secret.key")
        config.LEGACY_INI_FILE = os.path.join(self._tmp.name, "config.ini")
        install_keyring_mock(self)
        self._state_orig = (paths._current_root, paths._root_loaded)
        paths._root_loaded = False

    def tearDown(self):
        (paths._current_root, paths._root_loaded) = self._state_orig
        (config.CONFIG_DIR, config.CONFIG_FILE,
         config.KEY_FILE, config.LEGACY_INI_FILE) = self._cfg_orig
        self._tmp.cleanup()

    def test_no_storage_dir_defaults_to_config_dir(self):
        self.assertEqual(paths.storage_root(), config.CONFIG_DIR)

    def test_saved_dir_used_when_exists(self):
        saved = os.path.join(self._tmp.name, "data")
        os.makedirs(saved)
        config.save_storage_dir(saved)
        paths._root_loaded = False
        self.assertEqual(os.path.normcase(paths.storage_root()),
                         os.path.normcase(saved))

    def test_saved_dir_missing_falls_back_to_config_dir(self):
        config.save_storage_dir(os.path.join(self._tmp.name, "gone"))
        paths._root_loaded = False
        self.assertEqual(paths.storage_root(), config.CONFIG_DIR)


if __name__ == "__main__":
    unittest.main()
