# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""P8-A 判据：旧 SQLite → JSONL 导出器（派单 2026-10-27，契约与 P8-B 导入器钉死）。

模拟的真实用户动作：运维在迁移窗口对旧库快照跑一次
`python scripts/db_export.py --db 快照 --out 目录`。
断言的用户可见结果：一个目录五个 .jsonl、对账表可独立复算、
明文口令/验证码零泄漏、重复导出逐字节一致、源库零改动。

夹具全合成值：手机号取 tests/synthetic-fixtures.json 登记表；
信封十六进制用全零/重复段；口令明文用 `plaintext-fixture-0000`（保留段形态）。
users 的重复形状＝同 email 一活一软删（idx_users_email_live 只约束活跃行，
这是旧库里真实可存在的重复形态）。

形状考古（2026-10-27，ad5b3639）：旧库无 schools 表、accounts 无学校引用列
（yiban/adapters/persistence/migrations.py 基线建表）。
故本文件孤儿形状取「time_prefs.phone 无对应账号」——旧库中真实存在的孤儿形状；
schools.jsonl 恒为空文件，孤儿计数只按此口径。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(BASE, "scripts", "db_export.py")

# 登记过的合成手机号（tests/synthetic-fixtures.json）
PHONE_A = "13800000001"   # 正常账号，带自选时间片
PHONE_B = "13800000002"   # 软删账号
PHONE_C = "13800000003"   # 明文驻留账号
PHONE_ORPHAN = "13899999999"  # time_prefs 孤儿：无对应账号

# 合成信封（v2 形状：v/kid/nonce/ct/tag 全十六进制，值全零/重复段）
ENV_A = {"v": 2, "kid": "0" * 16, "nonce": "00" * 12, "ct": "ab" * 10, "tag": "cd" * 16}
ENV_B = {"v": 2, "kid": "0" * 16, "nonce": "11" * 12, "ct": "ef" * 10, "tag": "fe" * 16}
ENV_CODE = {"v": 2, "kid": "0" * 16, "nonce": "22" * 12, "ct": "12" * 10, "tag": "34" * 16}

# 明文驻留值（只在源库，绝不允许出现在导出物）
PLAIN_PASSWORD = "plaintext-fixture-0000"
PLAIN_CODE = "code-fixture-0000"

FILE_NAMES = ("users.jsonl", "schools.jsonl", "accounts.jsonl",
              "time_prefs.jsonl", "audit.jsonl")
ALL_OUTPUTS = (*FILE_NAMES, "reconciliation.jsonl", "summary.json")

ROW_COUNTS = {"users": 3, "schools": 0, "accounts": 3, "time_prefs": 2, "audit": 2}


def _jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f.read().splitlines()]


def _mask(phone):
    """掩码规则的测试侧独立实现（与实现不共享代码）。"""
    return phone[:3] + "****" + phone[7:]


def _recon_hash(table, key, row):
    """对账哈希的测试侧独立实现：掩码手机号 → 规范串 → sha256。

    序列化口径：字符串原样；其他类型 sort_keys 紧凑 JSON（与实现同一定义，
    独立重写——dict 的 repr 依赖键序，不许用）。
    """
    import hashlib
    masked = {k: (_mask(v) if k == "phone" else v) for k, v in row.items()}
    body = "\x1e".join(
        f"{k}={v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, sort_keys=True)}"
        for k, v in sorted(masked.items()))
    canonical = f"{table}\x1f{key}\x1f{body}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


