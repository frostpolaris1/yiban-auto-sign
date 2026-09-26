# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""审计追责链的活体反例：同事务、判码、欠账归零、删尾与整链重签。

MF-53 的缺陷是"写了但追不到人、丢了你不知道"：业务写与审计写永远两个事务（中间
被杀即"做了无留痕、欠账仍为 0"）；无会话/请求 id；`audit_head_hash` 读失败与空链同
返回 `""`；欠账单调无归零口径（urgent 永久刷屏）；`_rechain_audit_logs` 分批 commit
击穿原子承诺；`audit_verify.py` 无顶层兜底（`database is locked` 以 exit 1 冒充"检出
篡改"、无锚点把"没查"印成"通过"、删尾/整链重签检不出）。

本文件按"每道判据自带一条把输入改坏 ⇒ 工具必须响"的纪律逐条钉死修复后的行为：
业务+审计间 kill 注入不产生"做了无留痕"、审计写失败回滚业务、锁库判码 ≠ 篡改判码、
无锚点"未查" ≠ "通过"、删尾与整链重签必须可检出。e2e 三组（kill 注入 / 锁库实跑 /
重签检出）都在真子进程里跑，不 mock 判据本体。

标签：G · 安全：脱敏/审计/配置注入
覆盖：`audit_unit` / `record_in_txn` 的同事务原子性、`audit_or_refuse` 的 fail-closed、
`audit()` 的请求作用域标记、`audit_head_hash_ex` 的三态、欠账告警基线
（`audit_write_failures_unnotified` / `audit_alert_needs_attention`）、
`_rechain_audit_logs` 的单事务回滚，以及 `scripts/audit_verify.py` 的退出码映射
（0 通过 / 1 篡改 / 2 未查·锁住·无法定论）。
对应实现：`yiban/store/audit_chain.py`（`audit` / `audit_unit` / `record_in_txn` /
`audit_or_refuse` / `audit_head_hash_ex` / `_rechain_audit_logs` / 欠账基线）、
`yiban/store/accounts.py`（`add_account` / `update_account` 的 `audit_spec`）、
`scripts/audit_verify.py`。
关键断言：**"未查"与"通过"必须是两个不同返回值，"锁住"与"检出篡改"必须不同码**；
业务效果可见 ⇒ 审计行必在（kill 注入后两者同在或同不在）；重链失败必须回滚到原链。
依赖：临时库 + 临时 `.env` + 临时锚点/见证文件；CLI 与 kill 注入用例起真子进程，
故依赖 `sys.executable` 并对子进程 stdout 按本地代码页解码；无网络、无 skip。
"""
import contextlib
import locale
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
AUDIT_KEY = "b" * 64


class _Fixture(unittest.TestCase):
    """临时库 + 临时锚点/见证（互不干扰）；子进程用同一套 YIBAN_* 环境。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-audit-txn-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\nYIBAN_AUDIT_KEY={AUDIT_KEY}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_AUDIT_KEY"] = AUDIT_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        global db
        import db

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for key in ("YIBAN_ACCOUNTS_KEY", "YIBAN_AUDIT_KEY", "YIBAN_ENV_FILE",
                    "YIBAN_ACCOUNTS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR"):
            os.environ.pop(key, None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.remove(self.db_file + suffix)
        self.anchor = os.path.join(self.tmp, "audit-anchor.log")
        self.witness = os.path.join(self.tmp, "anchor-fingerprint.json")
        for p in (self.anchor, self.witness, self.witness + ".tmp"):
            with contextlib.suppress(OSError):
                os.remove(p)
        os.environ["YIBAN_DB_FILE"] = self.db_file
        os.environ["YIBAN_STATE_DIR"] = self.tmp
        db.set_request_scope(None)
        db.init_db(cleanup=False)
        db._reset_audit_fail_memory()

    # ---- 夹具 ----
    def _seed(self, n):
        for i in range(n):
            db.audit("tester", "seed", f"t{i}", f"d{i}")

    def _raw(self, sql, args=()):
        with db._conn_lock:
            conn = db.get_conn()
            cur = conn.execute(sql, args)
            conn.commit()
            return cur

    def _count(self, sql, args=()):
        with db._conn_lock:
            return db.get_conn().execute(sql, args).fetchone()[0]

    def _sub_env(self):
        env = dict(os.environ)
        env.update({
            "YIBAN_DB_FILE": self.db_file,
            "YIBAN_ENV_FILE": self.env_file,
            "YIBAN_STATE_DIR": self.tmp,
        })
        return env


class SameTransactionTest(_Fixture):
    """业务写与审计写同事务：中间被杀不会留下"做了无留痕"。"""

    def test_kill_between_business_and_audit_leaves_neither(self):
        """真子进程在 audit_unit 体内做完业务写后 os._exit ⇒ 业务与审计都不留。

        复现 MF-53 的丢法：旧实现里业务 COMMIT 与 audit() 的 COMMIT 之间被杀，
        业务效果在库、审计表无此条且欠账计数仍为 0（欠账检测结构性看不见）。
        同事务后该窗口不存在：未提交事务随进程退出被 SQLite 回滚。
        """
        script = "\n".join([
            "import os, sys",
            f"sys.path.insert(0, {BASE!r})",
            f"sys.path.insert(0, {os.path.join(BASE, 'scripts')!r})",
            "import db",
            "db.init_db(cleanup=False)",
            "with db.audit_unit('tester', 'killprobe', 't', 'kill') as conn:",
            "    conn.execute(\"INSERT OR REPLACE INTO app_meta (key, value) "
            "VALUES ('e2e_business','1')\")",
            "    os._exit(0)",
        ])
        r = subprocess.run([sys.executable, "-c", script], capture_output=True,
                           env=self._sub_env(), cwd=BASE)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace")[-2000:])
        db._conn = None
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM app_meta WHERE key='e2e_business'"), 0,
            "业务写必须随未提交事务一起回滚——'做了无留痕'状态不得存在")
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM audit_logs WHERE action='killprobe'"), 0,
            "审计行同样不得留下（两者同在或同不在）")

    def test_audit_write_failure_rolls_back_business(self):
        """审计写入注入失败 ⇒ 同一事务的业务写整体回滚（fail-closed）。"""
        with (
            mock.patch.object(db, "_audit_hash",
                              side_effect=RuntimeError("inject audit failure")),
            self.assertRaises(RuntimeError),
            db.audit_unit("tester", "failprobe", "t", "d") as conn,
        ):
            conn.execute("INSERT OR REPLACE INTO app_meta (key, value) "
                         "VALUES ('e2e_business','1')")
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM app_meta WHERE key='e2e_business'"), 0,
            "审计写失败必须回滚业务写，不得只做业务不留痕")

    def test_add_account_audit_is_same_transaction_and_rolls_back_on_failure(self):
        """生产调用点（add_account + audit_spec）：审计失败 ⇒ 账号不落库。"""
        fields = {"name": "n", "phone": "13800000000", "password": "p", "owner": "admin"}
        spec = {"username": "tester", "action": "account_add", "target": "138****0000",
                "detail": "e2e"}
        with (
            mock.patch.object(db, "_audit_hash",
                              side_effect=RuntimeError("inject audit failure")),
            self.assertRaises(RuntimeError),
        ):
            db.add_account(fields, audit_spec=spec)
        self.assertEqual(self._count("SELECT COUNT(*) FROM accounts"), 0,
                         "审计失败时账号必须回滚（凭据改了却无痕不可能存在）")
        # 正常路径：账号与审计行同事务落库
        new_id = db.add_account(fields, audit_spec=spec)
        self.assertTrue(new_id)
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM audit_logs WHERE action='account_add'"), 1)

    def test_audit_or_refuse_raises_when_audit_fails(self):
        """跨存储调用点的 fail-closed 审计：audit 返回 False ⇒ 抛 AuditWriteRefused。"""
        with (
            mock.patch.object(db, "audit", return_value=False),
            self.assertRaises(db.AuditWriteRefused),
        ):
            db.audit_or_refuse("tester", "env_write", "x", "d")
        with mock.patch.object(db, "audit", return_value=True):
            self.assertTrue(db.audit_or_refuse("tester", "env_write", "x", "d"))


