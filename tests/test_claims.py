# -*- coding: utf-8 -*-
"""签到领取池与账号级租约（`yiban/store/claims.py`，v17）的行为与并发断言。

多执行体的正确性全压在两条性质上，本文件因此**必须**同时覆盖它们：

1. **同一账号同一天只可能有一个执行体在跑**（原子领取 + 租约接管），
   ——两个执行体同时登录同一账号会加速触发易班侧风控，这是设计的第一红线；
2. **收尾写入带 owner 条件**（租约被接管后，旧执行体写不进去），
   否则"两个执行体都以为自己签成功了"，失败账号会被静默记成成功。

第 1 条用**真多进程**验证（`subprocess` 抢同一批账号）：单进程内的锁证明不了
跨进程原子性，而这正是本表存在的理由。并发用例在 Windows/WSL 都跑。
"""
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import db  # noqa: E402

TEST_KEY = "a" * 64
DAY = "2026-09-16"
PHONE = "13800138000"
PHONE_B = "13800138001"
OWNER_A = "hostA:100:090000"
OWNER_B = "hostB:200:090001"


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-claims-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_DB_FILE": cls.db_file,
            "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_LOG_FILE": os.path.join(cls.tmp, "sign.log"),
        })

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR", "YIBAN_LOG_FILE"):
            os.environ.pop(k, None)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)

    def tearDown(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None


class SchemaTest(_Base):
    def test_v17_creates_claim_table_with_unique_key(self):
        """唯一键 (phone, day) 是并发正确性的基础——缺了它 upsert 就退化成两条插入。"""
        cols = {r["name"]: r["pk"] for r in db.get_conn().execute(
            "PRAGMA table_info(sign_claims)").fetchall()}
        self.assertEqual(set(cols), {"phone", "day", "owner", "claimed_at",
                                     "heartbeat_at", "state", "result", "attempts"})
        self.assertEqual(cols["phone"], 1)
        self.assertEqual(cols["day"], 2)
        self.assertGreaterEqual(
            db.get_conn().execute("PRAGMA user_version").fetchone()[0], 17)
        idx = {r["name"] for r in db.get_conn().execute(
            "PRAGMA index_list(sign_claims)").fetchall()}
        self.assertIn("idx_sign_claims_day_state", idx)


class ClaimSemanticsTest(_Base):
    def test_claim_then_second_owner_is_refused_while_lease_live(self):
        self.assertTrue(db.claim_sign_account(PHONE, DAY, OWNER_A))
        self.assertFalse(db.claim_sign_account(PHONE, DAY, OWNER_B),
                         "租约有效期内其他执行体不得领到同一账号")

    def test_same_owner_may_reclaim(self):
        """自己重入是允许的：同一执行体内重试、进程重启后接管自己的记录。"""
        self.assertTrue(db.claim_sign_account(PHONE, DAY, OWNER_A))
        self.assertTrue(db.claim_sign_account(PHONE, DAY, OWNER_A))

    def test_expired_lease_can_be_taken_over(self):
        self.assertTrue(db.claim_sign_account(PHONE, DAY, OWNER_A))
        # lease_sec=0 表示"立刻可接管"（等价于心跳已过期）
        self.assertTrue(db.claim_sign_account(PHONE, DAY, OWNER_B, lease_sec=0),
                        "租约过期后任何执行体都应能接管（崩溃自愈的前提）")

    def test_settle_is_owner_scoped(self):
        """收尾必须带 owner 条件：被接管的旧执行体不能把结果写进去。"""
        db.claim_sign_account(PHONE, DAY, OWNER_A)
        self.assertFalse(db.claim_settle(PHONE, DAY, OWNER_B, db.CLAIM_STATE_DONE, "冒充"))
        self.assertTrue(db.claim_settle(PHONE, DAY, OWNER_A, db.CLAIM_STATE_DONE, "签到成功"))
        self.assertEqual(db.claim_states_for_day(DAY)[PHONE], db.CLAIM_STATE_DONE)

    def test_settled_row_needs_explicit_reopen(self):
        """已了结的账号默认不能再领；手动指定账号/补签重跑须显式 allow_settled。"""
        db.claim_sign_account(PHONE, DAY, OWNER_A)
        db.claim_settle(PHONE, DAY, OWNER_A, db.CLAIM_STATE_DONE, "ok")
        self.assertFalse(db.claim_sign_account(PHONE, DAY, OWNER_B))
        self.assertTrue(db.claim_sign_account(PHONE, DAY, OWNER_B, allow_settled=True))
        self.assertEqual(db.claim_states_for_day(DAY)[PHONE], db.CLAIM_STATE_CLAIMED)

    def test_settle_rejects_unknown_state(self):
        with self.assertRaises(ValueError):
            db.claim_settle(PHONE, DAY, OWNER_A, "半途而废")

    def test_give_up_releases_lease_immediately(self):
        """弃权（failed）必须**立刻**放开租约：否则补签轮领不到、当日彻底签不上。"""
        self.assertTrue(db.claim_sign_account(PHONE, DAY, OWNER_A))
        self.assertTrue(db.claim_give_up(PHONE, DAY, OWNER_A, "重试耗尽"))
        self.assertEqual(db.claim_states_for_day(DAY)[PHONE], db.CLAIM_STATE_FAILED)
        # 不等 900s，别的执行体立刻可接手
        self.assertTrue(db.claim_sign_account(PHONE, DAY, OWNER_B),
                        "failed 行应立刻可被其他执行体接手")

    def test_give_up_is_owner_scoped(self):
        db.claim_sign_account(PHONE, DAY, OWNER_A)
        self.assertFalse(db.claim_give_up(PHONE, DAY, OWNER_B, "冒充"))
        self.assertTrue(db.claim_give_up(PHONE, DAY, OWNER_A, "放弃"))

    def test_done_is_the_only_real_settlement(self):
        """done = 当日了结（需显式 allow_settled 才能再领）；failed = 未了结（可再领）。"""
        db.claim_sign_account(PHONE, DAY, OWNER_A)
        db.claim_give_up(PHONE, DAY, OWNER_A, "失败")
        self.assertEqual(db.claim_stats(DAY)["open"], 1, "failed 属于未了结")
        self.assertEqual(db.claim_stats(DAY)["settled"], 0)
        self.assertTrue(db.claim_sign_account(PHONE, DAY, OWNER_B))   # 未了结可直接领
        db.claim_settle(PHONE, DAY, OWNER_B, db.CLAIM_STATE_DONE, "ok")
        stats = db.claim_stats(DAY)
        self.assertEqual((stats["settled"], stats["open"]), (1, 0))
        self.assertFalse(db.claim_sign_account(PHONE, DAY, OWNER_A), "已了结需显式重开")

    def test_result_is_truncated(self):
        """结果只存摘要：本表可能被运维导出，无界文本会把它撑成第二个日志表。"""
        db.claim_sign_account(PHONE, DAY, OWNER_A)
        db.claim_give_up(PHONE, DAY, OWNER_A, "x" * 500)
        row = db.get_conn().execute(
            "SELECT result FROM sign_claims WHERE phone=? AND day=?", (PHONE, DAY)).fetchone()
        self.assertEqual(len(row["result"]), 200)

    def test_status_helpers(self):
        db.claim_sign_account(PHONE, DAY, OWNER_A)
        db.claim_sign_account(PHONE_B, DAY, OWNER_A)
        db.claim_settle(PHONE_B, DAY, OWNER_A, db.CLAIM_STATE_DONE, "ok")
        states = db.claim_states_for_day(DAY)
        self.assertEqual(states, {PHONE: db.CLAIM_STATE_CLAIMED, PHONE_B: db.CLAIM_STATE_DONE})
        stats = db.claim_stats(DAY)
        self.assertEqual((stats["claimed"], stats["done"], stats["settled"], stats["total"]),
                         (1, 1, 1, 2))
        in_flight = {r["phone"] for r in db.claim_in_flight(DAY)}
        self.assertEqual(in_flight, {PHONE}, "在飞只算租约未过期的 claimed")
        self.assertEqual({r["phone"] for r in db.claim_in_flight(DAY, lease_sec=0)}, set())

    def test_touch_requires_ownership(self):
        db.claim_sign_account(PHONE, DAY, OWNER_A)
        self.assertTrue(db.claim_touch(PHONE, DAY, OWNER_A))
        self.assertFalse(db.claim_touch(PHONE, DAY, OWNER_B))

    def test_purge_keeps_recent_days(self):
        db.claim_sign_account(PHONE, "2000-01-01", OWNER_A)
        db.claim_sign_account(PHONE_B, DAY, OWNER_A)
        removed = db.purge_sign_claims(14)
        self.assertEqual(removed, 1)
        self.assertNotIn("2000-01-01", {r["day"] for r in db.get_conn().execute(
            "SELECT day FROM sign_claims").fetchall()})

    def test_degrades_gracefully_without_table(self):
        """表未落地（迁移被延后）时领取按"没人在抢"处理——单执行体形态不能因此停摆。"""
        db.get_conn().execute("DROP TABLE sign_claims")
        db.get_conn().commit()
        self.assertTrue(db.claim_sign_account(PHONE, DAY, OWNER_A))
        self.assertFalse(db.claim_settle(PHONE, DAY, OWNER_A, db.CLAIM_STATE_DONE))
        self.assertEqual(db.claim_states_for_day(DAY), {})
        self.assertEqual(db.claim_stats(DAY)["total"], 0)


class SingleStatementClaimTest(_Base):
    """领取必须是**单条** upsert：拆成"先查后插"就出现了可被撞上的时间窗口。

    时序型用例撞不上那个窗口（见 CrossProcessClaimTest 的说明），所以这条用**调用形状**
    钉住：一次 `try_claim` 只能发一条 SQL，且必须是带 `ON CONFLICT ... DO UPDATE`
    的 upsert（冲突行不满足条件时 rowcount=0，正好就是"没领到"）。
    """

    class _FakeConn:
        def __init__(self, rowcount=1):
            self.calls = []
            self._rowcount = rowcount

        def execute(self, sql, params=()):
            self.calls.append((" ".join(sql.split()), params))
            return type("Cur", (), {"rowcount": self._rowcount})()

        def commit(self):
            self.calls.append(("COMMIT", ()))

    def _run_claim(self, rowcount=1, **kw):
        fake = self._FakeConn(rowcount)
        with mock.patch.object(db, "get_conn", return_value=fake),                 mock.patch.object(db, "_conn_lock", contextlib.nullcontext()):
            got = db.claim_sign_account(PHONE, DAY, OWNER_A, **kw)
        return got, fake

    def test_claim_is_a_single_upsert_statement(self):
        got, fake = self._run_claim()
        statements = [c for c in fake.calls if c[0] != "COMMIT"]
        self.assertEqual(len(statements), 1,
                         f"领取必须只发一条语句，实际发了 {len(statements)} 条：{fake.calls}")
        sql = statements[0][0]
        self.assertIn("INSERT INTO sign_claims", sql)
        self.assertIn("ON CONFLICT(phone, day) DO UPDATE", sql)
        self.assertTrue(got)

    def test_rowcount_zero_means_lost_race(self):
        """upsert 的条件不满足时 rowcount=0，必须如实答"没领到"。"""
        got, fake = self._run_claim(rowcount=0)
        self.assertFalse(got)
        # 已了结行 + 未开 allow_settled：条件表达式里必须带上这两个开关
        sql = next(c[0] for c in fake.calls if c[0] != "COMMIT")
        self.assertIn("state = ? AND ?", sql)          # done 行只由 allow_settled 决定
        self.assertIn("state IN (?, ?)", sql)          # claimed/failed 走租约判据
        self.assertIn("heartbeat_at <= ?", sql)

    def test_settle_and_touch_are_owner_scoped_single_statements(self):
        for fn, args in ((db.claim_settle, (PHONE, DAY, OWNER_A)),
                         (db.claim_touch, (PHONE, DAY, OWNER_A))):
            with self.subTest(fn=fn.__name__):
                fake = self._FakeConn(0)
                with mock.patch.object(db, "get_conn", return_value=fake),                         mock.patch.object(db, "_conn_lock", contextlib.nullcontext()):
                    self.assertFalse(fn(*args))
                sql = next(c[0] for c in fake.calls if c[0] != "COMMIT")
                self.assertIn("owner=?", sql.replace(" ", ""))


# ---------------------------------------------------------------------------
# 真多进程竞争：设计的第一红线
# ---------------------------------------------------------------------------
_CHILD_SRC = r"""
import json, os, sys, time
sys.path.insert(0, os.environ["CLAIMS_SCRIPTS"])
import db
owner = sys.argv[1]
phones = json.loads(sys.argv[2])
out = sys.argv[3]
won = {}
# 起跑线对齐：都等到同一个时刻再抢（否则变成"谁先启动谁赢"）
start_at = float(os.environ["CLAIMS_START_AT"])
while time.time() < start_at:
    time.sleep(0.001)
for ph in phones:
    for _ in range(3):          # 抢不到就重试（模拟真实执行体的重试节奏）
        if db.claim_sign_account(ph, os.environ["CLAIMS_DAY"], owner):
            won[ph] = owner
            break
        time.sleep(0.005)
with open(out, "w", encoding="utf-8") as f:
    json.dump(won, f)
"""


class CrossProcessClaimTest(_Base):
    """K 个进程同时抢同一批账号：每个账号**恰好**被一个进程领到、赢家即库内持有者。

    ⚠ 这条用例证明的是**结果性质**（无丢失、无重复、持有者一致），不是"实现是原子的"：
    实测把 try_claim 改成"先查后插"后本用例依然通过（SQLite 写者串行 + 唯一键兜底，
    四次抢跑没能撞上那条微秒级的窗口）。**原子性由三件事共同保证并分别钉住**：
    ① 唯一键 `(phone, day)`（见 SchemaTest）；② 领取是**单条 upsert**（见
    SingleStatementClaimTest）；③ 冲突时答"没领到"（同上）。三者缺一才会出现
    "两个执行体同时登录同一账号"。
    """

    PROCS = 4
    ACCOUNTS = 24

    def test_each_account_claimed_exactly_once(self):
        phones = [f"1390000{i:04d}" for i in range(self.ACCOUNTS)]
        scripts_dir = os.path.join(BASE, "scripts")
        child = os.path.join(self.tmp, "claim_child.py")
        with open(child, "w", encoding="utf-8") as f:
            f.write(_CHILD_SRC)
        start_at = time.time() + 1.5      # 给所有子进程足够的启动时间
        env = dict(os.environ)
        env.update({
            "CLAIMS_SCRIPTS": scripts_dir,
            "CLAIMS_DAY": DAY,
            "CLAIMS_START_AT": f"{start_at:.3f}",
            "YIBAN_DB_FILE": self.db_file,
            "YIBAN_ENV_FILE": self.env_file,
        })
        outs = []
        procs = []
        for i in range(self.PROCS):
            out = os.path.join(self.tmp, f"won-{i}.json")
            outs.append(out)
            procs.append(subprocess.Popen(
                [sys.executable, child, f"owner{i}", json.dumps(phones), out],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            ))
        for p in procs:
            out, _ = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, out.decode("utf-8", "replace") if out else "")

        wins = []
        for out in outs:
            with open(out, encoding="utf-8") as f:
                wins.append(json.load(f))
        flat = [ph for w in wins for ph in w]
        self.assertEqual(len(flat), len(set(flat)),
                         "同一账号被多个进程领到——并发领取失去原子性")
        self.assertEqual(set(flat), set(phones), "每个账号都应恰好被领到一次")
        # **赢家必须真的是库里的持有者**：只比"谁报了赢"会漏掉"报了赢但没写进去"
        # 的实现（例如先查后插遇到唯一键冲突后把异常吞成成功）。
        rows = {r["phone"]: r["owner"] for r in db.get_conn().execute(
            "SELECT phone, owner FROM sign_claims WHERE day=?", (DAY,)).fetchall()}
        for w in wins:
            for phone, owner in w.items():
                self.assertIn(phone, rows, f"{phone} 报告领取成功但库里没有记录")
                self.assertEqual(rows[phone], owner,
                                 f"{phone} 的持有者是 {rows[phone]}，但 {owner} 报告领取成功")
        # 库内状态与获胜记录一致
        states = db.claim_states_for_day(DAY)
        self.assertEqual(set(states), set(phones))
        self.assertEqual({v for v in states.values()}, {db.CLAIM_STATE_CLAIMED})
        owners = {r["owner"] for r in db.get_conn().execute(
            "SELECT owner FROM sign_claims WHERE day=?", (DAY,)).fetchall()}
        self.assertLessEqual(len(owners), self.PROCS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