#: 夹具构建脚本：子进程里 init_db 建真 schema 并插全合成行。
#: argv：db, env, phone_a, phone_b, phone_c, plain_pw, plain_code, phone_orphan,
#:       env_a_json, env_b_json, env_code_json
_BUILD_CODE = r'''
import sys, json
from yiban.adapters.persistence import db
conn = db.init_db(db_file=sys.argv[1], env_file=sys.argv[2],
                  cleanup=False, migrate=True)
c = conn
pa, pb, pc, ppw, pcode, po = sys.argv[3:9]
env_a = json.loads(sys.argv[9])
env_b = json.loads(sys.argv[10])
env_code = json.loads(sys.argv[11])
c.execute("INSERT INTO accounts (sort_order, name, phone, password, phone_code,"
          " owner, status) VALUES (0,?,?,?,?,?,?)",
          ("acct-a", pa, json.dumps(env_a), "", "admin", "active"))
c.execute("INSERT INTO accounts (sort_order, name, phone, password, phone_code,"
          " owner, status, deleted, deleted_at, deleted_by)"
          " VALUES (1,?,?,?,?,?,?,1,?,?)",
          ("acct-b", pb, json.dumps(env_b), json.dumps(env_code),
           "admin", "active", "2026-01-02T00:00:00", "admin"))
c.execute("INSERT INTO accounts (sort_order, name, phone, password, phone_code,"
          " owner, status) VALUES (2,?,?,?,?,?,?)",
          ("acct-c", pc, ppw, pcode, "admin", "active"))
c.execute("INSERT INTO users (email, password_hash, role, created_at)"
          " VALUES (?,?,?,?)",
          ("user0000@example.invalid", "scrypt-fixture-0000", "admin",
           "2026-01-01T00:00:00"))
c.execute("INSERT INTO users (email, password_hash, role, created_at, deleted,"
          " deleted_at) VALUES (?,?,?,?,1,?)",
          ("user0000@example.invalid", "scrypt-fixture-0001", "user",
           "2026-01-01T00:00:00", "2026-01-02T00:00:00"))
c.execute("INSERT INTO users (email, password_hash, role, created_at, deleted,"
          " deleted_at) VALUES (?,?,?,?,1,?)",
          ("user0003@example.invalid", "scrypt-fixture-0002", "user",
           "2026-01-01T00:00:00", "2026-01-03T00:00:00"))
c.execute("INSERT INTO time_prefs (phone, slot_min, updated_at) VALUES (?,30,?)",
          (pa, "2026-01-01T00:00:00"))
c.execute("INSERT INTO time_prefs (phone, slot_min, updated_at) VALUES (?,45,?)",
          (po, "2026-01-01T00:00:00"))
c.execute("INSERT INTO audit_logs (ts, username, action, target, detail)"
          " VALUES (?,?,'export-fixture',?,?)",
          ("2026-01-01T00:00:00", "ops", "fixture-hash", "fixture"))
c.execute("INSERT INTO audit_logs (ts, username, action, target, detail)"
          " VALUES (?,?,'time_pref_set',?,?)",
          ("2026-01-01T00:01:00", "ops", "fixture-hash", "fixture"))
c.commit()
c.close()
'''