class RequestScopeTest(_Fixture):
    """审计行携带请求/进程作用域 id（来源列只有可伪造 IP 哈希，答不了"哪个请求"）。"""

    def test_web_request_scope_tagged_into_detail(self):
        db.set_request_scope("web-deadbeef")
        db.audit("tester", "scoped", "t", "d")
        row = db.get_conn().execute(
            "SELECT detail FROM audit_logs WHERE action='scoped'").fetchone()
        self.assertIn("[req=web-deadbeef]", row["detail"])

    def test_process_scope_when_no_request(self):
        db.set_request_scope(None)
        db.audit("tester", "procscoped", "t", "d")
        row = db.get_conn().execute(
            "SELECT detail FROM audit_logs WHERE action='procscoped'").fetchone()
        self.assertIn(f"[req=proc{os.getpid()}-", row["detail"])


class HeadHashStateTest(_Fixture):
    """`audit_head_hash` 读失败 ≠ 空链：三态分开。"""

    def test_empty_chain_is_empty_state(self):
        self.assertEqual(db.audit_head_hash_ex(), ("empty", ""))
        self.assertEqual(db.audit_head_hash(), "")

    def test_read_failure_is_error_state_not_empty(self):
        with mock.patch.object(db, "get_conn", side_effect=sqlite3.OperationalError("boom")):
            state, head = db.audit_head_hash_ex()
            self.assertEqual(state, "error")
            self.assertIsNone(head, "读失败必须是 None，不得与空链的 '' 混同")
            self.assertIsNone(db.audit_head_hash())


