"""登录对话框：微信风格。记住服务器/账号，密码是否落盘由「记住密码」决定。

勾选记住密码是自动登录的前提（见 MainWindow._try_auto_login）。
登录管道抽成 LoginRunner，供对话框与静默自动登录共用。
"""
from __future__ import annotations

import logging

from PyQt5.QtCore import Qt, pyqtSignal, QThread, QObject
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QLineEdit, QPushButton, QLabel, QCheckBox, QHBoxLayout,
)

from config import load_config, keyring_available
from core.api_client import ApiClient, ApiAuthError, ApiConnectionError

log = logging.getLogger(__name__)


class LoginWorker(QObject):
    """在后台线程执行的登录任务，避免阻塞 UI。"""
    success = pyqtSignal(object)   # ApiClient
    failed = pyqtSignal(str)

    def __init__(self, server_url: str, username: str, password: str,
                 e2e_password: str = "", upload_service_url: str = "",
                 upload_service_token: str = ""):
        super().__init__()
        self.server_url = server_url
        self.username = username
        self.password = password
        self.e2e_password = e2e_password
        self.upload_service_url = upload_service_url
        self.upload_service_token = upload_service_token

    def run(self):
        import time as _time
        t0 = _time.perf_counter()
        try:
            client = ApiClient(self.server_url, ssl_verify=True,
                               upload_service_url=self.upload_service_url,
                               upload_service_token=self.upload_service_token)
            client.login(self.username, self.password)
            t1 = _time.perf_counter()
            logging.info("[计时] login+me 耗时 %.0f ms", (t1 - t0) * 1000)
            if self.e2e_password:
                # 只构造 E2EManager（纯本地）；密钥初始化走网络，由主窗口
                # 登录成功后异步执行，不挡登录关键路径
                client.create_e2e(self.e2e_password)
            self.success.emit(client)
        except ApiAuthError as e:
            self.failed.emit(str(e))
        except ApiConnectionError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"登录出现未知错误：{e}")


# 放弃等待/父对象已销毁的登录线程由模块级引用保活：销毁仍在运行的
# QThread 会报 "QThread: Destroyed while thread is still running"。
# 这些残留随 main.py 的 os._exit 一起结束，不回收。
_abandoned: list = []


class LoginRunner(QObject):
    """在后台执行登录，并负责 QThread/worker 的收尾。

    登录对话框与主窗口的静默自动登录共用这一条管道。
    """

    success = pyqtSignal(object)   # ApiClient
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = None
        self._worker = None
        # 独立登录窗（parent=None 的 LoginDialog）在登录途中被关闭时会连同
        # 本对象子 QThread 一起析构；赶在被删前解绑（同 api_workers）。
        if parent is not None:
            parent.destroyed.connect(self._on_parent_destroyed)

    def _on_parent_destroyed(self, *_):
        if self._thread is None:
            return
        self.setParent(None)
        self._thread.quit()
        _abandoned.append(self)
        log.debug("登录框已销毁，登录线程已解绑收尾")

    @property
    def running(self) -> bool:
        return self._thread is not None

    def wait_finished(self, timeout_ms: int) -> bool:
        """阻塞等待登录线程结束（主窗口退出时的兜底，勿在别处调用）。"""
        try:
            return self._thread is not None and self._thread.wait(timeout_ms)
        except RuntimeError:
            return True

    def detach(self):
        """退出放弃等待时解绑出主窗口，并保证线程必然能自己结束。

        主窗口销毁会连带销毁还活着的本对象（其内部 QThread 也就跟着被
        销毁，报 "QThread: Destroyed while thread is still running"）；
        解绑后由主窗口的 _login_runner 属性引用保活。worker 可能早已跑完
        （结果排队回主线程，而主线程事件循环随退出关闭，_cleanup 永远
        轮不到），不能靠下一次信号触发收尾——直接 quit 线程事件循环
        （线程安全）：worker 还在跑则循环在 run() 返回后退出，已跑完则
        立即退出。
        """
        self.setParent(None)
        if self._thread is not None:
            self._thread.quit()

    def start(self, server_url: str, username: str, password: str,
              e2e_password: str = "", upload_service_url: str = "",
              upload_service_token: str = ""):
        """server_url 可省略协议，内部自动补 https://。"""
        server = normalize_server(server_url)
        self._cleanup()
        self._thread = QThread(self)
        self._worker = LoginWorker(server, username, password, e2e_password,
                                   upload_service_url=upload_service_url,
                                   upload_service_token=upload_service_token)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.success.connect(self._on_success)
        self._worker.failed.connect(self._on_failed)
        self._thread.start()

    def _on_success(self, client: ApiClient):
        self._cleanup()
        self.success.emit(client)

    def _on_failed(self, msg: str):
        self._cleanup()
        self.failed.emit(msg)

    def _cleanup(self):
        """终止上一轮登录线程并放下引用。"""
        thread, worker = self._thread, self._worker
        self._thread = self._worker = None
        try:
            _abandoned.remove(self)
        except ValueError:
            pass
        if thread is not None:
            if thread.isRunning():
                thread.quit()
                if not thread.wait(1000):
                    # 卡在网络调用里时绝不能 deleteLater：销毁仍在运行的
                    # QThread 正是那条 ERROR 的来源，交模块表保活；
                    # 先脱离父树，否则 parent 被回收仍会连带销毁线程
                    thread.setParent(None)
                    _abandoned.append((thread, worker))
                    return
            thread.deleteLater()
        if worker is not None:
            worker.deleteLater()


