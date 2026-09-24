# -*- coding: utf-8 -*-
"""Phase 3：审计日志 HMAC 哈希链测试。

覆盖：
- 迁移后版本 3 且存在 prev_hash/hash 列；
- 连续写入后校验通过；
- 篡改任意一行后校验失败；
- 清理旧日志后剩余链仍可校验（新根生效）；
- 存量旧数据回填后校验通过。
"""
import contextlib
import hashlib
import importlib.util
import json
import locale
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
AUDIT_KEY = "b" * 64


class AuditChainTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-audit-chain-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\nYIBAN_AUDIT_KEY={AUDIT_KEY}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_AUDIT_KEY"] = AUDIT_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        global db
        import db

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        os.environ.pop("YIBAN_ACCOUNTS_KEY", None)
        os.environ.pop("YIBAN_AUDIT_KEY", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, env_file=self.env_file)

    def _reset_conn(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    def test_migration_adds_hash_columns_and_version_3(self):
        conn = db.init_db(self.db_file)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                         db._MIGRATIONS[-1][0])
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(audit_logs)").fetchall()}
        self.assertIn("prev_hash", cols)
        self.assertIn("hash", cols)

    def test_audit_chain_verify_ok(self):
        db.audit("admin", "account_add", "138****8001", "测试1")
        db.audit("admin", "account_update", "138****8002", "测试2")
        db.audit("user1@test.local", "user_register", "user1@test.local", "测试3")
        ok, broken, first = db.verify_audit_chain()
        self.assertTrue(ok)
        self.assertEqual(broken, 0)
        self.assertIsNone(first)

    def test_tamper_detected(self):
        db.audit("admin", "account_add", "138****8001", "测试1")
        db.audit("admin", "account_update", "138****8002", "测试2")
        db.audit("admin", "account_delete", "138****8003", "测试3")
        conn = sqlite3.connect(self.db_file)
        conn.execute("UPDATE audit_logs SET detail='被篡改' WHERE action='account_update'")
        conn.commit()
        conn.close()
        ok, broken, _ = db.verify_audit_chain()
        self.assertFalse(ok)
        self.assertGreaterEqual(broken, 1)

    def test_cleanup_old_rows_keeps_chain_verifiable(self):
        for i in range(5):
            db.audit("admin", "test", f"target-{i}", f"detail-{i}")
        conn = sqlite3.connect(self.db_file)
        conn.execute("DELETE FROM audit_logs WHERE id <= 2")
        conn.commit()
        conn.close()
        # 模拟 _audit_cleanup 的“删除后重建链”
        db._rechain_audit_logs(db.get_conn())
        ok, broken, first = db.verify_audit_chain()
        self.assertTrue(ok, (broken, first))

    def test_backfill_old_rows(self):
        self._reset_conn()
        # 清掉当前库，手工构造一个 user_version=2 且 audit_logs 无哈希列的旧库
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        conn = sqlite3.connect(self.db_file)
        try:
            conn.executescript(
                """
                CREATE TABLE audit_logs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  ts TEXT NOT NULL,
                  username TEXT NOT NULL,
                  action TEXT NOT NULL,
                  target TEXT NOT NULL DEFAULT '',
                  detail TEXT NOT NULL DEFAULT ''
                );
                """
            )
            conn.execute("PRAGMA user_version = 2")
            conn.execute(
                "INSERT INTO audit_logs (ts, username, action, target, detail) VALUES "
                "('2026-08-16 10:00:00','admin','account_add','138****8001','旧数据1'),"
                "('2026-08-16 10:01:00','admin','account_update','138****8002','旧数据2')"
            )
            conn.commit()
        finally:
            conn.close()

        conn = db.init_db(self.db_file)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                         db._MIGRATIONS[-1][0])
        ok, broken, first = db.verify_audit_chain()
        self.assertTrue(ok, (broken, first))


