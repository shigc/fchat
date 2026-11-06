"""core/e2e.py 验证：私钥解密 salt 变体、房间密钥解密、消息解密回环与失败路径。

运行：python -m unittest discover -s tests -v
全部离线：网络请求通过桩替换，不依赖真实 Rocket.Chat 服务器。
"""
import base64
import json
import os
import sys
import threading
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa, padding  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC  # noqa: E402

from core.e2e import E2EManager  # noqa: E402


def _b64url_int(x: int) -> str:
    n = (x.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(x.to_bytes(n, "big")).decode().rstrip("=")


def _b64url_raw(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def make_rsa_jwk():
    """生成 RSA 私钥并导出测试用 JWK（与服务器 JWK 结构一致）。"""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nums = priv.private_numbers()
    pub = nums.public_numbers
    return {
        "kty": "RSA", "alg": "A256GCM",
        "n": _b64url_int(pub.n), "e": _b64url_int(pub.e),
        "d": _b64url_int(nums.d), "p": _b64url_int(nums.p),
        "q": _b64url_int(nums.q), "dp": _b64url_int(nums.dmp1),
        "dq": _b64url_int(nums.dmq1), "qi": _b64url_int(nums.iqmp),
    }, priv


def encrypt_private_key_jwk(jwk: dict, password: str, salt: bytes,
                           salt_str: str = "",
                           iterations: int = 1000) -> dict:
    """按服务器格式 {iv, ciphertext, salt, iterations} 加密私钥 JWK。

    salt 是实际参与 PBKDF2 的字节；salt_str 是写回 JSON 的字符串表示
    （三者可能不同，对应服务器的 salt 变体）。"""
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=iterations).derive(password.encode("utf-8"))
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, json.dumps(jwk).encode("utf-8"), None)
    return {"iv": base64.b64encode(iv).decode(),
            "ciphertext": base64.b64encode(ct).decode(),
            "salt": salt_str, "iterations": iterations}


def wrap_room_key(priv, kid: str, aes_key: bytes) -> str:
    """模拟服务器 subscription.E2EKey：36 字符 kid + base64(RSA-OAEP-256)。"""
    key_jwk = {"alg": "A256GCM", "k": _b64url_raw(aes_key)}
    wrapped = priv.public_key().encrypt(
        json.dumps(key_jwk).encode("utf-8"),
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                     algorithm=hashes.SHA256(), label=None))
    return kid + base64.b64encode(wrapped).decode()


def make_e2e_message(aes_key: bytes, plaintext: str, kid: str = "") -> dict:
    iv = os.urandom(12)
    ct = AESGCM(aes_key).encrypt(iv, plaintext.encode("utf-8"), None)
    content = {"algorithm": "rc.v2.aes-sha2",
               "iv": base64.b64encode(iv).decode(),
               "ciphertext": base64.b64encode(ct).decode()}
    if kid:
        content["kid"] = kid
    return {"_id": "msg1", "t": "e2e", "rid": "room1", "content": content}


class E2ETestBase(unittest.TestCase):
    """离线构造 E2EManager：不走 init_keys，直接注入已解开的私钥 JWK。"""

    PASSWORD = "correct-e2e-pass"

    def setUp(self):
        self.jwk, self.rsa_priv = make_rsa_jwk()
        self.mgr = E2EManager("https://server.invalid", "uid", "token",
                              self.PASSWORD, ssl_verify=False)
        self.mgr.enabled = True
        self.mgr._priv_key = self.jwk
        self.room_key = os.urandom(32)
        self.kid = "k" * 36

    def stub_subscription(self, e2e_key=None):
        """把订阅接口替换为桩，返回给定的 E2EKey。"""
        calls = []

        def fake_get(endpoint, **params):
            calls.append((endpoint, params))
            if endpoint == "subscriptions.getOne":
                if e2e_key is None:
                    return {"success": True, "subscription": {}}
                return {"success": True,
                        "subscription": {"E2EKey": e2e_key}}
            raise AssertionError(f"unexpected endpoint: {endpoint}")

        self.mgr._api_get = fake_get
        return calls

    def _prepare_room(self):
        e2e_key = wrap_room_key(self.rsa_priv, self.kid, self.room_key)
        self.stub_subscription(e2e_key)


