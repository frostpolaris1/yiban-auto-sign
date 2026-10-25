# -*- coding: utf-8 -*-
"""v25：accounts 补 `profile` / `profile_expose` 两列（工单 qmz4）。

覆盖四类：
1. 旧库 24→25 升级：保数据（造行、升级前后逐列对比）、补列默认空、版本与
   完成记录到位；
2. 全新库：整链跑完 user_version=25、两列在、SQL 层可读写；
3. 幂等：重入与直调都不再改 schema；
4. 产物登记：v25 在 `_ARTIFACTS` 在册（缺列拒启的行为由
   `test_migrations_schema_drift.ArtifactGateTest` 对 REQUIRED_ARTIFACTS 逐项验）。

库是**真的升到 v24**（跑 v1..v24 整链）——不手工拼半截 schema，免得夹具本身是
完整性校验会拒收的漂移库（v20 同法）。

标签：C · 存储：迁移与库完整性
对应实现：`yiban/adapters/persistence/migrations.py::migrate_v25` 与 `_ARTIFACTS`。
依赖：临时库文件；无网络、无 skip。
"""
import contextlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from typing import ClassVar
from unittest import mock

from yiban.adapters.config import account_crypto
from yiban.adapters.persistence import connection, migrations

V25_COLUMNS = ("profile", "profile_expose")

TEST_KEY = "3f9a2c7e5b1d8046a7c3e9f2b6d10485c2a7f4b9e1d63058fa4c7b2e9d15803a"


