# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""推送/邮件配置的写侧输入上限与"按落盘后实际类型校验"。

覆盖两件事：
1. 通知通道的校验依据必须是**落盘后生效的类型**——`type` 置空但带密钥时加载侧会把空类型
   解析成 custom，若沿用请求里的类型判定，`{"type": "", "secret": "http://内网/x"}` 就能
   绕开地址白名单把内网地址写进去。
2. 输入长度/条数上限：裸机直连形态没有 nginx 的请求体兜底，无上限的值会原样加密进 .env
   并在每次推送/发信时带出。

标签：G · 安全：脱敏/审计/配置注入
覆盖：通知通道"按落盘后生效类型校验"的一条绕过面（`type` 置空 + 内网/明文 http URL）、
Server 酱 Key 与自定义 URL 的长度上限、管理员收件人条数与 SMTP 主机条数上限、
SMTP 主机写侧的网段门禁（回环/保留段/私网 + 显式放行开关）。
对应实现：`web/app.py` 的推送配置与邮件配置写接口，校验落点在
`yiban/notify/config.py`（`is_safe_url`）与 `yiban/mail/config.py` 的写侧。
关键断言：`{"type": "", "secret": "http://内网/x"}` 必须被拒——这条守的是"请求里说的类型"
与"落盘后解析出的类型"不是同一个**这一格**绕过，加载侧把空类型解析成 custom 才成立；
上限类用例一律配一条"恰好等于上限仍可写入"的正向对照，否则把 cap 调成 0 也能全绿；
`reserved`（组播/保留段）即使带显式放行开关也要拒，私网段才允许带开关放行——两者不同级。
依赖：Flask test client + 临时 `.env`（`_master` 现取主管理员口令），无网络、无 skip。
条数/URL 长度上限从 `webapp.MAIL_ADMIN_TO_MAX` / `MAIL_SMTPS_MAX` / `NOTIFY_URL_MAX_LEN`
取值（与实现同源，改实现会自动跟进）；Server 酱 Key 的 `SCT123` / `"SCT"+"a"*200` 是
硬编码字面量，实现侧改那两个边界不会让本文件自动变红，需人工对一遍。
"""

import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
ADMIN_PASS = "MasterPass#2026"

_ENV_KEYS = (
    "YIBAN_MAIL_ENABLE", "YIBAN_MAIL_ADMIN_TO", "YIBAN_MAIL_SMTPS_ENC",
    "YIBAN_NOTIFY_TYPE", "YIBAN_NOTIFY_SECRET_ENC",
)


def _load_webapp(tag):
    import db as _db
    spec = importlib.util.spec_from_file_location(
        f"webapp_{tag}", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"webapp_{tag}"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return _db, mod


class _Base(unittest.TestCase):
    """临时 .env/DB + webapp 加载 + 主管理员登录（照抄 test_mailer.py 的管理员写信脚手架）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-caps-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls._old_env = {k: os.environ.get(k) for k in (*_ENV_KEYS, "YIBAN_ENV_FILE")}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        cls.db, cls.webapp = _load_webapp(cls.__name__)

    @classmethod
    def tearDownClass(cls):
        if cls.db._conn is not None:
            with contextlib.suppress(Exception):
                cls.db._conn.close()
            cls.db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        if self.db._conn is not None:
            with contextlib.suppress(Exception):
                self.db._conn.close()
            self.db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        self.db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )

    def _master(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin@test.local", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        t = c.get("/api/me").get_json()["csrf_token"]
        return c, {"X-CSRF-Token": t}

    def _env(self, key):
        with io.open(self.env_file, encoding="utf-8") as f:
            for line in f.read().splitlines():
                if line.startswith(key + "="):
                    return line.split("=", 1)[1]
        return None


class NotifyTypeValidationTest(_Base):
    """校验依据 = 落盘后实际生效的类型（空类型 + 密钥 = custom）。"""

    def test_empty_type_with_internal_url_is_rejected(self):
        """`type` 置空但带密钥：加载侧会当 custom 用，故必须按 custom 校验地址。"""
        c, h = self._master()
        r = c.put("/api/notify-config",
                  json={"type": "", "secret": "http://127.0.0.1:9/hook",
                        "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIsNone(self._env("YIBAN_NOTIFY_SECRET_ENC"),
                          "被拒的请求不得留下任何落盘")

    def test_empty_type_with_plain_http_public_url_is_rejected(self):
        """非 HTTPS 同样不行——空类型不是"免检通道"。"""
        c, h = self._master()
        r = c.put("/api/notify-config",
                  json={"type": "", "secret": "http://hooks.test.local/abc",
                        "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))

    def test_empty_type_with_https_url_still_accepted(self):
        """合法的旧写法（只配密钥、不配类型）必须继续可用。"""
        c, h = self._master()
        r = c.put("/api/notify-config",
                  json={"type": "", "secret": "https://hooks.test.local/abc",
                        "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self._env("YIBAN_NOTIFY_SECRET_ENC"), "合法密钥应已加密落盘")


class NotifyInputCapsTest(_Base):
    """Server酱密钥与自定义地址的长度上限。"""

    def test_serverchan_key_too_short_rejected(self):
        c, h = self._master()
        r = c.put("/api/notify-config",
                  json={"type": "serverchan", "secret": "SCT123",
                        "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIsNone(self._env("YIBAN_NOTIFY_SECRET_ENC"))

    def test_serverchan_key_too_long_rejected(self):
        c, h = self._master()
        r = c.put("/api/notify-config",
                  json={"type": "serverchan", "secret": "SCT" + "a" * 200,
                        "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))

    def test_serverchan_key_within_bounds_accepted(self):
        c, h = self._master()
        r = c.put("/api/notify-config",
                  json={"type": "serverchan", "secret": "SCT" + "a" * 32,
                        "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_custom_url_too_long_rejected(self):
        c, h = self._master()
        long_url = "https://hooks.test.local/" + "a" * self.webapp.NOTIFY_URL_MAX_LEN
        r = c.put("/api/notify-config",
                  json={"type": "custom", "secret": long_url,
                        "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))


class MailInputCapsTest(_Base):
    """告警收件人与 SMTP 条目列表的条数上限。"""

    def test_admin_to_count_capped(self):
        c, h = self._master()
        addrs = [f"a{i}@test.local" for i in range(self.webapp.MAIL_ADMIN_TO_MAX + 1)]
        r = c.put("/api/mail-config",
                  json={"admin_to": ",".join(addrs), "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIsNone(self._env("YIBAN_MAIL_ADMIN_TO"), "被拒的请求不得落盘")

    def test_admin_to_at_cap_accepted(self):
        c, h = self._master()
        addrs = [f"a{i}@test.local" for i in range(self.webapp.MAIL_ADMIN_TO_MAX)]
        r = c.put("/api/mail-config",
                  json={"admin_to": ",".join(addrs), "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._env("YIBAN_MAIL_ADMIN_TO"), ",".join(addrs))

    def test_smtps_count_capped(self):
        c, h = self._master()
        entries = [{"host": f"smtp{i}.test.local", "user": "u@test.local",
                    "pass": "pw", "port": 465}
                   for i in range(self.webapp.MAIL_SMTPS_MAX + 1)]
        r = c.put("/api/mail-config",
                  json={"smtps": entries, "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIsNone(self._env("YIBAN_MAIL_SMTPS_ENC"), "被拒的请求不得落盘")

    def test_smtps_at_cap_accepted(self):
        c, h = self._master()
        entries = [{"host": f"smtp{i}.test.local", "user": "u@test.local",
                    "pass": "pw", "port": 465}
                   for i in range(self.webapp.MAIL_SMTPS_MAX)]
        r = c.put("/api/mail-config",
                  json={"smtps": entries, "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self._env("YIBAN_MAIL_SMTPS_ENC"))


class SmtpTargetWriteGuardTest(_Base):
    """SMTP 目标地址在写侧硬拦：内网/不可路由段默认拒，显式开关才放行。

    没有这道拦，拿到被窃主管理员会话的人改一次 SMTP 目标，就能用发信失败日志当内网
    端口扫描器（连接被拒、超时、无路由在这类日志里互不相同）。
    """

    def _put_one_host(self, host, c, h, extra=""):
        if extra:
            with io.open(self.env_file, "a", encoding="utf-8") as f:
                f.write(extra + "\n")
        return c.put("/api/mail-config",
                     json={"smtps": [{"host": host, "user": "u@test.local",
                                      "pass": "pw", "port": 465}],
                           "confirm_password": ADMIN_PASS}, headers=h)

    def test_loopback_host_rejected_by_default(self):
        c, h = self._master()
        r = self._put_one_host("127.0.0.1", c, h)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("YIBAN_MAIL_ALLOW_PRIVATE_HOST", r.get_json()["error"],
                      "拒绝文案要告诉运维怎么保留内网 MTA")
        self.assertIsNone(self._env("YIBAN_MAIL_SMTPS_ENC"), "被拒的请求不得落盘")

    def test_reserved_host_rejected_even_with_flag(self):
        """元数据/链路本地这类不可路由段不是"自建内网"，开关也不放行。"""
        c, h = self._master()
        r = self._put_one_host("169.254.169.254", c, h, extra="YIBAN_MAIL_ALLOW_PRIVATE_HOST=1")
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIsNone(self._env("YIBAN_MAIL_SMTPS_ENC"))

    def test_private_host_accepted_with_explicit_flag(self):
        """本机/内网 postfix 的部署：显式开关后照常可配。"""
        c, h = self._master()
        r = self._put_one_host("127.0.0.1", c, h, extra="YIBAN_MAIL_ALLOW_PRIVATE_HOST=1")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self._env("YIBAN_MAIL_SMTPS_ENC"))

    def test_public_host_accepted(self):
        c, h = self._master()
        r = self._put_one_host("smtp.example.com", c, h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
