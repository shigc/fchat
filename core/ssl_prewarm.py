"""SSL 预热与 REST 并发控制。

背景（打包版 1.3.0 崩溃定位）：打包版启动时多个线程同时做
REST/HTTPS，崩溃栈显示 9 个线程同时卡在 OpenSSL 的 `load_verify_locations`
（`urllib3/util/ssl_.py:461`，加载 CA 证书），Windows fatal exception
access violation。源码版不崩、打包版崩，本地无法复现，怀疑是 OpenSSL 3
并发加载 CA 证书的竞态被打包版线程时序放大。

本模块做两层防御，从源头降低「多线程并发加载 CA 证书」的压力：
1. `prewarm_ssl()`：启动时**单线程**加载一次 CA 证书（把 certifi 的
   cacert.pem 解析进 OpenSSL），避免首次并发握手时大家同时解析大证书文件；
2. `ConcurrencyAdapter`：给 REST session 挂一个**全局并发信号量**，限制
   同时进行的 HTTPS 请求数，避免极端并发（101 会话状态 + 多历史 + DDP
   轮询同时发起）把 OpenSSL 的证书加载压到临界点。

这两层都不改变请求语义（仍走共享 session、仍验证证书），只是约束并发度。
"""
from __future__ import annotations

import logging
import threading

import certifi
import ssl

from requests.adapters import HTTPAdapter

log = logging.getLogger(__name__)

# 同时进行的 REST/HTTPS 请求上限。启动瞬间历史/成员/回执/用户状态会多路
# 并发（真机日志曾出现并发峰值超过连接池 10 的情况），这里把并发收紧到
# 一个不会把 OpenSSL 证书加载打穿的量级；太低会明显拖慢批量拉取。
_MAX_CONCURRENT_REST = 6

# 全局信号量：所有走 ConcurrencyAdapter 的请求共享，跨 ApiClient 实例生效。
_rest_semaphore = threading.BoundedSemaphore(_MAX_CONCURRENT_REST)

# 是否已预热（进程内只预热一次）
_prewarmed = False
_prewarm_lock = threading.Lock()


def prewarm_ssl() -> None:
    """启动时单线程加载一次 CA 证书（幂等，进程内只做一次）。

    必须在任何并发 HTTPS 请求发出之前调用（main.py 启动早期）。提前把
    certifi 的 cacert.pem 解析进 OpenSSL，让后续并发握手不再各自从头解析
    这个大证书文件（约 200KB、130+ 个 CA），从而避开多线程并发
    `load_verify_locations` 的竞态窗口。
    """
    global _prewarmed
    if _prewarmed:
        return
    with _prewarm_lock:
        if _prewarmed:
            return
        try:
            # 与 requests 默认 verify=True 的加载路径一致：cafile=certifi。
            ctx = ssl.create_default_context(cafile=certifi.where())
            # 触发一次真实的证书加载（create_default_context 已加载，这里
            # 再显式走一遍 load_verify_locations，确保 OpenSSL 侧就绪）。
            ctx.load_verify_locations(certifi.where())
            _prewarmed = True
            log.info("SSL 证书预热完成（%d 个 CA）",
                     _count_certs(ctx))
        except Exception:  # noqa: BLE001
            # 预热失败不致命：后续请求照常走默认加载路径，只是少了预热。
            log.exception("SSL 证书预热失败，按默认路径继续")


def _count_certs(ctx: ssl.SSLContext) -> int:
    """尽力统计 context 里已加载的 CA 数量（失败返回 -1，仅用于日志）。"""
    try:
        store = ctx.cert_store_stats()
        return int(store.get("x509_ca", 0))
    except Exception:  # noqa: BLE001
        return -1


class ConcurrencyAdapter(HTTPAdapter):
    """限制并发请求数的 HTTPAdapter。

    在 `send` 前后获取/释放全局信号量，把同一时刻在途的 REST 请求数约束在
    `_MAX_CONCURRENT_REST` 以内。阻塞等待连接时持有信号量，避免大量线程
    同时进入 OpenSSL 握手/证书加载。
    """

    def send(self, request, *args, **kwargs):
        _rest_semaphore.acquire()
        try:
            return super().send(request, *args, **kwargs)
        finally:
            _rest_semaphore.release()
