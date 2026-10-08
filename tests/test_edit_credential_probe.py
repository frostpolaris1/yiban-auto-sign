# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""编辑账号路径的异步凭据探针：改口令/识别码后后台复核，失败留痕但不动账号状态。

**问题（工单 yiban-auto-sign-ws9v）** 易班口令对本地口令策略豁免，理由是"正确校验
方式是真实登录探针，不是复杂度规则"。新增路径有探针，编辑路径（管理端
`api_account_update`、人端 `api_my_account_update`）既无探针也无策略——管理员/用户把
易班口令填错，系统静默接受，直到下次签到失败才暴露；账号处于用户暂停态时错误凭据
可无限期潜伏。

**修法** 两处编辑端点在口令或识别码实际变更时，接 `web/services/verify_queue.py` 的
**异步**探针（复用现成席位、每用户配额、`yiban.attempt.jobs` 真源与失败落库），不退回
同步闸门（同步会占住请求线程与外呼席位，而编辑已落盘、不能回滚）。探针失败**不改账号
可用状态语义**（不自动禁用、不回审翻转）。

标签：E · Web：认证/权限/API
覆盖：管理端编辑与人端编辑的探针触发（口令变更 / 识别码变更）、无凭据变更不触发、
     校验开关关闭时不触发、探针失败落终态与审计但账号状态不动（active/pending 两态）、
     探针成功落 done
对应实现：`web/routes/accounts_api.py` 的 api_account_update、
     `web/routes/my.py` 的 api_my_account_update、`web/services/verify_queue.py` 的
     start_edit_probe
关键断言：编辑口令为错值 → 200 且产生 status=rejected 的校验任务、账号 status 不变；
     编辑正确凭据 → 任务 status=done；只改名称 → 零任务（零误报）
承重突变（逐个实测过，摘掉即本套必红）：
     ① 摘掉编辑端点的探针接线（条件恒假）→ 该端点断言红：人端 2 红、管理端 3 红；
     ② 探针任务改绑真实账号 id（`start_job(..., None, ...)` 的 None 换成 1）→ 失败回调
        把账号翻成 rejected，"账号状态不变 / 待审仍在待审"3 条断言红。
依赖：纯本地 Flask test client + 临时 `.env`/SQLite；`signin.verify_account` 一律打桩，
     绝不联网、不访问真实易班接口；无需 node
用法（项目根目录）：
    python -m pytest tests/test_edit_credential_probe.py -v
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64

ADMIN_PASS = "TestPass1234!"

USER_PASS = "UserPass123!"

PHONE = "13800000000"

PHONE2 = "13900000001"

OWNER = "u1@test.local"

AUTH_FAIL_MSG = "账号验证未通过：登录失败（账号或密码错误）: 138****0000"