def normalize_server(server: str) -> str:
    """补全服务器地址协议，未写则按 https:// 处理。"""
    server = (server or "").strip()
    if server and not server.startswith(("http://", "https://")):
        server = "https://" + server
    return server


class LoginDialog(QDialog):
    login_ok = pyqtSignal(object)  # ApiClient

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("LoginDialog")
        self.setWindowTitle("登录 Rocket.Chat")
        self.setModal(True)
        self.setFixedSize(360, 540)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

        self._runner = LoginRunner(self)
        # 登录结果必须接回来，否则 _set_loading(True) 之后没人解开
        self._runner.success.connect(self._on_success)
        self._runner.failed.connect(self._on_failed)

        self._build_ui()
        self._load_config()

    # ------------------------------------------------------------------ #
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(36, 40, 36, 30)
        root.setSpacing(0)

        title = QLabel("欢迎回来")
        title.setObjectName("LoginTitle")
        title.setAlignment(Qt.AlignCenter)
        root.addWidget(title)

        sub = QLabel("登录你的 Rocket.Chat 工作区")
        sub.setObjectName("LoginSubtitle")
        sub.setAlignment(Qt.AlignCenter)
        root.addSpacing(8)
        root.addWidget(sub)

        root.addSpacing(30)

        server_label = QLabel("服务器地址")
        server_label.setObjectName("LoginLabel")
        server_label.setStyleSheet("color:#666;font-size:12px;margin-bottom:4px;")
        root.addWidget(server_label)
        self.server_input = QLineEdit()
        self.server_input.setObjectName("LoginInput")
        self.server_input.setPlaceholderText("例如 https://chat.example.com")
        root.addWidget(self.server_input)

        root.addSpacing(16)

        user_label = QLabel("用户名")
        user_label.setObjectName("LoginLabel")
        user_label.setStyleSheet("color:#666;font-size:12px;margin-bottom:4px;")
        root.addWidget(user_label)
        self.user_input = QLineEdit()
        self.user_input.setObjectName("LoginInput")
        self.user_input.setPlaceholderText("用户名或邮箱")
        root.addWidget(self.user_input)

        root.addSpacing(16)

        pass_label = QLabel("密码")
        pass_label.setObjectName("LoginLabel")
        pass_label.setStyleSheet("color:#666;font-size:12px;margin-bottom:4px;")
        root.addWidget(pass_label)
        self.pass_input = QLineEdit()
        self.pass_input.setObjectName("LoginInput")
        self.pass_input.setPlaceholderText("输入密码")
        self.pass_input.setEchoMode(QLineEdit.Password)
        self.pass_input.returnPressed.connect(self._on_login_clicked)
        root.addWidget(self.pass_input)

        root.addSpacing(16)

        # E2E 密码不在登录框输入：统一在设置页配置（config 已保存的密码
        # 在登录管道里生效），避免两处入口不同步

        row = QHBoxLayout()
        self.remember_check = QCheckBox("记住密码（自动登录的前提）")
        self.remember_check.setChecked(True)
        row.addWidget(self.remember_check)
        row.addStretch()
        root.addLayout(row)

        # keyring 不可用时密码不落盘，勾选记住密码也无效，提示用户
        if not keyring_available():
            kr_hint = QLabel("当前环境无法安全保存密码，每次需手动登录。")
            kr_hint.setObjectName("LoginHint")
            kr_hint.setWordWrap(True)
            root.addWidget(kr_hint)

        root.addSpacing(24)

        self.error_label = QLabel("")
        self.error_label.setObjectName("LoginError")
        self.error_label.setWordWrap(True)
        self.error_label.setAlignment(Qt.AlignCenter)
        self.error_label.setMaximumHeight(40)
        root.addWidget(self.error_label)

        root.addSpacing(10)

        self.login_button = QPushButton("登 录")
        self.login_button.setObjectName("LoginButton")
        self.login_button.setCursor(Qt.PointingHandCursor)
        self.login_button.clicked.connect(self._on_login_clicked)
        root.addWidget(self.login_button)

        root.addStretch()

    def _load_config(self):
        cfg = load_config()
        self.server_input.setText(cfg["server_url"])
        self.user_input.setText(cfg["username"])
        if cfg["remember_password"] and cfg["password"]:
            self.pass_input.setText(cfg["password"])
        self.remember_check.setChecked(cfg["remember_password"])

    # ------------------------------------------------------------------ #
    def _on_login_clicked(self, *_):
        server = self.server_input.text().strip()
        user = self.user_input.text().strip()
        password = self.pass_input.text()
        if not server or not user or not password:
            self._show_error("请填写服务器地址、用户名和密码")
            return
        self._set_loading(True)
        self._show_error("")
        from config import load_config as _lc
        _cfg = _lc()
        up_url = (_cfg.get("upload_service_url", "")
                  if _cfg.get("upload_service_enabled") else "")
        up_token = (_cfg.get("upload_service_token", "")
                    if _cfg.get("upload_service_enabled") else "")
        self._runner.start(server, user, password,
                           e2e_password=_cfg.get("e2e_password", ""),
                           upload_service_url=up_url,
                           upload_service_token=up_token)

    def _set_loading(self, loading: bool):
        self.login_button.setEnabled(not loading)
        self.login_button.setText("登录中…" if loading else "登 录")
        for w in (self.server_input, self.user_input, self.pass_input,
                  self.remember_check):
            w.setEnabled(not loading)

    def _show_error(self, msg: str):
        self.error_label.setText(msg)

    def set_error(self, msg: str):
        """供自动登录失败时回落使用：预填好账号并显示错误原因。"""
        self._show_error(msg)

    def _on_success(self, client: ApiClient):
        self._set_loading(False)
        # 记住密码时才落盘密码，自动登录才有凭据可用
        from config import save_login
        save_login(
            server_url=normalize_server(self.server_input.text()),
            username=self.user_input.text().strip(),
            password=self.pass_input.text(),
            remember_password=self.remember_check.isChecked(),
        )
        self.login_ok.emit(client)
        self.accept()

    def _on_failed(self, msg: str):
        self._set_loading(False)
        self._show_error(msg)
