# -*- coding: utf-8 -*-
"""批次20 需求3 回归：设置页「账号容量」拆解（2026-09-10）。

背景：容量按**会发起易班请求的账号**统计（非删除且审核态已通过），含用户自暂停与
账密故障暂停（熔断/半开试探中）的账号 —— 会出现"停签/故障账号占满名额、新账号被拒
但实际负载并不高"。用户已拍板：**不改配额判定逻辑，只改显示**（指暂停类账号：
它们仍占名额，只把它们分类展示出来）。未通过审核（pending/rejected）的行不属"暂停类"
——它们永不签到，2026-09-15 起不计容量（见 `yiban.store.accounts.signs_in`）。

本文件锁住四条：
1. `/api/settings` 的 `capacity.accounts_breakdown` 三桶互斥且求和 = `capacity.accounts`；
2. 软删账号不计入任何桶；
3. cred-state.json 缺失/损坏时接口仍 200 且 cred_paused=0（不得 500）；
4. **配额判定与 accounts 同源**：`_accounts_at_capacity` 看的账号数与 `capacity.accounts`
   一致（含暂停账号），显示层拆分不影响它。

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
from unittest import mock

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

    def test_settings_stats_from_raw_snapshot_no_decrypt(self):
        """2026-09-14 性能回归：设置页统计改用不解密读取并去重。

        钉死两点：
        (a) 判定路径不触达解密（打桩为抛错，命中即 500）；A2 后 web 侧的解密入口是
            `db.decrypt_account_rows`，原始读入口是 `db.accounts_snapshot`；
        (b) accounts 原始读 / users 读各恰一次（去重），且三分类、owners、
            潜在负载、活跃计数都能用不解密原始行独立复算，口径不变。
        """
        self._add("13900000011")                         # 正常
        self._add("13900000012", paused=True)            # 用户自暂停
        self._add("13900000013")                         # 账密故障暂停
        self._add("13900000014", owner="u1@test.local")  # 有主 + 账密故障暂停
        self._write_cred_state(["13900000013", "13900000014"])
        self.db.create_user(email="u1@test.local", password_hash="x")
        self.db.create_user(email="u2@test.local", password_hash="x")  # 空用户 → 潜在负载

        app = self.webapp.create_app()
        c = app.test_client()
        r = c.post("/api/login", json={"username": "admin@test.local", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

        with mock.patch.object(
            self.db, "decrypt_account_rows",
            side_effect=AssertionError("设置页不应触发解密"),
        ) as m_dec, mock.patch.object(
            self.db, "accounts_snapshot", wraps=self.db.accounts_snapshot
        ) as m_raw, mock.patch.object(
            self.db, "load_users", wraps=self.db.load_users
        ) as m_users:
            rs = c.get("/api/settings")
        self.assertEqual(rs.status_code, 200, rs.get_data(as_text=True))
        m_dec.assert_not_called()
        self.assertEqual(m_raw.call_count, 1, "账号原始读须去重为单次")
        self.assertEqual(m_users.call_count, 1, "用户读取须去重为单次")

        data = rs.get_json()
        cap, bd = data["capacity"], data["capacity"]["accounts_breakdown"]
        live = [a for a in self.db.load_accounts_raw() if not a["deleted"]]
        cred = self.webapp._cred_paused_phones()
        exp_user = sum(1 for a in live if a.get("user_paused"))
        exp_cred = sum(
            1 for a in live
            if not a.get("user_paused") and str(a.get("phone", "")) in cred
        )
        self.assertEqual(
            bd,
            {"normal": len(live) - exp_user - exp_cred,
             "user_paused": exp_user, "cred_paused": exp_cred},
            "三分类口径：自暂停优先于账密故障",
        )
        self.assertEqual(cap["accounts"], len(live))
        self.assertEqual(sum(bd.values()), cap["accounts"], "三桶求和 = 计容量的账号数")
        owners = {a.get("owner") for a in live if a.get("owner")}
        self.assertEqual(
            data["capacity_estimate"]["potential_load"],
            sum(1 for u in self.db.load_users() if u["email"] not in owners),
        )
        # 计数/配额入口同样不得解密
        with mock.patch.object(
            self.db, "decrypt_account_rows",
            side_effect=AssertionError("计数不应触发解密"),
        ):
            self.assertEqual(self.webapp._capacity_account_count(), len(live))
            self.assertFalse(self.webapp._accounts_at_capacity(0))


if __name__ == "__main__":
    unittest.main()