def _load_webapp():
    """独立名字加载 web/app.py（共用模块对象会读到别的测试的 .env 常量快照）。"""
    spec = importlib.util.spec_from_file_location(
        "webapp_editprobe", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webapp_editprobe"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return mod


class _ProbeBase(unittest.TestCase):
    """临时 .env/DB + 每用例全新 app。`verify_on` 控制 YIBAN_ACCOUNT_VERIFY。"""

    verify_on = True

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-editprobe-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_ACCOUNTS_FILE": cls.accounts_file,
            "YIBAN_USERS_FILE": cls.users_file,
            "YIBAN_DB_FILE": cls.db_file,
            "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_LOG_FILE": os.path.join(cls.tmp, "sign.log"),
            "YIBAN_DISABLE_PURGE_LOOP": "1",
        })
        cls.webapp = _load_webapp()

    @classmethod
    def tearDownClass(cls):
        import db
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_LOG_FILE", "YIBAN_DISABLE_PURGE_LOOP", "YIBAN_VERIFY_ASYNC"):
            os.environ.pop(k, None)

    def setUp(self):
        # 编辑探针一律走异步任务，不读 YIBAN_VERIFY_ASYNC（同步闸门会占住请求线程与
        # 外呼席位，而编辑已落盘不能回滚）。显式钉成 0，证明探针不依赖新增路径的开关。
        os.environ["YIBAN_VERIFY_ASYNC"] = "0"
        self._write_env()
        self._reset_db()
        self.webapp.migrate_admin_password_to_hash(self.env_file)

    def _write_env(self):
        lines = [
            f"YIBAN_ACCOUNTS_KEY={TEST_KEY}",
            "YIBAN_ADMIN_USER=admin",
            f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}",
        ]
        if self.verify_on:
            lines.append("YIBAN_ACCOUNT_VERIFY=1")
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def _reset_db(self):
        import db
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        with io.open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)
        db.create_user(OWNER, self.webapp.generate_password_hash(USER_PASS))

    def tearDown(self):
        mock.patch.stopall()

    # ---- 工具 ----
    def _client(self, username, password):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c, c.get("/api/me").get_json()["csrf_token"]

    def _admin_client(self):
        return self._client("admin", ADMIN_PASS)

    def _user_client(self):
        return self._client(OWNER, USER_PASS)

    def _seed_account(self, phone=PHONE, code="", status="active"):
        import db
        db.add_account({"name": "A", "phone": phone, "password": "pw-old",
                        "phone_model": "", "phone_code": code,
                        "owner": OWNER, "status": status})
        return db.load_accounts()[0]

    def _row(self, phone=PHONE):
        import db
        rows = [a for a in db.load_accounts() if a["phone"] == phone]
        self.assertEqual(len(rows), 1, f"应恰有一行 {phone}，实际 {len(rows)}")
        return rows[0]

    def _jobs(self):
        import db
        return db.get_conn().execute(
            "SELECT * FROM verify_jobs ORDER BY id").fetchall()

    def _wait_one_job(self, timeout=10.0):
        """轮询到恰有一条任务并落终态。**必须在 patch 存活期内调用**——否则后台
        线程拿到真实 `signin.verify_account` 会真的外呼易班。"""
        end = time.time() + timeout
        while time.time() < end:
            rows = self._jobs()
            if len(rows) == 1 and rows[0]["status"] in self.webapp.VERIFY_JOB_TERMINAL:
                return rows[0]
            time.sleep(0.05)
        self.fail(f"编辑探针任务未落终态：{self._jobs()}")

    def _audit_actions(self):
        import db
        return [r["action"] for r in db.get_conn().execute(
            "SELECT action FROM audit_logs ORDER BY id").fetchall()]


class AdminEditProbeTest(_ProbeBase):
    """管理端编辑账号：改口令走后台探针，失败留痕但不动账号状态。"""

    def test_改错口令产生失败记录且账号状态不变(self):
        self._seed_account(status="active")
        c, t = self._admin_client()
        with mock.patch.object(self.webapp.signin, "verify_account",
                               return_value=(False, AUTH_FAIL_MSG)):
            r = c.put("/api/accounts/0",
                      json={"name": "A", "phone": PHONE, "password": "222333",
                            "confirm_password": ADMIN_PASS},
                      headers={"X-CSRF-Token": t})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            job = self._wait_one_job()
        self.assertEqual(job["status"], "rejected", dict(job))
        self.assertIn("账号或密码错误", job["error"])
        self.assertIsNone(job["account_id"],
                          "探针不得绑定账号 id：绑定后失败回调会翻账号状态")
        row = self._row()
        self.assertEqual(row["status"], "active", "探针失败不得改动账号可用状态语义")
        self.assertEqual(row["reject_reason"], "", "探针失败不得写账号拒绝理由")
        self.assertEqual(row["password"], "222333", "凭据照常落库（编辑已成功）")
        self.assertIn("account_verify_job_fail", self._audit_actions(),
                      "失败必须留可查记录（审计）")

    def test_改对口令产生通过记录(self):
        self._seed_account(status="active")
        c, t = self._admin_client()
        with mock.patch.object(self.webapp.signin, "verify_account",
                               return_value=(True, "账号健康，可正常签到")):
            r = c.put("/api/accounts/0",
                      json={"name": "A", "phone": PHONE, "password": "GoodPw#2468",
                            "confirm_password": ADMIN_PASS},
                      headers={"X-CSRF-Token": t})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            job = self._wait_one_job()
        self.assertEqual(job["status"], "done", dict(job))
        self.assertEqual(job["error"], "")
        self.assertEqual(self._row()["status"], "active")

    def test_只改名称不触发探针(self):
        self._seed_account(status="active")
        c, t = self._admin_client()
        with mock.patch.object(self.webapp.signin, "verify_account") as va:
            r = c.put("/api/accounts/0",
                      json={"name": "改名后", "phone": PHONE},
                      headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._jobs(), [], "无凭据变更不得建探针任务（零误报）")
        va.assert_not_called()

    def test_只改识别码触发探针(self):
        self._seed_account(status="active", code="")
        c, t = self._admin_client()
        with mock.patch.object(self.webapp.signin, "verify_account",
                               return_value=(True, "ok")):
            r = c.put("/api/accounts/0",
                      json={"name": "A", "phone": PHONE, "phone_code": "code-new-1"},
                      headers={"X-CSRF-Token": t})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            job = self._wait_one_job()
        self.assertEqual(job["status"], "done", dict(job))


