# -*- coding: utf-8 -*-
"""v20 backfill：把 `sign-state-*.json` 里的**终态**补进 `sign_tasks`。

覆盖六类：
1. 幂等与版本提升（19→20，重入 0 行新增）；
2. JSON 状态 → 池状态的逐类映射（终态才落库，在途/无记录不落）；
3. 只认 `sign-state-<day>.json`（`sign-daily-<day>.json` 是符号表，不得作输入）；
4. 不覆盖已有行（`INSERT OR IGNORE`）；
5. 容错（某日损坏 / 目录缺失 → 跳过，不抛）；
6. `run_at` 时间回退与批量提交不丢行。

库是**手工搭到 v19** 的（v17 建 `sign_claims`、v18 建 `sign_tasks`）——不借 `db` 的
全局连接，免得多用例共享单例连接互相干扰。
"""
import contextlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import timedelta

from yiban import clock
from yiban.store import migrations

PHONE_A = "13800000001"


def _day(offset=0):
    """相对今天的日期串（`offset` 为正表示往前第几天）。"""
    return (clock.now() - timedelta(days=offset)).strftime("%Y-%m-%d")


class MigrateV20Test(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="yiban-v20-")
        self.state_dir = os.path.join(self.root, "state")
        os.makedirs(self.state_dir)
        self._prev_state_dir = os.environ.get("YIBAN_STATE_DIR")
        os.environ["YIBAN_STATE_DIR"] = self.state_dir
        self.conn = self._open_at_v19()

    def tearDown(self):
        with contextlib.suppress(Exception):
            self.conn.close()
        if self._prev_state_dir is None:
            os.environ.pop("YIBAN_STATE_DIR", None)
        else:
            os.environ["YIBAN_STATE_DIR"] = self._prev_state_dir
        shutil.rmtree(self.root, ignore_errors=True)

    # ---- 夹具 ----
    def _open_at_v19(self):
        conn = sqlite3.connect(os.path.join(self.root, "yiban.db"))
        conn.row_factory = sqlite3.Row
        migrations.migrate_v17(conn)
        migrations.migrate_v18(conn)
        conn.execute("PRAGMA user_version = 19")
        conn.commit()
        return conn

    def _write_state(self, day, payload, name="sign-state", raw=None):
        path = os.path.join(self.state_dir, f"{name}-{day}.json")
        with open(path, "w", encoding="utf-8") as f:
            if raw is not None:
                f.write(raw)
            else:
                json.dump(payload, f, ensure_ascii=False)

    def _rows(self):
        return {r["phone"]: dict(r) for r in
                self.conn.execute("SELECT * FROM sign_tasks").fetchall()}

    def _count(self):
        return self.conn.execute("SELECT COUNT(*) FROM sign_tasks").fetchone()[0]

    def _insert_task(self, phone, day, owner, state="done"):
        self.conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (phone, day, -1, owner, f"{day} 06:31:00", 5, state, 1, "", "ok",
             f"{day} 06:31:00"),
        )
        self.conn.commit()

    # ---- 1. 幂等与版本提升 ----
    def test_promotes_version_once_and_second_run_inserts_nothing(self):
        day = _day()
        self._write_state(day, {PHONE_A: {"status": "success", "time": "07:00:01"}})
        migrations._run_migrations(self.conn)
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 20)
        self.assertEqual(self._count(), 1)
        self.assertEqual(migrations.migrate_v20(self.conn), 0, "重入不得新增行")
        self.assertEqual(self._count(), 1)
        migrations._run_migrations(self.conn)
        self.assertEqual(self._count(), 1, "版本已到位，整链跳过")

    # ---- 2. 映射逐类 ----
    def test_only_terminal_json_states_are_folded(self):
        day = _day()
        self._write_state(day, {
            "13800000001": {"status": "success", "time": "07:00:00"},
            "13800000002": {"status": "already"},
            "13800000003": {"status": "no_task"},
            "13800000004": {"status": "failed"},
            "13800000005": {"status": "skipped_window"},
            "13800000006": {"status": "skipped_norange"},
            "13800000007": {"status": "no_position"},
            "13800000008": {"status": "paused"},
            "13800000009": {"status": "user_cancelled"},
            "13800000010": {"status": "global_paused"},
            "13800000011": {"status": "retrying"},
            "13800000012": {"status": "pending"},
            "13800000013": {"status": ""},
            "13800000014": {"time": "07:00:00"},
            "13800000015": "not-a-dict",
        })
        migrations.migrate_v20(self.conn)
        rows = self._rows()
        self.assertEqual(
            {p: r["state"] for p, r in rows.items()},
            {
                "13800000001": "done", "13800000002": "done", "13800000003": "done",
                "13800000004": "failed",
                "13800000005": "failed", "13800000006": "failed",
                "13800000007": "failed",
                "13800000008": "skipped", "13800000009": "skipped",
                "13800000010": "skipped",
            },
        )
        for phone in ("13800000011", "13800000012", "13800000013", "13800000014",
                      "13800000015"):
            self.assertNotIn(phone, rows, f"非终态/无记录不得补入: {phone}")

    def test_backfilled_rows_are_lazy(self):
        """补账行与平移行同形（vshard=-1）——不进入任何执行体的分片集合。"""
        day = _day()
        self._write_state(day, {PHONE_A: {"status": "paused"}})
        migrations.migrate_v20(self.conn)
        row = self._rows()[PHONE_A]
        self.assertEqual(row["vshard"], -1)
        self.assertEqual(row["owner"], "backfill")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["lease_until"], "")
        self.assertEqual(row["result"], "")
        self.assertEqual(row["priority"], 5)

    # ---- 3. 不读 sign-daily，且不越出回看窗口 ----
    def test_sign_daily_symbol_table_is_not_an_input(self):
        day = _day()
        self._write_state(day, {PHONE_A: "✅"}, name="sign-daily")
        self.assertEqual(migrations.migrate_v20(self.conn), 0)
        self.assertEqual(self._count(), 0)

    def test_days_outside_window_are_not_read(self):
        outside = _day(migrations._BACKFILL_DAYS)
        self._write_state(outside, {PHONE_A: {"status": "success"}})
        self.assertEqual(migrations.migrate_v20(self.conn), 0)
        self._write_state(_day(migrations._BACKFILL_DAYS - 1),
                          {PHONE_A: {"status": "success"}})
        self.assertEqual(migrations.migrate_v20(self.conn), 1, "窗口边缘那天应被读到")

    # ---- 4. 不覆盖已有行 ----
    def test_existing_row_is_not_overwritten(self):
        day = _day()
        self._insert_task(PHONE_A, day, "worker-0@host", state="done")
        self._write_state(day, {PHONE_A: {"status": "failed"}})
        self.assertEqual(migrations.migrate_v20(self.conn), 0)
        row = self._rows()[PHONE_A]
        self.assertEqual(row["owner"], "worker-0@host")
        self.assertEqual(row["state"], "done", "已有结论不得被 JSON 终态改写")

    # ---- 5. 容错 ----
    def test_corrupt_or_unusable_days_are_skipped(self):
        self._write_state(_day(0), None, raw="{not json")
        self._write_state(_day(1), [1, 2])
        self._write_state(_day(2), {})
        self._write_state(_day(3), {PHONE_A: {"status": "failed"}})
        self.assertEqual(migrations.migrate_v20(self.conn), 1)
        self.assertEqual(self._rows()[PHONE_A]["day"], _day(3))

    def test_missing_state_dir_is_not_an_error(self):
        os.environ["YIBAN_STATE_DIR"] = os.path.join(self.root, "absent")
        self.assertEqual(migrations.migrate_v20(self.conn), 0)
        self.assertEqual(self._count(), 0)

    # ---- 6. run_at 回退与批量提交 ----
    def test_run_at_falls_back_to_midnight_when_time_is_unusable(self):
        day = _day()
        self._write_state(day, {
            "13800000001": {"status": "success", "time": "07:03:11"},
            "13800000002": {"status": "failed"},
            "13800000003": {"status": "paused", "time": "7:3"},
        })
        migrations.migrate_v20(self.conn)
        rows = self._rows()
        self.assertEqual(rows["13800000001"]["run_at"], f"{day} 07:03:11")
        self.assertEqual(rows["13800000002"]["run_at"], f"{day} 00:00:00")
        self.assertEqual(rows["13800000003"]["run_at"], f"{day} 00:00:00")
        self.assertEqual(rows["13800000002"]["created_at"], f"{day} 00:00:00")
        self.assertEqual(rows["13800000001"]["created_at"], f"{day} 07:03:11")

    def test_batched_commits_lose_no_rows(self):
        day = _day()
        self._write_state(day, {
            f"1380000{i:04d}": {"status": "success", "time": "07:00:00"}
            for i in range(120)
        })
        self.assertEqual(migrations.migrate_v20(self.conn), 120)
        self.assertEqual(self._count(), 120)


if __name__ == "__main__":
    unittest.main()
