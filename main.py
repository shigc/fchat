"""程序入口：Fchat — 界面高仿微信的 Rocket.Chat 桌面客户端。

运行：python main.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _preload_msvc_runtime():
    """先于 PyQt5 把 System32 里的 VC++ 运行库载入进程。

    PyQt5/Qt5/bin 捆着 2020 年的 msvcp140/vcruntime140，PyQt5 导入时会把
    该目录加进 DLL 搜索路径，之后 onnxruntime 的 pyd 按文件名解析运行库
    就命中 Qt 的老副本，DllMain 初始化直接失败（「DLL 初始化例程失败」，
    离线 OCR 因此不可用）。Windows 加载器对同名 DLL 只认先载入的那份，
    所以必须在 PyQt5 之前把系统的新版运行库加载进来；机器上没装 VC++
    运行库时跳过，行为与从前一致。
    """
    if sys.platform != "win32":
        return
    import ctypes
    sys32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                         "System32")
    for name in ("msvcp140", "msvcp140_1", "msvcp140_2",
                 "vcruntime140", "vcruntime140_1", "concrt140"):
        try:
            ctypes.WinDLL(os.path.join(sys32, name + ".dll"))
        except OSError:
            pass


_preload_msvc_runtime()

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QFont, QIcon

RESOURCES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources")
ICON_FILE = os.path.join(RESOURCES_DIR, "fchat.ico")


def main():
    # 日志必须最先初始化（含 excepthook/Qt handler/faulthandler）
    from core.log_setup import setup_logging
    setup_logging()

    # 启动早期、任何并发 HTTPS 之前，单线程预热 SSL 证书（打包版并发
    # 加载 CA 证书会触发 OpenSSL access violation，见 core/ssl_prewarm）
    from core.ssl_prewarm import prewarm_ssl
    prewarm_ssl()

    # 必须在 QApplication 之前设置：否则 Qt5 默认按系统 DPI 感知运行，
    # pt 字号随显示缩放膨胀而代码里的 px 常量不跟着变，高缩放屏幕上
    # 气泡内的时间标签/头像首字等会溢出裁切（换电脑显示异常的根因）。
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)

    app = QApplication(sys.argv)
    app.setApplicationName("Fchat")
    app.setOrganizationName("Fchat")

    # 程序图标
    if os.path.exists(ICON_FILE):
        app.setWindowIcon(QIcon(ICON_FILE))

    # 中文字体
    app.setFont(QFont("Microsoft YaHei", 9))

    # 单实例：已有实例在跑就把它的窗口唤到前台，本进程直接退出。
    # 放在导入 MainWindow 之前，二次启动不付界面初始化成本。
    from ui.single_instance import SingleInstance, instance_key
    single = SingleInstance(instance_key())
    if single.notify_existing():
        return 0
    single.start()

    from ui.main_window import MainWindow
    window = MainWindow()
    window.resize(1080, 700)
    window.show()
    # 窗口就绪前收到的激活请求会补发（见 SingleInstance._pending）
    single.set_activation_handler(window.bring_to_front)

    rc = app.exec_()

    # 退出收尾：closeEvent 放弃等待而解绑的后台线程（登录线程、卡在网络
    # 调用上的 run_async）可能还在跑，正常解释器析构会销毁这些 QThread，
    # 报 "QThread: Destroyed while thread is still running"。这里先按预算
    # 等它们自然结束（quit 已在 detach 时发出），随后必须用 os._exit 跳过
    # 解释器析构——任何还在跑的线程连同 Qt 对象都不再逐一销毁，从根上
    # 消灭该 ERROR。日志 FileHandler 逐条 flush，跳过析构不丢日志
    import logging
    from ui.api_workers import drain_detached
    try:
        left = drain_detached(3000)
        if left:
            logging.getLogger("fchat").warning(
                "退出时有 %d 个后台线程未结束，直接结束进程", left)
    except Exception:  # noqa: BLE001
        pass
    logging.shutdown()
    os._exit(rc)


if __name__ == "__main__":
    main()
