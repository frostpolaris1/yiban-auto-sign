# -*- coding: utf-8 -*-
"""批次20 需求3 回归：设置页「账号容量」拆解（纯显示层，2026-09-10）。

背景：容量是「注册/持有名额制」（用户 500 / 账号 200），账号容量按**全部非删除账号**
统计，含用户自暂停与账密故障暂停（熔断/半开试探中）的账号 —— 会出现"停签/故障账号
占满名额、新账号被拒但实际负载并不高"。用户已拍板：**不改配额判定逻辑，只改显示**。

本文件锁住四条：
1. `/api/settings` 的 `capacity.accounts_breakdown` 三桶互斥且求和 = `capacity.accounts`；
2. 软删账号不计入任何桶；
3. cred-state.json 缺失/损坏时接口仍 200 且 cred_paused=0（不得 500）；
4. **配额判定行为不变**：`_accounts_at_capacity` 仍按"全部非删除账号"计（含暂停账号），
   显示层拆分不影响它。

用法（项目根目录）：
    py -m pytest tests/test_capacity_breakdown_0910.py -q
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

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
ADMIN_PASS = "MasterPass#2026"


def _load_webapp():
    spec = importlib.util.spec_from_file_location("webapp_capbd", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webapp_capbd"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return mod


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-capbd-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp  # cred-state.json 落在此
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

    @property
    def cred_state_file(self):
        return os.path.join(self.tmp, "cred-state.json")

    def setUp(self):
        if self.db._conn is not None:
            with contextlib.suppress(Exception):
                self.db._conn.close()
            self.db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        if os.path.exists(self.cred_state_file):
            os.remove(self.cred_state_file)
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        self.db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)

    # ---- 脚手架 ----
    def _add(self, phone, owner="admin", paused=False):
        self.db.add_account({
            "name": "N", "phone": phone, "password": "pw",
            "phone_model": "", "phone_code": "", "owner": owner,
            "status": "active", "reject_reason": "",
        })
        if paused:
            acc = next(a for a in self.db.load_accounts_raw() if a["phone"] == phone)
            self.db.set_user_paused(acc["id"], 1)

    def _write_cred_state(self, phones, raw=None):
        with io.open(self.cred_state_file, "w", encoding="utf-8") as f:
            f.write(raw if raw is not None else json.dumps(
                {p: {"fail_days": 3, "paused_since": "2026-09-10 06:00:00"} for p in phones}
            ))

    def _capacity(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin@test.local", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        rs = c.get("/api/settings")
        self.assertEqual(rs.status_code, 200, rs.get_data(as_text=True))
        return rs.get_json()["capacity"]


class CapacityBreakdownTest(_Base):
    def test_three_buckets_mutually_exclusive_and_sum(self):
        """2 正常 + 1 自暂停 + 1 账密故障暂停 + 1 两者都命中 → 优先级归自暂停。"""
        self._add("13900000001")                      # 正常
        self._add("13900000002")                      # 正常
        self._add("13900000003", paused=True)         # 用户自暂停
        self._add("13900000004")                      # 账密故障暂停（cred-state）
        self._add("13900000005", paused=True)         # 两者都命中 → 归自暂停
        self._write_cred_state(["13900000004", "13900000005"])

        cap = self._capacity()
        bd = cap["accounts_breakdown"]
        self.assertEqual(bd["normal"], 2, bd)
        self.assertEqual(bd["user_paused"], 2, bd)
        self.assertEqual(bd["cred_paused"], 1, bd)
        self.assertEqual(
            bd["normal"] + bd["user_paused"] + bd["cred_paused"], cap["accounts"],
            "三桶必须互斥且求和等于账号总数",
        )

    def test_soft_deleted_excluded(self):
        """软删账号不占名额、也不计入任何桶。"""
        self._add("13900000001")
        self._add("13900000002", paused=True)
        self._write_cred_state(["13900000003"])
        self._add("13900000003")
        cap0 = self._capacity()
        self.assertEqual(cap0["accounts"], 3)
        acc = next(a for a in self.db.load_accounts_raw() if a["phone"] == "13900000003")
        self.db.set_account_deleted(acc["id"], 1, "2026-09-10 06:00:00")
        cap1 = self._capacity()
        self.assertEqual(cap1["accounts"], 2)
        self.assertEqual(cap1["accounts_breakdown"]["cred_paused"], 0)
        self.assertEqual(
            sum(cap1["accounts_breakdown"].values()), cap1["accounts"],
            "软删后三桶求和仍须等于总数",
        )

    def test_broken_cred_state_does_not_break_settings(self):
        """cred-state.json 损坏/缺失时接口仍 200，cred_paused=0（不得 500）。"""
        self._add("13900000001")
        with io.open(self.cred_state_file, "w", encoding="utf-8") as f:
            f.write("{ 这不是合法 JSON")
        cap = self._capacity()
        self.assertEqual(cap["accounts_breakdown"]["cred_paused"], 0)
        self.assertIn("normal", cap["accounts_breakdown"])
        # 文件不存在同样安全（"无暂停 = 文件不存在"语义）
        os.remove(self.cred_state_file)
        cap2 = self._capacity()
        self.assertEqual(cap2["accounts_breakdown"]["cred_paused"], 0)

    def test_quota_judgement_unchanged(self):
        """配额判定不受显示层影响：暂停账号仍占额（拆解只是展示）。"""
        self._add("13900000001")
        self._add("13900000002", paused=True)
        self._write_cred_state(["13900000002"])
        self.webapp.write_env_key(self.env_file, "YIBAN_MAX_ACCOUNTS", "2")
        try:
            # 2 个非删除账号（含 1 个自暂停）已到上限 → 再新增 1 个应被拒
            self.assertTrue(
                self.webapp._accounts_at_capacity(1),
                "暂停账号必须仍占名额（配额判定口径未变）",
            )
            cap = self._capacity()
            self.assertEqual(cap["accounts"], 2)
            self.assertEqual(cap["accounts_max"], 2)
        finally:
            self.webapp.write_env_key(self.env_file, "YIBAN_MAX_ACCOUNTS", "")


if __name__ == "__main__":
    unittest.main()
