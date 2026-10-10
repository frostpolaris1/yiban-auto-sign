# -*- coding: utf-8 -*-
"""限速持久键 = **出口标识**，运行期按**出口预算均分**（工单 `yiban-auto-sign-2cwd`）。

标签：B · 调度：领取/队列/执行体

覆盖：
- 出口标识口径：空（直连）归一到共享标识 `direct`；非空取 `scheme://host[:port]`
  （去 userinfo）；**IPv6 保留方括号**（否则不可往返、管理面误判）；**带不同凭据的同址代理
  归一到同一标识**。
- 持久化：同一出口的多个执行体写**同一行** `egress_state`（跨连接可见）；那行存的是
  **出口级**速率；`tat`（令牌位置）不落库、不跨进程共享（重启即新鲜令牌）。
- 预算均分：`EgressLimiter(shares=n)` 的子桶速率 = 出口级 λ ÷ n，且份额**不夹到 `RATE_MIN``
  （否则 n 个进程合计超过 λ）。运行期"合计 ≤ λ"的可执行证明在
  `tests/test_egress_budget_split_e2e.py`（真子进程）。
- 旧行去向：执行体身份键的旧行在新口径下**不被读**（键不同）；`migrate_v22`
  把它们**一次性清除**（重建 = 回到出厂速率，保守方向）。

对应实现：`yiban/egress.py`（`DIRECT_EGRESS` / `egress_identity` / `is_outlet_identity` /
`outlet_label` / `outlet_executor_count`）、`yiban/engine/token_bucket.py`（`EgressLimiter`
的 `shares` / `share_rate`）、`yiban/store/queue_store.py`、`yiban/store/migrations.py`。

依赖：临时 sqlite（真 schema）+ 迁移函数；全部注入浮点秒 `now`，不真实 sleep、不联网。
"""

import contextlib
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

import db

from yiban import egress
from yiban.engine import token_bucket
from yiban.store import migrations, queue_store

TEST_KEY = "a" * 64

#: 同一台 squid 的两个不同 userinfo：出口**物理同一处**，标识必须相同。
SAME_OUTLET_A = "http://user-a:pw-a@127.0.0.1:3128"
SAME_OUTLET_B = "http://user-b:pw-b@127.0.0.1:3128"
SAME_OUTLET_ID = "http://127.0.0.1:3128"
#: 另一个出口：标识必须不同。
OTHER_OUTLET = "http://user-c:pw-c@10.0.0.9:8080"
OTHER_OUTLET_ID = "http://10.0.0.9:8080"


def _manifest_env(rows):
    """按执行体清单造环境映射（只带清单键，取值交给 `egress.resolve`）。"""
    return {egress.ENV_MANIFEST: egress.dump_manifest(rows)}


