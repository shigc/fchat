"""异步头像/图片加载器：用 QNetworkAccessManager 拉取并缓存。

两条通道：
- AvatarLoader：头像，供 QLabel 型控件（会话列表、资料卡）直接应用，
  也可被委托用 pixmap_for() 拉取。带内存 + 磁盘缓存。
- ImageLoader：聊天内图片/视频封面，委托在 paint 时调 pixmap_for()，
  图到货后发 image_ready(url)，委托据此重绘等待中的行。

内存防护（大量消息房间闪退的嫌疑点）：
- 缓存条目有上限（LRU 淘汰），避免大房间图片把内存耗尽
- 图片按目标显示尺寸缩放后入缓存，不缓存原始分辨率的巨型 QPixmap
- 回调时检测目标控件是否已销毁，防止悬空引用
- 请求按 url 去重，失败入哨兵不再重试，在途请求有超时兜底

磁盘缓存目录统一取自 core/paths（存储根可被设置页迁移，不能写死）：
头像存 avatars_dir()（.png，加载时磁盘优先于网络）；聊天图片把原始字节
存 images_dir()（内存缓存淘汰后仍能免网络重出图，缩略图按需现缩）。
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time as _time
from collections import OrderedDict
from typing import Optional

from PyQt5.QtCore import QObject, Qt, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QImageReader, QPixmap
from PyQt5.QtNetwork import (
    QNetworkAccessManager, QNetworkReply, QNetworkRequest,
)

from core.paths import avatars_dir, images_dir
from core.url_guard import can_carry_credentials

from .widgets import AvatarWidget

log = logging.getLogger(__name__)

# 头像缓存上限（条）。头像缩略图体积小，200 条足够覆盖会话列表常用回看范围。
# 与聊天图片缓存分离：滚动聊天历史时图片不再把会话列表头像挤出缓存。
_AVATAR_CACHE_LIMIT = 200
# 聊天图片缓存上限（条）。缩略图体积小，300 条覆盖常用回看范围。
_IMAGE_CACHE_LIMIT = 300

# 缩图解码上限：超过该像素数的图片先缩小解码再入缓存，
# 防止单张超大截图（如 8000x6000 ≈ 190MB 位图）拖垮内存
_MAX_DECODE_PIXELS = 12_000_000

# 头像统一按这个边长缩放后入缓存
_AVATAR_PX = 80

# 在途请求超时（秒）：覆盖 finished 信号永不触发的情况
_INFLIGHT_TIMEOUT = 15.0

# 聊天图片缩略图按 HiDPI 冗余倍数缓存：高清屏（devicePixelRatio≥2）下
# 委托把逻辑尺寸画满物理像素，缩略图若只按逻辑尺寸大小生成会被上采样变糊。
# 生成 size*2 的缩略图后委托只做下采样，任意 DPR 下都清晰。
_HIDPI_SCALE = 2

# 加载失败的哨兵：入缓存以免每帧重绘都重发请求
IMG_FAILED = "__img_failed__"


def canon_url(url: str) -> str:
    """URL 统一转成百分号编码形式作在途表/缓存键。

    QUrl.toString() 默认 PrettyDecoded 会把 %20、中文转义解码成明文，
    回包后按 request URL 反查在途键就对不上了（文件名含中文/空格的
    附件全部表现为"图片请求超时"）。键必须统一取 FullyEncoded。
    """
    return QUrl(url).toString(QUrl.FullyEncoded)


def _url_to_cache_path(url: str) -> str:
    """将 URL 转换为头像磁盘缓存文件路径。"""
    h = hashlib.md5(url.encode()).hexdigest()
    return os.path.join(avatars_dir(), f"{h}.png")


def _image_disk_path(url: str) -> str:
    """聊天图片原始字节的磁盘缓存路径（无扩展名，键为 md5(url)）。"""
    h = hashlib.md5(url.encode()).hexdigest()
    return os.path.join(images_dir(), h)


def _save_image_disk(url: str, data: bytes):
    try:
        os.makedirs(images_dir(), exist_ok=True)
        with open(_image_disk_path(url), "wb") as f:
            f.write(data)
    except OSError as e:
        log.warning("图片缓存写盘失败 %s：%s", url, e)


def _load_image_disk(url: str) -> Optional[QPixmap]:
    path = _image_disk_path(url)
    if not os.path.exists(path):
        return None
    pm = QPixmap(path)
    return None if pm.isNull() else pm


def _disk_image_size(url: str) -> Optional[tuple]:
    """从磁盘缓存的原图读尺寸，只解析文件头不解码像素。"""
    path = _image_disk_path(url)
    if not os.path.exists(path):
        return None
    sz = QImageReader(path).size()
    if sz.isValid() and sz.width() > 0 and sz.height() > 0:
        return (sz.width(), sz.height())
    return None


def _is_default_avatar(pm: QPixmap) -> bool:
    """识别服务端给未上传头像用户生成的默认字母图。

    实测：Qt 渲染服务端字母 SVG 的结果是内容只占左上
    四分之一画布（TL 全不透明、其余三象限全透明），与旧版 PNG 默认图
    签名一致，所以按"内容是否挤在左上象限"判。
    不能用全局不透明占比（旧实现 <0.7）：真实头像常见大片透明区，
    实测用户头像占比仅 0.64，会被误杀成占位图——这正是"网页端换了
    头像客户端永远显示不出来"的根因之一。
    """
    img = pm.toImage()
    w, h = img.width(), img.height()
    if w == 0 or h == 0:
        return True
    hw, hh = (w + 1) // 2, (h + 1) // 2
    tl_total = tl_opaque = 0
    rest_total = rest_opaque = 0
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            is_tl = x < hw and y < hh
            if is_tl:
                tl_total += 1
            else:
                rest_total += 1
            if img.pixelColor(x, y).alpha() > 32:
                if is_tl:
                    tl_opaque += 1
                else:
                    rest_opaque += 1
    if not tl_total or not rest_total:
        return False
    return (tl_opaque / tl_total > 0.5
            and rest_opaque / rest_total < 0.02)


class _LruCache(OrderedDict):
    """简单 LRU：超出上限淘汰最旧条目。

    缓存被主线程（委托绘制、pixmap_for）和网络回复线程（put 写回）并发访问，
    不加锁时缓存更新会撞上遍历，抛 OrderedDict mutated during iteration，
    QPainter 里抛异常会直接让整个进程崩掉。所有读写都经同一把 RLock 串行化。
    """

    def __init__(self, limit: int):
        super().__init__()
        self._limit = limit
        self._lock = threading.RLock()

    def put(self, key, value):
        with self._lock:
            if key in self:
                self.move_to_end(key)
            self[key] = value
            while len(self) > self._limit:
                evicted, _ = self.popitem(last=False)
                log.debug("图片缓存淘汰 %s（当前 %d 条）", evicted, len(self))

    def get_pixmap(self, key):
        with self._lock:
            if key in self:
                self.move_to_end(key)
                return self[key]
            return None

    def find_largest(self, match_kind: str, match_url: str):
        """按缓存顺序返回最久未用的匹配缩略图，供查看器开原图前垫图。

        kind/url 都要相等，size 取迭代到的第一个（reversed 即 LRU 最旧侧）。
        全程持锁，避免遍历中被并发 put 改写。返回 QPixmap 或 None。
        """
        with self._lock:
            for (u, kind, size) in reversed(self.keys()):
                if kind == match_kind and u == match_url:
                    # 缓存键是完整三元组，(u,kind,size)；只按 url 查会拿不到值。
                    # 用 get 而不是 get_pixmap：后者 move_to_end 会在遍历中改写字典
                    pm = self.get((u, kind, size))
                    if pm is not None and pm is not IMG_FAILED:
                        return pm
            return None

    def discard(self, key):
        with self._lock:
            self.pop(key, None)


# 聊天图片缓存（kind="img"）与头像缓存（kind="avatar"）分离，
# 避免滚动聊天历史时图片把会话列表头像挤出缓存导致重复加载。
_IMAGE_CACHE = _LruCache(_IMAGE_CACHE_LIMIT)
_AVATAR_CACHE = _LruCache(_AVATAR_CACHE_LIMIT)


def _scaled_pixmap(pm: QPixmap, size: int, scale: int = 1) -> QPixmap:
    """按显示尺寸缩放；超大图先降采样，控制位图内存。

    scale > 1 时为 HiDPI 冗余缓存：缩略图生成到 size*scale，
    交给委托做下采样，避免高清屏下上采样导致发糊。
    """
    if pm.width() * pm.height() > _MAX_DECODE_PIXELS:
        k = (_MAX_DECODE_PIXELS / (pm.width() * pm.height())) ** 0.5
        pm = pm.scaled(max(1, int(pm.width() * k)),
                       max(1, int(pm.height() * k)),
                       Qt.KeepAspectRatio, Qt.SmoothTransformation)
        log.warning("超大图片已降采样至 %dx%d", pm.width(), pm.height())
    target = size * max(1, scale)
    return pm.scaled(target, target, Qt.KeepAspectRatio,
                     Qt.SmoothTransformation)


def _http_error(reply: QNetworkReply) -> str:
    """把网络/HTTP 错误归成一句人话，无错返回空串。"""
    err = reply.error()
    if err == QNetworkReply.NoError:
        return ""
    status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
    # PyQt5 的 NetworkError 枚举没有 .name()（PyQt6 才有），人话描述用 errorString
    return reply.errorString() + (f" HTTP {status}" if status else "")


def _decode_pixmap_data(data: bytes) -> Optional[QPixmap]:
    """把原始字节解成非空 QPixmap，失败返回 None。"""
    pm = QPixmap()
    if not pm.loadFromData(data) or pm.isNull():
        log.warning("图片解码失败（%d 字节，可能不是图片）", len(data))
        return None
    return pm


def _decoded_pixmap(reply: QNetworkReply) -> Optional[QPixmap]:
    """把响应解成非空 QPixmap，网络/HTTP 失败或解码失败返回 None。"""
    problem = _http_error(reply)
    if problem:
        log.warning("图片下载失败：%s", problem)
        return None
    return _decode_pixmap_data(bytes(reply.readAll()))


class ImageLoader(QObject):
    """聊天图片/视频封面加载器。

    委托在 paint 里调 pixmap_for()：命中就画，未命中则登记等待并发请求，
    到货后由 image_ready(url) 通知委托重绘对应消息行。
    """

    image_ready = pyqtSignal(str)      # 已解析的绝对 url

    def __init__(self, server_url: str = "", parent=None):
        super().__init__(parent)
        self.server_url = server_url.rstrip("/")
        self._nam = QNetworkAccessManager(self)
        self._nam.finished.connect(self._on_finished)
        # url -> (等待的显示尺寸集合, 发起时间)
        self._inflight: dict = {}
        # Rocket.Chat 附件认证：/file-upload/ 需要 X-Auth-Token / X-User-Id
        self.auth_uid = ""
        self.auth_token = ""
        self.ssl_verify = True
        self._sweeper = QTimer(self)
        self._sweeper.setInterval(500)
        self._sweeper.timeout.connect(self._sweep_stale)
        self._sweeper.start()
        # url -> (原始宽, 原始高)，图片加载完成后记录
        self._original_sizes: dict = {}

    # ------------------------------------------------------------------ #
    def set_auth(self, user_id: str, auth_token: str):
        """设置附件下载认证头（登录成功后调用）。"""
        self.auth_uid = user_id or ""
        self.auth_token = auth_token or ""

    def resolve(self, url: str) -> str:
        """公开接口：补全相对路径（供点击打开/保存附件等场景使用）。"""
        return self._resolve(url)

    def _resolve(self, url: str) -> str:
        # 修正相对地址（Rocket.Chat 的附件 URL 常是 /file-upload/...）
        if url.startswith("/") and self.server_url:
            return self.server_url + url
        return url

    def _new_request(self, url: str) -> QNetworkRequest:
        """构造请求；认证头只在同源时挂（外链不送令牌）。"""
        req = QNetworkRequest(QUrl(url))
        req.setTransferTimeout(8000)
        if can_carry_credentials(url, self.server_url):
            if self.auth_token:
                req.setRawHeader(b"X-Auth-Token", self.auth_token.encode())
            if self.auth_uid:
                req.setRawHeader(b"X-User-Id", self.auth_uid.encode())
        return req

    # ------------------------------------------------------------------ #
    def pixmap_for(self, url: str, size: int) -> Optional[QPixmap]:
        """命中缓存返回图；否则发请求并返回 None。失败后不再重试。"""
        if not url:
            return None
        url = canon_url(self._resolve(url))
        cached = _IMAGE_CACHE.get_pixmap((url, "img", size))
        if cached is not None:
            return None if cached is IMG_FAILED else cached
        # 内存淘汰后从磁盘原图补一张缩略图，省一次网络往返
        disk = _load_image_disk(url)
        if disk is not None:
            scaled = _scaled_pixmap(disk, size, _HIDPI_SCALE)
            _IMAGE_CACHE.put((url, "img", size), scaled)
            return scaled
        entry = self._inflight.get(url)
        if entry is None:
            self._inflight[url] = ({size}, _time.monotonic())
            log.debug("图片请求 %s", url)
            self._nam.get(self._new_request(url))
        else:
            entry[0].add(size)
        return None

    def _on_finished(self, reply: QNetworkReply):
        # 必须用请求 URL 做键：/file-upload/ 会 302，reply.url() 是跳转后的
        # 地址，拿它当键就找不到等待方，图片永远贴不上。
        # FullyEncoded 与 pixmap_for 登记的键形式一致（见 canon_url）
        url = reply.request().url().toString(QUrl.FullyEncoded)
        sizes, _started = self._inflight.pop(url, (set(), 0.0))
        # 原始字节要留一份落盘（磁盘缓存存原图，缩略图按显示尺寸现缩）
        data = bytes(reply.readAll())
        problem = _http_error(reply)
        pm = None if problem else _decode_pixmap_data(data)
        if problem:
            log.warning("图片下载失败：%s", problem)
        if pm is None:
            for size in sizes:
                _IMAGE_CACHE.put((url, "img", size), IMG_FAILED)
        else:
            _save_image_disk(url, data)
            self._original_sizes[url] = (pm.width(), pm.height())
            for size in sizes:
                _IMAGE_CACHE.put((url, "img", size),
                                 _scaled_pixmap(pm, size, _HIDPI_SCALE))
            log.debug("图片就绪 %s 原始 %dx%d", url, pm.width(), pm.height())
        reply.deleteLater()
        self.image_ready.emit(url)

    def _sweep_stale(self):
        """回收超时未回调的请求，避免委托一直停在占位态。"""
        now = _time.monotonic()
        for url, (sizes, started) in list(self._inflight.items()):
            if now - started < _INFLIGHT_TIMEOUT:
                continue
            self._inflight.pop(url, None)
            log.warning("图片请求超时 %s", url)
            for size in sizes:
                _IMAGE_CACHE.put((url, "img", size), IMG_FAILED)
            self.image_ready.emit(url)

    def is_failed(self, url: str, size: int) -> bool:
        """该 url 是否已确认加载失败（用于区分"加载中"与"加载失败"）。"""
        if not url:
            return False
        return _IMAGE_CACHE.get_pixmap(
            (canon_url(self._resolve(url)), "img", size)) is IMG_FAILED

    def original_size(self, url: str) -> Optional[tuple]:
        """返回已加载图片的原始尺寸 (w, h)，未加载过返回 None。

        内存没有时查磁盘缓存兜底：磁盘存的是原始字节，只读文件头就能
        拿到尺寸。不兜底的话重启后 original_size 一直空，行高与命中
        矩形按 240x180 回退值算，直到原图重新走一遍网络。
        """
        if not url:
            return None
        key = canon_url(self._resolve(url))
        size = self._original_sizes.get(key)
        if size is None:
            size = _disk_image_size(key)
            if size is not None:
                self._original_sizes[key] = size
        return size

    def cached_pixmap(self, url: str) -> Optional[QPixmap]:
        """取任意已入缓存的缩略图（按缓存顺序找最大值），无则返回 None。

        供图片查看器在开原图前先垫一张已加载的缩略图，避免打开时一片
        "加载中"空白——画面上本该已经有的图，先让它立刻显示出来。
        """
        if not url:
            return None
        key = canon_url(self._resolve(url))
        # 遍历必须持锁（find_largest 内部已加锁），否则并发写回会
        # 抛 OrderedDict mutated during iteration
        return _IMAGE_CACHE.find_largest("img", key)

    def cached_original(self, url: str) -> Optional[QPixmap]:
        """磁盘缓存里的原图（原始字节整幅解码，无缩放），无则返回 None。

        服务器会清理历史粘贴文件（实测带认证头仍 404），
        气泡能显示全靠磁盘缓存现缩；查看器若只走网络，404 后会静默
        停在放大的缩略图上，表现为"查看的图永远模糊"。磁盘存的就是
        当年下载的原始字节，命中即等同拿到原图，无需再发请求。
        """
        if not url:
            return None
        pm = _load_image_disk(canon_url(self._resolve(url)))
        return None if pm is None or pm.isNull() else pm

    def cached_original_path(self, url: str) -> Optional[str]:
        """磁盘缓存里原图字节的文件路径（无扩展名），没有则返回 None。

        离线 OCR 直接吃文件路径，不必先解码成 QPixmap 再落一份临时图。
        """
        if not url:
            return None
        path = _image_disk_path(canon_url(self._resolve(url)))
        return path if os.path.isfile(path) else None


class AvatarLoader(QObject):
    """全局共享的头像加载器。

    既支持直接把头像贴到 AvatarWidget 上（会话列表、资料卡），
    也支持委托用 pixmap_for() 拉取并在就绪时通过 avatar_ready 通知重绘。
    """

    avatar_ready = pyqtSignal(str)      # 头像 url

    def __init__(self, parent=None):
        super().__init__(parent)
        self._nam = QNetworkAccessManager(self)
        self._nam.finished.connect(self._on_finished)
        self._pending = {}          # url -> [AvatarWidget, ...]
        self._watching = set()      # 通过 pixmap_for 等待的 url
        self._refreshing = set()    # 正在做远端一致性检查的 url
        self._retry_count = {}      # url -> 失败重试次数
        self.auth_uid = ""
        self.auth_token = ""
        # 判同源用（登录时随 set_auth 注入）：外链头像不能带走会话 Cookie
        self.server_url = ""

    def set_auth(self, user_id: str, auth_token: str,
                 server_url: str = ""):
        """设置登录态（登录成功后调用）。服务端 /avatar/ 只认 Cookie：
        带 X-Auth-Token 头或不带认证，都会被降级成默认字母 SVG，
        真头像永远拉不下来（实测）。"""
        self.auth_uid = user_id or ""
        self.auth_token = auth_token or ""
        self.server_url = (server_url or self.server_url or "").rstrip("/")

    # ------------------------------------------------------------------ #
    def _cache_key(self, url: str):
        return (url, "avatar", _AVATAR_PX)

    def is_failed(self, url: str) -> bool:
        if not url:
            return False
        return _AVATAR_CACHE.get_pixmap(self._cache_key(canon_url(url))) is IMG_FAILED

    def pixmap_for(self, url: str) -> Optional[QPixmap]:
        """命中缓存返回圆形裁剪前的缩略图；否则登记等待并发请求。"""
        if not url:
            return None
        url = canon_url(url)
        cached = _AVATAR_CACHE.get_pixmap(self._cache_key(url))
        if cached is not None:
            return None if cached is IMG_FAILED else cached
        if url in self._watching or url in self._pending:
            return None
        if not self.auth_token:
            # 未登录不发请求：/avatar/ 无 Cookie 会被降级成默认字母 SVG，
            # 反而写入 IMG_FAILED 哨兵，登录后这头像就永远不重试了
            return None
        self._watching.add(url)
        self._fetch(url)
        return None

    def load(self, avatar_widget: AvatarWidget, url: str):
        """把头像异步加载到 widget。url 为空时跳过（保持占位）。"""
        if not url:
            return
        url = canon_url(url)
        cached = _AVATAR_CACHE.get_pixmap(self._cache_key(url))
        if cached is not None and cached is not IMG_FAILED:
            log.info("[诊断] 头像内存命中 %s", url)
            self._apply(avatar_widget, cached)
            return
        if cached is IMG_FAILED:
            return
        # 磁盘缓存优先，省一次网络往返
        disk_path = _url_to_cache_path(url)
        if os.path.exists(disk_path):
            pm = QPixmap(disk_path)
            if not pm.isNull():
                if _is_default_avatar(pm):
                    # 磁盘里缓存的是服务端默认字母图，直接按无头像处理
                    _AVATAR_CACHE.put(self._cache_key(url), IMG_FAILED)
                    return
                scaled = _scaled_pixmap(pm, _AVATAR_PX)
                _AVATAR_CACHE.put(self._cache_key(url), scaled)
                self._apply(avatar_widget, scaled)
                return
        self._pending.setdefault(url, [])
        if avatar_widget not in self._pending[url]:
            self._pending[url].append(avatar_widget)
        if len(self._pending[url]) == 1:
            # 未登录不发请求（原因同 pixmap_for）：保持占位，等登录后重渲染补载
            if not self.auth_token:
                return
            self._fetch(url)

    def refresh_if_changed(self, url: str):
        """后台拉一次远端头像，与磁盘缓存比对，仅在不一致时更新缓存。

        登录时调用：界面先用磁盘缓存即时显示（load 磁盘优先），这里只负责
        发现远端变化（如网页端换过头像），避免每次登录 purge 强制重拉。
        """
        if not url or not self.auth_token:
            return
        url = canon_url(url)
        if url in self._watching or url in self._pending \
                or url in self._refreshing:
            return
        self._refreshing.add(url)
        self._fetch(url)

    @staticmethod
    def _disk_differs(url: str, data: bytes) -> bool:
        """远端字节与磁盘缓存是否不一致（磁盘无文件视为不一致）。"""
        try:
            with open(_url_to_cache_path(url), "rb") as f:
                return f.read() != data
        except OSError:
            return True

    def _on_refresh_finished(self, url: str, reply: QNetworkReply):
        self._refreshing.discard(url)
        self._retry_count.pop(url, None)
        problem = _http_error(reply)
        data = bytes(reply.readAll())
        reply.deleteLater()
        if problem or not data:
            # 检查失败只放弃本次，本地缓存照用（下次登录再查）
            log.debug("头像一致性检查未完成（保留本地缓存）：%s %s",
                      url, problem)
            return
        if not self._disk_differs(url, data):
            log.debug("头像与远端一致，不更新 %s", url)
            return
        try:
            os.makedirs(avatars_dir(), exist_ok=True)
            path = _url_to_cache_path(url)
            tmp = f"{path}.tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except OSError:
            log.warning("头像缓存更新写盘失败 %s", url, exc_info=True)
            return
        # QPixmap 读取时自动识别格式，磁盘上直接存原始字节即可
        _AVATAR_CACHE.discard(self._cache_key(url))
        log.info("[诊断] 头像检测到远端变化，已更新缓存 %s", url)
        self.avatar_ready.emit(url)

    def _fetch(self, url: str):
        log.info("[诊断] 头像请求发出 %s", url)
        req = QNetworkRequest(QUrl(url))
        req.setTransferTimeout(8000)
        if self.auth_token and can_carry_credentials(url, self.server_url):
            req.setRawHeader(
                b"Cookie",
                f"rc_uid={self.auth_uid}; rc_token={self.auth_token}".encode())
        self._nam.get(req)

    def _apply(self, widget: AvatarWidget, pm: QPixmap):
        try:
            widget.set_pixmap_rounded(pm)
        except RuntimeError:
            log.debug("头像回调时目标控件已销毁，跳过")

    def _on_finished(self, reply):
        # 同 ImageLoader：用请求 URL 而非 reply.url()，避免重定向后找不到
        # 等待方；FullyEncoded 保证与 load()/pixmap_for() 登记的键一致
        url = reply.request().url().toString(QUrl.FullyEncoded)
        if url in self._refreshing:
            self._on_refresh_finished(url, reply)
            return
        widgets = self._pending.pop(url, [])
        watching = url in self._watching
        self._watching.discard(url)
        pm = _decoded_pixmap(reply)
        log.info("[诊断] 头像回应 url=%s error=%s bytes=%s pm=%s",
                 url, reply.error(), reply.bytesAvailable(), pm is not None)
        reply.deleteLater()
        if pm is not None and _is_default_avatar(pm):
            # 服务端默认字母图当无头像处理：落失败哨兵走本地汉字占位，
            # 且不值得重试
            log.info("[诊断] 头像被识别为默认字母图 %s", url)
            _AVATAR_CACHE.put(self._cache_key(url), IMG_FAILED)
            self._retry_count.pop(url, None)
        elif pm is not None:
            scaled = _scaled_pixmap(pm, _AVATAR_PX)
            _AVATAR_CACHE.put(self._cache_key(url), scaled)
            for w in widgets:
                self._apply(w, scaled)
            self._save_to_disk(url, scaled)
            self._retry_count.pop(url, None)
        elif widgets or watching:
            # 失败重试一次，仍失败才落占位
            retries = self._retry_count.get(url, 0)
            if retries < 1:
                self._retry_count[url] = retries + 1
                self._pending[url] = widgets
                log.debug("头像加载失败，将重试 %s", url)
                self._fetch(url)
                return
            _AVATAR_CACHE.put(self._cache_key(url), IMG_FAILED)
            self._retry_count.pop(url, None)
        if widgets or watching:
            self.avatar_ready.emit(url)

    @staticmethod
    def _save_to_disk(url: str, pm: QPixmap):
        try:
            # core/paths 的目录不会预创建，首次落盘前自己建，
            # 否则全新安装的机器上头像磁盘缓存永远写不进去
            os.makedirs(avatars_dir(), exist_ok=True)
            path = _url_to_cache_path(url)
            ok = pm.save(path, "PNG")
            log.info("[诊断] 头像落盘 %s ok=%s", path, ok)
        except Exception:  # noqa: BLE001
            log.warning("头像缓存写盘失败 %s", url, exc_info=True)

    def purge(self, url: str):
        """清掉某头像的内存与磁盘缓存（上传新头像后强制重新拉取）。"""
        if not url:
            return
        url = canon_url(url)
        _AVATAR_CACHE.discard(self._cache_key(url))
        log.info("[诊断] 头像清缓存 %s", url)
        # 在途登记也要清：否则紧接着的 load() 被 _pending/_watching 去重
        # 拦截，旧地址的回应答后新头像就永远不来了
        self._pending.pop(url, None)
        self._watching.discard(url)
        self._retry_count.pop(url, None)
        try:
            os.remove(_url_to_cache_path(url))
        except OSError:
            pass
