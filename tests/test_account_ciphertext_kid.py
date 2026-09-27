# -*- coding: utf-8 -*-
"""密文带 kid（MF-50 验收不变量①）：形状、自证与 v1 兼容矩阵。

v1 密文不带密钥标识，"这把钥对不对"只能在解密撞 tag 后才知道——换钥/两侧分叉时
动手前后都无法自证。v2 起密文携带 kid（HMAC-SHA256 前 8 字节的单向指纹）：
解密前先比 kid，错钥直接拿到"密文 kid vs 当前钥 kid"两个可比对指纹。
兼容红线：既有 v1 密文（库内两列、`.env` 的 SMTPS_ENC/SECRET_ENC）**永久可读**，
新写入一律 v2；升级=读出旧格式按需重写，不强制全量迁移。

功能：密文形状与密钥指纹的向后兼容回归（双钥族：账号口径与固定 AAD 口径）。
归属：`yiban/infra/account_crypto.py` 的密文格式契约。
复用：`encrypt_password/decrypt_password/encrypt_text/decrypt_text/key_fingerprint`、
`SCHEMA_VERSION/SUPPORTED_SCHEMA_VERSIONS`。

标签：G · 安全：脱敏/审计/配置注入
覆盖：新密文形状（v2+kid）；kid 稳定性与单向性；v1 手工构造密文照常解密（兼容
矩阵核心格）；v2 错钥报双 kid；v1 错钥仍按 tag 拒；v2 缺 kid 拒；v3 版本拒；
AAD 语义不被 kid 检查削弱；两钥族（password/text）形状一致。
对应实现：`yiban/infra/account_crypto.py` 的 `key_fingerprint`、`_check_entry_version`、
`encrypt_password`、`encrypt_text`、`decrypt_password`、`decrypt_text`。
关键断言：v1 兼容格必须**同时**断两件事——对的钥能解出、错的钥仍拒绝（只断前者
会让"v1 全放行"的假兼容通过）；错钥消息必须含两侧 kid 才算可自证，只断抛
ValueError 不够。
依赖：无网络、无 skip；纯内存密钥，不触碰 `.env`。
"""
import json
import secrets
import unittest

from Crypto.Cipher import AES

from yiban.infra import account_crypto

KEY = bytes(range(1, 33))
OTHER_KEY = bytes(range(33, 65))
PHONE = "13800138000"


