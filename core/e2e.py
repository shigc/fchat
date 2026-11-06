"""Rocket.Chat E2E 端到端加密：解密支持。

流程（与官方 Web 客户端 / 已验证的 rocket_bot.py 一致）：
1. REST e2e.fetchMyKeys 取回密钥对：
   private_key = {"iv","ciphertext","salt":"v2:<uuid>","iterations":100000}
   PBKDF2-SHA256(password, salt 变体, iterations) 派生 AES-256-GCM 密钥，
   解出 RSA 私钥 JWK。
2. 房间密钥：订阅的 E2EKey = 36 字符 kid + base64(RSA-OAEP-256 密文)，
   私钥解出 JWK {"alg":"A256GCM","k":"<b64url 密钥>"}，即房间 AES 密钥。
3. 消息：t=="e2e"，content={"algorithm":"rc.v2.aes-sha2","kid","iv","ciphertext"}
   （GCM tag 附在 ciphertext 尾部）；若 content 带 encryptedKey 则为消息级
   密钥（RSA 解出 {"key": b64}），否则用房间共享密钥。
   明文为 JSON，取其中的 msg 字段。

线程安全：REST 历史线程与 DDP 实时线程都会调用 decrypt_message，
内部加锁保护密钥缓存；网络请求仅在首次取密钥时发生，之后走缓存。
"""
from __future__ import annotations

import base64
import json
import logging
import threading

import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

log = logging.getLogger(__name__)


class E2EError(Exception):
    pass


def _b64(s: str) -> bytes:
    return base64.b64decode(s)


def _b64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * ((4 - len(s) % 4) % 4))


