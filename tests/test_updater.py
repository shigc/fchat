"""自动更新模块测试：core/updater.py 纯逻辑 + 设置页/主窗口接线。

覆盖：
- 版本比较（补齐/畸形/v 前缀）
- 更新配置加载（开发态读 update.yaml / 打包态 _update_cfg 模块 / 缺失降级）
- manifest 拉取（格式校验）与下载（流式落盘、.part 原子改名、进度回调、取消）
- sha256 校验
- config.update_auto_check 读写往返、局部保存器不抹掉
- 设置页开关接线；MainWindow._check_for_update 门控（dev 跳过自动检查、
  手动检查走 run_async、已是最新不弹窗、确认弹窗触发）

GUI 测试 offscreen 渲染、不联网；requests 全部打桩。
运行：python -m unittest discover -s tests -v
"""
import hashlib
import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402
from tests.helpers import install_keyring_mock  # noqa: E402
from core import updater  # noqa: E402
from core.version import APP_VERSION  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def sync_run_async(fn, on_done=None, on_fail=None, parent=None):
    """run_async 的同步替身：直接在当前线程执行并回调。"""
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001
        if on_fail:
            on_fail(e)
        return
    if on_done:
        on_done(result)


# --------------------------------------------------------------------- #
# 版本比较
# --------------------------------------------------------------------- #
class VersionCompareTest(unittest.TestCase):

    def test_strictly_greater(self):
        self.assertTrue(updater.is_new_version("1.0.1", "1.0.0"))
        self.assertTrue(updater.is_new_version("2.0", "1.9.9"))
        self.assertFalse(updater.is_new_version("1.0.0", "1.0.0"))
        self.assertFalse(updater.is_new_version("1.0.0", "1.0.1"))

    def test_pads_short_versions(self):
        self.assertTrue(updater.is_new_version("1.1", "1.0.9"))
        self.assertFalse(updater.is_new_version("1.0", "1.0.0"))

    def test_v_prefix(self):
        self.assertTrue(updater.is_new_version("v1.2.4", "1.2.3"))

    def test_malformed_is_never_new(self):
        self.assertFalse(updater.is_new_version("abc", "1.0.0"))
        self.assertFalse(updater.is_new_version("1.0.0", ""))
        self.assertFalse(updater.is_new_version("", ""))
        self.assertFalse(updater.is_new_version("1.x.0", "1.0.0"))


# --------------------------------------------------------------------- #
# 更新配置加载
# --------------------------------------------------------------------- #
class UpdateConfigTest(unittest.TestCase):

    def tearDown(self):
        sys.modules.pop("_update_cfg", None)

    def test_frozen_uses_compiled_module(self):
        """打包态：_update_cfg 模块（机器码）优先，不读任何 yaml。"""
        sys.modules["_update_cfg"] = types.SimpleNamespace(
            BASE_URL="http://10.0.0.1/upd", MANIFEST="m.json")
        with mock.patch.object(updater, "_project_root",
                               return_value="Z:/nonexistent"):
            cfg = updater.load_update_config()
        self.assertEqual(cfg["base_url"], "http://10.0.0.1/upd")
        self.assertEqual(cfg["manifest"], "m.json")

    def test_dev_reads_env_config(self):
        # 开发态：从环境变量（.env 已由 load_dotenv 灌入）读取更新地址
        with mock.patch.dict(os.environ,
                             {"FCHAT_UPDATE_BASE_URL": "http://x/upd",
                              "FCHAT_UPDATE_MANIFEST": "m.json"}):
            cfg = updater.load_update_config()
        self.assertEqual(cfg["base_url"], "http://x/upd")
        self.assertEqual(cfg["manifest"], "m.json")

    def test_dev_manifest_defaults_when_blank(self):
        with mock.patch.dict(os.environ,
                             {"FCHAT_UPDATE_BASE_URL": "http://x/upd",
                              "FCHAT_UPDATE_MANIFEST": ""}):
            cfg = updater.load_update_config()
        self.assertEqual(cfg["manifest"], "manifest.json")

    def test_missing_env_degrades_to_disabled(self):
        # 打包态下 staging 里有 _update_cfg.pyd（sys.modules 打桩为 None
        # 使其 import 失败），才能走到 .env 回退分支
        # load_dotenv 也必须一并 mock：有本机 .env 的开发机上，clear 掉的
        # os.environ 会被它重新灌入，base_url 永远非空
        with mock.patch.dict(sys.modules, {"_update_cfg": None}), \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("dotenv.load_dotenv"):
            cfg = updater.load_update_config()
        self.assertEqual(cfg["base_url"], "")

    def test_manifest_url_join(self):
        self.assertEqual(
            updater.manifest_url({"base_url": "http://x/y/",
                                  "manifest": "manifest.json"}),
            "http://x/y/manifest.json")