class EgressIdentityTest(unittest.TestCase):
    """出口标识口径：空=共享直连；非空=去凭据的寻址形态（IPv6 保留方括号）。"""

    def test_direct_is_one_shared_identity(self):
        for raw in ("", "   ", None):
            with self.subTest(raw=raw):
                self.assertEqual(egress.egress_identity(raw), egress.DIRECT_EGRESS)
        self.assertEqual(egress.DIRECT_EGRESS, "direct",
                         "直连标识是固定串 `direct`，全部直连执行体归到这一个出口")

    def test_non_empty_identity_strips_userinfo(self):
        """标识进日志与库：不得含 userinfo（凭据）——`egress.py` 模块头的脱敏红线。"""
        ident = egress.egress_identity(SAME_OUTLET_A)
        self.assertEqual(ident, SAME_OUTLET_ID)
        self.assertNotIn("user-a", ident)
        self.assertNotIn("pw-a", ident)

    def test_same_outlet_different_credentials_share_identity(self):
        """同址、不同凭据的两个代理字符串 ⇒ 同一个出口标识（同一物理出口）。"""
        self.assertEqual(egress.egress_identity(SAME_OUTLET_A),
                         egress.egress_identity(SAME_OUTLET_B))

    def test_different_outlets_get_different_identities(self):
        self.assertNotEqual(egress.egress_identity(SAME_OUTLET_A),
                            egress.egress_identity(OTHER_OUTLET))
        self.assertNotEqual(egress.egress_identity(SAME_OUTLET_A),
                            egress.egress_identity(""))

    def test_ipv6_identity_keeps_brackets_and_is_recognized(self):
        """IPv6 出口：标识保留方括号（否则不可再解析），展示**与标识同形**（复审 F5）。"""
        ident = egress.egress_identity("http://user:pw@[::1]:3128")
        self.assertEqual(ident, "http://[::1]:3128")
        self.assertTrue(egress.is_outlet_identity(ident),
                        "IPv6 出口标识必须被识别（否则管理面误标「已弃用」且无法定向复位）")
        self.assertEqual(egress.outlet_label(ident), "http://[::1]:3128",
                         "展示与标识同形：保留方括号")

    def test_unparseable_sentinel_is_not_an_outlet_identity(self):
        """哨兵 `UNPARSEABLE` 不是出口标识（复审 F4）：否则两个坏代理会被当成同一出口。"""
        self.assertFalse(egress.is_outlet_identity(egress.UNPARSEABLE))
        self.assertEqual(egress.outlet_label(egress.UNPARSEABLE), "已弃用（旧执行体身份键）")
        self.assertEqual(egress.egress_identity(egress.UNPARSEABLE), egress.UNPARSEABLE,
                         "坏代理仍归到同一哨兵键（可接受：它们本就不出网）")

    def test_is_outlet_identity_rejects_executor_identities(self):
        for key in ("worker-0@host", "fallback@host", "single@host",
                    "host:workers:1:w0", "fallback-x", "exec-x", ""):
            with self.subTest(key=key):
                self.assertFalse(egress.is_outlet_identity(key))

    def test_outlet_label_never_echoes_legacy_or_credentials(self):
        self.assertEqual(egress.outlet_label(egress.DIRECT_EGRESS), "直连（本机出口）")
        self.assertEqual(egress.outlet_label("worker-0@host"), "已弃用（旧执行体身份键）")
        # 带凭据的串不是出口标识（产物已去 userinfo），展示为「已弃用」且**不回声**
        shown = egress.outlet_label("http://user:pw@10.0.0.1:80")
        self.assertEqual(shown, "已弃用（旧执行体身份键）")
        self.assertNotIn("pw", shown)

    def test_resolve_then_identity_groups_by_declared_outlet(self):
        """两执行体声明同一出口 ⇒ 同一标识；直连与直连同标识；不同出口不同标识。"""
        env = _manifest_env([
            {"slot": 0, "type": "worker", "proxy": ""},
            {"slot": 1, "type": "worker", "proxy": SAME_OUTLET_A},
            {"slot": 2, "type": "worker", "proxy": SAME_OUTLET_B},
            {"slot": 3, "type": "fallback", "proxy": ""},
        ])

        def key(role, index):
            return egress.egress_identity(egress.resolve(role, index, env=env))

        self.assertEqual(key(egress.ROLE_WORKER, 0), egress.DIRECT_EGRESS)
        self.assertEqual(key(egress.ROLE_FALLBACK, 0), egress.DIRECT_EGRESS,
                         "直连的 worker 与兜底归到同一个出口")
        self.assertEqual(key(egress.ROLE_WORKER, 1), SAME_OUTLET_ID)
        self.assertEqual(key(egress.ROLE_WORKER, 2), SAME_OUTLET_ID,
                         "同址不同凭据的两个 worker 归到同一个出口")
        self.assertNotEqual(key(egress.ROLE_WORKER, 1), egress.DIRECT_EGRESS)


