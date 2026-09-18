# -*- coding: utf-8 -*-
"""审计链可追回性（删尾后追加 / 无留痕批量删除 / 锚点文件被截断）。

既有 tests/test_audit_anchor.py 只覆盖"锚点比 max_id"这一种判据，本文件按
**攻击者视角**逐条复现锚点致盲路径，并钉住新的判定口径：

- 锚点行 v2 携带 `count` 与 `prev_line_hash`（上一行原文哈希），锚点文件自身
  成链——改任一行、删中间一行可被链内检出；
- app_meta 另存"最近一次锚点的行数 + 末行哈希"指纹，删掉末行（链自洽、无后继
  行可对照）也能检出；
- 存量 4 段（实际 5 token）旧格式行仍可读且不因新判据误报。

用法：
    py -m pytest tests/test_audit_tamper_94.py -v
"""
import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest

TEST_KEY = "a" * 64
AUDIT_KEY = "b" * 64


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
