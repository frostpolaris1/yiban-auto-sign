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
import os
import shutil
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