# --------------------------------------------------------------------- #
# manifest 拉取 / 下载 / 校验
# --------------------------------------------------------------------- #
def _resp(json_data=None, body=b"", headers=None):
    r = mock.Mock()
    r.headers = headers or {}
    if json_data is not None:
        r.json.return_value = json_data
        r.raise_for_status = mock.Mock()

        def _iter(n):
            yield body
        r.iter_content = _iter
    else:
        r.raise_for_status.side_effect = RuntimeError("http 404")
    return r


class FetchManifestTest(unittest.TestCase):

    def test_valid_manifest(self):
        data = {"version": "1.1.0", "url": "Fchat-1.1.0-setup.exe"}
        with mock.patch.object(updater.requests, "get",
                               return_value=_resp(json_data=data)) as g:
            out = updater.fetch_manifest("https://x/upd")
        g.assert_called_once()
        self.assertEqual(out["version"], "1.1.0")
        args, kw = g.call_args
        self.assertEqual(args[0], "https://x/upd/manifest.json")

    def test_plain_http_manifest_url_refused(self):
        """更新链路禁止明文 http（清单与安装包同源目录，sha256 防不了篡改）。"""
        with mock.patch.object(updater.requests, "get") as g:
            with self.assertRaises(ValueError):
                updater.fetch_manifest("http://x/upd")
        g.assert_not_called()

    def test_malformed_manifest_raises(self):
        for bad in ({"version": "1.1.0"},          # 缺 url
                    {"url": "x.exe"},              # 缺 version
                    "not-a-dict"):
            with mock.patch.object(updater.requests, "get",
                                   return_value=_resp(json_data=bad)):
                with self.assertRaises(ValueError):
                    updater.fetch_manifest("https://x")

    def test_http_error_propagates(self):
        with mock.patch.object(updater.requests, "get",
                               return_value=_resp()):
            with self.assertRaises(RuntimeError):
                updater.fetch_manifest("https://x")


class DownloadTest(unittest.TestCase):

    def test_download_streams_and_atomic_rename(self):
        body = b"setup-exe-bytes" * 1000
        resp = _resp(json_data={"ok": True}, body=body,
                     headers={"Content-Length": str(len(body))})
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(updater.requests, "get",
                                   return_value=resp) as g:
                out = updater.download_installer(
                    "https://x/upd/Fchat-1.1.0-setup.exe", tmp,
                    lambda d, t: seen.append((d, t)))
            self.assertEqual(os.path.basename(out),
                             "Fchat-1.1.0-setup.exe")
            with open(out, "rb") as f:
                self.assertEqual(f.read(), body)
            self.assertFalse(os.path.exists(out + ".part"))
        self.assertTrue(seen)
        self.assertEqual(seen[-1][1], len(body))
        args, _ = g.call_args
        self.assertEqual(args[0], "https://x/upd/Fchat-1.1.0-setup.exe")

    def test_cancel_raises_and_keeps_part(self):
        body = b"x" * 4096
        resp = _resp(json_data={"ok": True}, body=body,
                     headers={"Content-Length": str(len(body))})

        def _cancel(_d, _t):
            raise updater.DownloadCancelled()

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(updater.requests, "get",
                                   return_value=resp):
                with self.assertRaises(updater.DownloadCancelled):
                    updater.download_installer("https://x/upd/a.exe", tmp,
                                               _cancel)
            self.assertFalse(os.path.exists(
                os.path.join(tmp, "a.exe")))

    def test_installer_filename_strips_query(self):
        self.assertEqual(
            updater.installer_filename("https://x/a.exe?t=1"), "a.exe")
        self.assertEqual(updater.installer_filename(""), "fchat-setup.exe")

    def test_installer_filename_never_escapes_quotes_or_dir(self):
        """名字会被写进「等退出→起安装器」的 .cmd 批处理。

        裸引号能跳出引号执行命令，反斜杠/`..` 能写到更新目录之外，
        所以只允许 [A-Za-z0-9._-] 且必须以 .exe 结尾。
        """
        for bad in ('a".exe & calc &"', "a&b.exe", r"..\..\evil.exe",
                    "a|b.exe", "a b.exe", "%FAKE%.exe", ".."):
            got = updater.installer_filename(f"https://x/{bad}")
            self.assertRegex(got, r"^[A-Za-z0-9._-]+$")
            self.assertNotIn("..", got, got)
        self.assertEqual(updater.installer_filename("https://x/.exe"),
                         "fchat-setup.exe")

    def test_plain_http_installer_url_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(updater.requests, "get") as g:
                with self.assertRaises(ValueError):
                    updater.download_installer("http://x/upd/a.exe", tmp)
        g.assert_not_called()