class E2EManager:
    """管理 E2E 私钥解密、房间密钥缓存与消息解密。"""

    def __init__(self, server_url: str, user_id: str, auth_token: str,
                 e2e_password: str, ssl_verify: bool = True):
        self.server_url = server_url.rstrip("/")
        self.user_id = user_id
        self.password = e2e_password
        self.enabled = False
        self._session = requests.Session()
        self._session.verify = ssl_verify
        self._headers = {
            "X-Auth-Token": auth_token,
            "X-User-Id": user_id,
            "Content-Type": "application/json",
        }
        self._priv_key = None            # cryptography RSA 私钥对象
        self._room_keys = {}             # room_id -> (kid, aes_key bytes)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # REST
    # ------------------------------------------------------------------ #
    def _api_get(self, endpoint: str, **params) -> dict:
        url = f"{self.server_url}/api/v1/{endpoint}"
        r = self._session.get(url, headers=self._headers, params=params,
                              timeout=10)
        data = r.json()
        if r.status_code != 200 or not data.get("success"):
            raise E2EError(f"{endpoint}: {r.status_code} {r.text[:120]}")
        return data

    # ------------------------------------------------------------------ #
    # 初始化：取回并解开自己的私钥
    # ------------------------------------------------------------------ #
    def init_keys(self) -> bool:
        """取回加密私钥并用 E2E 密码解开。成功置 enabled=True。"""
        with self._lock:
            if self.enabled:
                return True
            if not self.password:
                return False
            try:
                keys = self._api_get("e2e.fetchMyKeys")
            except E2EError as e:
                log.warning("E2E fetchMyKeys 失败：%s", e)
                return False
            priv = keys.get("private_key") or keys.get("privateKey") or ""
            if not priv:
                log.info("服务器未为该用户生成 E2E 密钥，跳过 E2E 解密")
                return False
            try:
                enc = priv if isinstance(priv, dict) else json.loads(priv)
                self._priv_key = self._decrypt_private_key(enc, self.password)
            except Exception as e:  # noqa: BLE001
                log.error("E2E 私钥解密失败（密码错误或格式变化）：%s", e)
                return False
            if self._priv_key is None:
                # 各 salt 变体都解不开：最常见是密码与服务器当前密码不一致
                # （网页端改过密码），必须留日志，否则排障只能瞎猜
                log.warning(
                    "E2E 私钥解密失败（密码错误或私钥数据损坏），"
                    "加密房间将显示占位文本")
                return False
            self.enabled = True
            log.info("E2E 私钥解密成功，端到端解密已启用")
            return True

    # ------------------------------------------------------------------ #
    # 私钥：PBKDF2 + AES-GCM 解密
    # ------------------------------------------------------------------ #
    @staticmethod
    def _decrypt_private_key(enc: dict, password: str):
        """解开 {iv, ciphertext, salt, iterations} 格式的私钥，返回 JWK dict。"""
        iv = _b64(enc["iv"])
        ciphertext = _b64(enc["ciphertext"])
        salt_str = str(enc.get("salt", ""))
        iterations = int(enc.get("iterations", 100000))

        # salt 变体（服务器/版本差异，逐一尝试；uuid_hex 为 Rocket.Chat 实际用法）
        variants = []
        if salt_str.startswith("v2:"):
            parts = salt_str.split(":")
            if len(parts) >= 3:
                uuid_hex = parts[2].replace("-", "")
                try:
                    variants.append(bytes.fromhex(uuid_hex))
                except ValueError:
                    pass
                variants.append(salt_str.encode("utf-8"))
                variants.append(parts[2].encode("utf-8"))
                if len(parts) >= 3:
                    variants.append(f"{parts[1]}:{parts[2]}".encode("utf-8"))
        elif salt_str:
            variants.append(salt_str.encode("utf-8"))
        variants.append(b"")

        for salt in variants:
            try:
                key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                                 salt=salt, iterations=iterations).derive(
                    password.encode("utf-8"))
                if len(iv) == 12:  # AES-GCM
                    plain = AESGCM(key).decrypt(iv, ciphertext, None)
                else:
                    from cryptography.hazmat.primitives.ciphers import (
                        Cipher, algorithms, modes)
                    from cryptography.hazmat.primitives import padding as _pad
                    dec = Cipher(algorithms.AES(key),
                                 modes.CBC(iv)).decryptor()
                    data = dec.update(ciphertext) + dec.finalize()
                    un = _pad.PKCS7(128).unpadder()
                    plain = un.update(data) + un.finalize()
                jwk = json.loads(plain.decode("utf-8"))
                if "d" in jwk:
                    log.debug("E2E 私钥解密成功（salt len=%s）", len(salt))
                    return jwk
            except Exception:  # noqa: BLE001
                continue
        return None

    # ------------------------------------------------------------------ #
    # 房间密钥：订阅 E2EKey -> RSA-OAEP-256 -> JWK(k)
    # ------------------------------------------------------------------ #
    def _load_private_key(self):
        jwk = None
        # self._priv_key 存的是 JWK dict（延迟构造 RSA 对象）
        if isinstance(self._priv_key, dict):
            jwk = self._priv_key
        if jwk is None:
            return None
        n = int.from_bytes(_b64url(jwk["n"]), "big")
        e = int.from_bytes(_b64url(jwk["e"]), "big")
        d = int.from_bytes(_b64url(jwk["d"]), "big")
        p = int.from_bytes(_b64url(jwk["p"]), "big")
        q = int.from_bytes(_b64url(jwk["q"]), "big")
        dp = int.from_bytes(_b64url(jwk["dp"]), "big")
        dq = int.from_bytes(_b64url(jwk["dq"]), "big")
        qi = int.from_bytes(_b64url(jwk["qi"]), "big")
        return rsa.RSAPrivateNumbers(
            p, q, d, dp, dq, qi, rsa.RSAPublicNumbers(e, n)).private_key()

    def invalidate_room(self, room_id: str):
        """失效房间密钥缓存。开关房间加密后必须调用：旧密钥已作废，
        缓存不失效会把新密文按旧密钥解（必失败）。"""
        with self._lock:
            self._room_keys.pop(room_id, None)

    def _room_key(self, room_id: str):
        """返回 (kid, aes_key bytes)；失败返回 (None, None)。"""
        with self._lock:
            if room_id in self._room_keys:
                return self._room_keys[room_id]
        try:
            sub = self._api_get("subscriptions.getOne", roomId=room_id)
        except E2EError as e:
            log.warning("E2E 订阅密钥获取失败 rid=%s：%s", room_id, e)
            return None, None
        e2e_key = (sub.get("subscription") or {}).get("E2EKey") or ""
        if not e2e_key:
            return None, None
        kid, enc_b64 = e2e_key[:36], e2e_key[36:]
        try:
            priv = self._load_private_key()
            plain = priv.decrypt(
                _b64(enc_b64),
                padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                             algorithm=hashes.SHA256(), label=None))
            key_jwk = json.loads(plain.decode("utf-8"))
            aes_key = _b64url(key_jwk.get("k", ""))
        except Exception as e:  # noqa: BLE001
            log.error("E2E 房间密钥解密失败 rid=%s：%s", room_id, e)
            return None, None
        with self._lock:
            self._room_keys[room_id] = (kid, aes_key)
        log.debug("E2E 房间密钥解密成功 rid=%s kid=%s", room_id, kid)
        return kid, aes_key

    # ------------------------------------------------------------------ #
    # 消息解密
    # ------------------------------------------------------------------ #
    def decrypt_to_dict(self, m: dict, room_id: str) -> dict:
        """把 t=='e2e' 的原始消息 dict 解密为普通消息 dict（REST/realtime 共用）。

        成功：剥掉 t/content，把明文写回 msg；若明文 JSON 中带
        attachments/file 等字段则一并还原，交给 parse_message 正常解析。
        失败（无密钥/密码错误）：原样返回，上层会展示为密文占位。
        """
        if m.get("t") != "e2e":
            return m
        content = m.get("content") or {}
        if not content.get("iv") or not content.get("ciphertext"):
            return m
        if not self.enabled and not self.init_keys():
            return m
        text = self._decrypt_text(m, room_id)
        if text is None:
            return m
        out = {k: v for k, v in m.items() if k not in ("t", "content")}
        out["msg"] = ""
        # 明文是 JSON：msg 之外可能还有附件/文件元数据
        payload = None
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            if "msg" in payload:
                out["msg"] = str(payload["msg"])
            for k in ("attachments", "file", "md"):
                if payload.get(k):
                    out[k] = payload[k]
        else:
            out["msg"] = text
        log.debug("E2E 消息解密成功 rid=%s mid=%s", room_id, m.get("_id", ""))
        return out

    def decrypt_message(self, m: dict, room_id: str):
        """解密 t=='e2e' 的消息，返回明文文本；失败返回 None。"""
        text = self._decrypt_text(m, room_id)
        if text is None:
            return None
        try:
            payload = json.loads(text)
            if isinstance(payload, dict) and "msg" in payload:
                return str(payload["msg"])
        except (json.JSONDecodeError, ValueError):
            pass
        return text

    def _decrypt_text(self, m: dict, room_id: str):
        """解密消息并返回完整明文文本（含 JSON 全部字段）；失败返回 None。"""
        if not self.enabled and not self.init_keys():
            return None
        content = m.get("content") or {}
        iv_b64 = content.get("iv") or ""
        ct_b64 = content.get("ciphertext") or ""
        kid = content.get("kid", "")
        if not iv_b64 or not ct_b64:
            return None
        try:
            aes_key = None
            # 消息级密钥（encryptedKey）优先
            enc_key_b64 = content.get("encryptedKey")
            if enc_key_b64:
                try:
                    priv = self._load_private_key()
                    plain = priv.decrypt(
                        _b64(enc_key_b64),
                        padding.OAEP(mgf=padding.MGF1(
                            algorithm=hashes.SHA256()),
                            algorithm=hashes.SHA256(), label=None))
                    aes_key = _b64(json.loads(plain.decode("utf-8"))["key"])
                except Exception as e:  # noqa: BLE001
                    log.debug("消息级 encryptedKey 解密失败，退回房间密钥：%s", e)
            if aes_key is None:
                cached_kid, rk = self._room_key(room_id)
                if rk is None:
                    return None
                if kid and cached_kid and kid != cached_kid:
                    # kid 变化：房间密钥可能已轮换，清缓存重取一次
                    with self._lock:
                        self._room_keys.pop(room_id, None)
                    _, rk = self._room_key(room_id)
                    if rk is None:
                        return None
                aes_key = rk
            plain = AESGCM(aes_key).decrypt(_b64(iv_b64), _b64(ct_b64), None)
            return plain.decode("utf-8")
        except Exception as e:  # noqa: BLE001
            log.warning("E2E 消息解密失败 rid=%s：%s", room_id, e)
            return None