class TestPrivateKeyDecrypt(E2ETestBase):
    """私钥解密：salt 变体逐一尝试，与服务器 rc.v2.aes-sha2 格式一致。"""

    def test_salt_v2_full_string_variant(self):
        # 三段式 salt（真机实测 salt len=57）：全串 UTF-8 为实际生效变体
        salt_str = f"v2:{uuid.uuid4()}:{uuid.uuid4().hex[:17]}"
        enc = encrypt_private_key_jwk(self.jwk, self.PASSWORD,
                                      salt_str.encode("utf-8"),
                                      salt_str=salt_str)
        jwk = E2EManager._decrypt_private_key(enc, self.PASSWORD)
        self.assertIsNotNone(jwk)
        self.assertEqual(jwk["d"], self.jwk["d"])

    def test_salt_v2_uuid_hex_variant(self):
        # 三段式 salt：hex 解码变体排在首位（优先命中）
        uuid_hex = uuid.uuid4().hex
        salt_str = f"v2:2:{uuid_hex}"
        enc = encrypt_private_key_jwk(self.jwk, self.PASSWORD,
                                      bytes.fromhex(uuid_hex),
                                      salt_str=salt_str)
        jwk = E2EManager._decrypt_private_key(enc, self.PASSWORD)
        self.assertIsNotNone(jwk)
        self.assertEqual(jwk["n"], self.jwk["n"])

    def test_wrong_password_returns_none(self):
        salt_str = f"v2:{uuid.uuid4()}:{uuid.uuid4().hex[:17]}"
        enc = encrypt_private_key_jwk(self.jwk, self.PASSWORD,
                                      salt_str.encode("utf-8"),
                                      salt_str=salt_str)
        self.assertIsNone(
            E2EManager._decrypt_private_key(enc, "wrong-password"))

    def test_corrupt_ciphertext_returns_none(self):
        salt_str = f"v2:{uuid.uuid4()}:{uuid.uuid4().hex[:17]}"
        enc = encrypt_private_key_jwk(self.jwk, self.PASSWORD,
                                      salt_str.encode("utf-8"),
                                      salt_str=salt_str)
        enc["ciphertext"] = base64.b64encode(b"garbage-ciphertext").decode()
        self.assertIsNone(
            E2EManager._decrypt_private_key(enc, self.PASSWORD))


class TestMessageDecrypt(E2ETestBase):

    def test_room_key_decrypt_and_message_roundtrip(self):
        self._prepare_room()
        msg = make_e2e_message(self.room_key, "你好，E2E！", kid=self.kid)
        out = self.mgr.decrypt_to_dict(dict(msg), "room1")
        self.assertNotIn("t", out)
        self.assertNotIn("content", out)
        self.assertEqual(out["msg"], "你好，E2E！")

    def test_payload_with_attachments_restored(self):
        self._prepare_room()
        payload = json.dumps({"msg": "file caption",
                              "attachments": [{"title": "a.pdf"}]})
        msg = make_e2e_message(self.room_key, payload, kid=self.kid)
        out = self.mgr.decrypt_to_dict(dict(msg), "room1")
        self.assertEqual(out["msg"], "file caption")
        self.assertEqual(out["attachments"], [{"title": "a.pdf"}])

    def test_room_key_cached_no_refetch(self):
        self._prepare_room()
        msg = make_e2e_message(self.room_key, "m1", kid=self.kid)
        self.mgr.decrypt_to_dict(dict(msg), "room1")
        calls = self.mgr._api_get("subscriptions.getOne", roomId="room1")
        del calls  # 二次解密不应再触发网络：直接校验缓存
        in_cache = "room1" in self.mgr._room_keys
        self.assertTrue(in_cache)
        before = self.mgr._room_keys["room1"]
        out = self.mgr.decrypt_to_dict(dict(msg), "room1")
        self.assertEqual(out["msg"], "m1")
        self.assertEqual(self.mgr._room_keys["room1"], before)

    def test_message_level_encrypted_key(self):
        # 房间订阅无 E2EKey，消息自带 encryptedKey
        self.stub_subscription(e2e_key=None)
        key_jwk = json.dumps({"key": base64.b64encode(self.room_key).decode()})
        wrapped = self.rsa_priv.public_key().encrypt(
            key_jwk.encode("utf-8"),
            padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                         algorithm=hashes.SHA256(), label=None))
        msg = make_e2e_message(self.room_key, "带消息级密钥")
        msg["content"]["encryptedKey"] = base64.b64encode(wrapped).decode()
        out = self.mgr.decrypt_to_dict(dict(msg), "room1")
        self.assertEqual(out["msg"], "带消息级密钥")

    def test_kid_rotation_refetches_and_decrypts(self):
        e2e_key = wrap_room_key(self.rsa_priv, self.kid, self.room_key)
        self.stub_subscription(e2e_key)
        rotated_kid = "r" * 36
        msg = make_e2e_message(self.room_key, "轮换后消息", kid=rotated_kid)
        out = self.mgr.decrypt_to_dict(dict(msg), "room1")
        self.assertEqual(out["msg"], "轮换后消息")


