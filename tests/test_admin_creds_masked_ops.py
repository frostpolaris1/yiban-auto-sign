# -*- coding: utf-8 -*-
"""管理员凭据门禁、账号 idx 防错位与 my-accounts 出站脱敏的回归。

分条说明：
1. 空凭据/部分配置管理员登录拒绝（`verify_admin` 曾对空配置直通）
2. idx 寻址防错位校验：单账号 mutation 携带 phone 不匹配 → 409（列表漂移会错位操作）
3. 批量接口 phones 对齐校验 + bool 索引混淆修复
4. /api/my-accounts 日志出站脱敏（与 /api/my-logs 同口径）

全程 mock / 纯本地（Flask test client），无任何网络请求。
用法（项目根目录）：
    python -m pytest tests/test_admin_creds_masked_ops.py -v

标签：G · 安全：脱敏/审计/配置注入
覆盖：登录侧空/半配置凭据（内置管理员）、账号 mutation 的 idx↔phone 对齐（单条与批量）、
`true` 被当成索引的混淆、my-accounts 响应体里日志行的出站脱敏。
对应实现：`web/app.py` 的 `verify_admin` / 账号单条与批量路由 / `api_my_accounts`，
出站脱敏走展示层 `_mask_log_phones`（与 `/api/my_logs` 同一函数）。
关键断言：409 之外还必须断"库里那行没被动过"——错位操作即使回错状态码也可能已写；
出站脱敏只喂了**方括号形态**的号（`[13800138000]`），裸号形态的出站面不在本用例覆盖内
（那是 `test_logs_export_masking.py` 与 `test_log_masking_formatter.py` 的口径）。
依赖：Flask test client + 临时库/临时日志目录，无网络；内置管理员口令哈希经
`_write_env` 现写现恢复，改的是临时 `.env` 而非仓库那份。
"""
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import timedelta

from werkzeug.security import generate_password_hash

from yiban.core import clock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "UserPass123!"