class Sha256Test(unittest.TestCase):

    def test_verify(self):
        data = b"hello-installer"
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "a.exe")
            with open(p, "wb") as f:
                f.write(data)
            good = hashlib.sha256(data).hexdigest()
            self.assertTrue(updater.verify_sha256(p, good))
            self.assertTrue(updater.verify_sha256(p, good.upper()))
            self.assertFalse(updater.verify_sha256(p, "0" * 64))
            self.assertFalse(updater.verify_sha256(p, "tooshort"))
            self.assertFalse(updater.verify_sha256(p, ""))
            self.assertFalse(updater.verify_sha256(
                os.path.join(tmp, "missing.exe"), good))


# --------------------------------------------------------------------- #
# 配置链路
# --------------------------------------------------------------------- #
class ConfigTestBase(unittest.TestCase):
    """把配置目录/密钥隔离到临时目录（照 test_config 惯例）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = self._tmp.name
        self._orig = (config.CONFIG_DIR, config.CONFIG_FILE,
                      config.KEY_FILE, config.LEGACY_INI_FILE)
        config.CONFIG_DIR = os.path.join(tmp, "cfgdir")
        config.CONFIG_FILE = os.path.join(config.CONFIG_DIR, "config.yaml")
        config.KEY_FILE = os.path.join(config.CONFIG_DIR, "secret.key")
        config.LEGACY_INI_FILE = os.path.join(tmp, "config.ini")
        install_keyring_mock(self)

    def tearDown(self):
        (config.CONFIG_DIR, config.CONFIG_FILE,
         config.KEY_FILE, config.LEGACY_INI_FILE) = self._orig
        self._tmp.cleanup()


class UpdateAutoCheckConfigTest(ConfigTestBase):

    def test_default_on(self):
        self.assertTrue(config.load_config()["update_auto_check"])

    def test_roundtrip_and_preserved_by_other_savers(self):
        config.save_config(server_url="https://s", username="u",
                           password="pw", remember_password=True,
                           update_auto_check=False)
        self.assertFalse(config.load_config()["update_auto_check"])
        # 局部保存器不得抹掉开关（回归 save_config 全量覆盖的坑）
        config.save_e2e_password("e2e")
        config.save_appearance(theme="dark")
        config.save_upload_service("http://img")
        cfg = config.load_config()
        self.assertFalse(cfg["update_auto_check"])
        self.assertEqual(cfg["server_url"], "https://s")
        self.assertEqual(cfg["theme"], "dark")

    def test_save_update_auto_check(self):
        config.save_config(server_url="https://s", username="u")
        config.save_update_auto_check(False)
        self.assertFalse(config.load_config()["update_auto_check"])
        config.save_update_auto_check(True)
        self.assertTrue(config.load_config()["update_auto_check"])


# --------------------------------------------------------------------- #
# 设置页 / 主窗口接线
# --------------------------------------------------------------------- #
class SettingsPageUpdateTest(unittest.TestCase):

    def setUp(self):
        from ui.settings_page import SettingsPage
        self.page = SettingsPage()

    def tearDown(self):
        self.page.deleteLater()

    def test_switch_exists_and_emits(self):
        seen = []
        self.page.update_auto_check_toggled.connect(seen.append)
        # 初始未勾选（与其它开关一致，主窗口启动时从配置恢复）
        self.assertFalse(self.page.update_auto_switch.isChecked())
        self.page.update_auto_switch.setChecked(True)
        self.assertEqual(seen, [True])

    def test_setter_does_not_emit(self):
        seen = []
        self.page.update_auto_check_toggled.connect(seen.append)
        self.page.set_update_auto_check(False)
        self.assertFalse(self.page.update_auto_switch.isChecked())
        self.assertEqual(seen, [])

    def test_about_page_version_from_single_source(self):
        from ui.settings_page import APP_VERSION as PAGE_VER
        self.assertEqual(PAGE_VER, APP_VERSION)


class MainWindowUpdateWiringTest(unittest.TestCase):
    """主窗口更新链路：门控 / 已最新不弹窗 / 有新版弹确认。"""

    def setUp(self):
        from ui.main_window import MainWindow
        self._tmp = tempfile.TemporaryDirectory()
        tmp = self._tmp.name
        self._orig = (config.CONFIG_DIR, config.CONFIG_FILE,
                      config.KEY_FILE, config.LEGACY_INI_FILE)
        config.CONFIG_DIR = os.path.join(tmp, "cfgdir")
        config.CONFIG_FILE = os.path.join(config.CONFIG_DIR, "config.yaml")
        config.KEY_FILE = os.path.join(config.CONFIG_DIR, "secret.key")
        config.LEGACY_INI_FILE = os.path.join(tmp, "config.ini")

        class _NoLogin(MainWindow):
            def _try_auto_login(self):
                pass

        self.win = _NoLogin()

    def tearDown(self):
        self.win.realtime and self.win.realtime.stop()
        self.win.deleteLater()
        (config.CONFIG_DIR, config.CONFIG_FILE,
         config.KEY_FILE, config.LEGACY_INI_FILE) = self._orig
        self._tmp.cleanup()

    def test_auto_check_skipped_in_dev(self):
        """非打包环境（sys.frozen 缺失）自动检查直接跳过。"""
        with mock.patch("ui.main_window.run_async") as ra:
            self.win._check_for_update(manual=False)
        ra.assert_not_called()

    def test_auto_check_skipped_when_switch_off(self):
        config.save_update_auto_check(False)
        with mock.patch("sys.frozen", True, create=True), \
                mock.patch("ui.main_window.run_async") as ra:
            self.win._check_for_update(manual=False)
        ra.assert_not_called()

    def test_auto_check_runs_when_frozen_and_enabled(self):
        manifest = {"version": "99.0.0", "url": "a.exe", "sha256": "0" * 64}
        with mock.patch("sys.frozen", True, create=True), \
                mock.patch.dict(os.environ,
                                {"FCHAT_UPDATE_BASE_URL": "http://x/upd"}), \
                mock.patch("ui.main_window.run_async", sync_run_async), \
                mock.patch("core.updater.fetch_manifest",
                           return_value=manifest) as fm, \
                mock.patch("ui.main_window.confirm",
                           return_value=False) as cf:
            self.win._check_for_update(manual=False)
        fm.assert_called_once()
        # 有新版 → 弹确认（默认按钮为取消，返回 False 即不继续）
        self.assertEqual(cf.call_args.args[2], "发现新版本")
        self.assertFalse(self.win._update_busy)

    def test_manual_check_up_to_date_no_confirm(self):
        manifest = {"version": APP_VERSION, "url": "a.exe"}
        with mock.patch.dict(os.environ,
                             {"FCHAT_UPDATE_BASE_URL": "http://x/upd"}), \
                mock.patch("ui.main_window.run_async", sync_run_async), \
                mock.patch("core.updater.fetch_manifest",
                           return_value=manifest), \
                mock.patch("ui.main_window.confirm") as cf:
            self.win._check_for_update(manual=True)
        cf.assert_not_called()
        self.assertFalse(self.win._update_busy)

    def test_launch_installer_writes_restart_batch(self):
        """打包态：写重启批处理经 cmd 启动（等本进程退出→装→自动重启）。"""
        with tempfile.TemporaryDirectory() as td:
            installer = os.path.join(td, "Fchat-99.0.0-setup.exe")
            open(installer, "w").close()
            app_exe = os.path.join(td, "appdir", "fchat.exe")
            with mock.patch("sys.frozen", True, create=True), \
                    mock.patch("sys.executable", app_exe), \
                    mock.patch("PyQt5.QtCore.QProcess.startDetached",
                               return_value=(True, 0)) as sd, \
                    mock.patch("ui.main_window.QTimer.singleShot") as shot:
                self.win._launch_installer(installer)
            self.assertEqual(sd.call_args.args[0], "cmd.exe")
            # 工作目录须在安装包目录：cmd 若留在安装目录会破坏安装器的
            # 目录改名占用探测（1.0.4 真机踩坑）
            self.assertEqual(sd.call_args.args[2], td)
            bat = sd.call_args.args[1][1]
            content = open(bat, encoding="mbcs").read()
            self.assertIn(f'"{installer}"', content)
            self.assertIn(f'"{app_exe}"', content)
            self.assertIn("/SILENT", content)
            # 必须先轮询本进程 PID 退出才动安装器（1.0.2 半卸载踩坑）
            self.assertIn('tasklist /FI "PID eq ', content)
            self.assertIn(str(os.getpid()), content)
            self.assertIn("goto waitpid", content)
            self.assertIn("/LOG=", content)
            shot.assert_called_once()

    def test_launch_installer_dev_runs_installer_only(self):
        """开发态：只起安装器，不写重启批处理、不计划退出。"""
        with mock.patch("PyQt5.QtCore.QProcess.startDetached",
                        return_value=True) as sd, \
                mock.patch("ui.main_window.QTimer.singleShot") as shot:
            self.win._launch_installer("Z:/x/setup.exe")
        self.assertNotEqual(sd.call_args.args[0], "cmd.exe")
        shot.assert_not_called()

    def test_manual_check_failure_degrades(self):
        with mock.patch("ui.main_window.run_async", sync_run_async), \
                mock.patch("core.updater.fetch_manifest",
                           side_effect=RuntimeError("网络不可达")):
            self.win._check_for_update(manual=True)
        self.assertFalse(self.win._update_busy)


if __name__ == "__main__":
    unittest.main()
