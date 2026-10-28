# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""锚点自检的两条持久化不变量：指纹缺失不得静默、锚点快照必须取自同一事务。

两条都对应 2026-10-20 复核的工单：

- `ba-p01-04`：库内指纹（app_meta `audit_anchor_meta`）缺失时，旧实现让行数三支与
  末行哈希支整段跳过、直接返回 ok。指纹缺失并不罕见——`_record_anchor_trace` 写
  app_meta 失败只记 WARNING（且明说"不影响锚点本身"）。此时行间链只保证"历史行彼此
  自洽"，对"截掉末行再补一条自洽的新末行"无效，锚点防线悄悄回到没有指纹之前的强度。
  修法：`recorded == 0` 且锚点文件非空时判 indeterminate（由 `audit_health` 判不健康），
  下一次成功写锚点即自愈。
- `ba-p01-05`：`record_audit_anchor` 的 purge_total / MIN-MAX-COUNT / 链头是同一连接上的
  三条独立 SELECT，只持进程内 `_conn_lock`。跨进程的合法保留期清理在两次读之间提交时，
  锚点行把它记成"count 少了 k 行、留痕累计没变"，下一次校验落入 `missing < exp` 一支，
  把一次合法清理判成"留痕被伪造"，占掉紧急告警额度。
  修法：三读取进同一只读事务（BEGIN）后再落锚点行。