def _handcraft_v1(plain, key, phone=PHONE):
    """按 v1 形状（无 kid）手工构造密文对象——模拟现网存量，不经新写入路径。"""
    nonce = secrets.token_bytes(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    cipher.update(str(phone).encode("utf-8"))
    ct, tag = cipher.encrypt_and_digest(str(plain).encode("utf-8"))
    return {"v": 1, "nonce": nonce.hex(), "ct": ct.hex(), "tag": tag.hex()}


class KidShapeTest(unittest.TestCase):
    def test_new_password_ciphertext_is_v2_with_kid(self):
        ct = account_crypto.encrypt_password("pw123", KEY, PHONE)
        self.assertEqual(ct["v"], account_crypto.SCHEMA_VERSION)
        self.assertEqual(ct["v"], 2)
        self.assertEqual(len(ct["kid"]), account_crypto.KID_HEX_LEN)
        int(ct["kid"], 16)  # 十六进制可解析
        self.assertEqual(ct["kid"], account_crypto.key_fingerprint(KEY))
        self.assertEqual(account_crypto.decrypt_password(ct, KEY, PHONE), "pw123")

    def test_new_text_ciphertext_is_v2_with_kid(self):
        ct = account_crypto.encrypt_text("SCTabcdefghij", KEY)
        self.assertEqual(ct["v"], 2)
        self.assertEqual(ct["kid"], account_crypto.key_fingerprint(KEY))
        self.assertEqual(account_crypto.decrypt_text(ct, KEY), "SCTabcdefghij")

    def test_empty_plaintext_stays_empty_string(self):
        """空值语义不因 kid 改变：空即空，不产出"看似密文的空串"。"""
        self.assertEqual(account_crypto.encrypt_password("", KEY, PHONE), "")
        self.assertEqual(account_crypto.encrypt_text(None, KEY), "")

    def test_kid_is_stable_one_way_and_key_specific(self):
        kid_a = account_crypto.key_fingerprint(KEY)
        kid_b = account_crypto.key_fingerprint(OTHER_KEY)
        self.assertEqual(kid_a, account_crypto.key_fingerprint(KEY))
        self.assertNotEqual(kid_a, kid_b)
        self.assertNotIn(KEY.hex(), kid_a)  # 指纹不是密钥本身（单向：不泄露材料）
        # kid 不得被 is_encrypted 之外的形状判据排除：v2 仍是"密文对象"
        self.assertTrue(account_crypto.is_encrypted(
            account_crypto.encrypt_password("x", KEY, PHONE)))

    def test_ciphertexts_of_same_key_share_kid_across_families(self):
        """同钥跨账号列/邮件/SendKey 全同面：一把钥的 kid 在两族入口里同值。"""
        ct_pwd = account_crypto.encrypt_password("p", KEY, PHONE)
        ct_code = account_crypto.encrypt_password("c", KEY, PHONE)
        ct_text = account_crypto.encrypt_text("smtps-json", KEY)
        self.assertEqual(ct_pwd["kid"], ct_code["kid"])
        self.assertEqual(ct_code["kid"], ct_text["kid"])


class V1CompatibilityMatrixTest(unittest.TestCase):
    """兼容红线：既有无 kid 密文必须仍可解；错钥仍必须拒。"""

    def test_v1_password_decrypts_with_right_key(self):
        v1 = _handcraft_v1("old-password", KEY)
        self.assertEqual(account_crypto.decrypt_password(v1, KEY, PHONE), "old-password")

    def test_v1_password_still_rejects_wrong_key(self):
        # 只断"能解"会漏掉"v1 全放行"的假兼容——错钥这一半必须同样钉死
        v1 = _handcraft_v1("old-password", KEY)
        with self.assertRaises(ValueError) as ctx:
            account_crypto.decrypt_password(v1, OTHER_KEY, PHONE)
        self.assertIn("解密失败", str(ctx.exception))

    def test_v1_text_decrypts_and_rejects(self):
        v1 = {"v": 1}
        nonce = secrets.token_bytes(12)
        cipher = AES.new(KEY, AES.MODE_GCM, nonce=nonce)
        cipher.update(b"yiban-notify")
        ct, tag = cipher.encrypt_and_digest(b"https://sct.ftqq.com/x")
        v1.update({"nonce": nonce.hex(), "ct": ct.hex(), "tag": tag.hex()})
        self.assertEqual(account_crypto.decrypt_text(v1, KEY),
                         "https://sct.ftqq.com/x")
        with self.assertRaises(ValueError):
            account_crypto.decrypt_text(v1, OTHER_KEY)

    def test_v1_ciphertext_survives_json_store_roundtrip(self):
        """库内存的是 JSON 串——v1 串经 dumps/loads 后形状判据与解密都照常。"""
        blob = _handcraft_v1("pw", KEY)
        loaded = json.loads(json.dumps(blob))
        self.assertTrue(account_crypto.is_encrypted(loaded))
        self.assertEqual(account_crypto.decrypt_password(loaded, KEY, PHONE), "pw")


class KidSelfProofTest(unittest.TestCase):
    def test_v2_wrong_key_message_carries_both_kids(self):
        ct = account_crypto.encrypt_password("secret", KEY, PHONE)
        with self.assertRaises(ValueError) as ctx:
            account_crypto.decrypt_password(ct, OTHER_KEY, PHONE)
        msg = str(ctx.exception)
        self.assertIn(ct["kid"], msg, "错钥诊断必须带密文 kid（可自证）")
        self.assertIn(account_crypto.key_fingerprint(OTHER_KEY), msg,
                      "错钥诊断必须带当前钥 kid")

    def test_v2_text_wrong_key_message_carries_both_kids(self):
        ct = account_crypto.encrypt_text("smtps", KEY)
        with self.assertRaises(ValueError) as ctx:
            account_crypto.decrypt_text(ct, OTHER_KEY)
        self.assertIn(ct["kid"], str(ctx.exception))

    def test_v2_missing_kid_rejected_as_mismatch(self):
        # 手造 v2 但不带 kid：不得静默当成 v1 放行
        ct = account_crypto.encrypt_password("p", KEY, PHONE)
        ct.pop("kid")
        with self.assertRaises(ValueError) as ctx:
            account_crypto.decrypt_password(ct, KEY, PHONE)
        self.assertIn("<缺失>", str(ctx.exception))

    def test_unsupported_version_still_rejected(self):
        for ver in (0, 3, 99, "2"):
            with self.assertRaises(ValueError) as ctx:
                account_crypto.decrypt_password(
                    {"v": ver, "kid": "0" * 16, "nonce": "00" * 12,
                     "ct": "aa", "tag": "bb"}, KEY, PHONE)
            self.assertIn("不支持的密文版本", str(ctx.exception))

    def test_aad_semantics_not_weakened_by_kid_check(self):
        """kid 检查只挡"钥不对"，不得顶替 AAD：同钥改手机号仍须撞 tag 失败。"""
        ct = account_crypto.encrypt_password("p", KEY, PHONE)
        with self.assertRaises(ValueError) as ctx:
            account_crypto.decrypt_password(ct, KEY, "13900139000")
        self.assertIn("解密失败", str(ctx.exception))

    def test_tampered_v2_ciphertext_rejected(self):
        ct = account_crypto.encrypt_password("p", KEY, PHONE)
        flipped = ("0" if ct["ct"][0] != "0" else "1") + ct["ct"][1:]
        ct["ct"] = flipped
        with self.assertRaises(ValueError):
            account_crypto.decrypt_password(ct, KEY, PHONE)


class BadKeyValueErrorTest(unittest.TestCase):
    def test_encrypt_rejects_bad_key_with_value_error(self):
        """加密侧的坏钥同样收口成 ValueError（与解密侧对称，调用方只 except ValueError）。"""
        for bad in (None, "1" * 64, b"short", bytearray(31)):
            with self.assertRaises(ValueError):
                account_crypto.encrypt_password("p", bad, PHONE)
            with self.assertRaises(ValueError):
                account_crypto.encrypt_text("t", bad)

    def test_empty_plaintext_with_bad_key_still_returns_empty(self):
        # 空值短路排在钥校验之前：空凭据的写路径不依赖密钥存在与否（语义不变）
        self.assertEqual(account_crypto.encrypt_password("", None, PHONE), "")


if __name__ == "__main__":
    unittest.main()
