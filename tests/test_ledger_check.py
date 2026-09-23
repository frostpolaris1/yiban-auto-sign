# -*- coding: utf-8 -*-
"""`scripts/ledger_check.py` 的契约用例：三项检查、退出码 `0/1/2`、白名单补项。

口径（为什么起子进程）：退出码与 stdout 是**进程级契约**，在测试进程里调 `main()`
会把它们测成另一回事。库与状态目录都用临时路径——绝不碰本机真实 `.env` 与状态目录。

库是先建到 schema 顶（空状态目录，故 backfill 不补行）再手工插入 `sign_tasks` 行的
——对账结论不依赖 backfill 行为，两个被测面互不牵连。
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

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


def _load_script():
    """把脚本按模块载入——异常路径无法从 CLI 触发，只能对同一 `main()` 注入故障。"""
    spec = importlib.util.spec_from_file_location("_ledger_check_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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

    def _insert_task(self, phone, day, state, owner="backfill", vshard=-1):
        self.conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (phone, day, vshard, owner, f"{day} 07:00:00", 5, state, 0, "", "",
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

    # ---- 12. 计数自洽的两个分支 ----
    def test_translated_row_counts_toward_reconciliation(self):
        """平移行是 `vshard=-1` 且 `owner` 为领取池原值（v18 平移保留 owner）；
        `owner='backfill'` 的是补账行——两者在计数检查里分列两项，不能混作一格。"""
        self._write_state(DAY, {PHONE_OK: {"status": "success"}})
        self._insert_task(PHONE_OK, DAY, "done", owner="worker-0@host")
        r = _run(["--day", DAY], self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("对账平", r.stdout)

    def test_third_writer_breaks_count_reconciliation(self):
        """行数 ≠ 平移 + 补账 ⇒ exit 1 并打印该差异（真实分片行既非平移也非补账）。"""
        self._write_state(DAY, {
            PHONE_OK: {"status": "success"},
            PHONE_MISSING: {"status": "paused"},
        })
        self._insert_task(PHONE_OK, DAY, "done", owner="worker-0@host")
        self._insert_task(PHONE_MISSING, DAY, "skipped", owner="worker-1@host", vshard=0)
        r = _run(["--day", DAY], self.env)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("计数不自洽", r.stdout)
        self.assertIn("无来源 1 行", r.stdout)

    # ---- 13. 零覆盖与未预期异常都归"无法定论" ----
    def test_zero_coverage_exits_two(self):
        """状态目录在、但一天的处理文件都没有 ⇒ 无法定论，不是平账。

        三项检查在空输入上空转全部"通过"，那盏绿灯说明不了台账对不对。
        """
        r = _run(["--day", DAY], self.env)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("零覆盖", r.stdout)

    def test_unexpected_exception_exits_two(self):
        """非 sqlite 异常也归"无法定论"——`1` 只留给明确探测到的差异。

        故障注入点只能从进程内给（CLI 不暴露），故直接断言 `main()` 的返回值：它就是
        `sys.exit(main())` 交给进程的退出码。
        """
        module = _load_script()
        with mock.patch.object(module.db, "init_db",
                               side_effect=RuntimeError("注入的连接层故障")):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = module.main(["--day", DAY])
        self.assertEqual(rc, 2, buf.getvalue())
        self.assertIn("无法定论", buf.getvalue())

    # ---- 14. 白名单补项 ----
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