class _DbCase(unittest.TestCase):
    """临时库基座（与 `test_token_bucket.PersistTest` 同手法）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-egress-key-")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_DB_FILE": cls.db_file,
        })

    @classmethod
    def tearDownClass(cls):
        cls._close_conn()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE"):
            os.environ.pop(k, None)

    @staticmethod
    def _close_conn():
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    def _remove_db(self):
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)

    def setUp(self):
        self._close_conn()
        self._remove_db()
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)

    def tearDown(self):
        self._close_conn()

    def _row_count(self):
        conn = db.get_conn()
        return int(conn.execute("SELECT COUNT(*) FROM egress_state").fetchone()[0])


class SharedPersistenceTest(_DbCase):
    """同一出口的多个执行体写同一行；那行是**出口级**速率，`tat` 不落库。"""

    def test_shared_row_holds_outlet_rate_seen_by_another_connection(self):
        key = egress.egress_identity(SAME_OUTLET_A)
        # 执行体 A（份额 1/2）：出口级 λ=2.0 ⇒ 子桶 1.0
        a = token_bucket.EgressLimiter(rate=2.0, burst=0, shares=2)
        self.assertTrue(a.acquire(key, 0.0))
        self.assertTrue(a.persist(key))
        # 执行体 B：**另一条连接**直读同一行（跨进程语义）
        raw = sqlite3.connect(self.db_file)
        try:
            row = raw.execute(
                "SELECT rate, burst, tat FROM egress_state WHERE egress=?", (key,)).fetchone()
        finally:
            raw.close()
        self.assertIsNotNone(row, "同一出口的速率必须落在同一行（键 = 出口标识）")
        self.assertAlmostEqual(row[0], 2.0, places=9, msg="落库的是**出口级**速率，不是份额")
        self.assertAlmostEqual(row[2], 0.0, places=9, msg="tat 不落库（多进程不共享令牌位置）")

    def test_second_executor_restores_outlet_rate_with_fresh_tat(self):
        key = egress.egress_identity(SAME_OUTLET_A)
        queue_store.save_egress_state(key, 2.0, 0.0, 0.0)
        b = token_bucket.EgressLimiter(rate=1.0, burst=0, shares=2)
        self.assertTrue(b.restore_from_store(key, now=0.0))
        self.assertAlmostEqual(b.rate, 2.0, places=9, msg="装回出口级速率")
        self.assertAlmostEqual(b.bucket(key).rate, 1.0, places=9, msg="子桶 = 出口级 ÷ 2")
        self.assertAlmostEqual(b.bucket(key).tat, 0.0, places=9, msg="重启即新鲜令牌")

    def test_two_executors_one_outlet_share_a_single_row(self):
        key = egress.egress_identity(SAME_OUTLET_B)
        queue_store.save_egress_state(key, 1.0, 6, 0.0)   # 执行体 A 落库
        queue_store.save_egress_state(key, 0.8, 6, 0.0)   # 执行体 B 落库（同键）
        self.assertEqual(self._row_count(), 1, "同一出口只许一行（UPSERT 到同一键）")

    def test_separate_outlets_keep_separate_rows(self):
        queue_store.save_egress_state(egress.egress_identity(SAME_OUTLET_A), 1.0, 6, 0.0)
        queue_store.save_egress_state(egress.egress_identity(OTHER_OUTLET), 1.0, 6, 0.0)
        self.assertEqual(self._row_count(), 2, "不同出口各持一行")


class SharesDriftWarningTest(_DbCase):
    """F3：`persist` 按当前配置重算 n，与启动快照不同即告警一次（只告警，不重算份额）。"""

    def _manifest(self, n, proxy):
        return {egress.ENV_MANIFEST: egress.dump_manifest(
            [{"slot": i, "type": "worker", "proxy": proxy} for i in range(n)])}

    def test_persist_warns_once_when_shares_drift(self):
        key = egress.egress_identity(SAME_OUTLET_A)
        lim = token_bucket.EgressLimiter(rate=2.0, burst=0, shares=2)
        self.assertTrue(lim.acquire(key, 0.0))
        with mock.patch.dict(os.environ, self._manifest(3, SAME_OUTLET_A)), \
                self.assertLogs("yiban.engine.token_bucket", level="WARNING") as cm:
            lim.persist(key)
            lim.persist(key)   # 第二次不再告警（只喊一次）
        self.assertEqual(sum(1 for m in cm.output if "同出口执行体数已变" in m), 1)
        self.assertAlmostEqual(lim.share_rate, 1.0, places=9, msg="份额不重算（仍按启动快照 2）")

    def test_persist_silent_when_shares_match(self):
        key = egress.egress_identity(SAME_OUTLET_A)
        lim = token_bucket.EgressLimiter(rate=2.0, burst=0, shares=1)
        self.assertTrue(lim.acquire(key, 0.0))
        with mock.patch.dict(os.environ, self._manifest(1, SAME_OUTLET_A)), \
                self.assertNoLogs("yiban.engine.token_bucket", level="WARNING"):
            lim.persist(key)


class LegacyRowFateTest(_DbCase):
    """旧行（执行体身份键）在新口径下的去向：不被读，且被 v22 一次性清除。"""

    LEGACY = ("worker-0@host", "worker-2@host", "fallback@host")

    def _seed_legacy(self):
        for i, old in enumerate(self.LEGACY):
            queue_store.save_egress_state(old, 0.5 + i, 6, 0.0)

    def test_new_identity_does_not_read_legacy_key(self):
        self._seed_legacy()
        self.assertIsNone(queue_store.load_egress_state(egress.DIRECT_EGRESS),
                          "新的出口标识读不到旧的执行体身份键——旧行不再被引擎消费")
        self.assertIsNotNone(queue_store.load_egress_state("worker-0@host"))

    def test_migrate_v22_clears_legacy_rows(self):
        self._seed_legacy()
        self.assertEqual(self._row_count(), 3)
        conn = db.get_conn()
        migrations.migrate_v22(conn)
        conn.commit()
        self.assertEqual(self._row_count(), 0,
                         "一次性重建：旧执行体身份键的行全部清除（回到出厂速率）")

    def test_migrate_v22_is_registered_optional(self):
        entry = [m for m in migrations._MIGRATIONS if m[0] == 22]
        self.assertEqual(len(entry), 1, "v22 必须登记在迁移表里")
        self.assertFalse(entry[0][3], "v22 是数据清理，失败不得阻断启动（可选档）")

    def test_full_init_clears_legacy_rows_via_migration(self):
        """引擎启动（init_db）时旧行被清：升级部署后库里不再有旧键行。"""
        # 造"升级前"的库：只跑到 v21，种下旧键行，再跑完整链（含 v22）
        self._close_conn()
        self._remove_db()
        saved = db._MIGRATIONS
        db._MIGRATIONS = [m for m in saved if m[0] <= 21]
        try:
            db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        finally:
            db._MIGRATIONS = saved
        self._seed_legacy()
        self.assertEqual(self._row_count(), 3)
        self._close_conn()
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)  # 跑到 v22
        self.assertEqual(self._row_count(), 0)


if __name__ == "__main__":
    unittest.main()