标签：G · 安全：脱敏/审计/配置注入
覆盖：`_anchor_status` / `_anchor_file_state_ex` / `audit_health` 对"库内指纹缺失"的结论；
`record_audit_anchor` 三读的同一快照性（用另一条连接在两次读之间提交真实清理）。
对应实现：`yiban/adapters/persistence/audit_chain.py` 的 `_anchor_file_state_ex` /
`_anchor_meta_line_count` / `record_audit_anchor`。
关键断言：缺指纹 ⇒ indeterminate 且 healthy=False（≠ ok）；同一配置下一次成功写锚点
必须自愈（否则"收紧"就变成了永久误报）。清理落在两次读之间时，锚点行的 count 与
purge_total 必须描述同一时刻，且随后 `verify_audit_anchor` 仍为 ok。
依赖：临时库 + 临时锚点文件；并发清理用**另一条 sqlite 连接**真实提交，不用 sleep。
"""
import contextlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
AUDIT_KEY = "b" * 64

_PURGE_TOTAL_KEY = "audit_purge_total"
_PURGE_EVENTS_KEY = "audit_purge_events"
_ANCHOR_META_KEY = "audit_anchor_meta"


class _Fixture(unittest.TestCase):
    """临时库 + 临时锚点文件；每个用例重建。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-anchor-hardening-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\nYIBAN_AUDIT_KEY={AUDIT_KEY}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls._old = {}
        for k, v in (("YIBAN_ACCOUNTS_KEY", TEST_KEY), ("YIBAN_AUDIT_KEY", AUDIT_KEY),
                     ("YIBAN_ENV_FILE", cls.env_file), ("YIBAN_DB_FILE", cls.db_file),
                     ("YIBAN_ACCOUNTS_FILE", os.path.join(cls.tmp, "accounts.json")),
                     ("YIBAN_STATE_DIR", cls.tmp)):
            cls._old[k] = os.environ.get(k)
            os.environ[k] = v
        global db
        import db

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for k, v in cls._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
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
        with contextlib.suppress(OSError):
            os.remove(self.anchor)
        db.init_db(cleanup=False)

    def _seed(self, n):
        for i in range(n):
            db.audit("tester", "seed", f"t{i}", f"d{i}")

    def _raw(self, sql, args=()):
        with db._conn_lock:
            conn = db.get_conn()
            cur = conn.execute(sql, args)
            conn.commit()
            return cur

    def _last_anchor_fields(self):
        with open(self.anchor, encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        parts = lines[-1].split()
        return {"min_id": int(parts[-6]), "max_id": int(parts[-5]),
                "count": int(parts[-4]), "purge_total": int(parts[-3]),
                "head": parts[-2], "prev": parts[-1]}


class MissingFingerprintIsIndeterminateTest(_Fixture):
    """`ba-p01-04`：库内指纹缺失时锚点自检必须判"无法定论"，不得返回 ok。"""

    def test_missing_fingerprint_with_nonempty_file_is_indeterminate(self):
        self._seed(3)
        db.record_audit_anchor(self.anchor)
        self.assertEqual(db._anchor_status(self.anchor)[0], "ok", "前置：指纹在时应为 ok")
        self._raw("DELETE FROM app_meta WHERE key=?", (_ANCHOR_META_KEY,))

        status, msg = db._anchor_status(self.anchor)
        self.assertEqual(status, "indeterminate",
                         "库内指纹缺失时锚点文件仍可被追加/裁剪，必须判无法定论：" + msg)
        ok, vmsg = db.verify_audit_anchor(self.anchor)
        self.assertFalse(ok, "缺指纹不是通过")
        self.assertTrue(vmsg.startswith("无法定论："), vmsg)
        h = db.audit_health(self.anchor)
        self.assertFalse(h["healthy"], "缺指纹必须让综合体检不健康")
        self.assertEqual(h["anchor_status"], "indeterminate")

    def test_missing_fingerprint_is_not_treated_as_tamper(self):
        """缺指纹是"没验成"而不是"确证篡改"——两档的处置方向不同。"""
        self._seed(2)
        db.record_audit_anchor(self.anchor)
        self._raw("DELETE FROM app_meta WHERE key=?", (_ANCHOR_META_KEY,))
        status, _ = db._anchor_status(self.anchor)
        self.assertNotEqual(status, "tampered")

    def test_next_anchor_write_restores_health(self):
        """自愈：下一次成功写锚点会补回指纹，不得把一次留痕失败变成永久误报。"""
        self._seed(3)
        db.record_audit_anchor(self.anchor)
        self._raw("DELETE FROM app_meta WHERE key=?", (_ANCHOR_META_KEY,))
        self.assertEqual(db._anchor_status(self.anchor)[0], "indeterminate")
        self.assertIsNotNone(db.record_audit_anchor(self.anchor))
        self.assertEqual(db._anchor_status(self.anchor)[0], "ok")
        self.assertTrue(db.verify_audit_anchor(self.anchor)[0])

    def test_empty_file_without_fingerprint_stays_not_a_failure(self):
        """空锚点文件没有可保护的历史行，不因缺指纹凭空判红（首次运行的口径不变）。"""
        status, _ = db._anchor_file_state_ex(self.anchor, lines=[], meta={})
        self.assertEqual(status, "ok")


class AnchorSnapshotConsistencyTest(_Fixture):
    """`ba-p01-05`：三读取必须来自同一快照，跨进程清理不得把锚点写成自相矛盾。"""

    def _purge_on_other_connection(self, keep_from_id):
        """在另一条连接上提交一次**合法**保留期清理（删除 + 留痕同事务）。"""
        other = sqlite3.connect(self.db_file)
        try:
            before = other.execute(
                "SELECT MIN(id), MAX(id) FROM audit_logs").fetchone()
            cur = other.execute("DELETE FROM audit_logs WHERE id < ?", (keep_from_id,))
            deleted = cur.rowcount
            after = other.execute(
                "SELECT MIN(id), MAX(id) FROM audit_logs").fetchone()
            row = other.execute(
                "SELECT value FROM app_meta WHERE key=?", (_PURGE_EVENTS_KEY,)).fetchone()
            events = json.loads(row[0]) if row and row[0] else []
            row = other.execute(
                "SELECT value FROM app_meta WHERE key=?", (_PURGE_TOTAL_KEY,)).fetchone()
            total = int(row[0]) if row and row[0] else 0
            events.append({
                "kind": "audit_cleanup", "table": "audit_logs", "cutoff": "2020-01-01 00:00:00",
                "deleted": deleted,
                "before_min": before[0], "before_max": before[1],
                "after_min": after[0], "after_max": after[1],
                "ts": "2026-09-18 20:00:00", "audit_seq": total + deleted,
            })
            other.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                          (_PURGE_EVENTS_KEY, json.dumps(events, ensure_ascii=False)))
            other.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                          (_PURGE_TOTAL_KEY, str(total + deleted)))
            other.commit()
            return deleted
        finally:
            other.close()

    def test_legal_purge_between_reads_does_not_poison_anchor(self):
        self._seed(10)
        db.record_audit_anchor(self.anchor)   # 基线：10 行
        real_total = db._audit_purge_total
        seen = {}

        def _inject(conn_arg):
            out = real_total(conn_arg)
            # 模拟跨进程清理恰在"读计数器之后、取行快照之前"提交
            seen["deleted"] = self._purge_on_other_connection(keep_from_id=4)
            return out

        db._audit_purge_total = _inject
        try:
            line = db.record_audit_anchor(self.anchor)
        finally:
            db._audit_purge_total = real_total
        self.assertIsNotNone(line)
        self.assertEqual(seen["deleted"], 3, "前置：真实清理必须删掉 3 行")

        fields = self._last_anchor_fields()
        self.assertIn(
            (fields["count"], fields["purge_total"]), {(10, 0), (7, 3)},
            "锚点行的 count 与 purge_total 必须描述同一时刻：要么都在清理前"
            "（10/0），要么都在清理后（7/3）；旧实现记成清理后行数 + 清理前累计"
            "（7/0），下一次校验会落入「留痕比消失多」的伪篡改分支",
        )

        ok, msg = db.verify_audit_anchor(self.anchor)
        self.assertTrue(ok, f"一次合法保留期清理不得被判成篡改：{msg}")
        self.assertIn("回收", msg, "前缀回收应作为正常现象提示")


if __name__ == "__main__":
    unittest.main(verbosity=2)