class AdversarialFixes0820Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-adv-fix-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        cls.log_dir = os.path.join(cls.tmp, "logs")
        os.makedirs(cls.log_dir, exist_ok=True)
        cls._write_env(admin_user="admin", admin_pass=ADMIN_PASS)
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = cls.users_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        global db
        import db
        spec = importlib.util.spec_from_file_location("webapp", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR"):
            os.environ.pop(k, None)

    # ---- 环境辅助 ----
    @classmethod
    def _write_env(cls, admin_user=None, admin_pass=None):
        """按需写 .env：admin_user/admin_pass 任一为 None 即不写该行（构造部分配置场景）。"""
        lines = [f"YIBAN_ACCOUNTS_KEY={TEST_KEY}",
                 # 本类钉的是"口令正确时的防错位 409 / 脱敏回显"，固定在 full——
                 # 默认档 risk 下 purge 会先要倒计时确认，把 409 抢先成 delay_ack_required
                 "YIBAN_PW_GATE=full"]
        if admin_user is not None:
            lines.append(f"YIBAN_ADMIN_USER={admin_user}")
        if admin_pass is not None:
            lines.append(f"YIBAN_ADMIN_PASSWORD={admin_pass}")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def _reset_db(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        with open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)

    def _client(self):
        return self.webapp.create_app().test_client()

    def _login_admin(self, c):
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _add_account(self, phone, name="A"):
        return db.add_account({"name": name, "phone": phone, "password": "p1",
                               "status": "active"})

    # ---- 1. 空凭据 / 部分配置管理员登录（P1 修复）----
    def test_fully_unconfigured_admin_empty_creds_rejected(self):
        """完全未配置管理员：空用户名+空口令不得通过 verify_admin。"""
        self._reset_db()
        self._write_env(admin_user=None, admin_pass=None)
        try:
            c = self._client()
            r = c.post("/api/login", json={"username": "", "password": ""})
            self.assertEqual(r.status_code, 401, r.get_data(as_text=True))
            r = c.post("/api/login", json={"username": "admin", "password": ""})
            self.assertEqual(r.status_code, 401, r.get_data(as_text=True))
        finally:
            self._write_env(admin_user="admin", admin_pass=ADMIN_PASS)

    def test_partial_config_admin_empty_password_rejected(self):
        """只配了用户名没配密码：空口令不得登录（原实现 compare_digest(b'',b'') 直通）。"""
        self._reset_db()
        self._write_env(admin_user="admin", admin_pass=None)
        try:
            c = self._client()
            r = c.post("/api/login", json={"username": "admin", "password": ""})
            self.assertEqual(r.status_code, 401, r.get_data(as_text=True))
            self.assertNotIn("role", r.get_json())
        finally:
            self._write_env(admin_user="admin", admin_pass=ADMIN_PASS)

    def test_configured_admin_login_unaffected(self):
        """正常完整配置：正确口令仍可登录（修复不改变正常路径）。"""
        self._reset_db()
        c = self._client()
        token = self._login_admin(c)
        self.assertTrue(token)

    # ---- 2. idx 防错位校验（P1 修复）----
    def test_delete_with_mismatched_phone_rejected_409(self):
        self._reset_db()
        self._add_account("13800138000")
        self._add_account("13900139000")
        c = self._client()
        token = self._login_admin(c)
        # phone 与服务端 idx 解析结果不一致 → 409，且未删除任何账号
        r = c.delete("/api/accounts/0", json={"phone": "13900139000"},
                     headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertEqual(len(db.load_accounts()), 2, "409 后不应有账号被删除")
        # phone 匹配 → 正常软删除
        r = c.delete("/api/accounts/0", json={"phone": "13800138000"},
                     headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_delete_with_masked_phone_from_list_works(self):
        """模拟真实浏览器路径：/api/accounts 出站为脱敏号，前端原样回传——
        守卫必须双侧 _mask_phone 归一，否则管理端六个操作恒 409（回归审查发现）。"""
        self._reset_db()
        self._add_account("13800138000")
        self._add_account("13900139000")
        c = self._client()
        token = self._login_admin(c)
        listing = c.get("/api/accounts").get_json()["accounts"]
        masked = listing[0]["phone"]
        self.assertIn("*", masked, "列表出站应为脱敏号（前提校验）")
        # 用脱敏号（浏览器真实行为）删除 → 应成功而非 409
        r = c.delete("/api/accounts/0", json={"phone": masked},
                     headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 用 A 的脱敏号去删 idx1（B 账号）→ 错配应被拦
        r = c.delete("/api/accounts/1", json={"phone": masked},
                     headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 409, "伪造/错配的脱敏号应 409")
        self.assertFalse(db.load_accounts()[1].get("deleted"), "409 后 B 不应被删除")

    def test_batch_with_masked_phones_works(self):
        """批量接口同样按脱敏号归一比对（浏览器路径）。"""
        self._reset_db()
        self._add_account("13800138000")
        self._add_account("13900139000")
        c = self._client()
        token = self._login_admin(c)
        listing = c.get("/api/accounts").get_json()["accounts"]
        phones = [a["phone"] for a in listing]
        r = c.post("/api/accounts/batch",
                   json={"action": "delete", "ids": [0, 1], "phones": phones},
                   headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(all(a.get("deleted") for a in db.load_accounts()))

    def test_purge_with_mismatched_phone_rejected_409(self):
        self._reset_db()
        self._add_account("13800138000")
        id2 = self._add_account("13900139000")
        db.set_account_deleted(id2, 1, clock.ts())
        c = self._client()
        token = self._login_admin(c)
        # idx1 是已软删的 139 账号；携带错误 phone 应 409（防漂移后误删他人）
        # purge 先过二次鉴权，故补 confirm_password——本用例要钉的
        # 仍是"口令正确时的防错位 409"，断言与尝试次序均未放宽
        r = c.post("/api/accounts/1/purge",
                   json={"phone": "13800138000", "confirm_password": ADMIN_PASS},
                   headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertEqual(len(db.load_accounts()), 2, "409 后不应有账号被物理删除")

    # ---- 4. 自助面下标：本人视图 vs 全局（R3-023）----
    def test_my_accounts_exposes_global_admin_index(self):
        """`index` 是本人视图下标，`admin_index` 是全局下标；detail 只认后者。"""
        self._reset_db()
        db.add_account({"name": "别人", "phone": "13800138000", "password": "p1",
                        "status": "active", "owner": "u1@test.local"})
        db.add_account({"name": "管理员的", "phone": "13900139000", "password": "p2",
                        "status": "active", "owner": "admin"})
        c = self._client()
        self._login_admin(c)
        r = c.get("/api/my-accounts")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        mine = r.get_json()["accounts"]
        self.assertEqual(len(mine), 1, "管理员只应看到自己名下账号")
        self.assertEqual(mine[0]["index"], 0, "本人视图下标从 0 起")
        self.assertEqual(mine[0]["admin_index"], 1, "全局下标必须是真实位置")
        self.assertEqual(db.load_accounts()[mine[0]["admin_index"]]["phone"], "13900139000")

    # ---- 5. purge 事务与可选表（R3-005 / R3-012）----
    def _soft_deleted_account(self, phone="13800138000"):
        acc_id = self._add_account(phone)
        db.set_account_deleted(acc_id, 1, clock.ts())
        return acc_id

    def test_purge_writes_audit_in_same_transaction(self):
        """审计写失败：删除必须回滚，账号仍在，且不留审计行。"""
        from unittest import mock
        self._reset_db()
        self._soft_deleted_account()
        c = self._client()
        token = self._login_admin(c)
        with mock.patch.object(db, "record_in_txn", side_effect=RuntimeError("audit down")):
            try:
                r = c.post("/api/accounts/0/purge",
                           json={"phone": "13800138000", "confirm_password": ADMIN_PASS},
                           headers={"X-CSRF-Token": token})
            except RuntimeError:
                pass  # 测试客户端默认向上抛；两种形态都要求非 2xx
            else:
                self.assertGreaterEqual(r.status_code, 500, r.get_data(as_text=True))
        self.assertEqual(len(db.load_accounts()), 1, "审计失败必须回滚物理删除")
        rows = db.get_conn().execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action='account_purge'").fetchone()[0]
        self.assertEqual(rows, 0)

    def test_purge_success_writes_audit_and_clears_state_files(self):
        """正常路径：删除、审计、状态文件手机号清理三者一致（R3-005 / R3-006）。"""
        self._reset_db()
        self._soft_deleted_account()
        state_path = os.path.join(self.tmp, f"sign-state-{clock.today()}.json")
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({"13800138000": {"status": "success", "message": "13800138000"}}, f)
        cred_path = os.path.join(self.tmp, "cred-state.json")
        with open(cred_path, "w", encoding="utf-8") as f:
            json.dump({"13800138000": {"fail_days": 1}}, f)
        # 保留期是 log 档 365 天：200 天前的文件也必须清（审查 D-1）。
        old_day = (clock.now() - timedelta(days=200)).strftime("%Y-%m-%d")
        old_state = os.path.join(self.tmp, f"sign-state-{old_day}.json")
        old_daily = os.path.join(self.tmp, f"sign-daily-{old_day}.json")
        for p in (old_state, old_daily):
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"13800138000": "success"}, f)
        c = self._client()
        token = self._login_admin(c)
        r = c.post("/api/accounts/0/purge",
                   json={"phone": "13800138000", "confirm_password": ADMIN_PASS},
                   headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(db.load_accounts(), [], "账号必须被物理删除")
        rows = db.get_conn().execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action='account_purge'").fetchone()[0]
        self.assertEqual(rows, 1)
        for p in (state_path, cred_path, old_state, old_daily):
            text = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
            self.assertNotIn("13800138000", text, f"{os.path.basename(p)} 仍残留手机号")

    def test_purge_tolerates_missing_optional_tables(self):
        """v8/v15 可选迁移未落地：删号不得因缺表 500（R3-012）。"""
        self._reset_db()
        self._soft_deleted_account()
        conn = db.get_conn()
        conn.execute("DROP TABLE session_cache")
        conn.execute("DROP TABLE verify_jobs")
        conn.commit()
        c = self._client()
        token = self._login_admin(c)
        r = c.post("/api/accounts/0/purge",
                   json={"phone": "13800138000", "confirm_password": ADMIN_PASS},
                   headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(db.load_accounts(), [])

    def test_expiry_cleanup_clears_state_files(self):
        """软删到期自动清除也必须清状态文件（R3-006 同族）。"""
        self._reset_db()
        acc_id = self._add_account("13800138000")
        db.set_account_deleted(acc_id, 1, "2020-01-01 00:00:00")
        state_path = os.path.join(self.tmp, f"sign-state-{clock.today()}.json")
        daily_path = os.path.join(self.tmp, f"sign-daily-{clock.today()}.json")
        for p in (state_path, daily_path):
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"13800138000": "success"}, f)
        db.purge_expired_deleted_accounts()
        self.assertEqual(db.load_accounts(), [], "超期软删账号必须被物理清除")
        for p in (state_path, daily_path):
            text = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
            self.assertNotIn("13800138000", text,
                             f"到期清除后 {os.path.basename(p)} 仍残留手机号")

    def test_user_delete_and_batch_delete_clear_state_files(self):
        """管理员删用户 / 批量删用户也要清状态文件（R3-006 同族 D-10）。"""
        for mode in ("single", "batch"):
            with self.subTest(mode=mode):
                self._reset_db()
                db.create_user("u1@test.local", generate_password_hash(USER_PASS),
                               role="user")
                db.add_account({"name": "U", "phone": "13800138000", "password": "p1",
                                "status": "active", "owner": "u1@test.local"})
                state_path = os.path.join(self.tmp, f"sign-state-{clock.today()}.json")
                daily_path = os.path.join(self.tmp, f"sign-daily-{clock.today()}.json")
                for p in (state_path, daily_path):
                    with open(p, "w", encoding="utf-8") as f:
                        json.dump({"13800138000": "success"}, f)
                if mode == "single":
                    db.delete_user_with_accounts("u1@test.local")
                else:
                    db.batch_user_ops([("delete_user_with_accounts", "u1@test.local")])
                self.assertIsNone(db.find_user("u1@test.local"))
                for p in (state_path, daily_path):
                    text = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
                    self.assertNotIn("13800138000", text,
                                     f"{mode} 删用户后 {os.path.basename(p)} 仍残留手机号")

    def test_batch_purge_audit_failure_rolls_back(self):
        """批量彻底删除的审计与 ops 同事务：审计失败整体回滚（R3-005）。"""
        from unittest import mock
        self._reset_db()
        self._soft_deleted_account()
        c = self._client()
        token = self._login_admin(c)
        with mock.patch.object(db, "record_in_txn", side_effect=RuntimeError("audit down")):
            r = c.post("/api/accounts/batch",
                       json={"action": "purge", "ids": [0],
                             "phones": ["13800138000"],
                             "confirm_password": ADMIN_PASS},
                       headers={"X-CSRF-Token": token})
        self.assertGreaterEqual(r.status_code, 500, r.get_data(as_text=True))
        self.assertEqual(len(db.load_accounts()), 1, "审计失败必须回滚批量物理删除")

    def test_self_delete_audit_failure_rolls_back(self):
        """自助注销的确认审计与软删同事务：审计失败必须回滚（R3-005 同族）。"""
        from unittest import mock
        self._reset_db()
        db.create_user("u1@test.local", generate_password_hash(USER_PASS), role="user")
        c = self._client()
        r = c.post("/api/login", json={"username": "u1@test.local",
                                       "password": USER_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        token = c.get("/api/me").get_json()["csrf_token"]
        with mock.patch.object(db, "record_in_txn", side_effect=RuntimeError("audit down")):
            try:
                r = c.post("/api/me/delete", json={"password": USER_PASS},
                           headers={"X-CSRF-Token": token})
            except RuntimeError:
                pass
            else:
                self.assertGreaterEqual(r.status_code, 500, r.get_data(as_text=True))
        u = db.find_user("u1@test.local")
        self.assertIsNotNone(u, "审计失败不得删除用户行")
        self.assertFalse(u.get("deleted"), "审计失败必须回滚软注销")

    def test_core_table_missing_fails_loudly(self):
        """核心表缺表必须响亮失败，不能被可选表守卫掩盖（R3-012 边界）。"""
        self._reset_db()
        self._soft_deleted_account()
        conn = db.get_conn()
        conn.execute("DROP TABLE time_prefs")
        conn.commit()
        c = self._client()
        token = self._login_admin(c)
        try:
            r = c.post("/api/accounts/0/purge",
                       json={"phone": "13800138000", "confirm_password": ADMIN_PASS},
                       headers={"X-CSRF-Token": token})
        except Exception:
            pass  # 测试客户端可能上抛；关键是删除必须回滚
        else:
            self.assertGreaterEqual(r.status_code, 500, r.get_data(as_text=True))
        self.assertEqual(len(db.load_accounts()), 1,
                         "核心表缺表时删号必须回滚，不得静默成功")

    def test_review_with_mismatched_phone_rejected_409(self):
        self._reset_db()
        db.add_account({"name": "P", "phone": "13800138000", "password": "p1",
                        "status": "pending", "owner": "u1@test.local"})
        db.add_account({"name": "Q", "phone": "13900139000", "password": "p2",
                        "status": "pending", "owner": "u2@test.local"})
        c = self._client()
        token = self._login_admin(c)
        r = c.post("/api/accounts/0/review",
                   json={"action": "approve", "phone": "13900139000"},
                   headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        accs = {a["phone"]: a["status"] for a in db.load_accounts()}
        self.assertEqual(accs["13800138000"], "pending", "409 后审核状态不应变化")

    def test_batch_phones_mismatch_rejected_409(self):
        self._reset_db()
        self._add_account("13800138000")
        self._add_account("13900139000")
        c = self._client()
        token = self._login_admin(c)
        r = c.post("/api/accounts/batch",
                   json={"action": "delete", "ids": [0], "phones": ["13900139000"]},
                   headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertTrue(all(not a.get("deleted") for a in db.load_accounts()))

    def test_batch_bool_id_not_treated_as_index(self):
        """JSON true 此前因 isinstance(True, int) 被当作索引 1（bool 混淆修复）。"""
        self._reset_db()
        self._add_account("13800138000")
        self._add_account("13900139000")
        c = self._client()
        token = self._login_admin(c)
        r = c.post("/api/accounts/batch",
                   json={"action": "delete", "ids": [True]},
                   headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 404, r.get_data(as_text=True))
        self.assertTrue(all(not a.get("deleted") for a in db.load_accounts()),
                        "true 不应再作用于索引 1")

    # ---- 3. my-accounts 日志出站脱敏（P3 修复）----
    def test_my_accounts_logs_masked(self):
        self._reset_db()
        h = generate_password_hash
        db.create_user("u1@test.local", h(USER_PASS), role="user")
        db.add_account({"name": "Mine", "phone": "13800138000", "password": "p1",
                        "status": "active", "owner": "u1@test.local"})
        # 构造今日日志行（含完整手机号）
        today = clock.today()
        log_path = os.path.join(self.log_dir, f"sign-{today}.log")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"[{today} 07:10:00] [INFO] yiban: [13800138000] ✅ 签到成功\n")
        self.webapp.LOG_FILE = os.path.join(self.log_dir, "sign.log")
        try:
            c = self._client()
            r = c.post("/api/login", json={"username": "u1@test.local", "password": USER_PASS})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            r = c.get("/api/my-accounts")
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            accounts = r.get_json()["accounts"]
            self.assertEqual(len(accounts), 1)
            logs = accounts[0].get("logs", [])
            self.assertTrue(logs, "应能读到今日日志")
            joined = "\n".join(logs)
            self.assertNotIn("13800138000", joined, "出站日志不应含完整手机号")
            self.assertIn("138****8000", joined)
        finally:
            self.webapp.LOG_FILE = self.webapp.LOG_DEFAULT


if __name__ == "__main__":
    unittest.main()
