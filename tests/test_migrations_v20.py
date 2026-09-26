# -*- coding: utf-8 -*-
"""v20 backfill：把 `sign-state-*.json` 里的**终态**补进 `sign_tasks`。

覆盖六类：
1. 幂等与版本提升（19→20，重入 0 行新增）；
2. JSON 状态 → 池状态的逐类映射（终态才落库，在途/无记录不落）；
3. 只认 `sign-state-<day>.json`（`sign-daily-<day>.json` 是符号表，不得作输入）；
4. 不覆盖已有行（`INSERT OR IGNORE`）；
5. 容错（某日损坏 / 目录缺失 → 跳过，不抛）；
6. `run_at` 时间回退与批量提交不丢行。

另有一条守卫：冻结映射表与 `yiban.status.ALL_STATUSES` 的键绑定（少一格即两头静默失效）。

库是**手工搭到 v19** 的（v17 建 `sign_claims`、v18 建 `sign_tasks`+`epoch`、v19 补
`sign_claims.epoch`）——不借 `db` 的全局连接，免得多用例共享单例连接互相干扰。

标签：C · 存储：迁移与库完整性
覆盖：v20 backfill 的六类——版本提升与重入零新增、JSON 终态到池状态的逐类映射（在途/
无记录不落库）、只认 `sign-state-<day>.json`（`sign-daily-*` 是符号表不得作输入）、
不覆盖已有行（`INSERT OR IGNORE`）、某日损坏或目录缺失只跳过不抛（目录**在而读不动**
⇒ 延后不提版本，反例见 `tests/test_migrations_fail_closed.py`）、`run_at` 时间回退
与批量提交不丢行；另钉映射表与 `ALL_STATUSES` 的键绑定。
对应实现：`yiban/store/` 的 v20 backfill 与 `yiban/status.py::ALL_STATUSES`、
`yiban/engine/state_io.py` 写出的状态文件形状。
关键断言：`TerminalMapGuardTest` 是**防静默失效**的一条——映射表少一格时两侧都不报错、
只是那一类的终态永远补不进账本，故断"键集合 = 全词表减在途集合"而不是逐条抽查；
`test_sign_daily_symbol_table_is_not_an_input` 与"某日损坏跳过"合起来守的是
"输入面宁可少补，不可补错"。只覆盖**新代码打开 v19 库**方向。
依赖：手工搭到 v19 + 临时状态目录（写真 `sign-state-*.json`），无网络、无 skip。
"""
import contextlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

from yiban import clock, status
from yiban.engine import hrw
from yiban.store import connection, migrations, queue_store

PHONE_A = "13800000001"
PHONE_B = "13800000002"


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
        # v19 真的跑：user_version=19 的库必须有 sign_claims.epoch——迁移完整性校验
        # 会拒"版本声称已过 v19 但产物缺失"的漂移库，夹具不得是那种漂移库。
        migrations.migrate_v19(conn)
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

    def _insert_task(self, phone, day, owner, state="done", vshard=-1):
        self.conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (phone, day, vshard, owner, f"{day} 06:31:00", 5, state, 1, "", "ok",
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

        # 惰性直证：-1 落在任何执行体的分片集之外，不靠"两个集合不相交"的组合推理。
        v = hrw.V_DEFAULT
        executors = ("worker-0@host", "worker-1@host", "worker-2@host")
        self.assertNotIn(-1, hrw.assignment(executors, day, v),
                         "assignment 的键恰为 0..v-1，不含 -1")
        for executor in executors:
            with self.subTest(executor=executor):
                self.assertNotIn(-1, hrw.shards_of(executor, executors, day, v))
        self.assertGreaterEqual(hrw.vshard_of(PHONE_A, day, v), 0, "vshard_of 只产 0..v-1")

        # 反证：先把该行改成"待领取"以隔离出 vshard 这一个变量（否则"领不到"可能只是
        # state 不是 pending 的结果），再传入全部真实分片——批领仍领不到它。同批领到
        # vshard=0 的对照行，说明"领不到"不是批领整体失效。
        self.conn.execute("UPDATE sign_tasks SET state='pending' WHERE phone=?", (PHONE_A,))
        self._insert_task(PHONE_B, day, "", state="pending", vshard=0)
        prev_conn = connection._conn
        connection.set_conn(self.conn)
        try:
            claimed = queue_store.claim_batch(
                "worker-0@host", day, tuple(range(v)), now=f"{day} 23:59:59")
        finally:
            if prev_conn is None:
                connection.reset_conn()
            else:
                connection.set_conn(prev_conn)
        self.assertEqual([c["phone"] for c in claimed], [PHONE_B])

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

    def test_unreadable_state_file_logs_a_sanitized_exception(self):
        """异常文本入日志前走 `sanitize_text`（与 `state_io` 同口径）。

        sqlite/json 的异常消息可能回显库内值（cookie/csrf/凭据），直接落日志等于把
        脱敏口径开个后门。
        """
        day = _day()
        self._write_state(day, {PHONE_A: {"status": "success"}})
        with mock.patch.object(migrations.json, "load",
                               side_effect=ValueError("第一行\npassword=hunter2")), \
                self.assertLogs("yiban.store.migrations", level="WARNING") as logs:
            self.assertIsNone(migrations._read_sign_state(self.state_dir, day))
        logged = "\n".join(logs.output)
        self.assertIn("password=***", logged)
        self.assertNotIn("hunter2", logged)
        self.assertIn(r"\n", logged, "换行应被转义（防日志注入），不是原样落盘")

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


class TerminalMapGuardTest(unittest.TestCase):
    """冻结映射表与状态词表的绑定守卫。"""

    def test_map_keys_are_all_statuses_minus_in_flight(self):
        """表键 == `ALL_STATUSES` − {`retrying`, `pending`}。

        这张表被 v20 补账与 `scripts/ledger_check.py` 的对账判定**共用**，故它少一格时
        两头同时失效：该补的行不会进台账，而用同一张表做的对账还报"平"——没有任何信号。
        用差集表达而不是手写枚举：手写枚举会在新增状态码时静默漏掉一格。
        """
        expected = set(status.ALL_STATUSES) - {status.STATUS_RETRYING,
                                               status.STATUS_PENDING}
        self.assertEqual(set(migrations._JSON_TERMINAL_TO_TASK_STATE), expected)


if __name__ == "__main__":
    unittest.main()