class TestMessageDecryptFailure(E2ETestBase):
    """负面用例：密钥缺失 / 密文损坏必须原样返回，不得抛异常或丢消息。"""

    def test_wrong_room_key_returns_original(self):
        e2e_key = wrap_room_key(self.rsa_priv, self.kid, self.room_key)
        self.stub_subscription(e2e_key)
        wrong_key = os.urandom(32)
        msg = make_e2e_message(wrong_key, "secret", kid=self.kid)
        out = self.mgr.decrypt_to_dict(dict(msg), "room1")
        self.assertEqual(out, msg)  # 原样返回，上层展示密文占位

    def test_corrupt_ciphertext_returns_original(self):
        self._prepare_room()
        msg = make_e2e_message(self.room_key, "x", kid=self.kid)
        msg["content"]["ciphertext"] = base64.b64encode(b"broken").decode()
        out = self.mgr.decrypt_to_dict(dict(msg), "room1")
        self.assertEqual(out, msg)

    def test_no_subscription_key_returns_original(self):
        self.stub_subscription(e2e_key=None)
        msg = make_e2e_message(self.room_key, "x", kid=self.kid)
        out = self.mgr.decrypt_to_dict(dict(msg), "room1")
        self.assertEqual(out, msg)

    def test_non_e2e_message_untouched(self):
        plain = {"_id": "m2", "msg": "普通消息"}
        self.assertEqual(self.mgr.decrypt_to_dict(plain, "room1"), plain)

    def test_missing_iv_or_ciphertext_untouched(self):
        msg = {"_id": "m3", "t": "e2e", "content": {"iv": ""}}
        self.assertEqual(self.mgr.decrypt_to_dict(dict(msg), "room1"), msg)

    def test_not_enabled_and_no_password_no_network(self):
        mgr = E2EManager("https://server.invalid", "uid", "token", "")

        def fail_get(endpoint, **params):  # 不应发生任何网络请求
            raise AssertionError("unexpected network call: " + endpoint)

        mgr._api_get = fail_get
        msg = make_e2e_message(self.room_key, "x", kid=self.kid)
        self.assertEqual(mgr.decrypt_to_dict(dict(msg), "room1"), msg)


class TestThreadSafety(E2ETestBase):

    def test_concurrent_decrypts_share_cache_safely(self):
        self._prepare_room()
        msg = make_e2e_message(self.room_key, "并发消息", kid=self.kid)
        errors = []

        def worker():
            try:
                for _ in range(20):
                    out = self.mgr.decrypt_to_dict(dict(msg), "room1")
                    if out.get("msg") != "并发消息":
                        errors.append("bad plaintext")
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