def _line_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _DbFixture(unittest.TestCase):
    """每个用例一套临时库 + 临时锚点文件（互不干扰）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-audit-tamper-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\nYIBAN_AUDIT_KEY={AUDIT_KEY}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_AUDIT_KEY"] = AUDIT_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
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
        self.anchor = db.audit_anchor_path()
        for p in (self.anchor, self.anchor + ".tmp"):
            with contextlib.suppress(OSError):
                os.remove(p)
        db.init_db(cleanup=False)

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

    def _anchor_lines(self):
        with open(self.anchor, encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip()]


class AnchorLineFormatTest(_DbFixture):
    """锚点行 v2 格式：count + prev_line_hash + 行间链。"""

    def test_v2_line_carries_count_and_prev_line_hash(self):
        self._seed(6)
        line = db.record_audit_anchor()
        self.assertIsNotNone(line)
        parts = line.split()
        # ts 含一个空格 → 2 token，另有 min/max/count/purge_total/head/prev 6 段
        self.assertEqual(len(parts), 8, f"锚点行应为 8 token，实得：{line}")
        got = db._last_audit_anchor(self.anchor)
        self.assertEqual(got["count"], 6, "v2 行必须携带链内记录数（稠密性判据的数据源）")
        self.assertEqual(got["max_id"], 6)
        self.assertEqual(got["min_id"], 1)
        self.assertEqual(got["purge_total"], 0, "未发生过留痕清理时累计删除数应为 0")
        self.assertEqual(
            got["prev_line_hash"], db._ANCHOR_GENESIS,
            "首行前驱用定长哨兵而非空串——空串会少一个 token 让整行不可解析",
        )
        self.assertEqual(got["head"], db.audit_head_hash())

    def test_consecutive_lines_are_chained(self):
        self._seed(3)
        first = db.record_audit_anchor()
        self._seed(2)
        second = db.record_audit_anchor()
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        got = db._last_audit_anchor(self.anchor)
        self.assertEqual(
            got["prev_line_hash"], _line_hash(first),
            "第二行必须哈希第一行原文，否则删除/改写历史锚点行无从检出",
        )
        self.assertEqual(got["count"], 5)

    def test_legacy_five_token_line_still_parses_and_no_false_alarm(self):
        """存量生产锚点文件（4 段 / 5 token，无 count、无行间链）必须能读且不报错。"""
        self._seed(4)
        head = db.audit_head_hash()
        legacy = f"2026-08-28 18:50:00 1 4 {head}"
        with open(self.anchor, "w", encoding="utf-8") as f:
            f.write(legacy + "\n")
        got = db._last_audit_anchor(self.anchor)
        self.assertIsNotNone(got, "旧格式行必须可读（升级后不得致盲）")
        self.assertEqual((got["min_id"], got["max_id"], got["head"]), (1, 4, head))
        self.assertIsNone(got["count"], "旧行没有 count，稠密性判据须降级而非误报")
        ok, msg = db.verify_audit_anchor()
        self.assertTrue(ok, f"仅有旧格式锚点时不得误报：{msg}")

    def test_legacy_line_then_v2_line_chains_from_legacy_text(self):
        """新旧行混排：v2 行的 prev_line_hash 取前一行原文（可为旧格式行）。"""
        self._seed(4)
        legacy = f"2026-08-28 18:50:00 1 4 {db.audit_head_hash()}"
        with open(self.anchor, "w", encoding="utf-8") as f:
            f.write(legacy + "\n")
        self._seed(1)
        new = db.record_audit_anchor()
        self.assertIsNotNone(new)
        self.assertEqual(db._last_audit_anchor(self.anchor)["prev_line_hash"], _line_hash(legacy))
        ok, msg = db.verify_audit_anchor()
        self.assertTrue(ok, msg)

    def test_unparseable_lines_are_skipped(self):
        self._seed(2)
        db.record_audit_anchor()
        with open(self.anchor, "a", encoding="utf-8") as f:
            f.write("这一行是人为写坏的垃圾内容\n")
        got = db._last_audit_anchor(self.anchor)
        self.assertEqual(got["count"], 2, "尾部垃圾行应被跳过而不是整体致盲")


class AnchorFileIntegrityTest(_DbFixture):
    """锚点文件自身的截断/改写必须可检出。"""

    def _write_chain(self):
        self._seed(3)
        db.record_audit_anchor()
        self._seed(2)
        db.record_audit_anchor()
        self._seed(1)
        db.record_audit_anchor()
        return self._anchor_lines()

    def test_clean_chain_passes(self):
        lines = self._write_chain()
        self.assertEqual(len(lines), 3)
        ok, msg = db.verify_audit_anchor()
        self.assertTrue(ok, msg)

    def test_truncating_last_anchor_line_is_detected(self):
        """删掉锚点文件最后一行：剩余行彼此仍自洽，只能靠库内指纹发现。"""
        lines = self._write_chain()
        with open(self.anchor, "w", encoding="utf-8") as f:
            f.write("\n".join(lines[:-1]) + "\n")
        ok, msg = db.verify_audit_anchor()
        self.assertFalse(ok, "锚点文件被截断必须检出（否则'删锚点末行+删审计'完全静默）")
        self.assertIn("锚点", msg)

    def test_editing_middle_anchor_line_is_detected(self):
        lines = self._write_chain()
        tampered = lines[:]
        # 按 token 改写一个数值字段（不依赖具体值，确保改动必然生效）
        parts = tampered[1].split()
        parts[3] = str(int(parts[3]) + 100)
        tampered[1] = " ".join(parts)
        self.assertNotEqual(tampered[1], lines[1], "夹具没改到内容")
        with open(self.anchor, "w", encoding="utf-8") as f:
            f.write("\n".join(tampered) + "\n")
        ok, msg = db.verify_audit_anchor()
        self.assertFalse(ok, f"改写历史锚点行必须被行间链检出：{msg}")

    def test_dropping_middle_anchor_line_is_detected(self):
        lines = self._write_chain()
        with open(self.anchor, "w", encoding="utf-8") as f:
            f.write("\n".join([lines[0], lines[2]]) + "\n")
        ok, msg = db.verify_audit_anchor()
        self.assertFalse(ok, f"删掉中间锚点行必须被行间链检出：{msg}")

    def test_missing_meta_fingerprint_does_not_false_alarm(self):
        """存量部署（app_meta 里没有锚点指纹）不得凭空判失败。"""
        self._seed(3)
        db.record_audit_anchor()
        self._raw("DELETE FROM app_meta WHERE key='audit_anchor_meta'")
        ok, msg = db.verify_audit_anchor()
        self.assertTrue(ok, f"缺指纹时降级为行间链校验，不得误报：{msg}")


class AnchorJudgmentTest(_DbFixture):
    """锚点判定三重：定点（锚点 max_id 那一行必须在且哈希对得上）、
    稠密（min..max 区间该有多少行）、留痕（物理删除必须逐次记账）。
    """

    def test_positive_control_suffix_delete_alone(self):
        """缺陷表第 1 行：删链尾 10 条、之后无写入——原本就能检出，保持不回退。"""
        self._seed(12)
        db.record_audit_anchor()
        self._raw("DELETE FROM audit_logs WHERE id > 2")
        h = db.audit_health()
        self.assertFalse(h["anchor_ok"], "删尾必须检出")
        self.assertFalse(h["healthy"])

    def test_suffix_delete_then_append_is_detected(self):
        """缺陷表第 2 行：删链尾 2 条 + 之后任意 1 条新写入。

        原判据只在 cur_max == 锚点 max_id 时才比链头，且只在 cur_max < 锚点 max_id
        时报删除——删尾后只要再写一条，max_id 反而更大，整套判据完全静默。
        """
        self._seed(6)
        db.record_audit_anchor()
        self._raw("DELETE FROM audit_logs WHERE id > 4")
        db.audit("tester", "later", "t-later", "事后写入")  # id 7
        h = db.audit_health()
        self.assertFalse(h["anchor_ok"], "删尾后追加新行不得掩盖删除（定点判据）")
        self.assertFalse(h["healthy"])
        self.assertIn("id=6", h["anchor_msg"])

    def test_middle_tamper_positive_control(self):
        """缺陷表第 5 行：改中间行内容——链内哈希校验本就检出，保持不回退。"""
        self._seed(6)
        db.record_audit_anchor()
        self._raw("UPDATE audit_logs SET detail='篡改' WHERE id=3")
        h = db.audit_health()
        self.assertFalse(h["chain_ok"])
        self.assertFalse(h["healthy"])

    def test_bulk_delete_without_provenance_is_detected(self):
        """缺陷表第 3 行：删掉 52/53 条历史（min_id 1→53），无任何清理留痕。

        原实现把 min_id 增大定性为"保留期清理的正常现象，非告警"，
        于是整段历史被抹掉也能自证清白。
        """
        self._seed(53)
        db.record_audit_anchor()
        self._raw("DELETE FROM audit_logs WHERE id <= 52")
        h = db.audit_health()
        self.assertFalse(h["anchor_ok"], "无留痕的批量删除必须检出")
        self.assertFalse(h["healthy"])
        self.assertIn("留痕", h["anchor_msg"])

    def test_anchor_truncation_plus_day_wipe_is_detected(self):
        """缺陷表第 4 行：删锚点文件最后一行 + 清空当日全部审计。"""
        self._seed(3)
        db.record_audit_anchor()
        self._seed(3)
        db.record_audit_anchor()
        self._seed(2)
        db.record_audit_anchor()  # 末行 max_id=8
        lines = self._anchor_lines()
        with open(self.anchor, "w", encoding="utf-8") as f:
            f.write("\n".join(lines[:-1]) + "\n")
        self._raw("DELETE FROM audit_logs WHERE id > 6")  # 回到倒数第二行锚点的状态
        h = db.audit_health()
        self.assertFalse(h["anchor_ok"], "删锚点末行+清当日审计必须检出")
        self.assertIn("锚点文件", h["anchor_msg"])

    # ---------------- 留痕：物理删除必须逐次记账 ----------------
    def test_audit_cleanup_records_purge_event(self):
        old_ts = "2020-01-01 00:00:00"
        conn = db.get_conn()
        for i in range(3):
            conn.execute(
                "INSERT INTO audit_logs (ts, username, action, target, detail, prev_hash, hash) "
                "VALUES (?,?,?,?,?,'','')",
                (old_ts, "tester", "old", f"t{i}", f"d{i}"),
            )
            conn.commit()
        db._rechain_audit_logs(conn)
        self._seed(2)
        conn = db.get_conn()
        db._audit_cleanup(conn)
        events = db.audit_purge_events()
        self.assertEqual(len(events), 1, "每次物理删除都要往 app_meta 写一条留痕")
        ev = events[0]
        self.assertEqual(ev["table"], "audit_logs")
        self.assertEqual(ev["deleted"], 3)
        self.assertEqual((ev["before_min"], ev["before_max"]), (1, 5))
        self.assertEqual((ev["after_min"], ev["after_max"]), (4, 5))
        self.assertIn("cutoff", ev)
        self.assertIn("ts", ev)
        self.assertEqual(db.audit_purge_total(), 3)

    def test_event_cleanup_and_user_purge_record_events(self):
        """_event_cleanup / purge_deleted_users 的物理删除同样要留痕（运维可追）。"""
        db.get_conn().execute(
            "INSERT INTO sign_events (ts, phone, status, message, stage, attempt) "
            "VALUES ('2020-01-01 00:00:00','13900000000','ok','','sign',0)"
        )
        db.get_conn().commit()
        db._event_cleanup(db.get_conn())
        kinds = {e["table"] for e in db.audit_purge_events()}
        self.assertIn("sign_events", kinds, "sign_events 的物理删除也要留痕")

        db.create_user("purge@test.local", "pw-hash", role="user")
        self._raw(
            "UPDATE users SET deleted=1, deleted_at='2020-01-01 00:00:00' "
            "WHERE email='purge@test.local'"
        )
        db.purge_deleted_users()
        kinds = {e["table"] for e in db.audit_purge_events()}
        self.assertIn("users", kinds, "注销用户物理清除也要留痕")

    def test_explained_prefix_cleanup_is_not_an_alarm(self):
        """有留痕的保留期清理：min_id 跃迁与事件累计严格相等 → 只提示，不告警。"""
        conn = db.get_conn()
        old_ts = "2020-01-01 00:00:00"
        for i in range(4):
            conn.execute(
                "INSERT INTO audit_logs (ts, username, action, target, detail, prev_hash, hash) "
                "VALUES (?,?,?,?,?,'','')",
                (old_ts, "tester", "old", f"t{i}", f"d{i}"),
            )
            conn.commit()
        db._rechain_audit_logs(conn)
        self._seed(2)
        db.record_audit_anchor()
        db._audit_cleanup(db.get_conn())  # 删掉 4 条超期旧行，并写留痕
        h = db.audit_health()
        self.assertTrue(h["anchor_ok"], f"合法清理不得告警：{h['anchor_msg']}")
        self.assertTrue(h["healthy"])
        self.assertIn("回收", h["anchor_msg"])

    def test_min_jump_larger_than_events_is_rejected(self):
        """min_id 跃迁量必须与留痕累计**严格相等**——多出来的跃迁即非法删除。"""
        conn = db.get_conn()
        old_ts = "2020-01-01 00:00:00"
        for i in range(6):
            conn.execute(
                "INSERT INTO audit_logs (ts, username, action, target, detail, prev_hash, hash) "
                "VALUES (?,?,?,?,?,'','')",
                (old_ts, "tester", "old", f"t{i}", f"d{i}"),
            )
            conn.commit()
        db._rechain_audit_logs(conn)
        self._seed(2)
        db.record_audit_anchor()
        db._audit_cleanup(db.get_conn())  # 留痕：删 6 条
        self._raw("DELETE FROM audit_logs WHERE id = 7")  # 再多删一条且无留痕
        h = db.audit_health()
        self.assertFalse(h["anchor_ok"], "跃迁量 > 留痕累计 → 必须判非法删除")

    def test_balanced_but_inconsistent_purge_record_is_rejected(self):
        """留痕**条数**对得上、但留痕声称的"删除后 min_id"与事实不符 → 只有第三条判据能检出。

        稠密性判据只做数量守恒（缺 3 条 / 声称删 3 条 → 放行）；本例数量恰好守恒，
        而声称的 after_min=99 与当前 min_id=4 对不上——即伪造或错位的清理留痕。
        """
        self._seed(8)
        db.record_audit_anchor()
        self._raw("DELETE FROM audit_logs WHERE id <= 3")  # 无留痕的真实前缀删除
        fake = [{
            "kind": "audit_cleanup", "table": "audit_logs", "cutoff": "2020-01-01 00:00:00",
            "deleted": 3, "before_min": 1, "before_max": 8,
            "after_min": 99, "after_max": 8, "ts": "2026-01-01 00:00:00", "audit_seq": 3,
        }]
        self._raw("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                  ("audit_purge_total", "3"))
        self._raw("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                  ("audit_purge_events", json.dumps(fake)))
        h = db.audit_health()
        self.assertFalse(h["anchor_ok"], "数量守恒但留痕内容与事实不符必须检出（第三条判据）")
        self.assertIn("min_id", h["anchor_msg"])

    def test_inflated_purge_records_rejected(self):
        """留痕被**超额**写入（声称删 4 条、实际只少 2 条）→ 只有稠密性判据能检出。

        本例刻意让另两条判据都满足：链尾行 id=6 还在且哈希对得上（定点通过）、
        伪造事件的 after_min=3 与当前 min_id 相等（留痕判据通过）、前缀删除后首行
        自锚所以链内哈希也自洽。超额的留痕等于攻击者预留"无痕删除额度"，必须当场判失败。
        """
        self._seed(6)
        db.record_audit_anchor()
        self._raw("DELETE FROM audit_logs WHERE id <= 2")  # 实际只删 2 条
        over = [{
            "kind": "audit_cleanup", "table": "audit_logs", "cutoff": "2020-01-01 00:00:00",
            "deleted": 4, "before_min": 1, "before_max": 6,
            "after_min": 3, "after_max": 6, "ts": "2026-01-01 00:00:00", "audit_seq": 4,
        }]
        self._raw("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                  ("audit_purge_total", "4"))
        self._raw("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                  ("audit_purge_events", json.dumps(over)))
        h = db.audit_health()
        self.assertTrue(h["chain_ok"], "夹具前提：链本身应仍自洽")
        self.assertTrue(h["anchor_ok"] is False, f"超额留痕必须被稠密性判据检出：{h['anchor_msg']}")
        self.assertIn("不符", h["anchor_msg"])

    def test_v1_anchor_does_not_false_alarm_on_min_jump(self):
        """存量 v1 锚点没有 count 字段：稠密性判据降级，但定点判据仍生效。"""
        self._seed(5)
        head = db.audit_head_hash()
        with open(self.anchor, "w", encoding="utf-8") as f:
            f.write(f"2026-08-28 18:50:00 1 5 {head}\n")
        self._raw("DELETE FROM audit_logs WHERE id <= 2")  # 无留痕的前缀删除
        ok, msg = db.verify_audit_anchor()
        self.assertTrue(ok, f"v1 锚点无法承载稠密性判据，降级为不报错：{msg}")
        # 但定点判据与格式无关：删链尾后追加照样要检出
        self._raw("DELETE FROM audit_logs WHERE id = 5")
        db.audit("tester", "later", "t", "d")
        ok, _msg = db.verify_audit_anchor()
        self.assertFalse(ok, "v1 锚点下的'删尾后追加'仍须由定点判据检出")


class RechainGuardTest(_DbFixture):
    """migrate_v3 全表重链：只允许在真正升级那一次发生，且必须留痕。"""

    def _make_v2_db(self, rows=2):
        """造一个 user_version=2、audit_logs 尚无哈希列的旧库。"""
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.remove(self.db_file + suffix)
        conn = sqlite3.connect(self.db_file)
        try:
            conn.executescript(
                "CREATE TABLE audit_logs ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,"
                "username TEXT NOT NULL, action TEXT NOT NULL,"
                "target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '');"
            )
            for i in range(rows):
                conn.execute(
                    "INSERT INTO audit_logs (ts, username, action, target, detail) "
                    "VALUES (?,?,?,?,?)",
                    (f"2026-08-16 10:0{i}:00", "admin", "account_add", f"t{i}", f"旧数据{i}"),
                )
            conn.execute("PRAGMA user_version = 2")
            conn.commit()
        finally:
            conn.close()

    def test_v2_upgrade_records_rechain_event(self):
        self._make_v2_db(rows=3)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        events = db.audit_rechain_events()
        self.assertEqual(len(events), 1, "真正从 v2 升级的重链必须留痕")
        ev = events[0]
        self.assertEqual(ev["from_version"], 2)
        self.assertEqual(ev["rows"], 3)
        self.assertEqual(ev["empty_hash_rows"], 3)
        self.assertEqual(ev["head_before"], "", "重链前全表 hash 为空")
        self.assertEqual(ev["head_after"], db.audit_head_hash())
        self.assertIn("ts", ev)
        ok, broken, _first = db.verify_audit_chain()
        self.assertTrue(ok, f"回填后链应自洽: broken={broken}")

    def test_no_rechain_event_when_nothing_to_sign(self):
        """没有空 hash 行就不许留重链痕——否则"重链留痕"会失去指认伪造的能力。"""
        self._seed(3)
        self.assertEqual(db.audit_rechain_events(), [])
        db.init_db(self.db_file, cleanup=False)  # 已迁移库重启：不应产生事件
        self.assertEqual(db.audit_rechain_events(), [])

    def test_rechain_event_after_anchor_is_unhealthy(self):
        """锚点之后出现全表重链：合法升级必然发生在任何锚点之前。"""
        self._seed(4)
        db.record_audit_anchor()
        forged = [{
            "ts": "2099-01-01 00:00:00", "from_version": 2, "rows": 4,
            "empty_hash_rows": 4, "head_before": "", "head_after": "f" * 64,
        }]
        db.set_meta(db._RECHAIN_EVENTS_KEY, json.dumps(forged))
        h = db.audit_health()
        self.assertTrue(h["anchor_ok"], "夹具前提：锚点判据本身应仍通过")
        self.assertFalse(h["healthy"], "锚点之后的重链事件必须让体检判失败")
        self.assertEqual(h["rechain_events"], forged)
        self.assertIn("重链", h["note"])

    def test_unhealthy_but_all_green_still_explains_itself(self):
        """这种"各项都正常却报警"的体检结果，告警正文必须自带原因。

        每日线程把 `audit_health` 摊成事实清单发管理员邮件；链自洽与库外锚点两行
        都显示"正常"时，唯一说得出为什么报警的就是 `note`。漏掉它，管理员看到的
        就是一条看着像误报的告警（真出问题时第一反应是忽略）。
        """
        self._seed(4)
        db.record_audit_anchor()
        db.set_meta(db._RECHAIN_EVENTS_KEY, json.dumps([{
            "ts": "2099-01-01 00:00:00", "from_version": 2, "rows": 4,
            "empty_hash_rows": 4, "head_before": "", "head_after": "f" * 64,
        }]))
        h = db.audit_health()
        self.assertFalse(h["healthy"])
        import web.app as webapp  # 惰性：本文件其余用例只碰 db，不加载 web
        facts = dict(webapp._audit_alert_facts(h))
        self.assertEqual(facts["链自洽"], "是", "夹具前提：链本身仍自洽")
        self.assertEqual(facts["库外锚点"], "一致", "夹具前提：锚点判据仍通过")
        self.assertIn("全表重链", facts["诊断备注"], "正文必须写明这次报警的原因")
        self.assertEqual(len(facts["诊断备注"].splitlines()), 1, "日志行必须保持单行")

    def test_runtime_empty_hash_rows_are_reported(self):
        """运行期出现 hash='' 行（清空哈希等着被重签）→ 体检须给出可诊断信息。"""
        self._seed(5)
        db.record_audit_anchor()
        self._raw("UPDATE audit_logs SET hash='', prev_hash='' WHERE id=3")
        h = db.audit_health()
        self.assertFalse(h["chain_ok"], "空 hash 行必须断链")
        self.assertFalse(h["healthy"], "空 hash 行必须参与 healthy 结论（不能只是诊断信息）")
        self.assertEqual(h["empty_hash_rows"], 1)
        self.assertIn("hash 为空", h["note"])


class WriteDebtTest(_DbFixture):
    """审计写入欠账必须落库：进程内计数器重启即归零，等于把"有操作未留痕"忘掉。"""

    def _force_one_failure(self):
        real = db._audit_hash

        def _boom(*_a, **_kw):
            raise RuntimeError("模拟审计写入失败")

        db._audit_hash = _boom
        try:
            self.assertFalse(db.audit("tester", "unit_test", "t", "should fail"))
        finally:
            db._audit_hash = real

    def _simulate_process_restart(self):
        """清掉进程内状态并重开连接——真正的重启只剩库里那份数据。"""
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        db._reset_audit_fail_memory()

    def test_write_debt_is_persisted_and_monotonic(self):
        self._force_one_failure()
        self.assertEqual(db.audit_write_failures(), 1)
        self.assertEqual(db.audit_persisted_write_failures(), 1, "欠账必须落 app_meta")
        self._force_one_failure()
        self._simulate_process_restart()
        self.assertEqual(
            db.audit_write_failures(), 2,
            "重启后仍须看得见累计欠账——原实现进程内计数重启归零，未留痕的操作就此忘掉",
        )
        self.assertEqual(db.audit_persisted_write_failures(), 2)

    def test_write_debt_makes_health_unhealthy(self):
        self._seed(2)
        db.record_audit_anchor()
        self.assertTrue(db.audit_health()["healthy"])
        self._force_one_failure()
        h = db.audit_health()
        self.assertEqual(h["write_failures"], 1)
        self.assertFalse(h["healthy"], "有欠账即不健康（既有行为保持）")
        self._simulate_process_restart()
        self.assertFalse(db.audit_health()["healthy"], "重启后依旧不得洗白")

    def test_write_debt_does_not_leak_across_databases(self):
        """欠账是"这个库"的事实：换库必须归零，否则测试/多实例会互相污染。"""
        self._force_one_failure()
        self.assertEqual(db.audit_write_failures(), 1)
        self._simulate_process_restart()
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.remove(self.db_file + suffix)
        db.init_db(cleanup=False)
        self.assertEqual(db.audit_write_failures(), 0, "全新库不应继承旧库的欠账")


class AuditVerifyCliTest(_DbFixture):
    """scripts/audit_verify.py 必须真的比对锚点（README 早已承诺这一点）。"""

    @staticmethod
    def _out(r):
        """子进程输出解码。Windows 上子进程管道 stdout 用本地 ANSI 代码页（本项目
        为 GBK），一律按 utf-8 解码会得到乱码而误判失败——仓库既有的 backup.sh /
        scheduler 用例正因此干脆不断言中文输出。"""
        enc = locale.getpreferredencoding(False) or "utf-8"
        try:
            return r.stdout.decode(enc)
        except (LookupError, UnicodeDecodeError):
            return r.stdout.decode("utf-8", errors="replace")

    def _run(self, extra=(), db_file=None):
        env = dict(os.environ)
        env["YIBAN_DB_FILE"] = db_file or self.db_file
        env["YIBAN_ENV_FILE"] = self.env_file
        env["YIBAN_STATE_DIR"] = self.tmp
        return subprocess.run(
            [sys.executable, os.path.join(BASE, "scripts", "audit_verify.py"), *extra],
            capture_output=True, env=env, cwd=BASE,
        )

    def test_cli_exit_zero_when_healthy(self):
        self._seed(3)
        db.record_audit_anchor()
        r = self._run()
        self.assertEqual(r.returncode, 0, self._out(r))

    def test_cli_detects_anchor_only_tampering(self):
        """链自洽但锚点判据失败（删尾后追加）——CLI 必须照样 exit 1。"""
        self._seed(6)
        db.record_audit_anchor()
        self._raw("DELETE FROM audit_logs WHERE id > 4")
        db.audit("tester", "later", "t", "d")
        self.assertTrue(db.verify_audit_chain()[0], "夹具前提：链本身仍自洽")
        r = self._run()
        self.assertEqual(r.returncode, 1, self._out(r))
        self.assertIn("锚点", self._out(r))

    def test_cli_accepts_explicit_anchor_path(self):
        self._seed(3)
        other = os.path.join(self.tmp, "custom-anchor.log")
        db.record_audit_anchor(other)
        r = self._run(["--anchor", other])
        self.assertEqual(r.returncode, 0, self._out(r))
        self.assertIn(os.path.normpath(other), self._out(r))
        # 默认路径此刻并不存在锚点：显式 --anchor 必须优先于默认解析
        r2 = self._run()
        self.assertEqual(r2.returncode, 1, self._out(r2))

    def test_cli_refuses_missing_db(self):
        """三条只读纪律之一：库不存在时 exit 2，绝不新建空库把"无篡改"误报成通过。"""
        r = self._run(db_file=os.path.join(self.tmp, "nope.db"))
        self.assertEqual(r.returncode, 2)


class BackupScriptContractTest(unittest.TestCase):
    """备份脚本的取证契约（文本级断言，与 tests/test_backup_require_encrypt.py 同口径：
    脚本含中文输出，Windows 子进程按 GBK 解码 stdout 会误报，故不跑子进程，只做
    源码级断言 + `bash -n` 语法核验）。
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BASE, "scripts", "backup.sh"), encoding="utf-8") as f:
            cls.src = f.read()
        with open(os.path.join(BASE, "docker", "backup-docker.sh"), encoding="utf-8") as f:
            cls.docker_src = f.read()

    def _block(self, start_marker, end_marker):
        return self.src[self.src.index(start_marker):self.src.index(end_marker)]

    def test_cp_fallback_must_pass_integrity_check(self):
        """(a) .backup 失败回退 cp 后必须 integrity_check，不通过就不落归档 + 非 0 退出。"""
        block = self._block("警告：sqlite3 .backup 失败", "# 2) 密钥：优先")
        self.assertIn("verify_db_snapshot", block, "cp 回退必须跑 integrity_check")
        self.assertIn("rm -f \"${TMPDIR_BAK}/data/${DB_FILE}\"", block,
                      "校验不过必须删掉坏快照（不落该归档）")
        self.assertIn("exit 1", block, "校验不过必须以非 0 退出")
        self.assertIn("integrity_check", self._block("verify_db_snapshot()", "if [ -f \"${APP_DIR}/${DB_FILE}\" ]"))

    def test_corrupt_source_keeps_archive_but_exits_nonzero(self):
        """.backup 成功但 integrity 不过 = 源库损坏：归档照留（最后一份素材），退出码非 0。"""
        self.assertIn("CORRUPT_SOURCE=1", self.src)
        tail = self.src[self.src.index("if [ \"${CORRUPT_SOURCE:-0}\" -eq 1 ]"):]
        self.assertIn("exit 4", tail)

    def test_manifest_includes_anchor_and_gate_files(self):
        """(b) 备份清单必须含外部锚点与闸门/账本状态文件。

        切片只取 state_files=( ... ) 数组本体：范围放宽到后面的日志文案就会
        把"未发现审计锚点"那句也圈进来，从数组里删掉条目照样通过（突变验证暴露）。
        """
        start = self.src.index("state_files=(")
        arr = self.src[start:self.src.index("\n    )", start)]
        for name in ("audit-anchor.log", "sched-run-*.json", "sched-snapshot-*.json",
                     "notify-ledger.json", "notify-throttle.json"):
            self.assertIn('"${SIGN_STATE_DIR}"/' + name, arr,
                          f"{name} 不在备份数组里——恢复后该类状态静默丢失")

    def test_retention_covers_sha256_sidecars(self):
        """(c) 清理 glob 必须覆盖 .sha256 侧车（否则无限堆积并泄露每日归档清单）。"""
        self.assertIn("-name 'yiban-*.sha256'", self.src)

    def test_restore_section(self):
        """(d) --restore 必须有停服提示、删残留 -wal/-shm、恢复锚点、双验。"""
        block = self._block('restore() {', 'if [ "${1:-}" = "--restore" ]')
        self.assertIn("systemctl stop yiban-web", block, "缺停服提示")
        self.assertIn("-wal", block)
        self.assertIn("-shm", block)
        self.assertIn("PRAGMA integrity_check", block, "恢复后必须核验完整性")
        self.assertIn("audit_verify.py", block, "恢复后必须校验审计链与锚点")
        self.assertIn("audit-anchor.log", block, "必须说明锚点要与库同批次落位")
        self.assertIn("return \"$rc\"", block, "核验结论必须传出去（不得无条件报恢复成功）")

    def test_cron_template_requires_encrypt_and_daily_verify(self):
        """(e) cron 模板带 --require-encrypt，并追加每日 audit_verify 跑。"""
        header = self.src[:self.src.index("# 依赖：")]
        self.assertIn("yiban-backup.sh --require-encrypt", header,
                      "cron 模板不带 --require-encrypt 时，加密失效当天会静默产出明文归档")
        self.assertIn("audit_verify.py", header, "锚点判据不能只挂在 web 每日线程上")
        self.assertNotIn("docs/web-console/DEPLOY-CHECKLIST.md", self.src,
                         "引用了仓库里不存在的部署清单")

    def test_docker_backup_asserts_anchor_in_archive(self):
        self.assertIn("audit-anchor", self.docker_src)
        self.assertIn("ANCHOR_IN_ARCHIVE", self.docker_src, "锚点入包必须是显式断言而非假设")


