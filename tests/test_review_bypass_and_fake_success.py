# -*- coding: utf-8 -*-
"""绕审直进主链与"假成功"回归（MF-59 ①②）。

标签：E · 存储：账号表 / Web：个人自助
覆盖：① `replace_accounts` 缺 status 的行必须与 `add_account`、建表 DDL 同一缺省口径
    （pending），不得以 active 落库被引擎真实外呼；显式带 status 的整表回环原样保留。
    ② `POST /api/my-accounts` 异步校验路径：配额已扣而 `_start_verify_job` 抛
    `VerifyGateBusy` 时，响应不得静默 `ok:true` 装作无事发生——如实给
    `status=verify_deferred` 并在 msg 交代"本次未排上在线校验"。
对应实现：`yiban/store/accounts.py::replace_accounts`；
    `web/routes/my.py::api_my_account_add` 的 VerifyGateBusy 分支。
关键断言：①含引擎选号面（`_load_accounts_from_file` 只跳 pending/rejected/deleted，
    pending 的行必须缺席）；②账号仍入库待审核（拒绝的是"假装在验证"，不是提交本身）。
依赖：纯本地 SQLite + Flask test client，patch 掉 `_verify_queue_full`/
    `_start_verify_job`，不联网。
"""
import contextlib
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "c" * 64
ADMIN_PASS = "MasterPass#2026"
IMPORT_PHONE = "13800000004"   # 假号（遮罩形态 138****0004）
VERIFY_PHONE = "13800000009"   # 假号（遮罩形态 138****0009）
USER_EMAIL = "user9@test.local"
GOODPW = "Abcdef12345!"


class ReplaceAccountsDefaultTest(unittest.TestCase):
    """① 整表替换/导入的缺省状态与 add_account 同口径。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-replace4a-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                    "YIBAN_ADMIN_USER=admin\n"
                    f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        global db
        import db

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        db.init_db(self.db_file, env_file=self.env_file)

    def test_statusless_rows_land_pending_not_active(self):
        db.replace_accounts([
            {"name": "无状态导入", "phone": IMPORT_PHONE, "password": "pw1"},
            {"name": "显式回环", "phone": "13800000006", "password": "pw2",
             "status": "active"},
        ])
        rows = {a["phone"]: a for a in db.load_accounts()}
        self.assertEqual(rows[IMPORT_PHONE]["status"], "pending",
                         "缺省必须与 add_account/DDL 同口径：未带状态的导入绕不开审核")
        self.assertEqual(rows["13800000006"]["status"], "active",
                         "显式带 status 的整表回环（导出→导入）原样保留")
        from yiban.engine import accounts as accounts_mod
        signed = [a.phone for a in accounts_mod._load_accounts_from_file(migrate=False)]
        self.assertNotIn(IMPORT_PHONE, signed,
                         "未过审的行不得被引擎选为当日签到账号")


class VerifyGateBusyHonestTest(unittest.TestCase):
    """② 配额已扣而任务未起：如实报，不静默 ok。"""

    @classmethod
    def setUpClass(cls):
        global db
        import db
        cls.env_file4a = os.path.join(tempfile.mkdtemp(prefix="yiban-verify4a-"), ".env")
        cls.db_file4a = os.path.join(os.path.dirname(cls.env_file4a), "yiban.db")
        with open(cls.env_file4a, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                    "YIBAN_ADMIN_USER=admin\n"
                    f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
                    "YIBAN_ACCOUNT_VERIFY=1\nYIBAN_VERIFY_ASYNC=1\n")
        os.environ["YIBAN_ENV_FILE"] = cls.env_file4a
        os.environ["YIBAN_DB_FILE"] = cls.db_file4a
        os.environ["YIBAN_STATE_DIR"] = os.path.dirname(cls.env_file4a)
        os.environ["YIBAN_LOG_FILE"] = os.path.join(
            os.path.dirname(cls.env_file4a), "sign.log")
        spec = importlib.util.spec_from_file_location(
            "webapp_verify4a", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_verify4a"] = cls.webapp
        spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        sys.modules.pop("webapp_verify4a", None)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file4a + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file4a, env_file=self.env_file4a)
        db.create_user(USER_EMAIL, self.webapp.generate_password_hash(GOODPW))

    def test_gate_busy_response_is_honest(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": USER_EMAIL, "password": GOODPW})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        token = c.get("/api/me").get_json()["csrf_token"]
        with mock.patch.object(self.webapp, "_verify_queue_full", return_value=False), \
             mock.patch.object(self.webapp, "_start_verify_job",
                               side_effect=self.webapp.VerifyGateBusy()):
            r = c.post("/api/my-accounts", json={
                "name": "验证竞态", "phone": VERIFY_PHONE, "password": "pw12345678",
                "phone_model": "", "phone_code": ""},
                headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        j = r.get_json()
        self.assertEqual(j.get("status"), "verify_deferred",
                         "任务没建成，响应必须如实报（此前无 job_id 也无状态说明=假成功）")
        self.assertNotIn("job_id", j)
        self.assertIn("未排上在线校验", j.get("msg", ""))
        row = next(a for a in db.load_accounts() if a["phone"] == VERIFY_PHONE)
        self.assertEqual(row["status"], "pending", "账号本身仍正常入库待审核")


if __name__ == "__main__":
    unittest.main()
