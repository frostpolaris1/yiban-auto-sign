# -*- coding: utf-8 -*-
"""账号容量口径：**未通过审核的账号不占容量**（2026-09-15 修订）。

缺陷背景（DAT-3）：容量原先按"全部非删除账号"统计，含 pending/rejected。这两类行
永不发起易班请求（引擎加载与运行期复核都按同一条件过滤），却：

1. 长期占满名额 → 新账号在提交时被「账号数量已达上限」误拒（管理员只能逐个手工删除）；
2. 在总览/设置页的容量拆解里被算成"正常"——而那三分类的**全部意义**就是解释
   "名额为什么满了而负载不高"，把永不签到的行混进"正常"正好把这层解释变成误导。

修订后的口径（判据唯一来源 `yiban.store.accounts.signs_in`）：
- 计容量 = 非删除 且 审核态已通过（user_paused / 账密故障暂停仍计入，三分类单独展示）；
- 审核通过是"让这一行开始产生负载"的动作 → `api_account_review` 的 approve 同样过闸门。

用法（项目根目录）：
    py -m pytest tests/test_capacity_audit_scope.py -q
"""
import contextlib
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
ADMIN_PASS = "MasterPass#2026"


def _load_webapp():
    spec = importlib.util.spec_from_file_location("webapp_capaudit", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webapp_capaudit"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return mod


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-capaudit-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
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
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        self.db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)

    # ---- 脚手架 ----
    def _add(self, phone, status="active", owner="admin"):
        return self.db.add_account({
            "name": "N", "phone": phone, "password": "pw",
            "phone_model": "", "phone_code": "", "owner": owner,
            "status": status, "reject_reason": "",
        })

    def _row(self, account_id):
        with self.db._conn_lock:
            return dict(self.db.get_conn().execute(
                "SELECT id, deleted, status FROM accounts WHERE id=?", (account_id,)
            ).fetchone())

    def _capacity(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin@test.local", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        rs = c.get("/api/settings")
        self.assertEqual(rs.status_code, 200, rs.get_data(as_text=True))
        return rs.get_json()["capacity"]

    def _client(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin@test.local", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c, c.get("/api/me").get_json()["csrf_token"]


# (status, deleted, 是否计容量)：审核态未通过的行永不签到
SIGNS_IN_CASES = (
    ("active", 0, True),
    ("pending", 0, False),
    ("rejected", 0, False),
    ("active", 1, False),      # 软删
    ("pending", 1, False),
    ("", 0, True),             # 旧档无 status 等于已通过审核
)


class SignsInSingleSourceTest(_Base):
    """`signs_in` 与 `is_signable` 必须对同一行给出一致判定（一套口径两处取用）。"""

    def test_predicate_matches_runtime_gate(self):
        for i, (status, deleted, expect) in enumerate(SIGNS_IN_CASES):
            with self.subTest(status=status, deleted=deleted):
                phone = f"1391000{i:04d}"
                aid = self._add(phone, status=status or "active")
                if status == "":
                    with self.db._conn_lock, self.db.get_conn() as conn:
                        conn.execute("UPDATE accounts SET status='' WHERE id=?", (aid,))
                if deleted:
                    self.db.set_account_deleted(aid, True, deleted_at="2026-09-15 06:00:00",
                                                deleted_by="admin")
                row = self._row(aid)
                self.assertEqual(self.db.account_signs_in(row), expect, row)
                self.assertEqual(self.db.account_is_signable(aid), expect, row)

    def test_missing_row_is_not_signable(self):
        self.assertFalse(self.db.account_is_signable(999999))
        # 非库内账号（JSON/环境变量模式）没有行，放行由调用方语义决定
        self.assertTrue(self.db.account_is_signable(0))


class CapacityExcludesAuditTest(_Base):
    """未通过审核的行：不计容量、不进"正常"桶、单独给计数。"""

    def test_pending_and_rejected_do_not_consume_capacity(self):
        live = self._add("13920000001")
        pending = self._add("13920000002", status="pending")
        rejected = self._add("13920000003", status="rejected")
        cap = self._capacity()
        self.assertEqual(cap["accounts"], 1, cap)
        self.assertEqual(cap["accounts_audit"], 2, cap)
        self.assertEqual(sum(cap["accounts_breakdown"].values()), cap["accounts"])
        self.assertEqual(cap["accounts_breakdown"]["normal"], 1,
                         "未通过审核的行不得算作「正常」")
        # 互补关系：计容量 + 未通过审核 = 全部非删除账号
        all_live = [a for a in self.db.load_accounts_raw() if not a["deleted"]]
        self.assertEqual(cap["accounts"] + cap["accounts_audit"], len(all_live))
        # 断言确实动了那两行（防夹具失效导致空断言）
        self.assertEqual(self._row(pending)["status"], "pending")
        self.assertEqual(self._row(rejected)["status"], "rejected")
        self.assertIsNotNone(live)

    def test_quota_ignores_audit_rows(self):
        """容量上限被"永不签到的存量"占满 → 不再误拒新账号。"""
        env_path = self.env_file
        with open(env_path, "a", encoding="utf-8") as f:
            f.write("YIBAN_MAX_ACCOUNTS=3\n")
        self._add("13930000001")
        self._add("13930000002", status="rejected")
        self._add("13930000003", status="rejected")
        # 计容量只有 1 个 → 还能再加 2 个（含本次新增）而不超上限
        self.assertFalse(self.webapp._accounts_at_capacity(2))
        self.assertTrue(self.webapp._accounts_at_capacity(3), "超过上限必须为 True")
        # 暂停类账号仍占容量（那是用户主动、一键可恢复的状态）
        paused = self._add("13930000004")
        self.db.set_user_paused(paused, 1)
        self.assertEqual(self.webapp._capacity_account_count(), 2)
        self.assertTrue(self.webapp._accounts_at_capacity(2))

    def test_estimate_counts_signing_accounts(self):
        """`capacity_estimate.current_accounts` 与 accounts 同口径（保存闸门要比它）。"""
        self._add("13940000001")
        self._add("13940000002", status="pending")
        c, _token = self._client()
        d = c.get("/api/settings").get_json()
        self.assertEqual(d["capacity_estimate"]["current_accounts"],
                         d["capacity"]["accounts"])
        self.assertEqual(d["capacity_estimate"]["current_accounts"], 1)


class ApproveGateTest(_Base):
    """审核通过 = 开始产生负载 → 必须过容量闸门（否则提交时受限、审批时无门）。"""

    def _approve(self, c, token, idx):
        return c.post(f"/api/accounts/{idx}/review", json={"action": "approve"},
                      headers={"X-CSRF-Token": token})

    def test_approve_refused_at_capacity(self):
        with open(self.env_file, "a", encoding="utf-8") as f:
            f.write("YIBAN_MAX_ACCOUNTS=2\n")
        self._add("13950000001")
        self._add("13950000002")
        self._add("13950000003", status="pending")
        c, token = self._client()
        idx = next(i for i, a in enumerate(self.db.load_accounts()) if a["phone"] == "13950000003")
        r = self._approve(c, token, idx)
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertIn("已达上限", r.get_json()["error"])
        self.assertEqual(self._row(
            next(a["id"] for a in self.db.load_accounts() if a["phone"] == "13950000003")
        )["status"], "pending", "被拒时状态不得变更")

    def test_approve_allowed_after_freeing_slot(self):
        with open(self.env_file, "a", encoding="utf-8") as f:
            f.write("YIBAN_MAX_ACCOUNTS=2\n")
        self._add("13960000001")
        victim = self._add("13960000002")
        self._add("13960000003", status="pending")
        # 让出一个名额（软删）→ 通过审核应当放行
        self.db.set_account_deleted(victim, True, deleted_at="2026-09-15 06:00:00",
                                    deleted_by="admin")
        c, token = self._client()
        idx = next(i for i, a in enumerate(self.db.load_accounts()) if a["phone"] == "13960000003")
        r = self._approve(c, token, idx)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._row(
            next(a["id"] for a in self.db.load_accounts() if a["phone"] == "13960000003")
        )["status"], "active")

    def test_reject_never_gated(self):
        """拒绝不需要名额（它是"减少负载"的方向）。"""
        with open(self.env_file, "a", encoding="utf-8") as f:
            f.write("YIBAN_MAX_ACCOUNTS=1\n")
        self._add("13970000001")
        self._add("13970000002", status="pending")
        c, token = self._client()
        idx = next(i for i, a in enumerate(self.db.load_accounts()) if a["phone"] == "13970000002")
        r = c.post(f"/api/accounts/{idx}/review", json={"action": "reject", "reason": "照片不清"},
                   headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_unlimited_quota_never_gates(self):
        """上限 0 = 不限 → 审核通过不受影响。"""
        self._add("13980000001")
        self._add("13980000002", status="pending")
        c, token = self._client()
        idx = next(i for i, a in enumerate(self.db.load_accounts()) if a["phone"] == "13980000002")
        self.assertEqual(self._approve(c, token, idx).status_code, 200)


class FrontendConsumesAuditFieldTest(_Base):
    """前端必须消费 accounts_audit（否则"账号管理里有很多行、容量只算 N 个"无法解释）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with open(os.path.join(BASE, "web", "static", "js", "pages", "data_dashboard.js"),
                  encoding="utf-8") as f:
            cls.js = f.read()

    def test_dashboard_reads_audit_count(self):
        self.assertIn("accounts_audit", self.js)
        # 账号容量行与 KPI 副文案都要带上（两处展示点，只改一处会让口径自相矛盾）
        self.assertGreaterEqual(self.js.count("accounts_audit"), 2, self.js.count("accounts_audit"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