class MigrateV25Test(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="yiban-v25-")
        # v20 会读状态目录：指向本用例的空目录，不依赖机器上的 /var/log/yiban 形态
        self.state_dir = os.path.join(self.root, "state")
        os.makedirs(self.state_dir)
        self._prev_state_dir = os.environ.get("YIBAN_STATE_DIR")
        os.environ["YIBAN_STATE_DIR"] = self.state_dir
        self.db_path = os.path.join(self.root, "yiban.db")
        self.conn = self._open_at_v24()

    def tearDown(self):
        with contextlib.suppress(Exception):
            self.conn.close()
        if self._prev_state_dir is None:
            os.environ.pop("YIBAN_STATE_DIR", None)
        else:
            os.environ["YIBAN_STATE_DIR"] = self._prev_state_dir
        shutil.rmtree(self.root, ignore_errors=True)

    # ---- 夹具 ----
    def _open_at_v24(self):
        """整链跑到 v24 的现网形态（v1..v24 真跑，产物齐全、记录齐全）。"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        migrations._create_tables(conn)
        with mock.patch.object(migrations, "_MIGRATIONS",
                               [m for m in migrations._MIGRATIONS if m[0] <= 24]):
            migrations._run_migrations(conn)
        self.assertEqual(int(conn.execute("PRAGMA user_version").fetchone()[0]), 24)
        return conn

    def _seed_accounts(self):
        """造 3 行可对比的存量数据（含 24 版已有列的取值形态）。"""
        rows = (
            (1, "甲", "13800138001", "cipher-json-alpha", "MI 9", "code-alpha",
             "admin", "active", "rr-1", 0, "", "", 0),
            (2, "乙", "13800138002", "", "", "", "u@t.local", "pending", "", 0, "", "", 1),
            (3, "丙", "13800138003", "cipher-json-gamma", "iPhone15", "code-gamma",
             "admin", "rejected", "材料不全", 1, "2026-10-01T00:00:00", "admin", 0),
        )
        for r in rows:
            self.conn.execute(
                "INSERT INTO accounts (sort_order, name, phone, password, phone_model,"
                " phone_code, owner, status, reject_reason, deleted, deleted_at,"
                " deleted_by, user_paused) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", r)
        self.conn.commit()

    def _accounts_rows(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM accounts ORDER BY id").fetchall()]

    # ---- 1. 旧库升级保数据 ----
    def test_upgrade_24_to_25_preserves_rows_and_adds_empty_columns(self):
        self._seed_accounts()
        before = self._accounts_rows()
        self.assertEqual(len(before), 3)
        migrations._run_migrations(self.conn)
        self.assertEqual(int(self.conn.execute("PRAGMA user_version").fetchone()[0]),
                         migrations._MIGRATIONS[-1][0],
                         "整链必须跑到链尾（v25 之后还有档位也算对）")
        cols = {r["name"] for r in
                self.conn.execute("PRAGMA table_info(accounts)").fetchall()}
        for c in V25_COLUMNS:
            self.assertIn(c, cols, f"v25 必须补出 {c} 列")
        after = self._accounts_rows()
        self.assertEqual(len(after), 3, "升级不得增删行")
        for b, a in zip(before, after, strict=True):
            for k, v in b.items():
                self.assertEqual(a[k], v, f"存量列 {k} 的值不得被 v25 改动")
            for c in V25_COLUMNS:
                self.assertEqual(a[c], "", f"新列 {c} 对存量行默认空串")
        # 完成记录：v25 有记录且 applied_at 是真值（不是继承回填）
        rec = self.conn.execute(
            "SELECT name, applied_at FROM schema_migrations WHERE version=25"
        ).fetchone()
        self.assertIsNotNone(rec, "v25 必须写完成记录")
        self.assertEqual(rec["name"], "v25_account_profile")
        self.assertNotEqual(rec["applied_at"], "inherited")

    def test_upgrade_rerun_is_idempotent(self):
        self._seed_accounts()
        migrations._run_migrations(self.conn)
        rows_first = self._accounts_rows()
        migrations._run_migrations(self.conn)  # 版本已到位，整链跳过
        self.assertEqual(self._accounts_rows(), rows_first)
        # 直调迁移体（旧调用面）：_ensure_column 幂等，不再改 schema
        migrations.migrate_v25(self.conn)
        self.assertEqual(self._accounts_rows(), rows_first)

    # ---- 2. 全新库 ----
    def test_fresh_db_gets_v25_columns_and_they_round_trip(self):
        with contextlib.suppress(Exception):
            self.conn.close()
        fresh = os.path.join(self.root, "fresh.db")
        conn = sqlite3.connect(fresh)
        conn.row_factory = sqlite3.Row
        migrations._create_tables(conn)
        migrations._run_migrations(conn)
        self.assertEqual(int(conn.execute("PRAGMA user_version").fetchone()[0]),
                         migrations._MIGRATIONS[-1][0])
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
        for c in V25_COLUMNS:
            self.assertIn(c, cols)
        conn.execute(
            "INSERT INTO accounts (sort_order, name, phone, password, owner, status,"
            " profile, profile_expose) VALUES (1, '甲', '13800138001', '', 'admin',"
            " 'active', '{\"ct\": \"x\"}', '{\"student_id\": true}')")
        conn.commit()
        row = dict(conn.execute("SELECT profile, profile_expose FROM accounts"
                                " WHERE phone='13800138001'").fetchone())
        self.assertEqual(row["profile"], '{"ct": "x"}')
        self.assertEqual(row["profile_expose"], '{"student_id": true}')
        conn.close()

    # ---- 4. 产物登记 ----
    def test_v25_artifacts_are_registered(self):
        """v25 产物必须在册：缺列拒启由链尾完整性门执行（schema_drift 族逐项验）。"""
        self.assertEqual(migrations._ARTIFACTS.get(25),
                         (("accounts", "profile"), ("accounts", "profile_expose")))


class ProfileImportReplayTest(unittest.TestCase):
    """导出 → 干净导入回放：二段资料与暴露开关不得在逃生门上静默丢失（审查中-2）。

    两条导入形态都测：`db_export` 默认给密文（`load_accounts_raw`），`--plaintext`
    给明文（`load_accounts`，profile 是明文 JSON 串）。导入走 `_maybe_migrate`。
    """

    PHONE = "13800138002"
    CONTENT: ClassVar[dict[str, str]] = {
        "real_name": "张三", "student_id": "20250001",
        "college": "计算机学院", "class_name": "软件2401",
        "email": "zhangsan@example.edu",
    }

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="yiban-v25-import-")
        self.db_path = os.path.join(self.root, "yiban.db")
        self.env_file = os.path.join(self.root, ".env")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        self.accounts_json = os.path.join(self.root, "accounts.json")
        self._prev = {k: os.environ.get(k) for k in
                      ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_STATE_DIR")}
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = self.env_file
        os.environ["YIBAN_STATE_DIR"] = self.root
        connection.set_env_file(self.env_file)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        migrations._create_tables(conn)
        migrations._run_migrations(conn)
        self.conn = conn
        prev_conn = connection._conn
        connection.set_conn(conn)
        self.addCleanup(self._restore_conn, prev_conn)

    def _restore_conn(self, prev_conn):
        if prev_conn is None:
            connection.reset_conn()
        else:
            connection.set_conn(prev_conn)
        with contextlib.suppress(Exception):
            self.conn.close()
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.root, ignore_errors=True)

    def _write_json(self, rows):
        with open(self.accounts_json, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)

    def _encrypted_profile(self):
        key = account_crypto.load_key(self.env_file)
        plain = json.dumps(self.CONTENT, ensure_ascii=False)
        return json.dumps(account_crypto.encrypt_password(plain, key, self.PHONE))

    def _row(self):
        return dict(self.conn.execute(
            "SELECT profile, profile_expose FROM accounts WHERE phone=?",
            (self.PHONE,)).fetchone())

    def _decrypted_content(self):
        key = account_crypto.load_key(self.env_file)
        return json.loads(account_crypto.decrypt_password(
            json.loads(self._row()["profile"]), key, self.PHONE))

    def _base_row(self, **extra):
        row = {"name": "乙", "phone": self.PHONE, "password": "", "phone_model": "",
               "phone_code": "", "owner": "u@t.local", "status": "pending",
               "reject_reason": "", "deleted": False, "deleted_at": ""}
        row.update(extra)
        return row

    def test_ciphertext_export_replay_keeps_profile_and_expose(self):
        """密文导出（逃生门默认形态）：profile 原样透传，expose 原样保留。"""
        self._write_json([self._base_row(
            profile=self._encrypted_profile(),
            profile_expose=json.dumps({"student_id": True}))])
        migrations._maybe_migrate(self.conn, self.accounts_json)
        self.assertEqual(self._decrypted_content(), self.CONTENT)
        self.assertEqual(json.loads(self._row()["profile_expose"]), {"student_id": True})
        self.assertIn("ct", json.loads(self._row()["profile"]), "导入后必须仍是密文")

    def test_plaintext_export_replay_encrypts_profile(self):
        """`--plaintext` 导出：profile 是明文 JSON 串，导入必须加密落库。

        同批还钉住 `.bak` 逃生门：源文件含明文资料时，备份必须被重写成加密版，
        否则资料明文会以 `.bak` 形态长期驻留磁盘。
        """
        self._write_json([self._base_row(
            profile=json.dumps(self.CONTENT, ensure_ascii=False),
            profile_expose='{"student_id": true}')])
        migrations._maybe_migrate(self.conn, self.accounts_json)
        self.assertEqual(self._decrypted_content(), self.CONTENT)
        self.assertIn("ct", json.loads(self._row()["profile"]), "明文导入后必须加密")
        self.assertNotIn("20250001", self._row()["profile"], "资料明文不得落库")
        bak = [p for p in os.listdir(self.root)
               if p.startswith("accounts.json.bak")]
        self.assertTrue(bak, "迁移成功必须留下 .bak 逃生门")
        with open(os.path.join(self.root, bak[0]), encoding="utf-8") as f:
            bak_text = f.read()
        self.assertNotIn("20250001", bak_text, "备份里的资料明文必须被重写为加密版")
        self.assertIn("ct", bak_text)


if __name__ == "__main__":
    unittest.main()
