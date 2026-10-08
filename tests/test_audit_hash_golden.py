# -*- coding: utf-8 -*-
"""审计链金样例：跨语言复现的定点契约。

**为什么需要**
- 既有两处校验用生产函数算期望值。
- 那是往返自证。Python 与 Go 各写一份，两侧各自绿，互验必断。
- 本文件把载荷口径固化成字面量 hex64。
- 字段序、分隔符、转义口径任何一处变动，金样例立刻变红。

**hex64 的来源**
- 一次性脚本独立算出：`work/gen_audit_golden.py`。
- 该脚本不导入本仓模块。它用 hashlib / hmac / json 直接拼载荷。
- 固定密钥经环境变量 `YIBAN_AUDIT_KEY` 注入。
- `_audit_key()` 先读环境变量，再回落 .env 与进程缓存。注入必定生效。

**覆盖**
- `GOLDEN_HASHES` 五条：纯 ASCII 基线 / 中文 detail / 恰好 200 字符 detail /
  转义字符与星平面字符 / JSON 结构 detail。
- `GOLDEN_ANCHOR_LINE` 一条：v2 锚点行原文 + 它的 sha256 + 字段解析结果。

**边界**
- 本文件不改生产行为。它只读生产代码。
- 它不改既有往返自证用例（test_db_integrity / test_audit_anchor_field_source）。
- 它只覆盖 `_TS_FMT`。别处的同名格式串属于别的工单。
"""
import contextlib
import os
import re
import shutil
import tempfile
import unittest

from yiban.store import audit_chain, db

# 固定审计密钥：bytes(range(32))。任何环境都能复现。
AUDIT_KEY_HEX = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"

#: 本次框定的格式串。`audit_chain` 内只允许出现一次（见 TsFormatSingleSourceTest）。
TS_LITERAL = "%Y-%m-%d %H:%M:%S"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

#: (样例名, `_audit_hash` 六元入参, 独立算出的 hex64)
#: 入参顺序 = [_audit_hash] 的签名：prev_hash / ts / username / action / target / detail。
GOLDEN_HASHES = (
    (
        "ascii_genesis",
        ("", "2026-01-02 03:04:05", "admin", "account_add", "13800000000", "ascii baseline"),
        "7876dbd78288c626775deb0f8994a2d1a909f6dabf7f93de632713436a23f7d9",
    ),
    (
        "chinese_detail",
        ("f" * 64, "2026-01-02 03:04:06", "管理员甲", "导出报表", "报表-2026Q1",
         "中文细节:批量导出(逗号,全角标点)"),
        "30e518445fb272c591fa9dfd0bb0b6054ab54eddf38ad5b2a9fc66852b9727dc",
    ),
    (
        "boundary_200",
        ("ab" * 32, "2026-01-02 03:04:07", "admin", "capacity_guard", "200", "审计边界" * 50),
        "f7a10c8ca41ee88bd9c695be2dc0a94c79e3a04294ce97b612f7af231646fb82",
    ),
    (
        "escape_astral",
        ("b" * 64, "2026-01-02 03:04:08", "admin", "config_set", "mail",
         "引号\" 反斜杠\\ 换行\n 制表\t 星😀"),
        "09d1808eb154e8a7ad63f53650a9c1fa35fd71d68cc7e0b0c40a47823ce8bae5",
    ),
    (
        "json_detail",
        ("c" * 64, "2026-01-02 03:04:09", "admin", "config_set", "app",
         '{"mail_enable":"0","page_size":50}'),
        "cbd12727b808be34a828ddfcdf188fdc63678908b6dbeb5997564bb45615609c",
    ),
)