class UserEditProbeTest(_ProbeBase):
    """人端编辑自己账号：同一探针口径，同样不改账号可用状态语义。"""

    def test_用户改错口令产生失败记录且状态不变(self):
        self._seed_account(status="active")
        c, t = self._user_client()
        with mock.patch.object(self.webapp.signin, "verify_account",
                               return_value=(False, AUTH_FAIL_MSG)):
            r = c.put("/api/my-accounts/0",
                      json={"name": "A", "phone": PHONE, "password": "222333"},
                      headers={"X-CSRF-Token": t})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            job = self._wait_one_job()
        self.assertEqual(job["status"], "rejected", dict(job))
        row = self._row()
        self.assertEqual(row["status"], "active", "探针失败不得改动账号可用状态语义")
        self.assertEqual(row["reject_reason"], "")
        self.assertIn("account_verify_job_fail", self._audit_actions())

    def test_用户改错口令的待审账号仍留在待审(self):
        """被拒后重交（回 pending）的账号：探针失败不得把它再翻回 rejected。"""
        self._seed_account(status="rejected")
        c, t = self._user_client()
        with mock.patch.object(self.webapp.signin, "verify_account",
                               return_value=(False, AUTH_FAIL_MSG)):
            r = c.put("/api/my-accounts/0",
                      json={"name": "A", "phone": PHONE, "password": "Nope#2468"},
                      headers={"X-CSRF-Token": t})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            job = self._wait_one_job()
        self.assertEqual(job["status"], "rejected", dict(job))
        self.assertEqual(self._row()["status"], "pending",
                         "编辑回待审是既定语义，探针结果不得把它翻回 rejected")

    def test_只改名称不触发探针(self):
        self._seed_account(status="active")
        c, t = self._user_client()
        with mock.patch.object(self.webapp.signin, "verify_account") as va:
            r = c.put("/api/my-accounts/0",
                      json={"name": "改名后", "phone": PHONE},
                      headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._jobs(), [])
        va.assert_not_called()


class EditProbeSwitchOffTest(_ProbeBase):
    """校验开关关闭时与新增路径同口径：编辑不建探针任务（零行为变更）。"""

    verify_on = False

    def test_开关关闭时编辑不建任务(self):
        self._seed_account(status="active")
        c, t = self._admin_client()
        with mock.patch.object(self.webapp.signin, "verify_account") as va:
            r = c.put("/api/accounts/0",
                      json={"name": "A", "phone": PHONE, "password": "222333",
                            "confirm_password": ADMIN_PASS},
                      headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._jobs(), [], "开关关闭不得建任务")
        va.assert_not_called()