class ExportFixtureMixin:
    """一次性构建真 schema 夹具库（子进程 init_db，不污染本进程连接单例）。"""

    @classmethod
    def _build_db(cls, root):
        env = {k: v for k, v in os.environ.items() if not k.startswith("YIBAN_")}
        env.update({
            "YIBAN_ENV_FILE": os.path.join(root, ".env"),
            "YIBAN_DB_FILE": os.path.join(root, "yiban.db"),
            "YIBAN_STATE_DIR": os.path.join(root, "state"),
            "YIBAN_LOG_FILE": os.path.join(root, "state", "sign.log"),
            "PYTHONPATH": BASE,
            "PYTHONIOENCODING": "utf-8",
        })
        with open(os.path.join(root, ".env"), "w", encoding="utf-8") as f:
            f.write("YIBAN_ACCOUNTS_KEY=" + "a" * 64 + "\n")
        argv = [os.path.join(root, "yiban.db"), os.path.join(root, ".env"),
                PHONE_A, PHONE_B, PHONE_C, PLAIN_PASSWORD, PLAIN_CODE,
                PHONE_ORPHAN, json.dumps(ENV_A), json.dumps(ENV_B),
                json.dumps(ENV_CODE)]
        r = subprocess.run([sys.executable, "-c", _BUILD_CODE, *argv], cwd=BASE,
                           env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        assert r.returncode == 0, r.stderr[-800:]
        return env


class DbExportJsonlTest(ExportFixtureMixin, unittest.TestCase):
    """判据 1–6：五文件、零明文、信封透传、幂等、对账可复算、汇总形状。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="yiban-p8a-")
        cls.root = cls.tmp.name
        cls.env = cls._build_db(cls.root)
        cls.db_file = os.path.join(cls.root, "yiban.db")
        cls.out = cls._export("out")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def _export(cls, name, extra=()):
        out = os.path.join(cls.root, name)
        r = subprocess.run(
            [sys.executable, SCRIPT, "--db", cls.db_file, "--out", out, *extra],
            cwd=cls.root, env=cls.env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120)
        assert r.returncode == 0, r.stdout + r.stderr
        return out

    def test_1_five_files_row_counts_and_lines_parse(self):
        for name in FILE_NAMES:
            path = os.path.join(self.out, name)
            self.assertTrue(os.path.isfile(path), f"缺输出文件 {name}")
        with open(os.path.join(self.out, "summary.json"), encoding="utf-8") as f:
            summary = json.load(f)
        for table, want in ROW_COUNTS.items():
            rows = _jsonl(os.path.join(self.out, table + ".jsonl"))
            self.assertEqual(len(rows), want, f"{table} 行数不符")
            self.assertEqual(summary["rows"][table], want, f"{table} 汇总行数不符")

    def test_2_no_plaintext_credentials_in_export(self):
        blob = []
        for name in ALL_OUTPUTS:
            with open(os.path.join(self.out, name), "rb") as f:
                blob.append(f.read())
        joined = b"".join(blob)
        self.assertNotIn(PLAIN_PASSWORD.encode(), joined, "明文口令泄漏进导出物")
        self.assertNotIn(PLAIN_CODE.encode(), joined, "明文验证码泄漏进导出物")
        for row in _jsonl(os.path.join(self.out, "accounts.jsonl")):
            for field in ("password", "phone_code"):
                v = row[field]
                self.assertTrue(v == "" or isinstance(v, dict),
                                f"{field} 既非空串也非信封对象（原样串即泄漏）")
                if isinstance(v, dict):
                    self.assertLessEqual({"v", "nonce", "ct", "tag"}, set(v),
                                         "信封对象缺 GCM 必备字段")

    def test_3_envelope_passthrough_byte_identical(self):
        rows = {r["phone"]: r for r in _jsonl(os.path.join(self.out, "accounts.jsonl"))}
        self.assertEqual(rows[PHONE_A]["password"], ENV_A)
        self.assertEqual(rows[PHONE_B]["password"], ENV_B)
        self.assertEqual(rows[PHONE_B]["phone_code"], ENV_CODE)
        self.assertEqual(rows[PHONE_A]["phone_code"], "")

    def test_4_idempotent_rerun_byte_identical(self):
        out2 = self._export("out-rerun")
        for name in ALL_OUTPUTS:
            with open(os.path.join(self.out, name), "rb") as f1, \
                 open(os.path.join(out2, name), "rb") as f2:
                self.assertEqual(f1.read(), f2.read(), f"两遍导出不一致：{name}")

    def test_5_reconciliation_reproducible_from_export(self):
        recon = _jsonl(os.path.join(self.out, "reconciliation.jsonl"))
        by_key = {}
        for line in recon:
            by_key[(line["table"], line["key"])] = line["hash"]
        self.assertEqual(len(recon), sum(ROW_COUNTS.values()), "对账表行数不符")
        for table, key_field in (("users", "id"), ("accounts", "id"),
                                 ("time_prefs", "phone"), ("audit", "id")):
            for row in _jsonl(os.path.join(self.out, table + ".jsonl")):
                key = _mask(str(row[key_field])) if key_field == "phone" \
                    else str(row[key_field])
                want = _recon_hash(table, key, row)
                self.assertEqual(by_key[(table, key)], want,
                                 f"{table} 键 {key} 的对账哈希不可复算")
        # 对账表本身零手机号：time_prefs 的键是掩码形
        for line in recon:
            self.assertNotIn("phone\":", json.dumps(line), "对账行不得携带明文字段名")

    def test_6_summary_counts_orphan_duplicate_softdel_plaintext(self):
        with open(os.path.join(self.out, "summary.json"), encoding="utf-8") as f:
            s = json.load(f)
        self.assertEqual(s["orphans"]["time_prefs_phone_without_account"], 1)
        self.assertEqual(s["duplicates"]["users_by_email"], 1)
        self.assertEqual(s["duplicates"]["accounts_by_phone"], 0)
        self.assertEqual(s["soft_deleted"]["accounts"], 1)
        self.assertEqual(s["soft_deleted"]["users"], 2)
        self.assertEqual(s["plaintext_resident"]["accounts.password"], 1)
        self.assertEqual(s["plaintext_resident"]["accounts.phone_code"], 1)
        # 哈希与掩码口径必须写进汇总，两侧（导出器/导入器）才能逐行比对
        self.assertIn("sha256", s["recon_hash"])
        self.assertIn("****", s["mask_rule"])

    def test_7_soft_deleted_and_orphan_rows_exported_unfiltered(self):
        accounts = _jsonl(os.path.join(self.out, "accounts.jsonl"))
        self.assertIn(PHONE_B, {r["phone"] for r in accounts},
                      "软删账号行必须原样导出（策略由导入器定）")
        soft = next(r for r in accounts if r["phone"] == PHONE_B)
        self.assertEqual(soft["deleted"], 1)
        prefs = {r["phone"] for r in _jsonl(os.path.join(self.out, "time_prefs.jsonl"))}
        self.assertIn(PHONE_ORPHAN, prefs, "孤儿自选行必须原样导出")

    def test_8_source_db_untouched(self):
        before = open(self.db_file, "rb").read()
        self._export("out-untouched")
        after = open(self.db_file, "rb").read()
        self.assertEqual(before, after, "导出不得改动源库")
        siblings = os.listdir(os.path.dirname(self.db_file))
        self.assertNotIn("yiban.db-wal", siblings, "只读导出不得留下 WAL 副文件")
        self.assertNotIn("yiban.db-shm", siblings, "只读导出不得留下 SHM 副文件")

    def test_9_missing_required_table_refused(self):
        bare = os.path.join(self.root, "bare.db")
        import sqlite3
        conn = sqlite3.connect(bare)
        try:
            conn.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY)")
            conn.commit()
        finally:
            conn.close()
        r = subprocess.run(
            [sys.executable, SCRIPT, "--db", bare, "--out",
             os.path.join(self.root, "out-bare")],
            cwd=self.root, env=self.env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("缺表", r.stderr)

    def test_10_refuse_overwrite_without_force(self):
        r = subprocess.run(
            [sys.executable, SCRIPT, "--db", self.db_file, "--out", self.out],
            cwd=self.root, env=self.env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("拒绝执行", r.stderr)
        # 拒绝后既有导出物逐字节未动（对照一份全新导出）
        out2 = self._export("out-refusal-check")
        for name in ALL_OUTPUTS:
            with open(os.path.join(self.out, name), "rb") as f1, \
                 open(os.path.join(out2, name), "rb") as f2:
                self.assertEqual(f1.read(), f2.read())


class DbExportDryRunTest(ExportFixtureMixin, unittest.TestCase):
    """--dry-run 零写盘。"""

    def test_dry_run_writes_nothing(self):
        tmp = tempfile.TemporaryDirectory(prefix="yiban-p8a-dry-")
        self.addCleanup(tmp.cleanup)
        env = self._build_db(tmp.name)
        out = os.path.join(tmp.name, "out")
        r = subprocess.run(
            [sys.executable, SCRIPT, "--db", os.path.join(tmp.name, "yiban.db"),
             "--out", out, "--dry-run"],
            cwd=tmp.name, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("[dry-run]", r.stdout)
        self.assertFalse(os.path.exists(out), "--dry-run 不得写盘")


if __name__ == "__main__":
    unittest.main()
