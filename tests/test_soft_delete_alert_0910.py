# -*- coding: utf-8 -*-
"""批次20 Y1 回归：管理端「软删账号」必须告警 + 占用高危限速（2026-09-10）。

背景（对抗性审查批次20 Y1，已活体复现）：注册管理员（users.role='admin'，非 .env
内置主管理员）原先可用「单条 DELETE」或「批量 action=delete」**一次请求批量**软删
全站账号（含 owner='admin' 的主管理员直属账号）——不需要二次口令、**不占用任何高危
限速、不产生任何告警**；受害者 `POST /api/my-accounts/<idx>/restore` 会被 403
「该账号由管理员删除」，无法自救；7 天后物理清除。等价于"被盗的注册管理员会话可
静默让全站停签"。

用户裁决：**保留"不要求二次口令"**（既有裁决：软删可逆，加口令只增误伤），
但**要加告警 + 高危限速**。本文件锁住这三条：
1. 单条软删 → 产生「高危管理操作告警」（正文含脱敏手机号，不含完整号）；
2. 批量软删 → 产生一条汇总告警；
3. 超过 `YIBAN_ADMIN_DELETE_MAX` 后再软删 → 429；
4. 用户自助删除自己账号的路径**不受该限速影响**（不得误伤普通用户）。

用法（项目根目录）：
    py -m pytest tests/test_soft_delete_alert_0910.py -q
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
REG_ADMIN = "regadmin@test.local"
REG_ADMIN_PASS = "RegAdmin#2026"
USER = "stu@test.local"
USER_PASS = "Student#2026"


def _load_webapp():
    spec = importlib.util.spec_from_file_location("webapp_sdalert", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webapp_sdalert"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return mod


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-sdalert-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        os.environ["YIBAN_MAIL_ENABLE"] = "0"
        cls.db = __import__("db")
        cls.webapp = _load_webapp()

    @classmethod
    def tearDownClass(cls):
        if cls.db._conn is not None:
            with contextlib.suppress(Exception):
                cls.db._conn.close()
            cls.db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if self.db._conn is not None:
            with contextlib.suppress(Exception):
                self.db._conn.close()
            self.db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        # 每个用例重置 .env（限速阈值按用例调整）
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        self.db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)
        # 告警探针：替换模块级 send_notification（handler 走全局查找，运行时生效）
        self.alerts = []
        self._orig_send = self.webapp.send_notification

        def _probe(title, content, **kw):
            self.alerts.append((title, content, kw))
            return True

        self.webapp.send_notification = _probe

    def tearDown(self):
        self.webapp.send_notification = self._orig_send

    # ---- 脚手架 ----
    def _mk_registered_admin(self):
        from werkzeug.security import generate_password_hash
        self.db.create_user(REG_ADMIN, generate_password_hash(REG_ADMIN_PASS), role="admin")

    def _mk_user_with_account(self, email=USER, phone="13900000001"):
        from werkzeug.security import generate_password_hash
        self.db.create_user(email, generate_password_hash(USER_PASS), role="user")
        self.db.add_account({
            "name": "测试账号", "phone": phone, "password": "pw",
            "phone_model": "", "phone_code": "", "owner": email,
            "status": "active", "reject_reason": "",
        })

    def _login(self, username, password):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        token = c.get("/api/me").get_json()["csrf_token"]
        return c, {"X-CSRF-Token": token}

    def _alerts_with_title(self, kw="高危管理操作告警"):
        return [(t, c) for t, c, _ in self.alerts if t == kw]


class SoftDeleteAlertTest(_Base):
    def test_single_soft_delete_emits_alert(self):
        """注册管理员单条软删 → 200 且产生告警（脱敏号，不含完整号）。"""
        self._mk_registered_admin()
        self._mk_user_with_account()
        c, h = self._login(REG_ADMIN, REG_ADMIN_PASS)
        r = c.delete("/api/accounts/0", json={}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        hits = self._alerts_with_title()
        self.assertEqual(len(hits), 1, f"应产生 1 条软删告警，实际 {len(self.alerts)} 条: {self.alerts}")
        body = hits[0][1]
        self.assertIn("软删", body)
        self.assertIn("139****0001", body, "告警应含脱敏手机号")
        self.assertNotIn("13900000001", body, "告警绝不能含完整手机号")
        # 确认软删确实生效
        row = next(a for a in self.db.load_accounts_raw() if a["phone"] == "13900000001")
        self.assertEqual(row["deleted"], 1)

    def test_batch_soft_delete_emits_single_summary_alert(self):
        """批量软删 → 200 且只产生一条汇总告警（含数量与脱敏号清单）。"""
        self._mk_registered_admin()
        self._mk_user_with_account(USER, "13900000001")
        self.db.add_account({
            "name": "裸账号", "phone": "13900000002", "password": "pw",
            "phone_model": "", "phone_code": "", "owner": "admin",
            "status": "active", "reject_reason": "",
        })
        c, h = self._login(REG_ADMIN, REG_ADMIN_PASS)
        accts = c.get("/api/accounts").get_json()["accounts"]
        ids = [a["index"] for a in accts]
        phones = [a["phone"] for a in accts]
        r = c.post("/api/accounts/batch",
                   json={"action": "delete", "ids": ids, "phones": phones}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        hits = self._alerts_with_title()
        self.assertEqual(len(hits), 1, "批量软删应只发一条汇总告警")
        body = hits[0][1]
        self.assertIn(f"×{len(ids)}", body)
        self.assertNotIn("13900000001", body, "汇总告警同样不能含完整号")

    def test_soft_delete_rate_limited(self):
        """超过 YIBAN_ADMIN_DELETE_MAX 后软删返回 429（单条与批量共用同一额度）。"""
        self._mk_registered_admin()
        self._mk_user_with_account(USER, "13900000001")
        self.db.add_account({
            "name": "裸账号", "phone": "13900000002", "password": "pw",
            "phone_model": "", "phone_code": "", "owner": "admin",
            "status": "active", "reject_reason": "",
        })
        # 阈值设为 1：第一次成功，第二次 429（限速读 .env，热生效）
        self.webapp.write_env_key(self.env_file, "YIBAN_ADMIN_DELETE_MAX", "1")
        self.webapp.write_env_key(self.env_file, "YIBAN_ADMIN_DELETE_COOLDOWN_SEC", "600")
        c, h = self._login(REG_ADMIN, REG_ADMIN_PASS)
        r1 = c.delete("/api/accounts/0", json={}, headers=h)
        self.assertEqual(r1.status_code, 200, r1.get_data(as_text=True))
        r2 = c.delete("/api/accounts/1", json={}, headers=h)
        self.assertEqual(r2.status_code, 429, f"第二次软删应被限速，实际 {r2.status_code}")
        self.assertIn("频繁", r2.get_json()["error"])

    def test_user_self_delete_not_blocked_by_admin_limit(self):
        """用户自助删除自己账号不走高危限速（不得误伤普通用户）。"""
        self._mk_registered_admin()
        self._mk_user_with_account(USER, "13900000001")
        self.db.add_account({
            "name": "裸账号", "phone": "13900000002", "password": "pw",
            "phone_model": "", "phone_code": "", "owner": "admin",
            "status": "active", "reject_reason": "",
        })
        self.webapp.write_env_key(self.env_file, "YIBAN_ADMIN_DELETE_MAX", "1")
        self.webapp.write_env_key(self.env_file, "YIBAN_ADMIN_DELETE_COOLDOWN_SEC", "600")
        # 管理员先消耗掉额度（删 index=1 的裸账号，避免碰掉学生自己的账号）
        ca, ha = self._login(REG_ADMIN, REG_ADMIN_PASS)
        self.assertEqual(ca.delete("/api/accounts/1", json={}, headers=ha).status_code, 200)
        # 用户删自己（my-accounts 下标 0 = 本人唯一账号）→ 必须仍然 200
        cu, hu = self._login(USER, USER_PASS)
        ru = cu.delete("/api/my-accounts/0", json={}, headers=hu)
        self.assertEqual(ru.status_code, 200, f"用户自助删除不得被管理员额度影响: {ru.get_data(as_text=True)}")


if __name__ == "__main__":
    unittest.main()