class ArrearsNotificationTest(_Fixture):
    """欠账归零口径：总账单调，告警按"账目变化"触发（不永久刷屏）。"""

    def test_unnotified_zeroes_after_mark(self):
        db._bump_audit_write_failure()
        self.assertEqual(db.audit_write_failures_unnotified(), 1)
        self.assertEqual(db.audit_write_failures(), 1, "总账仍单调累加")
        self.assertTrue(db.mark_audit_write_failures_notified())
        self.assertEqual(db.audit_write_failures_unnotified(), 0, "同一笔欠账不重复告警")
        self.assertEqual(db.audit_write_failures(), 1, "归零的是告警基线，不是取证总账")
        db._bump_audit_write_failure()
        self.assertEqual(db.audit_write_failures_unnotified(), 1, "新欠账重新计为未确认")

    def test_alert_needs_attention_only_on_content_change(self):
        h = db.audit_health(path=self.anchor, fingerprint_path=self.witness)
        self.assertTrue(db.audit_alert_needs_attention(h), "首次结论必发")
        db.mark_audit_alert_sent(h)
        self.assertFalse(db.audit_alert_needs_attention(h), "同一故障态不得重发")
        changed = dict(h)
        changed["broken"] = 3
        self.assertTrue(db.audit_alert_needs_attention(changed), "结论变化必须重发")

    def test_health_exposes_new_arrears_field(self):
        h = db.audit_health(path=self.anchor, fingerprint_path=self.witness)
        self.assertIn("write_failures_new", h)


class RechainAtomicTest(_Fixture):
    """`_rechain_audit_logs` 单事务：失败回滚到重链前状态（不留半重链）。"""

    def test_rechain_failure_restores_original_chain(self):
        self._seed(4)
        conn = db.get_conn()
        before = [tuple(r) for r in conn.execute(
            "SELECT id, prev_hash, hash FROM audit_logs ORDER BY id").fetchall()]
        calls = {"n": 0}
        real = db._audit_hash

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] >= 3:
                raise RuntimeError("inject rechain failure")
            return real(*a, **k)

        with mock.patch.object(db, "_audit_hash", side_effect=flaky),                 self.assertRaises(RuntimeError):
            db._rechain_audit_logs(conn)
        after = [tuple(r) for r in conn.execute(
            "SELECT id, prev_hash, hash FROM audit_logs ORDER BY id").fetchall()]
        self.assertEqual(before, after, "重链失败必须回滚到重链前状态，不留半重链")
        self.assertTrue(db.verify_audit_chain()[0], "原链必须仍然自洽")

    def test_rechain_success_keeps_chain_consistent(self):
        self._seed(4)
        self._raw("UPDATE audit_logs SET hash='', prev_hash='' WHERE id<=2")
        db._rechain_audit_logs(db.get_conn())
        self.assertTrue(db.verify_audit_chain()[0], "重链成功后整条链必须自洽")


