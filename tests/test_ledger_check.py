# -*- coding: utf-8 -*-
"""`scripts/ledger_check.py` 的契约用例：三项检查、退出码 `0/1/2`、白名单补项。

口径（为什么起子进程）：退出码与 stdout 是**进程级契约**，在测试进程里调 `main()`
会把它们测成另一回事。库与状态目录都用临时路径——绝不碰本机真实 `.env` 与状态目录。

库是先建到 schema 顶（空状态目录，故 backfill 不补行）再手工插入 `sign_tasks` 行的
——对账结论不依赖 backfill 行为，两个被测面互不牵连。
"""
import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from yiban import clock
from yiban.masking import mask_phone
from yiban.store import migrations

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(BASE, "scripts", "ledger_check.py")

DAY = clock.now().strftime("%Y-%m-%d")
PHONE_OK = "13800000001"
PHONE_MISSING = "13800000002"


def _run(args, env):
    return subprocess.run([sys.executable, SCRIPT, *args], cwd=BASE, env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)


class LedgerCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-ledgercheck-")
        self.state_dir = os.path.join(self.tmp, "state")
        os.makedirs(self.state_dir)
        self.db_file = os.path.join(self.tmp, "yiban.db")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("YIBAN_")}
        self.env.update({
            "YIBAN_STATE_DIR": self.state_dir,
            "YIBAN_LOG_FILE": os.path.join(self.tmp, "sign.log"),
            "YIBAN_DB_FILE": self.db_file,
            "YIBAN_ENV_FILE": os.path.join(self.tmp, ".env"),
            "PYTHONPATH": BASE,
            "PYTHONIOENCODING": "utf-8",
        })
        # 建库那一步（migrate_v20）会读状态目录，故进程环境也得指向临时目录——否则
        # 它会去读本机真实部署的 sign-state-*.json（补出与夹具冲突的行）
        self._prev_state_dir = os.environ.get("YIBAN_STATE_DIR")
        os.environ["YIBAN_STATE_DIR"] = self.state_dir
        self.conn = self._make_db()

    def tearDown(self):
        with contextlib.suppress(Exception):
            self.conn.close()
        if self._prev_state_dir is None:
            os.environ.pop("YIBAN_STATE_DIR", None)
        else:
            os.environ["YIBAN_STATE_DIR"] = self._prev_state_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 夹具 ----
    def _make_db(self):
        """把库建到 schema 顶（空状态目录 ⇒ backfill 一行不补）。"""
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        migrations._create_tables(conn)
        migrations._run_migrations(conn)
        conn.commit()
        return conn

    def _write_state(self, day, entries):
        with open(os.path.join(self.state_dir, f"sign-state-{day}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False)

    def _insert_task(self, phone, day, state, owner="backfill"):
        self.conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (phone, day, -1, owner, f"{day} 07:00:00", 5, state, 0, "", "",
             f"{day} 07:00:00"),
        )
        self.conn.commit()

    # ---- 10. 全平 / 有差异 / 无法定论 ----
    def test_flat_reconciliation_exits_zero(self):
        self._write_state(DAY, {
            PHONE_OK: {"status": "success", "time": "07:00:00"},
            PHONE_MISSING: {"status": "paused", "time": "07:01:00"},
        })
        self._insert_task(PHONE_OK, DAY, "done")
        self._insert_task(PHONE_MISSING, DAY, "skipped")
        r = _run(["--day", DAY], self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("对账平", r.stdout)

    def test_all_days_window_reaches_the_same_verdict(self):
        self._write_state(DAY, {PHONE_OK: {"status": "success"}})
        self._insert_task(PHONE_OK, DAY, "done")
        r = _run(["--all-days", "14"], self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_json_terminal_missing_from_tasks_lists_the_row_and_exits_one(self):
        self._write_state(DAY, {
            PHONE_OK: {"status": "success", "time": "07:00:00"},
            PHONE_MISSING: {"status": "paused", "time": "07:01:00"},
        })
        self._insert_task(PHONE_OK, DAY, "done")
        r = _run(["--day", DAY], self.env)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f"{DAY} {mask_phone(PHONE_MISSING)} paused", r.stdout)
        self.assertNotIn(PHONE_MISSING, r.stdout, "对外输出不得含完整手机号")

    def test_missing_state_dir_or_db_exits_two(self):
        self._write_state(DAY, {PHONE_OK: {"status": "success"}})
        self.conn.close()
        env = dict(self.env, YIBAN_STATE_DIR=os.path.join(self.tmp, "absent"))
        r = _run(["--day", DAY], env)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("状态目录", r.stdout)

        self.conn = self._make_db()
        env = dict(self.env, YIBAN_DB_FILE=os.path.join(self.tmp, "absent.db"))
        r = _run(["--day", DAY], env)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("库", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "absent.db")),
                         "库缺失时不得被 connect 顺手新建出空库")

    # ---- 11. 状态值越界 ----
    def test_out_of_vocabulary_state_is_reported(self):
        self._write_state(DAY, {PHONE_OK: {"status": "success"}})
        self._insert_task(PHONE_OK, DAY, "done")
        self._insert_task(PHONE_MISSING, DAY, "halfway")
        r = _run(["--day", DAY], self.env)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("halfway", r.stdout)

    # ---- 12. 白名单补项 ----
    def test_allowed_tables_covers_egress_state_and_app_meta(self):
        self.assertIn("egress_state", migrations._ALLOWED_TABLES)
        self.assertIn("app_meta", migrations._ALLOWED_TABLES)
        self.assertNotIn("executor_heartbeats", migrations._ALLOWED_TABLES,
                         "零引用的死表不进白名单")
        migrations._ensure_column(
            self.conn, "egress_state", "probe_col", "INTEGER NOT NULL DEFAULT 0")
        cols = {r["name"] for r in
                self.conn.execute("PRAGMA table_info(egress_state)").fetchall()}
        self.assertIn("probe_col", cols)


if __name__ == "__main__":
    unittest.main()