TRACK_SALT = "read-audit-salt-0123456789"


ADMIN_PASS = "Master-Test-2026!"


SUB_PASS = "Subadmin-Test-2026!"


OWNER = "student@example.cn"


SUB_ADMIN = "sub.admin@example.cn"


N_ACCOUNTS = 70


def _phone(i):
    return f"138{i:08d}"


def _masked(i):
    return f"138****{i:04d}"


def _owner(i):
    return f"user{i}@example.cn"


_READ_ACTIONS = ("account_detail_read", "account_detail_denied", "users_list_read",
                 "users_deleted_read", "logs_read", "sign_events_read")


class ReadAuditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-read-audit-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                    "YIBAN_ADMIN_USER=admin\n"
                    f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        for k, v in {
            "YIBAN_ACCOUNTS_KEY": TEST_KEY, "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_ACCOUNTS_FILE": cls.accounts_file, "YIBAN_TRACK_SALT": TRACK_SALT,
            "YIBAN_USERS_FILE": os.path.join(cls.tmp, "users.json"),
            "YIBAN_DB_FILE": cls.db_file, "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_LOG_FILE": os.path.join(cls.tmp, "sign.log"),
            "YIBAN_DISABLE_PURGE_LOOP": "1",
        }.items():
            os.environ[k] = v
        global db
        import db
        spec = importlib.util.spec_from_file_location(
            "webapp_read_audit", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_read_audit"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_LOG_FILE", "YIBAN_TRACK_SALT", "YIBAN_DISABLE_PURGE_LOOP"):
            os.environ.pop(k, None)

    def setUp(self):
        from werkzeug.security import generate_password_hash
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        with open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        db.init_db(db_file=self.db_file, migrate_from=self.accounts_file,
                   env_file=self.env_file)
        db.create_user(OWNER, generate_password_hash("Stu-Test-2026!",
                       method=self.webapp.SCRYPT_METHOD),
                       role="user", created_at="2026-09-18 00:00:00", pw_version=1)
        db.create_user(SUB_ADMIN, generate_password_hash(SUB_PASS,
                       method=self.webapp.SCRYPT_METHOD),
                       role="admin", created_at="2026-09-18 00:00:00", pw_version=1)
        # 库里的账号数必须真的多过 DETAIL_MAX，"递增 idx 枚举整库会被挡住"这件事
        # 才是被演示出来的而不是被假设出来的。每用户限一个未删除账号（DB 层唯一
        # 约束），故一个账号配一个归属用户；这些 filler 用户永不登录，口令哈希
        # 用一轮 pbkdf2 即可（scrypt 每轮 ~100ms，70 个会把用例拖成摆设）。
        cheap = generate_password_hash("Filler-1234!", method="pbkdf2:sha256:1")
        for i in range(N_ACCOUNTS):
            db.create_user(_owner(i), cheap, role="user",
                           created_at="2026-09-18 00:00:00", pw_version=1)
            db.add_account({"name": f"号{i}", "phone": _phone(i), "password": "Pass1234!",
                            "phone_model": "", "phone_code": "", "owner": _owner(i),
                            "status": self.webapp.ACCOUNT_STATUS_ACTIVE})
        # 每个用例新建 app：限速与聚合计数都是 create_app 内的闭包字典，互不污染
        self.app = self.webapp.create_app()

    # ---- 助手 ----
    def _login(self, user, pw):
        c = self.app.test_client()
        r = c.post("/api/login", json={"username": user, "password": pw})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c

    def _sub(self):
        return self._login(SUB_ADMIN, SUB_PASS)

    def _master(self):
        return self._login("admin", ADMIN_PASS)

    def _rows(self, *actions):
        conn = db.get_conn()
        marks = ",".join("?" * len(actions))
        return conn.execute(
            f"SELECT username, action, target, detail FROM audit_logs "
            f"WHERE action IN ({marks}) ORDER BY id", actions).fetchall()

    def _read_rows(self):
        return self._rows(*_READ_ACTIONS)

    # ---- 聚合判据本体（突变验证的靶子：改成逐条这里就红）----
    def test_row_due_is_aggregated_not_per_request(self):
        due = self.webapp._read_audit_row_due
        self.assertTrue(due(1), "首次读取必须留痕（不能等窗口关闭才写）")
        for cnt in range(2, 10):
            self.assertFalse(due(cnt), f"窗口内第 {cnt} 次不该各写一行")
        self.assertTrue(due(10) and due(50), "批量档位要补行，否则看不出读了多少")
        self.assertFalse(due(51))
        self.assertTrue(due(200) and due(400))
        self.assertFalse(due(201))

    # ---- ① 连读 50 个详情：有限几行，不是 50 行 ----
    def test_fifty_detail_reads_leave_a_few_rows_not_fifty(self):
        c = self._sub()
        for i in range(50):
            r = c.get(f"/api/accounts/{i}/detail")
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        rows = self._rows("account_detail_read")
        self.assertEqual(len(rows), 3, f"聚合口径应为档位 1/10/50 三行，实得 {len(rows)}")
        self.assertNotEqual(len(rows), 50, "读审计绝不允许逐请求一行")
        self.assertTrue(all(r["username"] == SUB_ADMIN for r in rows),
                        "actor 必须是真实会话用户名")
        last = rows[-1]["detail"]
        self.assertIn("50", last, "末行要带窗口内累计次数，否则看不出被读了多少")
        self.assertIn(_masked(0), last, "detail 里应是被读目标的脱敏标识")
        self.assertNotIn(_phone(7), last, "审计里不得出现完整手机号")
        self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", rows[0]["target"]),
                        "target 走 db.hash_ip 口径（与 forbidden_path 同源）")

    # ---- ② 整库枚举式读：429，且文案不泄露阈值 ----
    def test_enumerating_every_detail_hits_429(self):
        c = self._sub()
        statuses = [c.get(f"/api/accounts/{i % N_ACCOUNTS}/detail").status_code
                    for i in range(N_ACCOUNTS)]
        self.assertIn(429, statuses, "idx 递增枚举整库必须被限速挡住")
        limit = self.webapp.DETAIL_MAX
        self.assertEqual(statuses[:limit], [200] * limit, "限额内不得误伤正常运维")
        self.assertEqual(statuses[limit], 429)
        body = c.get("/api/accounts/0/detail").get_json()["error"]
        self.assertFalse(re.search(r"\d", body), f"429 文案不该自报内部阈值数字：{body}")

    # ---- ④ 被拒的 429 不逐条写审计 ----
    def test_denied_detail_reads_do_not_write_one_row_each(self):
        c = self._sub()
        denied = 0
        for i in range(N_ACCOUNTS + 40):
            if c.get(f"/api/accounts/{i % N_ACCOUNTS}/detail").status_code == 429:
                denied += 1
        self.assertGreater(denied, 40, "本用例要真的打出一批 429")
        rows = self._rows("account_detail_denied")
        self.assertEqual(len(rows), 1,
                         f"拒绝面每窗口只许一行（实得 {len(rows)} 行 / {denied} 次被拒）")
        self.assertLess(len(self._read_rows()), 10,
                        "读面留下的总行数必须是有限几行，不能跟着请求数线性长")

    # ---- 其余四个只读接口同样按窗口聚合 ----
    def test_list_reads_aggregate_per_window(self):
        c = self._sub()
        for path, action in (("/api/users", "users_list_read"),
                             ("/api/users/deleted", "users_deleted_read"),
                             ("/api/logs", "logs_read"),
                             ("/api/admin/sign-events", "sign_events_read")):
            for _ in range(10):
                self.assertEqual(c.get(path).status_code, 200, path)
            rows = self._rows(action)
            self.assertEqual(len(rows), 2, f"{path} 应为档位 1/10 两行，实得 {len(rows)}")
            self.assertNotEqual(len(rows), 10, f"{path} 不得逐请求一行")
            self.assertIn("10", rows[-1]["detail"], f"{path} 末行要能看出累计次数")

    # ---- 详情面之外的读也不许把明文号带进审计 ----
    def test_read_audit_rows_never_carry_raw_phones(self):
        c = self._sub()
        for i in range(5):
            c.get(f"/api/accounts/{i}/detail")
        c.get("/api/admin/sign-events", query_string={"phone": _phone(3)})
        self.assertTrue(self._read_rows(), "读审计必须真的落了行，否则本用例是空跑")
        for row in self._read_rows():
            self.assertIsNone(re.search(r"1[3-9]\d{9}", row["detail"]),
                              f"读审计的 detail 泄漏了完整手机号：{row['detail']}")

    # ---- ③ 额度勘察字段仅主管理员；通道状态字段全体管理员可见 ----
    def test_quota_fields_only_for_builtin_admin(self):
        with open(self.env_file, "a", encoding="utf-8") as f:
            f.write("YIBAN_NOTIFY_COOLDOWN=77\n"
                    "YIBAN_NOTIFY_DAILY_MAX=9\n"
                    "YIBAN_NOTIFY_URGENT_DAILY_MAX=3\n")
        master = self._master().get("/api/notify-config").get_json()
        self.assertEqual(master["cooldown"], 77)
        self.assertEqual(master["urgent_daily_max"], 3)
        self.assertIsNotNone(master["daily_remaining"])
        self.assertIsNotNone(master["urgent_daily_remaining"])

        sub = self._sub().get("/api/notify-config").get_json()
        self.assertEqual(master["quota_visible"], True)
        self.assertEqual(sub["quota_visible"], False,
                         "余量的 null 原意是「不限」，无权查看必须靠这个标记区分")
        for key in self.webapp._NOTIFY_QUOTA_HIDDEN_KEYS:
            self.assertIn(key, sub, "刻意置 null 而非省键：响应形态必须稳定")
            self.assertIsNone(sub[key], f"{key} 属额度勘察字段，普通管理员不该看到")
        self.assertEqual(set(sub), set(master), "两套响应的键集合必须一致")
        # 日常运维要的信息一个字都不能少
        self.assertEqual(sub["daily_max"], master["daily_max"])
        self.assertEqual(sub["urgent_only"], master["urgent_only"])
        # 规则值（上限与节流）属"看得懂规则才能运维"，不是勘察面——两边同值
        self.assertEqual(sub["cooldown"], master["cooldown"])
        self.assertEqual(sub["urgent_daily_max"], master["urgent_daily_max"])
        for key in ("ok", "enabled", "type", "configured", "secret_masked"):
            self.assertIn(key, sub)
        self.assertEqual(sub["daily_max"], 9, "上限本身仍是配置读数，保持可见")

    # ---- 邮件侧：本就没有额度字段可分层，通道状态字段照旧可见 ----
    def test_mail_config_state_fields_visible_to_registered_admin(self):
        sub = self._sub().get("/api/mail-config").get_json()
        self.assertTrue(sub["ok"])
        for key in ("enabled", "admin_notify", "smtp_host", "smtp_port",
                    "user", "admin_to", "smtps"):
            self.assertIn(key, sub, "普通管理员必须看得到邮件通道开没开、配没配")
        self.assertFalse([k for k in sub if k in self.webapp._NOTIFY_QUOTA_HIDDEN_KEYS],
                         "邮件侧没有这两个余量键可分层（发送路径不受推送每日条数与节流约束）")


if __name__ == "__main__":
    unittest.main()
