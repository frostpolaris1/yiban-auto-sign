# -*- coding: utf-8 -*-
"""告警收件人可编辑 + 站点分享摘要（2026-09-09）。

背景（用户报告）：
- 告警收件人 YIBAN_MAIL_ADMIN_TO 是 A 线告警收件算法的唯一来源，但 2026-09-08
  摘除条目级 admin_to 死字段时，把唯一能改收件人的入口一起删了——此后改收件人
  只能登服务器改 .env。本组钉的是重新开放的写入路径。
- 全站此前无 meta description 与 og:* 标签，分享链接解析出的卡片只有标题、
  没有简介。

覆盖：
- PUT /api/mail-config admin_to：落盘/多地址/留空不改动/显式清空/非法格式 400/
  非主管理员 403/需口令/旧收件人收到"你已被移出"通知
- GET /api/mail-config：admin_to 打码展示
- 站点摘要：登录页与文档页含 description/og 标签，且可用 .env 覆盖

用法（项目根目录）：
    py -m pytest tests/test_mail_admin_to_edit_0909.py -v
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import env_io

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
ADMIN_PASS = "MasterPass#2026"

# 本文件内可能触碰的 YIBAN_MAIL_*/站点键（tearDownClass 统一还原，防泄漏到后续测试）
_ENV_KEYS = (
    "YIBAN_MAIL_ENABLE", "YIBAN_MAIL_ADMIN_TO", "YIBAN_MAIL_ADMIN_NOTIFY",
    "YIBAN_MAIL_USER", "YIBAN_MAIL_PASS", "YIBAN_MAIL_SMTPS_ENC",
    "YIBAN_SITE_DESCRIPTION", "YIBAN_SITE_IMAGE",
)


def _load_webapp(tag):
    import db as _db
    spec = importlib.util.spec_from_file_location(f"webapp_{tag}", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"webapp_{tag}"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return _db, mod


class _Base(unittest.TestCase):
    """共享脚手架：临时 .env/DB + webapp 加载 + 主管理员登录（照抄 test_mail_failover_0907）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-admin-to-")
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

    def _reset_env_file(self, extra=""):
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n" + extra
            )

    def _master(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin@test.local", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        t = c.get("/api/me").get_json()["csrf_token"]
        return c, {"X-CSRF-Token": t}

    def _env_admin_to(self):
        return env_io.parse_env_file(self.env_file).get("YIBAN_MAIL_ADMIN_TO")


class AdminToWriteTest(_Base):
    """PUT /api/mail-config admin_to：写入路径与门禁。"""

    def test_put_writes_single_address(self):
        self._reset_env_file("YIBAN_MAIL_ADMIN_TO=old@test.local\n")
        c, h = self._master()
        r = c.put("/api/mail-config",
                  json={"admin_to": "new@test.local", "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._env_admin_to(), "new@test.local")

    def test_put_writes_multiple_addresses_normalized(self):
        """逗号分隔多地址：去空白后按顺序落盘。"""
        self._reset_env_file()
        c, h = self._master()
        r = c.put("/api/mail-config",
                  json={"admin_to": " a@test.local , b@test.local ", "confirm_password": ADMIN_PASS},
                  headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._env_admin_to(), "a@test.local,b@test.local")

    def test_put_requires_confirm_password(self):
        self._reset_env_file("YIBAN_MAIL_ADMIN_TO=old@test.local\n")
        c, h = self._master()
        r = c.put("/api/mail-config", json={"admin_to": "new@test.local"}, headers=h)
        self.assertEqual(r.status_code, 400, "改收件人属高危动作，必须二次鉴权")
        self.assertEqual(self._env_admin_to(), "old@test.local", "未通过鉴权不得落盘")

    def test_put_requires_master_admin(self):
        """普通注册管理员不得改收件人（403，不落盘）。"""
        self._reset_env_file("YIBAN_MAIL_ADMIN_TO=old@test.local\n")
        self.db.create_user("regadmin@test.local",
                            self.webapp.generate_password_hash("RegPass#2026"),
                            role="admin", pw_version=1)
        c = self.webapp.create_app().test_client()
        c.post("/api/login", json={"username": "regadmin@test.local", "password": "RegPass#2026"})
        t = c.get("/api/me").get_json()["csrf_token"]
        r = c.put("/api/mail-config",
                  json={"admin_to": "new@test.local", "confirm_password": "RegPass#2026"},
                  headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self._env_admin_to(), "old@test.local")

    def test_invalid_address_rejected_and_no_write(self):
        """非法地址整请求拒绝（多地址中任一非法即拒绝），零写盘痕迹。"""
        self._reset_env_file("YIBAN_MAIL_ADMIN_TO=old@test.local\n")
        c, h = self._master()
        for bad in ("not-an-email", "a@test.local,broken", "@test.local", "a b@test.local"):
            r = c.put("/api/mail-config",
                      json={"admin_to": bad, "confirm_password": ADMIN_PASS}, headers=h)
            self.assertEqual(r.status_code, 400, f"{bad!r} 应被拒绝")
        self.assertEqual(self._env_admin_to(), "old@test.local", "非法输入不得改动配置")

    def test_put_empty_string_clears_recipient(self):
        """显式提交空串 = 清空收件人（write_env_batch 空值 = 删键）。"""
        self._reset_env_file("YIBAN_MAIL_ADMIN_TO=old@test.local\n")
        c, h = self._master()
        r = c.put("/api/mail-config",
                  json={"admin_to": "", "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIsNone(self._env_admin_to(), "清空后 ADMIN_TO 键应被删除")

    def test_put_absent_key_leaves_value_unchanged(self):
        """键缺失 = 不改动（部分更新不得把收件人静默清空）。"""
        self._reset_env_file("YIBAN_MAIL_ADMIN_TO=keep@test.local\n")
        c, h = self._master()
        r = c.put("/api/mail-config", json={"enabled": True}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._env_admin_to(), "keep@test.local")

    def test_get_masks_recipient(self):
        self._reset_env_file("YIBAN_MAIL_ADMIN_TO=boss@test.local\n")
        c, h = self._master()
        data = c.get("/api/mail-config", headers=h).get_json()
        self.assertIn("admin_to", data)
        self.assertNotIn("boss@test.local", json.dumps(data), "状态响应不得回显完整收件人地址")

    def test_audit_records_masked_recipient(self):
        self._reset_env_file()
        c, h = self._master()
        c.put("/api/mail-config",
              json={"admin_to": "audited@test.local", "confirm_password": ADMIN_PASS}, headers=h)
        conn = self.db._conn
        rows = conn.execute(
            "SELECT detail FROM audit_logs WHERE action='mail_config' ORDER BY id DESC LIMIT 1"
        ).fetchall()
        self.assertTrue(rows, "应写入 mail_config 审计")
        self.assertNotIn("audited@test.local", rows[0][0], "审计只记打码值")

    def test_old_recipient_notified_on_change(self):
        """被摘掉的旧收件人必须收到通知——否则被盗会话改收件人即致盲。"""
        self._reset_env_file("YIBAN_MAIL_ADMIN_TO=old@test.local\n")
        c, h = self._master()
        with mock.patch.object(self.webapp.mailer, "send_admin_alert", return_value=True) as m, \
                mock.patch.object(self.webapp, "send_notification", return_value=None):
            r = c.put("/api/mail-config",
                      json={"admin_to": "new@test.local", "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        targets = [call.kwargs.get("to") for call in m.call_args_list]
        self.assertIn("old@test.local", targets,
                      "旧收件人应收到『你已不再是收件人』通知")

    def test_no_stale_notice_when_address_unchanged(self):
        """地址没变（仅大小写/空白差异之外的同值）不应重复打扰。"""
        self._reset_env_file("YIBAN_MAIL_ADMIN_TO=same@test.local\n")
        c, h = self._master()
        with mock.patch.object(self.webapp.mailer, "send_admin_alert", return_value=True) as m, \
                mock.patch.object(self.webapp, "send_notification", return_value=None):
            r = c.put("/api/mail-config",
                      json={"admin_to": "same@test.local", "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(m.call_args_list, [], "同值提交不应发出旧收件人通知")


class SiteDescriptionTest(_Base):
    """站点分享摘要：登录页与文档页含 description/og，可用 .env 覆盖。"""

    def setUp(self):
        # unittest 按方法名字母序执行，.env 覆盖会跨用例残留（且 site_description
        # 每次读文件）——每个用例都从干净基线起步。
        super().setUp()
        self._reset_env_file()

    def test_login_page_has_description_and_og(self):
        c = self.webapp.create_app().test_client()
        html = c.get("/login").get_data(as_text=True)
        self.assertIn('<meta name="description"', html)
        self.assertIn('<meta property="og:title"', html)
        self.assertIn('<meta property="og:description"', html)
        self.assertIn(self.webapp.SITE_DESCRIPTION_DEFAULT, html)

    def test_doc_pages_have_description(self):
        c = self.webapp.create_app().test_client()
        for path in ("/terms", "/privacy"):
            html = c.get(path).get_data(as_text=True)
            self.assertIn('<meta name="description"', html, f"{path} 缺摘要")
            self.assertIn('<meta property="og:description"', html, f"{path} 缺 og 摘要")

    def test_env_overrides_description(self):
        self._reset_env_file("YIBAN_SITE_DESCRIPTION=自定义站点简介\n")
        c = self.webapp.create_app().test_client()
        html = c.get("/login").get_data(as_text=True)
        self.assertIn("自定义站点简介", html)
        self.assertNotIn(self.webapp.SITE_DESCRIPTION_DEFAULT, html)

    def test_description_is_escaped(self):
        """摘要来自 .env，进 content 属性前必须转义（防属性注入）。"""
        self._reset_env_file('YIBAN_SITE_DESCRIPTION=x"><script>alert(1)</script>\n')
        c = self.webapp.create_app().test_client()
        html = c.get("/login").get_data(as_text=True)
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn("&lt;script&gt;", html)

    def test_site_image_requires_https(self):
        """og:image 仅接受 https 绝对地址，其余一律忽略。"""
        self._reset_env_file("YIBAN_SITE_IMAGE=javascript:alert(1)\n")
        c = self.webapp.create_app().test_client()
        self.assertNotIn('property="og:image"', c.get("/login").get_data(as_text=True))
        self._reset_env_file("YIBAN_SITE_IMAGE=https://cdn.example.com/a.png\n")
        self.assertIn('content="https://cdn.example.com/a.png"',
                      c.get("/login").get_data(as_text=True))


class PlaceholderFontParityTest(_Base):
    """占位文字字号：三模板不得再把 placeholder 缩到 0.92em（与输入值不一致）。"""

    def _read(self, name):
        with io.open(os.path.join(BASE, "web", "templates", name), encoding="utf-8") as f:
            return f.read()

    def test_no_placeholder_font_shrink(self):
        for name in ("index.html", "user.html", "login.html"):
            src = self._read(name)
            self.assertNotIn("::placeholder { font-size", src, f"{name} 仍有 placeholder 字号缩放")
            self.assertNotIn("::placeholder{font-size", src, f"{name} 仍有 placeholder 字号缩放")

    def test_capacity_inputs_no_inline_shrink(self):
        """容量上限输入框不得再用内联 0.92em（与同卡其它输入框不一致）。"""
        src = self._read("index.html")
        self.assertNotIn('style="font-size:0.92em"', src)


if __name__ == "__main__":
    unittest.main()