class FullRechainDetectionTest(_Fixture):
    """整链重签（改内容保链自洽）必须被独立见证的 head 比对抓出。"""

    def test_content_change_plus_rechain_is_detected(self):
        self._seed(6)
        db.record_audit_anchor(self.anchor)
        self.assertEqual(
            db.record_audit_anchor_witness(self.anchor, self.witness)[0], "written")
        # 改内容后重链：链仍自洽，但链尾行的哈希已变，与见证记下的 head 不符
        self._raw("UPDATE audit_logs SET detail='tampered' WHERE id=6")
        db._rechain_audit_logs(db.get_conn())
        self.assertTrue(db.verify_audit_chain()[0], "夹具前提：重签后链本身自洽")
        h = db.audit_health(self.anchor, self.witness)
        self.assertFalse(h["healthy"], "整链重签（改内容保自洽）必须判红")
        self.assertEqual(h["anchor_status"], "tampered", h["anchor_msg"])


class CliExitCodeTest(_Fixture):
    """`audit_verify.py` 判码映射：0 通过 / 1 篡改 / 2 未查·锁住·无法定论。"""

    @staticmethod
    def _out(r):
        enc = locale.getpreferredencoding(False) or "utf-8"
        try:
            return r.stdout.decode(enc)
        except (LookupError, UnicodeDecodeError):
            return r.stdout.decode("utf-8", errors="replace")

    def _run(self):
        return subprocess.run(
            [sys.executable, os.path.join(BASE, "scripts", "audit_verify.py"),
             "--anchor", self.anchor],
            capture_output=True, env=self._sub_env(), cwd=BASE,
        )

    def test_healthy_is_zero(self):
        self._seed(3)
        db.record_audit_anchor(self.anchor)
        r = self._run()
        self.assertEqual(r.returncode, 0, self._out(r))

    def test_no_anchor_is_not_checked_exit_two(self):
        """无锚点 = "未查"，必须独立返回值（exit 2），不得印成"通过"（exit 0）。"""
        self._seed(3)  # 有审计行但从未写锚点
        r = self._run()
        self.assertEqual(r.returncode, 2, self._out(r))
        self.assertIn("未查", self._out(r))

    def test_tail_deletion_detected_exit_one(self):
        """删尾（删掉最近审计）必须 exit 1，与"未查/锁住"分码。"""
        self._seed(6)
        db.record_audit_anchor(self.anchor)
        self._raw("DELETE FROM audit_logs WHERE id > 4")
        db.audit("tester", "later", "t", "d")
        r = self._run()
        self.assertEqual(r.returncode, 1, self._out(r))

    def test_locked_db_exit_two_not_tampered(self):
        """真占写锁后跑校验：必须 exit 2（无法定论），不得用 exit 1 冒充"检出篡改"。"""
        self._seed(3)
        db.record_audit_anchor(self.anchor)
        # EXCLUSIVE 需独占库文件：先释放本进程的连接，否则取锁自己就被挡。
        with contextlib.suppress(Exception):
            db._conn.close()
        db._conn = None
        lock = sqlite3.connect(self.db_file, timeout=1)
        try:
            # WAL 下 writers 不挡 readers，必须显式取**排他**锁（EXCLUSIVE 模式下
            # 该连接独占库文件与 WAL，其他进程的读也被挡住）才能真正复现"锁库"。
            lock.execute("PRAGMA locking_mode=EXCLUSIVE")
            lock.execute("BEGIN EXCLUSIVE")
            lock.execute("INSERT OR REPLACE INTO app_meta (key, value) "
                         "VALUES ('e2e_lock','1')")
            r = self._run()
        finally:
            with contextlib.suppress(Exception):
                lock.rollback()
            lock.close()
        self.assertEqual(r.returncode, 2, self._out(r))
        self.assertTrue("无法定论" in self._out(r) or "锁" in self._out(r),
                        self._out(r))
        self.assertNotEqual(r.returncode, 1, "锁住不得与检出篡改同码")


if __name__ == "__main__":
    unittest.main()