#: v2 锚点行原文（7 字段 / 8 token——ts 自带一个空格）。
GOLDEN_ANCHOR_HEAD = "aa" * 32
GOLDEN_ANCHOR_PREV = "bb" * 32
GOLDEN_ANCHOR_LINE = f"2026-01-02 03:04:05 1 42 42 3 {GOLDEN_ANCHOR_HEAD} {GOLDEN_ANCHOR_PREV}"
#: 该行原文的 sha256（独立算出，`_anchor_line_sha` 的目标值）。
GOLDEN_ANCHOR_LINE_SHA = "56be5ffddd65723536cfa74a455df9ad72d423100c37e5223507e11c357d0861"
#: 该行的期望解析结果。
GOLDEN_ANCHOR_PARSED = {
    "version": 2,
    "ts": "2026-01-02 03:04:05",
    "min_id": 1,
    "max_id": 42,
    "count": 42,
    "purge_total": 3,
    "head": GOLDEN_ANCHOR_HEAD,
    "prev_line_hash": GOLDEN_ANCHOR_PREV,
}


class _KeyInjectedTest(unittest.TestCase):
    """把固定审计密钥注入进程环境的用例基类。"""

    def setUp(self):
        self._saved_key = os.environ.get("YIBAN_AUDIT_KEY")
        os.environ["YIBAN_AUDIT_KEY"] = AUDIT_KEY_HEX

    def tearDown(self):
        if self._saved_key is None:
            os.environ.pop("YIBAN_AUDIT_KEY", None)
        else:
            os.environ["YIBAN_AUDIT_KEY"] = self._saved_key


class AuditHashGoldenTest(_KeyInjectedTest):
    """`_audit_hash` 对固定输入的字面量金样例。"""

    def test_literals_are_lowercase_hex64(self):
        """金样例自身格式正确——防止有人贴进大写或短串。"""
        for name, _args, expected in GOLDEN_HASHES:
            with self.subTest(sample=name):
                self.assertTrue(_HEX64.match(expected), f"{name} 不是 64 位小写 hex：{expected}")

    def test_hashes_equal_literal_hex64(self):
        """被测函数逐字返回字面量。断言里不出现"用生产函数算期望值"。"""
        for name, args, expected in GOLDEN_HASHES:
            with self.subTest(sample=name):
                self.assertEqual(audit_chain._audit_hash(*args), expected)

    def test_facade_reexport_returns_same_golden(self):
        """门面 `db._audit_hash` 与定义点同值——两处入口都钉在同一份金样例上。"""
        for name, args, expected in GOLDEN_HASHES:
            with self.subTest(sample=name):
                self.assertEqual(db._audit_hash(*args), expected)


class AnchorLineGoldenTest(unittest.TestCase):
    """锚点行的金样例：v2 行的原文/原文哈希/字段解析，三个版本的 token 常数与门面再导出。"""

    def test_token_count_is_eight(self):
        """7 字段实为 8 token——ts 自带一个空格。"""
        self.assertEqual(audit_chain._ANCHOR_V2_TOKENS, 8)
        self.assertEqual(db._ANCHOR_V2_TOKENS, 8)
        self.assertEqual(len(GOLDEN_ANCHOR_LINE.split()), 8)

    def test_facade_exports_old_format_token_counts(self):
        """门面必须再导出老格式的 token 常数；删掉再导出这一行，本用例必红。

        V2 的再导出已由上面那条断言钉住。v0/v1 此前没有任何守卫：实测删掉
        `yiban/store/db.py` 的 `_ANCHOR_V0_TOKENS` 再导出后，全量 4353 条仍全绿——
        那行 diff 没人看着。本用例补上这一对，它就是那两行的校验者。
        """
        self.assertEqual(
            (audit_chain._ANCHOR_V0_TOKENS, audit_chain._ANCHOR_V1_TOKENS), (3, 5))
        self.assertEqual((db._ANCHOR_V0_TOKENS, db._ANCHOR_V1_TOKENS), (3, 5))

    def test_parse_matches_literal_fields(self):
        """字段一律从行尾取。改动偏移量会让本断言变红。"""
        self.assertEqual(audit_chain._parse_anchor_line(GOLDEN_ANCHOR_LINE),
                         GOLDEN_ANCHOR_PARSED)

    def test_parse_result_from_tail_not_from_head(self):
        """行尾取字段的证据：末尾 6 token 全是字段，ts 由前两 token 拼回。"""
        parts = GOLDEN_ANCHOR_LINE.split()
        self.assertEqual(" ".join(parts[:-6]), GOLDEN_ANCHOR_PARSED["ts"])
        self.assertEqual(parts[-6:], ["1", "42", "42", "3", GOLDEN_ANCHOR_HEAD,
                                      GOLDEN_ANCHOR_PREV])

    def test_line_sha_matches_literal(self):
        """行间链的原文哈希是金样例——无密钥 sha256。"""
        self.assertEqual(audit_chain._anchor_line_sha(GOLDEN_ANCHOR_LINE),
                         GOLDEN_ANCHOR_LINE_SHA)

    def test_seven_token_line_is_rejected(self):
        """按"7 字段"直读的写法必须解析失败，不许悄悄错位取数。"""
        seven = " ".join(GOLDEN_ANCHOR_LINE.split()[:-1])
        self.assertIsNone(audit_chain._parse_anchor_line(seven))


