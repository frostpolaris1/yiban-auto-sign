# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""发射账本（`emit_ledger`）：把「是否已经说过」从进程内变量搬进库（单 3zr5）。

**为什么需要它**：引擎里曾有若干模块级「上一个值」去重变量
（`_UNREACHED_PEER_LAST` / `_UNREACHED_UNCLAIMED_LAST`，本单已搬走）。它用"进程内内存"
回答一个**跨进程、跨业务日**的问题，于是每个成因都要手写三个边界：身份（多执行体）、
复位（跨天）、复活（N→0→N）。账本把回答交给库：`day` 与 `scope` 是**必填形参**，
键在数据层内部按内容构造，唯一约束是仲裁者——三个边界从"要靠人记得"变成
"构造不出来"。

**钉的不变式**（不钉编舞）：
1. 同一键发 100 次 ⇒ 表里 1 行，只有第一次返回"我来说"；
2. **两个进程**同时发同一键 ⇒ 恰 1 行（仲裁在库，不是进程内锁）；
3. 跨天同组成 ⇒ 各 1 行，且中间没有任何清理动作；
4. 两个执行体各发 ⇒ 各 1 行（身份在键里）；
5. `emit_on_change`：N→0→N 是 3 次变化；N→N 只有第 1 次是变化。

**依赖**：临时 SQLite（真迁移链）＋ 真子进程（跨进程用例）；无网络、无 skip。
跨进程用例必须用**独立进程**而不是两个线程：线程会被进程内 `_conn_lock` 串行化，
那样测不到"库是仲裁者"这个性质（AGENTS §32 的模拟器等价性）。
"""
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_KEY = "a" * 64

DAY = "2026-09-22"
NEXT_DAY = "2026-09-23"
SCOPE = "worker-0@test"
OTHER_SCOPE = "worker-1@test"


class EmitLedgerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-emit-ledger-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.state_dir = os.path.join(cls.tmp, "state")
        os.makedirs(cls.state_dir)
        cls._old_env = {}
        for key, value in (("YIBAN_DB_FILE", cls.db_file),
                           ("YIBAN_ENV_FILE", cls.env_file),
                           ("YIBAN_ACCOUNTS_KEY", TEST_KEY),
                           ("YIBAN_STATE_DIR", cls.state_dir)):
            cls._old_env[key] = os.environ.get(key)
            os.environ[key] = value
        global db
        import db

    @classmethod
    def tearDownClass(cls):
        _close_conn()
        for key, value in cls._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        _close_conn()
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.remove(self.db_file + suffix)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)

    def _rows(self, kind=None):
        sql = "SELECT key, day, scope, kind, value FROM emit_ledger"
        params = ()
        if kind is not None:
            sql += " WHERE kind = ?"
            params = (kind,)
        cur = db.get_conn().execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    # ---- 机制 A：一次且仅一次 ----
    def test_same_key_a_hundred_times_writes_one_row(self):
        from yiban.adapters.persistence import emit_ledger
        verdicts = [emit_ledger.emit_once(
            day=DAY, scope=SCOPE, kind="peer", payload="6 个账号") for _ in range(100)]
        self.assertEqual(verdicts, [True] + [False] * 99,
                         "只有第一个说的人返回 True（返回值＝我是不是说的那一个）")
        self.assertEqual(len(self._rows()), 1)

    def test_same_key_two_processes_exactly_one_row(self):
        from yiban.adapters.persistence import emit_ledger
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "from yiban.adapters.persistence import emit_ledger\n"
            "print(emit_ledger.emit_once(day=%r, scope=%r, kind='peer', payload='x'))"
            % (BASE, DAY, SCOPE)
        )
        env = dict(os.environ)
        procs = [subprocess.Popen([sys.executable, "-c", code], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True) for _ in range(2)]
        outs = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, f"子进程失败：{err}")
            outs.append(out.strip())
        self.assertEqual(sorted(outs), ["False", "True"],
                         f"两个进程必须恰好一个说：{outs}")
        self.assertEqual(len(self._rows()), 1, "库是仲裁者：只能有一行")
        # 父进程自己再发一次：仍不是"说的那一个"
        self.assertFalse(emit_ledger.emit_once(
            day=DAY, scope=SCOPE, kind="peer", payload="x"))

    def test_cross_day_same_composition_gets_one_row_per_day(self):
        from yiban.adapters.persistence import emit_ledger
        self.assertTrue(emit_ledger.emit_once(
            day=DAY, scope=SCOPE, kind="peer", payload="6 个账号"))
        self.assertTrue(emit_ledger.emit_once(
            day=NEXT_DAY, scope=SCOPE, kind="peer", payload="6 个账号"))
        rows = self._rows()
        self.assertEqual(len(rows), 2, "跨天是另一批键：各 1 行")
        self.assertEqual({r["day"] for r in rows}, {DAY, NEXT_DAY})

    def test_two_executors_each_get_a_row(self):
        from yiban.adapters.persistence import emit_ledger
        self.assertTrue(emit_ledger.emit_once(
            day=DAY, scope=SCOPE, kind="peer", payload="6 个账号"))
        self.assertTrue(emit_ledger.emit_once(
            day=DAY, scope=OTHER_SCOPE, kind="peer", payload="6 个账号"),
            "身份必须在键里：第二个执行体不得被判成'说过了'")
        self.assertEqual(len(self._rows()), 2)

    # ---- 机制 B：变化时且仅当变化 ----
    def test_on_change_n_zero_n_is_three_changes(self):
        from yiban.adapters.persistence import emit_ledger
        verdicts = [
            emit_ledger.emit_on_change(day=DAY, scope=SCOPE, kind="unreached",
                                       value="6|peer=6"),
            emit_ledger.emit_on_change(day=DAY, scope=SCOPE, kind="unreached",
                                       value=""),
            emit_ledger.emit_on_change(day=DAY, scope=SCOPE, kind="unreached",
                                       value="6|peer=6"),
        ]
        self.assertEqual(verdicts, [True, True, True],
                         "N→0→N 的第三次必须复活（值确实变了）")
        rows = self._rows()
        self.assertEqual(len(rows), 1, "「上一个值」只有一行")
        self.assertEqual(rows[0]["value"], "6|peer=6")

    def test_on_change_same_value_only_first_is_a_change(self):
        from yiban.adapters.persistence import emit_ledger
        first = emit_ledger.emit_on_change(
            day=DAY, scope=SCOPE, kind="unreached", value="6|peer=6")
        second = emit_ledger.emit_on_change(
            day=DAY, scope=SCOPE, kind="unreached", value="6|peer=6")
        self.assertEqual((first, second), (True, False), "值没变就不得再说")

    def test_on_change_same_value_across_days_is_a_new_change(self):
        from yiban.adapters.persistence import emit_ledger
        self.assertTrue(emit_ledger.emit_on_change(
            day=DAY, scope=SCOPE, kind="unreached", value="6|peer=6"))
        self.assertTrue(emit_ledger.emit_on_change(
            day=NEXT_DAY, scope=SCOPE, kind="unreached", value="6|peer=6"),
            "跨天复位由键承担：次日同一组成仍是一次变化")
        self.assertEqual(len(self._rows()), 2)


def _close_conn():
    if db._conn is not None:
        with contextlib.suppress(Exception):
            db._conn.close()
        db._conn = None


if __name__ == "__main__":
    unittest.main(verbosity=2)