class TruncationBoundaryTest(_KeyInjectedTest):
    """detail 截断上界 200：写入链路实测。"""

    REQUEST_ID = "TESTREQ"
    #: 作用域标记的字面量形态。前导空格属于标记本体（`_SCOPE_MARKER`）。
    TAG = " [req=TESTREQ]"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-audit-golden-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_AUDIT_KEY={AUDIT_KEY_HEX}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_ACCOUNTS_KEY"] = "a" * 64
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        db.init_db(cls.db_file, env_file=cls.env_file)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for key in ("YIBAN_ENV_FILE", "YIBAN_DB_FILE", "YIBAN_ACCOUNTS_KEY",
                    "YIBAN_ACCOUNTS_FILE"):
            os.environ.pop(key, None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        super().setUp()
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, env_file=self.env_file)

    def _write_detail(self, detail):
        """经真实写入口落一条审计行，返回库内 detail。"""
        self.assertTrue(db.audit("admin", "golden_boundary", "t", detail,
                                 request_id=self.REQUEST_ID))
        row = db.get_conn().execute(
            "SELECT ts, detail FROM audit_logs ORDER BY id DESC LIMIT 1").fetchone()
        return row["ts"], row["detail"]

    def test_detail_at_200_and_201_both_store_200_chars(self):
        """上界 200 生效：201 字符只丢尾 1 字符，作用域标记照旧留住。"""
        _ts200, at_200 = self._write_detail("x" * 200)
        _ts201, at_201 = self._write_detail("x" * 200 + "Y")
        expect = "x" * (200 - len(self.TAG)) + self.TAG
        self.assertEqual(len(expect), 200)
        self.assertEqual(at_200, expect)
        self.assertEqual(at_201, expect)
        self.assertNotIn("Y", at_201)

    def test_written_ts_keeps_wire_format(self):
        """写入口落库的 ts 形态固定：不因复用常量而变形。"""
        ts, _detail = self._write_detail("golden ts")
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_short_detail_is_not_truncated(self):
        """边界另一头：短 detail 原样入库，标记仍附加在尾。"""
        _ts, detail = self._write_detail("short")
        self.assertEqual(detail, "short" + self.TAG)


class TsFormatSingleSourceTest(unittest.TestCase):
    """格式串三处合一后的边界守卫。

    下一个改这里的人若又手抄一份，本用例变红。
    """

    def test_module_holds_the_literal_once(self):
        with open(audit_chain.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertEqual(src.count(TS_LITERAL), 1,
                         f"{audit_chain.__file__} 内该格式串应只出现 1 次（常量定义处）")

    def test_constant_value(self):
        self.assertEqual(audit_chain._TS_FMT, TS_LITERAL)


if __name__ == "__main__":
    unittest.main()
